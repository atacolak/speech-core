#!/usr/bin/env python3
"""Small live :8788 now-stamp. Does not spawn a second CosyVoice3 model.

Uses the already-running speech-out-aa86b67 daemon. Clock domain is daemon_mono
(request_received → first pcm frame). Never subtract daemon vs worker clocks.
"""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _pct(values: List[float], q: float) -> Optional[float]:
    if not values:
        return None
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0]
    k = (len(xs) - 1) * q
    f = int(k)
    c = min(f + 1, len(xs) - 1)
    return xs[f] * (c - k) + xs[c] * (k - f)


def _stats(values: List[float]) -> Dict[str, Any]:
    xs = [float(v) for v in values if v is not None]
    if not xs:
        return {"count": 0, "min": None, "max": None, "mean": None, "p50": None, "p95": None}
    return {
        "count": len(xs),
        "min": min(xs),
        "max": max(xs),
        "mean": statistics.fmean(xs),
        "p50": _pct(xs, 0.50),
        "p95": _pct(xs, 0.95),
    }


def load_prompts(path: Path) -> List[str]:
    return [
        ln.strip()
        for ln in path.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]


def parse_play_stderr(raw: str) -> Dict[str, Any]:
    events: List[dict] = []
    nonjson: List[str] = []
    for line in raw.splitlines():
        s = line.strip()
        if not s:
            continue
        try:
            events.append(json.loads(s))
        except json.JSONDecodeError:
            nonjson.append(s[:240])
    first_pcm = next((e for e in events if e.get("is_first_pcm")), None)
    completed = next((e for e in events if e.get("event") == "speech_out_completed"), None)
    received = next((e for e in events if e.get("event") == "speech_out_request_received"), None)
    started = next((e for e in events if e.get("event") == "speech_out_synthesis_started"), None)
    failed = next((e for e in events if e.get("event") in ("speech_out_failed", "speech_out_cancelled")), None)
    latency = (completed or {}).get("latency") or {}
    return {
        "n_events": len(events),
        "nonjson": nonjson,
        "utterance_id": (received or first_pcm or completed or {}).get("utterance_id"),
        "request_received": received is not None,
        "synthesis_started": started is not None,
        "saw_first_pcm": first_pcm is not None,
        "terminal": (completed or failed or {}).get("event"),
        "clock_domain": (received or {}).get("clock_domain")
        or (first_pcm or {}).get("clock_domain")
        or "daemon_process_clock_monotonic",
        "cross_process_clock_note": (received or {}).get("cross_process_clock_note"),
        "request_received_to_first_pcm_ms": (
            (first_pcm or {}).get("request_received_to_first_pcm_ms")
            or latency.get("request_received_to_first_pcm_ms")
        ),
        "first_pcm_latency_ms": (first_pcm or {}).get("first_pcm_latency_ms")
        or latency.get("first_pcm_latency_ms"),
        "worker_first_pcm_latency_ms": (completed or {}).get("worker_first_pcm_latency_ms"),
        "request_received_to_synthesis_started_ms": (
            (started or {}).get("request_received_to_synthesis_started_ms")
            or latency.get("request_received_to_synthesis_started_ms")
        ),
        "request_received_to_terminal_ms": latency.get("request_received_to_terminal_ms"),
        "total_frames": (completed or {}).get("total_frames"),
        "total_samples": (completed or {}).get("total_samples"),
        "provenance": (started or {}).get("provenance"),
        "token_acoustic_stages": "not_on_daemon_ws",
    }


def probe_one(play_bin: Path, url: str, text: str, timeout_s: float) -> Dict[str, Any]:
    cmd = [
        str(play_bin),
        "play",
        "--url",
        url,
        "--play-command",
        "true",
        "--chunk-min-chars",
        "8",
        "--chunk-max-chars",
        "400",
        text,
    ]
    t0 = time.perf_counter()
    proc = subprocess.run(
        cmd,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )
    host_s = time.perf_counter() - t0
    parsed = parse_play_stderr(proc.stderr or "")
    parsed.update(
        {
            "text": text,
            "text_chars": len(text),
            "play_exit": proc.returncode,
            "host_wall_s": host_s,
            "stdout": (proc.stdout or "")[:400],
        }
    )
    return parsed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--play-bin", type=Path, required=True)
    ap.add_argument("--url", default="ws://127.0.0.1:8788/ws/speech-out")
    ap.add_argument("--prompts-file", type=Path, required=True)
    ap.add_argument("--n", type=int, default=5)
    ap.add_argument("--skip", type=int, default=0, help="skip first N prompts (match harness warmup offset)")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--timeout-s", type=float, default=60.0)
    args = ap.parse_args()

    prompts = load_prompts(args.prompts_file)
    chosen = prompts[args.skip : args.skip + args.n]
    if len(chosen) < args.n:
        raise SystemExit(f"need {args.n} prompts after skip={args.skip}, have {len(chosen)}")

    report: Dict[str, Any] = {
        "schema": "speech-out.live8788.nowstamp.v1",
        "started_at": _now_iso(),
        "play_bin": str(args.play_bin),
        "url": args.url,
        "prompts_file": str(args.prompts_file),
        "n": args.n,
        "skip": args.skip,
        "clock_note": (
            "live D = daemon request_received → first ws pcm frame "
            "(request_received_to_first_pcm_ms). worker_first_pcm_latency_ms is "
            "worker-domain and must not be subtracted from D."
        ),
        "samples": [],
    }
    for i, text in enumerate(chosen, start=1):
        print(f"[live8788] {i}/{len(chosen)} {text[:56]!r}", flush=True)
        row = probe_one(args.play_bin, args.url, text, args.timeout_s)
        row["run"] = i
        report["samples"].append(row)
        print(
            f"  D_first_pcm_ms={row.get('request_received_to_first_pcm_ms')} "
            f"W_first_pcm_ms={row.get('worker_first_pcm_latency_ms')} "
            f"terminal={row.get('terminal')} exit={row.get('play_exit')}",
            flush=True,
        )
        time.sleep(0.15)

    dvals = [
        float(s["request_received_to_first_pcm_ms"])
        for s in report["samples"]
        if s.get("request_received_to_first_pcm_ms") is not None
    ]
    wvals = [
        float(s["worker_first_pcm_latency_ms"])
        for s in report["samples"]
        if s.get("worker_first_pcm_latency_ms") is not None
    ]
    report["summary"] = {
        "daemon_request_received_to_first_pcm_ms": _stats(dvals),
        "worker_first_pcm_latency_ms": _stats(wvals),
        "completed_count": sum(1 for s in report["samples"] if s.get("terminal") == "speech_out_completed"),
        "sample_count": len(report["samples"]),
    }
    report["finished_at"] = _now_iso()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"out": str(args.out), "summary": report["summary"]}, indent=2), flush=True)
    return 0 if report["summary"]["completed_count"] == report["summary"]["sample_count"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
