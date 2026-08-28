"""Dual-Nemotron barge-in path (harness-only).

Topology (contract rev 4 / checkpoint-010):
  Nemotron A = user mic path (existing speech-in; not owned here)
  Nemotron B = assistant self-ASR on speech-out synthesized PCM (separate process)

Production cut (rev 3 happy path, pad is FALLBACK):
  1. Pause playback on first user alphanumeric token
  2. Drain Nemotron B on already-emitted audio during user speech
  3. At user transcript_committed: drained + force-aligned intended prefix (primary)
  4. Fallback if drain incomplete: last known pos + pad_words(~2) from intended
  5. Commit truncated assistant message once; never revise

All artifacts are harness / eval_only. No speech-core-protocol schema edits.
"""

# Submodules are loaded by scripts/barge-in-dual-asr.py via importlib
# (hyphenated directory → barge_in_dual_asr package). Keep this file free of
# eager relative imports so `importlib` package bootstrap stays simple.

__all__ = [
    "cut",
    "simulator",
    "live_wiring",
    "record_client",
    "feed_assistant_asr",
]
