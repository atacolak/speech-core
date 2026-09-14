"""Record-only speech-out websocket client (binary WAV chunks + text events).

Optional live helper. Does not play audio. Captures synthesis-boundary PCM for
Nemotron B feed. Requires a websocket client library when used live.

Usage (library):
  from barge-in path via scripts/barge-in-dual-asr.py internals, or:
  python3 -c '...'  (see docs)

This module is safe to import without websockets installed; connect() fails
with a clear error.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable


def _import_websockets():
    try:
        import websockets  # type: ignore

        return websockets
    except Exception as exc:  # pragma: no cover - env dependent
        raise RuntimeError(
            "record_client requires the 'websockets' package. "
            "Install with: pip install websockets"
        ) from exc


async def record_speech_out(
    url: str,
    out_dir: Path,
    *,
    max_seconds: float = 60.0,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Connect to speech-out WS; write events.jsonl + chunk_XXXX.wav files.

    Stops after max_seconds or when a terminal speech_out_* event arrives
    after at least one audio chunk (utterance complete / cancelled / failed).
    """
    websockets = _import_websockets()
    out_dir.mkdir(parents=True, exist_ok=True)
    events_path = out_dir / "speech_out_events.jsonl"
    pcm_dir = out_dir / "wav_chunks"
    pcm_dir.mkdir(parents=True, exist_ok=True)

    summary: dict[str, Any] = {
        "url": url,
        "chunks": 0,
        "bytes": 0,
        "events": 0,
        "terminal": None,
        "chunk_paths": [],
    }
    terminal_names = {
        "speech_out_completed",
        "speech_out_cancelled",
        "speech_out_failed",
        "speech_out_playback_failed",
    }

    async with websockets.connect(url) as ws:  # type: ignore[attr-defined]
        deadline = time.monotonic() + max_seconds
        chunk_idx = 0
        while time.monotonic() < deadline:
            timeout = max(0.05, deadline - time.monotonic())
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=timeout)
            except asyncio.TimeoutError:
                break
            except Exception as exc:
                summary["terminal"] = f"recv_error:{exc}"
                break

            if isinstance(msg, bytes):
                chunk_idx += 1
                path = pcm_dir / f"chunk_{chunk_idx:04d}.wav"
                path.write_bytes(msg)
                summary["chunks"] += 1
                summary["bytes"] += len(msg)
                summary["chunk_paths"].append(str(path))
                rec = {
                    "event": "binary_wav_chunk",
                    "chunk_index": chunk_idx,
                    "bytes": len(msg),
                    "path": str(path),
                    "diagnostic_mono_ns": time.monotonic_ns(),
                    "label": "eval_only",
                    "capture_locus": "eval_synthesis",
                }
                with events_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(rec) + "\n")
                summary["events"] += 1
                if on_event:
                    on_event(rec)
                continue

            # text frame
            try:
                payload = json.loads(msg)
            except json.JSONDecodeError:
                payload = {"event": "non_json_text", "raw": msg[:500]}
            payload.setdefault("diagnostic_mono_ns", time.monotonic_ns())
            payload.setdefault("label", "eval_only")
            with events_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
            summary["events"] += 1
            if on_event:
                on_event(payload)
            ev = payload.get("event")
            if ev in terminal_names and summary["chunks"] > 0:
                summary["terminal"] = ev
                break

    (out_dir / "record_summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Record-only speech-out WS client")
    p.add_argument(
        "--url",
        default="ws://127.0.0.1:8788/ws/speech-out",
        help="speech-out websocket URL",
    )
    p.add_argument(
        "--out-dir",
        type=Path,
        required=True,
        help="Directory for events + wav_chunks",
    )
    p.add_argument("--max-seconds", type=float, default=60.0)
    args = p.parse_args(argv)
    try:
        summary = asyncio.run(
            record_speech_out(args.url, args.out_dir, max_seconds=args.max_seconds)
        )
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
