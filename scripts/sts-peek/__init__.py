"""sts-peek — live speech-to-speech peek surface (harness / eval_only).

Track L owns the audio+session layer under this package:
  session.py   run-dir layout, events.jsonl, stream ids
  audio.py     speech-out speak/cancel, barge watch, optional record
  run_audio.py CLI entry for audio-only / headless sessions

UI (Track U) and cut coordinator (Track C) attach via the published
run_dir contract — see docs/sts-peek-audio.md.
"""

from __future__ import annotations

__all__ = [
    "ASSISTANT_STREAM_ID",
    "DEFAULT_CORE_WS",
    "DEFAULT_OUT_WS",
    "USER_STREAM_ID",
]

ASSISTANT_STREAM_ID = "assistant.self_asr"
USER_STREAM_ID = "laptop.live_mic"
DEFAULT_CORE_WS = "ws://127.0.0.1:8765/ws/audio-ingress"
DEFAULT_OUT_WS = "ws://127.0.0.1:8788/ws/speech-out"
