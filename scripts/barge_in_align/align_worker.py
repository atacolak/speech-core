#!/usr/bin/env python3
"""Long-lived warm CTC align worker.

Keeps wav2vec2 weights loaded and serves JSON-line requests over a Unix socket.
One process per host; live-session reuses it across barges.

Protocol (one request/response per connection, JSON lines):
  client → {"cmd":"align","wav":"/path","intended":"...","played_ms":1234,"speed":1.3,"backend":"ctc_forced"}
  server → AlignCursor JSON (same shape as run_align.py)

  client → {"cmd":"ping"}
  server → {"ok":true,"preloaded":true,"model":"..."}

  client → {"cmd":"shutdown"}
  server → {"ok":true} then exit
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import sys
import time
import traceback
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SCRIPTS = _HERE.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


DEFAULT_SOCK = os.environ.get(
    "SPEECH_OUT_ALIGN_SOCK",
    "/tmp/speech-core-align-worker.sock",
)


def _log(msg: str) -> None:
    print(f"[align_worker] {msg}", file=sys.stderr, flush=True)


def _handle(req: dict) -> dict:
    from barge_in_align.backend import align_played_clip
    from barge_in_align.ctc_forced import preload, _model_choice

    cmd = (req.get("cmd") or "align").strip().lower()
    if cmd == "ping":
        pack = preload()
        return {
            "ok": True,
            "preloaded": True,
            "model": pack.get("id") if isinstance(pack, dict) else _model_choice(),
            "pid": os.getpid(),
        }
    if cmd == "shutdown":
        return {"ok": True, "shutdown": True}
    if cmd != "align":
        return {"ok": False, "error": f"unknown_cmd:{cmd}"}

    t0 = time.perf_counter()
    cursor = align_played_clip(
        backend=str(req.get("backend") or "ctc_forced"),
        intended_text=str(req.get("intended") or ""),
        played_ms=int(req.get("played_ms") or 0),
        wav_path=req.get("wav"),
        speed=float(req.get("speed") or 1.0),
    )
    payload = cursor.to_json()
    payload["ok"] = True
    payload["worker_wall_ms"] = (time.perf_counter() - t0) * 1000.0
    payload["worker_pid"] = os.getpid()
    return payload


def serve(sock_path: Path) -> int:
    from barge_in_align.ctc_forced import preload

    if sock_path.exists():
        try:
            sock_path.unlink()
        except OSError:
            pass

    _log(f"preloading model on pid={os.getpid()} …")
    t0 = time.perf_counter()
    pack = preload()
    _log(f"preloaded model={pack.get('id')} in {(time.perf_counter() - t0) * 1000.0:.0f}ms")

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock_path))
    server.listen(8)
    os.chmod(sock_path, 0o666)
    _log(f"listening on {sock_path}")

    stop = False

    def _sig(_signum, _frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    server.settimeout(1.0)
    while not stop:
        try:
            conn, _ = server.accept()
        except socket.timeout:
            continue
        except OSError:
            break
        try:
            with conn:
                conn.settimeout(120.0)
                buf = b""
                while b"\n" not in buf:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                if not buf:
                    continue
                line = buf.split(b"\n", 1)[0].decode("utf-8", errors="replace")
                try:
                    req = json.loads(line)
                except json.JSONDecodeError as exc:
                    resp = {"ok": False, "error": f"bad_json:{exc}"}
                else:
                    try:
                        resp = _handle(req)
                    except Exception as exc:
                        resp = {
                            "ok": False,
                            "error": f"{type(exc).__name__}: {exc}",
                            "trace": traceback.format_exc(limit=4),
                        }
                conn.sendall((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
                if resp.get("shutdown"):
                    stop = True
        except Exception as exc:
            _log(f"connection error: {type(exc).__name__}: {exc}")

    try:
        server.close()
    except OSError:
        pass
    try:
        sock_path.unlink(missing_ok=True)
    except TypeError:
        # py<3.8
        try:
            sock_path.unlink()
        except OSError:
            pass
    _log("exit")
    return 0


def client_request(sock_path: Path, req: dict, timeout: float = 60.0) -> dict:
    data = (json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    sock.connect(str(sock_path))
    try:
        sock.sendall(data)
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
    finally:
        sock.close()
    if not buf:
        raise RuntimeError("empty_worker_response")
    return json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Warm CTC align worker")
    p.add_argument("--sock", type=Path, default=Path(DEFAULT_SOCK))
    p.add_argument("--serve", action="store_true", help="run worker server")
    p.add_argument("--ping", action="store_true")
    p.add_argument("--shutdown", action="store_true")
    p.add_argument("--align", action="store_true")
    p.add_argument("--wav", type=Path, default=None)
    p.add_argument("--intended", default="")
    p.add_argument("--played-ms", type=int, default=0)
    p.add_argument("--speed", type=float, default=1.0)
    p.add_argument("--backend", default="ctc_forced")
    p.add_argument("--out", type=Path, default=None)
    args = p.parse_args(argv)

    if args.serve:
        return serve(args.sock)

    if args.ping:
        resp = client_request(args.sock, {"cmd": "ping"})
        print(json.dumps(resp, ensure_ascii=False))
        return 0 if resp.get("ok") else 1

    if args.shutdown:
        try:
            resp = client_request(args.sock, {"cmd": "shutdown"}, timeout=5.0)
        except Exception as exc:
            print(json.dumps({"ok": False, "error": str(exc)}))
            return 1
        print(json.dumps(resp, ensure_ascii=False))
        return 0

    if args.align or args.intended or args.wav is not None:
        req = {
            "cmd": "align",
            "wav": str(args.wav) if args.wav else None,
            "intended": args.intended,
            "played_ms": args.played_ms,
            "speed": args.speed,
            "backend": args.backend,
        }
        resp = client_request(args.sock, req)
        text = json.dumps(resp, ensure_ascii=False)
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json.dumps(resp, indent=2) + "\n", encoding="utf-8")
        print(text)
        return 0 if resp.get("ok") is not False else 1

    p.error("pass --serve, --ping, --shutdown, or --align")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
