#!/usr/bin/env python3
"""CTC barge-in align performance probe (host-local or TCP warm worker).

Usage:
  # Against warm TCP worker (preferred, matches dogfood):
  python3 scripts/barge_in_align/perf_probe.py --tcp 127.0.0.1:8791 \\
    --wav /tmp/speech-out-retain/<utt>_chunk_0000.wav \\
    --intended "This is a long assistant reply so you can barge in mid sentence and hear the stop." \\
    --played-ms 2200 --repeats 5

  # Cold local import path (measures process+model load — not product hot path):
  python3 scripts/barge_in_align/perf_probe.py --local \\
    --wav ... --intended "..." --played-ms 2200 --repeats 1

Exit 0 always prints a JSON summary to stdout; non-zero on hard failures.
"""
from __future__ import annotations

import argparse
import json
import socket
import statistics
import sys
import time
from pathlib import Path


def probe_tcp(host: str, port: int, payload: dict, timeout: float) -> dict:
    t0 = time.perf_counter()
    with socket.create_connection((host, port), timeout=timeout) as s:
        s.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        buf = b""
        while b"\n" not in buf and len(buf) < 1_000_000:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    wall_ms = (time.perf_counter() - t0) * 1000.0
    line = buf.decode("utf-8", errors="replace").strip().splitlines()[0]
    body = json.loads(line)
    body["_probe_wall_ms"] = wall_ms
    return body


def probe_local(wav: str, intended: str, played_ms: int, speed: float) -> dict:
    # Import only on local path so TCP probes stay light.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from barge_in_align.ctc_forced import align_cut  # type: ignore

    t0 = time.perf_counter()
    out = align_cut(
        wav_path=wav,
        intended_text=intended,
        played_ms=played_ms,
        speed=speed,
    )
    wall_ms = (time.perf_counter() - t0) * 1000.0
    if isinstance(out, dict):
        out = dict(out)
        out["_probe_wall_ms"] = wall_ms
        return out
    return {"spoken_prefix": str(out), "_probe_wall_ms": wall_ms}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tcp", default="", help="host:port of warm align worker")
    ap.add_argument("--local", action="store_true", help="call ctc_forced in-process")
    ap.add_argument("--wav", required=True)
    ap.add_argument("--intended", required=True)
    ap.add_argument("--played-ms", type=int, default=2200)
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--warmup", type=int, default=1, help="discard first N timings")
    args = ap.parse_args()

    if not args.tcp and not args.local:
        print("need --tcp host:port or --local", file=sys.stderr)
        return 2
    if not Path(args.wav).is_file():
        print(f"wav missing: {args.wav}", file=sys.stderr)
        return 2

    payload = {
        "cmd": "align",
        "wav": args.wav,
        "intended": args.intended,
        "played_ms": args.played_ms,
        "speed": args.speed,
        "backend": "ctc_forced",
    }

    rows = []
    for i in range(args.warmup + args.repeats):
        if args.local:
            body = probe_local(args.wav, args.intended, args.played_ms, args.speed)
        else:
            host, port_s = args.tcp.rsplit(":", 1)
            body = probe_tcp(host, int(port_s), payload, args.timeout)
        rows.append(body)

    measured = rows[args.warmup :]
    walls = [float(r.get("_probe_wall_ms") or r.get("align_latency_ms") or 0) for r in measured]
    aligns = [float(r.get("align_latency_ms") or 0) for r in measured]
    prefixes = [r.get("spoken_prefix") or r.get("cut_text") or "" for r in measured]

    def pct(xs, p):
        if not xs:
            return None
        s = sorted(xs)
        k = min(len(s) - 1, max(0, int(round((p / 100.0) * (len(s) - 1)))))
        return s[k]

    summary = {
        "mode": "local" if args.local else f"tcp:{args.tcp}",
        "wav": args.wav,
        "played_ms": args.played_ms,
        "repeats": args.repeats,
        "warmup": args.warmup,
        "probe_wall_ms": {
            "n": len(walls),
            "mean": statistics.fmean(walls) if walls else None,
            "p50": pct(walls, 50),
            "p95": pct(walls, 95),
            "min": min(walls) if walls else None,
            "max": max(walls) if walls else None,
        },
        "align_latency_ms": {
            "mean": statistics.fmean(aligns) if aligns else None,
            "p50": pct(aligns, 50),
            "p95": pct(aligns, 95),
        },
        "spoken_prefix_last": prefixes[-1] if prefixes else "",
        "spoken_prefix_stable": len(set(prefixes)) == 1 if prefixes else False,
        "samples": [
            {
                "probe_wall_ms": r.get("_probe_wall_ms"),
                "align_latency_ms": r.get("align_latency_ms"),
                "spoken_prefix": r.get("spoken_prefix") or r.get("cut_text"),
                "word_index": r.get("word_index"),
            }
            for r in measured
        ],
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
