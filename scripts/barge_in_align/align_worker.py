#!/usr/bin/env python3
"""Long-lived warm CTC align worker.

Keeps wav2vec2 weights loaded. Speaks JSON-lines over:
  - Unix socket (local, default /tmp/speech-core-align-worker.sock)
  - TCP (remote clients; default port 8791)  ← laptop → host without SSH

Protocol (one request/response per connection, JSON line):
  {"cmd":"align","wav":"/host/path.wav","intended":"...","played_ms":1234,"speed":1.3,"backend":"ctc_forced"}
  {"cmd":"ping"}
  {"cmd":"shutdown"}
"""

from __future__ import annotations

import argparse
import json
import os
import select
import signal
import socket
import sys
import time
import traceback
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_SCRIPTS = _HERE.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

DEFAULT_SOCK = os.environ.get(
    "SPEECH_OUT_ALIGN_SOCK",
    "/tmp/speech-core-align-worker.sock",
)
DEFAULT_TCP_PORT = int(os.environ.get("SPEECH_OUT_ALIGN_TCP_PORT", "8791"))


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


def _recv_line(conn: socket.socket) -> bytes:
    buf = b""
    while b"\n" not in buf:
        chunk = conn.recv(65536)
        if not chunk:
            break
        buf += chunk
    return buf.split(b"\n", 1)[0] if buf else b""


def _serve_conn(conn: socket.socket) -> bool:
    """Handle one connection. Returns True if shutdown requested."""
    try:
        conn.settimeout(120.0)
        raw = _recv_line(conn)
        if not raw:
            return False
        line = raw.decode("utf-8", errors="replace")
        try:
            req = json.loads(line)
        except json.JSONDecodeError as exc:
            resp: dict[str, Any] = {"ok": False, "error": f"bad_json:{exc}"}
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
        return bool(resp.get("shutdown"))
    except Exception as exc:
        _log(f"connection error: {type(exc).__name__}: {exc}")
        return False
    finally:
        try:
            conn.close()
        except OSError:
            pass


def serve(
    sock_path: Path | None = None,
    tcp_host: str | None = None,
    tcp_port: int | None = None,
) -> int:
    from barge_in_align.ctc_forced import preload

    sock_path = sock_path or Path(DEFAULT_SOCK)
    # Default: listen TCP on all interfaces so laptop can reach host worker.
    if tcp_host is None:
        tcp_host = os.environ.get("SPEECH_OUT_ALIGN_TCP_HOST", "0.0.0.0")
    if tcp_port is None:
        tcp_port = int(os.environ.get("SPEECH_OUT_ALIGN_TCP_PORT", str(DEFAULT_TCP_PORT)))

    _log(f"preloading model on pid={os.getpid()} …")
    t0 = time.perf_counter()
    pack = preload()
    _log(f"preloaded model={pack.get('id')} in {(time.perf_counter() - t0) * 1000.0:.0f}ms")

    listeners: list[socket.socket] = []

    # Unix socket
    if sock_path:
        if sock_path.exists():
            try:
                sock_path.unlink()
            except OSError:
                pass
        us = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        us.bind(str(sock_path))
        us.listen(8)
        os.chmod(sock_path, 0o666)
        us.setblocking(False)
        listeners.append(us)
        _log(f"listening unix {sock_path}")

    # TCP
    if tcp_host is not None and tcp_port and int(tcp_port) > 0:
        ts = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        ts.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        ts.bind((tcp_host, int(tcp_port)))
        ts.listen(16)
        ts.setblocking(False)
        listeners.append(ts)
        _log(f"listening tcp {tcp_host}:{tcp_port}")

    if not listeners:
        _log("no listeners configured")
        return 2

    stop = False

    def _sig(_signum, _frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    while not stop:
        try:
            readable, _, _ = select.select(listeners, [], [], 1.0)
        except InterruptedError:
            continue
        for ls in readable:
            try:
                conn, addr = ls.accept()
            except OSError:
                continue
            if _serve_conn(conn):
                stop = True
                break

    for ls in listeners:
        try:
            ls.close()
        except OSError:
            pass
    if sock_path:
        try:
            sock_path.unlink(missing_ok=True)  # type: ignore[call-arg]
        except TypeError:
            try:
                sock_path.unlink()
            except OSError:
                pass
        except OSError:
            pass
    _log("exit")
    return 0


def _parse_tcp(target: str) -> tuple[str, int]:
    """host:port → (host, port)."""
    target = target.strip()
    if not target:
        raise ValueError("empty tcp target")
    if target.startswith("["):
        # [ipv6]:port
        host, _, port_s = target[1:].partition("]")
        port_s = port_s.lstrip(":")
        return host, int(port_s or DEFAULT_TCP_PORT)
    if ":" in target:
        host, _, port_s = target.rpartition(":")
        return host, int(port_s)
    return target, DEFAULT_TCP_PORT


def client_request(
    req: dict,
    *,
    sock_path: Path | None = None,
    tcp: str | None = None,
    timeout: float = 60.0,
) -> dict:
    data = (json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8")
    if tcp:
        host, port = _parse_tcp(tcp)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect((host, port))
    else:
        path = sock_path or Path(DEFAULT_SOCK)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(str(path))
    try:
        sock.sendall(data)
        raw = _recv_line(sock)
    finally:
        sock.close()
    if not raw:
        raise RuntimeError("empty_worker_response")
    return json.loads(raw.decode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Warm CTC align worker")
    p.add_argument("--sock", type=Path, default=Path(DEFAULT_SOCK))
    p.add_argument(
        "--tcp",
        default=None,
        help="client: host:port to reach worker; server: ignored (use --tcp-bind)",
    )
    p.add_argument(
        "--tcp-bind",
        default=None,
        help="server: bind host (default 0.0.0.0). Empty string disables TCP.",
    )
    p.add_argument(
        "--tcp-port",
        type=int,
        default=DEFAULT_TCP_PORT,
        help=f"server TCP port (default {DEFAULT_TCP_PORT})",
    )
    p.add_argument("--no-unix", action="store_true", help="server: do not bind unix socket")
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
        tcp_host = args.tcp_bind
        if tcp_host is None:
            tcp_host = "0.0.0.0"
        if tcp_host == "":
            tcp_host = None
        sock = None if args.no_unix else args.sock
        return serve(sock_path=sock, tcp_host=tcp_host, tcp_port=args.tcp_port)

    tcp = args.tcp or os.environ.get("SPEECH_OUT_ALIGN_TCP") or None

    if args.ping:
        resp = client_request({"cmd": "ping"}, sock_path=args.sock, tcp=tcp, timeout=5.0)
        print(json.dumps(resp, ensure_ascii=False))
        return 0 if resp.get("ok") else 1

    if args.shutdown:
        try:
            resp = client_request(
                {"cmd": "shutdown"}, sock_path=args.sock, tcp=tcp, timeout=5.0
            )
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
        resp = client_request(req, sock_path=args.sock, tcp=tcp)
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
