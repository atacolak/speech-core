"""Dual-stream dry-run simulator for barge-in (no daemons required).

Simulates:
  - Nemotron A (user): first alnum token → pause; later transcript_committed
  - Nemotron B (assistant): drains already-emitted audio during user speech
  - Coordinator: primary cut from drained+aligned; fallback last_pos+pad

Proves the acceptance path: pause → drain window → cut primary=drain or fallback.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .cut import (
    DEFAULT_PAD_WORDS,
    commit_truncated_assistant_message,
    intended_prefix_at_word_index,
    normalize_whitespace,
    production_cut,
    tokenize_words,
)


@dataclass
class DualStreamSimConfig:
    intended_text: str
    # How many intended words were already in the emit/play path at pause.
    emitted_words_at_pause: int
    # Words Nemotron B has partially committed by pause (may lag emitted).
    b_pos_at_pause: int
    # During drain window, B advances toward emitted_words_at_pause.
    # If drain_complete, B reaches emitted_words_at_pause by user commit.
    drain_complete: bool
    # If drain incomplete, how many words B actually reaches by user commit.
    b_pos_at_user_commit: int | None = None
    # ASR noise: optional corrupted drain text (if None, use clean intended prefix).
    drained_asr_override: str | None = None
    pad_words: int = DEFAULT_PAD_WORDS
    pause_at_ms: float = 800.0
    user_stop_at_ms: float = 2200.0
    user_transcript: str = "wait stop talking please"
    utterance_id: str = "dual-sim-assistant-1"
    # Minimum confidence / words for drain path (passed to production_cut).
    min_align_words: int = 1
    min_align_confidence: float = 0.25


def _append_event(events_path: Path, event: dict[str, Any]) -> None:
    event = dict(event)
    event.setdefault("label", "eval_only")
    event.setdefault("diagnostic_mono_ns", time.monotonic_ns())
    with events_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")


def _write_text(path: Path, text: str) -> None:
    path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")


def run_dual_stream_simulation(cfg: DualStreamSimConfig, out_dir: Path) -> dict[str, Any]:
    """Run one dual-stream barge-in simulation; write artifacts; return summary."""
    intended = normalize_whitespace(cfg.intended_text)
    words = tokenize_words(intended)
    total = len(words)
    if not intended:
        raise ValueError("intended_text is empty")
    if cfg.user_stop_at_ms < cfg.pause_at_ms:
        raise ValueError("user_stop_at_ms must be >= pause_at_ms")

    out_dir.mkdir(parents=True, exist_ok=True)
    events_path = out_dir / "events.jsonl"
    if events_path.exists():
        events_path.unlink()

    emitted = max(0, min(total, int(cfg.emitted_words_at_pause)))
    b_at_pause = max(0, min(emitted, int(cfg.b_pos_at_pause)))

    if cfg.drain_complete:
        b_at_commit = emitted
    elif cfg.b_pos_at_user_commit is not None:
        b_at_commit = max(0, min(emitted, int(cfg.b_pos_at_user_commit)))
    else:
        # Default incomplete drain: stuck at pause position (no progress).
        b_at_commit = b_at_pause

    # Drain text = what B recovered from already-emitted audio by user commit.
    if cfg.drained_asr_override is not None:
        drained_text = normalize_whitespace(cfg.drained_asr_override)
    else:
        drained_text = " ".join(words[:b_at_commit])

    last_pos = b_at_commit  # last known alignment position for fallback base

    _append_event(
        events_path,
        {
            "event": "dual_sim_started",
            "mode": "dry-run",
            "topology": {
                "nemotron_a": "user_mic (simulated; not shared worker)",
                "nemotron_b": "assistant.self_asr (simulated; separate stream)",
                "shared_worker": False,
            },
            "intended_word_count": total,
            "pad_words": cfg.pad_words,
            "emitted_words_at_pause": emitted,
        },
    )

    # --- T_pause: first alphanumeric user token on Nemotron A ---------------
    user_tokens = tokenize_words(cfg.user_transcript)
    first_alnum = next((t for t in user_tokens if any(c.isalnum() for c in t)), None)
    _append_event(
        events_path,
        {
            "event": "user_first_alphanumeric_token",
            "stream": "nemotron_a_user",
            "token": first_alnum,
            "trigger": "transcript_token_committed",
            "action": "pause_playback",
            "pause_ms": cfg.pause_at_ms,
            "emitted_words_at_pause": emitted,
            "b_pos_at_pause": b_at_pause,
            "note": (
                "Mirrors scripts/speech-out-live-session.sh: first alphanumeric "
                "user token cancels/pauses active speech-out."
            ),
        },
    )
    _append_event(
        events_path,
        {
            "event": "speech_out_barge_in",
            "trigger": "transcript_token_committed",
            "action": "pause_and_cancel_further_synthesis",
            "pause_ms": cfg.pause_at_ms,
            "emitted_pcm_words": emitted,
        },
    )

    # --- Drain window: Nemotron B on already-emitted audio ------------------
    drain_window_ms = max(0.0, cfg.user_stop_at_ms - cfg.pause_at_ms)
    _append_event(
        events_path,
        {
            "event": "assistant_self_asr_drain_started",
            "stream": "nemotron_b_assistant",
            "stream_id": "assistant.self_asr",
            "drain_only_already_emitted": True,
            "emitted_words": emitted,
            "b_pos_at_pause": b_at_pause,
            "drain_window_ms": drain_window_ms,
            "note": (
                "Free on critical path: B is a separate Nemotron instance; "
                "cannot steal user stream compute (R7 out of scope for dual topology)."
            ),
        },
    )
    _append_event(
        events_path,
        {
            "event": "assistant_self_asr_drain_progress",
            "stream": "nemotron_b_assistant",
            "b_pos_words": b_at_commit,
            "target_emitted_words": emitted,
            "drain_complete": cfg.drain_complete and b_at_commit >= emitted,
            "drained_text": drained_text,
        },
    )

    # --- T0: user transcript_committed → finalize cut -----------------------
    _append_event(
        events_path,
        {
            "event": "user_transcript_committed",
            "stream": "nemotron_a_user",
            "t0": "user_stop",
            "user_stop_ms": cfg.user_stop_at_ms,
            "user_transcript": cfg.user_transcript,
            "action": "finalize_truncated_assistant",
            "b_pos_at_commit": b_at_commit,
        },
    )

    decision = production_cut(
        intended,
        drained_asr_text=drained_text,
        last_pos_words=last_pos,
        pad_words=cfg.pad_words,
        drain_complete=bool(cfg.drain_complete and b_at_commit >= emitted and emitted > 0),
        min_align_words=cfg.min_align_words,
        min_align_confidence=cfg.min_align_confidence,
    )
    # If drain was marked complete but alignment failed, production_cut falls back.
    commit = commit_truncated_assistant_message(
        decision,
        utterance_id=cfg.utterance_id,
        user_transcript=cfg.user_transcript,
    )
    _append_event(events_path, decision.to_dict())
    _append_event(events_path, commit)

    # Late B drain is diagnostic only.
    _append_event(
        events_path,
        {
            "event": "late_self_asr_drain_eval_only",
            "diagnostic_only": True,
            "revises_commit": False,
            "committed_production_cut_text": commit["production_cut_text"],
            "cut_source": decision.source,
        },
    )

    playback_proxy = intended_prefix_at_word_index(intended, emitted)

    metrics: dict[str, Any] = {
        "label": "eval_only",
        "mode": "dry-run",
        "topology": {
            "nemotron_a": "user_mic_path",
            "nemotron_b": "assistant.self_asr",
            "shared_worker": False,
            "contract_rev": 4,
        },
        "founder_cut_rule": {
            "pause_on": "first_user_alphanumeric_token",
            "finalize_on": "user_stop_transcript_committed",
            "primary": "drained_asr_force_aligned_to_intended",
            "fallback": "last_pos_words + pad_words from intended",
            "pad_words": cfg.pad_words,
            "commit_once": True,
            "late_self_asr": "diagnostic_only",
        },
        "timing_ms": {
            "pause_ms": cfg.pause_at_ms,
            "user_stop_ms": cfg.user_stop_at_ms,
            "drain_window_ms": drain_window_ms,
        },
        "streams": {
            "emitted_words_at_pause": emitted,
            "b_pos_at_pause": b_at_pause,
            "b_pos_at_user_commit": b_at_commit,
            "drain_complete_config": cfg.drain_complete,
            "drain_reached_emitted": b_at_commit >= emitted,
            "drained_text": drained_text,
            "playback_proxy_intended_prefix": playback_proxy,
        },
        "cut": decision.to_dict(),
        "prefix_valid": decision.prefix_valid,
        "production_cut_text": decision.production_cut_text,
        "cut_source": decision.source,
        "pad_words": decision.pad_words,
        "biases_labeled": [
            "simulated_dual_stream_not_live_nemotron",
            "no_acoustic_loop",
            "drain_text_derived_from_intended_unless_override",
            "results_are_eval_only_harness",
        ],
    }

    _write_text(out_dir / "assistant_intended.txt", intended)
    _write_text(out_dir / "production_cut_text", decision.production_cut_text)
    _write_text(out_dir / "drained_asr_text.txt", drained_text)
    _write_text(out_dir / "user_transcript.txt", cfg.user_transcript)
    _write_text(out_dir / "playback_proxy.txt", playback_proxy)
    (out_dir / "metrics.json").write_text(
        json.dumps(metrics, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (out_dir / "commit.json").write_text(
        json.dumps(commit, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (out_dir / "cut_decision.json").write_text(
        json.dumps(decision.to_dict(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    readme = f"""barge-in dual-Nemotron dry-run artifacts
========================================

label: eval_only
mode: dry-run
out_dir: {out_dir}

Topology:
  Nemotron A = user mic (simulated)
  Nemotron B = assistant.self_asr (simulated, separate)

Sequence:
  1. pause_playback @ first alnum user token ({cfg.pause_at_ms} ms)
  2. drain B on already-emitted audio (window {drain_window_ms} ms)
  3. finalize @ user transcript_committed ({cfg.user_stop_at_ms} ms)
  4. primary = drain+align  |  fallback = last_pos + pad({cfg.pad_words})
  5. commit once (commit.json); late self-ASR does not revise

Result:
  cut_source={decision.source}
  production_cut_text={decision.production_cut_text!r}
  prefix_valid={decision.prefix_valid}
  drain_complete={decision.drain_complete}
  aligned_word_count={decision.aligned_word_count}
  align_confidence={decision.align_confidence:.3f}
  fallback_text={decision.fallback_text!r}

Re-run:
  python3 scripts/barge-in-dual-asr.py --mode dry-run --out-dir {out_dir}

See docs/barge-in-dual-asr.md
"""
    _write_text(out_dir / "README-run.txt", readme)

    _append_event(
        events_path,
        {
            "event": "dual_sim_completed",
            "cut_source": decision.source,
            "prefix_valid": decision.prefix_valid,
            "production_cut_text": decision.production_cut_text,
            "out_dir": str(out_dir),
        },
    )

    return {
        "out_dir": str(out_dir),
        "cut_source": decision.source,
        "production_cut_text": decision.production_cut_text,
        "prefix_valid": decision.prefix_valid,
        "drain_complete": decision.drain_complete,
        "metrics": metrics,
        "commit": commit,
    }
