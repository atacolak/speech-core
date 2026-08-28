#!/usr/bin/env python3
"""Synthetic TTS↔TTS dual-role eval for assistant self-ASR cut (eval_only).

Both roles are TTS-shaped (no mic dirt, no acoustic echo):

  Assistant role — speech-out speaks intended LLM text (stream B / Nemotron-B)
  User role      — second TTS or fixture speaks a barge-in utterance after a
                   controlled delay (stream A / Nemotron-A shaped clock)

Cut measurement follows contract rev 3/4 + checkpoints 009–011:

  1. Pause assistant playback on first user alphanumeric token.
  2. Drain assistant Nemotron-B on already-emitted audio during user speech.
  3. At user transcript_committed:
       PRIMARY  — drained+aligned intended prefix
       FALLBACK — last pos + pad_words (~2) from intended
  4. Commit once; label=eval_only.

Offline dry/mock path needs no daemons. Optional live/synth wiring is
documented in docs/assistant-self-asr-eval.md and emit_live_synth_checklist().

Biases (must stay labeled):
  - TTS↔TTS is not acoustic-echo truth
  - No mic dirt / no play→mic loop
  - Dual clocks are mock-linear unless live instrumentation is attached
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from assistant_self_asr_cut import (
    DEFAULT_PAD_WORDS,
    CutSource,
    commit_truncated_assistant_message,
    mock_alignment_clock,
    mock_drain_during_user_speech,
    normalize_whitespace,
    score_three_way,
    tokenize_words,
)

DEFAULT_ASSISTANT_TEXT = (
    "The quick brown fox jumps over the lazy dog while the assistant "
    "continues speaking about barge in calibration and pad words."
)
DEFAULT_USER_BARGE_TEXT = "wait stop please hold on a second"


@dataclass
class DualClockConfig:
    """Dual-Nemotron-shaped synthetic timeline knobs."""

    intended_text: str = DEFAULT_ASSISTANT_TEXT
    user_barge_text: str = DEFAULT_USER_BARGE_TEXT
    pad_words: int = DEFAULT_PAD_WORDS
    # Assistant stream (synthesis / Nemotron-B shaped)
    assistant_words_per_second: float = 3.0
    # Playback trails synthesis (inquisitor R1)
    playback_lag_words: int = 1
    # User barge-in schedule (from assistant playback start)
    barge_in_delay_ms: float = 1200.0
    # User speech duration until transcript_committed (free drain window)
    user_speech_ms: float = 1000.0
    # Assistant drain rate during user speech (Nemotron-B)
    drain_words_per_second: float = 12.0
    # Optional lag before drain starts after pause
    drain_start_lag_ms: float = 0.0
    # Force fallback path even if drain would complete (eval of pad path)
    force_fallback: bool = False
    # Incomplete-drain simulation: cap drained words below emitted
    drain_cap_words: int | None = None
    utterance_id: str = "eval-tts-tts-1"
    label: str = "eval_only"


@dataclass
class DualClockResult:
    """Artifacts + metrics for one TTS↔TTS dry run."""

    config: DualClockConfig
    intended_text: str
    user_barge_text: str
    pause_ms: float
    user_stop_ms: float
    assistant_emitted_at_pause: int
    playback_pos_at_pause: int
    drained_text: str
    drained_pos_words: int
    drain_complete: bool
    primary_cut_source: CutSource
    production_cut_text: str
    intended_at_playback: str
    asr_recovered_at_stop: str
    prefix_valid: bool
    overspeak_words: int
    underspeak_words: int
    pad_words: int
    commit: dict[str, Any]
    metrics: dict[str, Any]
    events: list[dict[str, Any]] = field(default_factory=list)
    biases: list[str] = field(default_factory=list)

    def write_artifacts(self, out_dir: Path) -> None:
        out_dir.mkdir(parents=True, exist_ok=True)

        def _w(name: str, text: str) -> None:
            p = out_dir / name
            p.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")

        _w("assistant_intended.txt", self.intended_text)
        _w("user_barge_text.txt", self.user_barge_text)
        _w("production_cut_text", self.production_cut_text)
        _w("intended_at_playback.txt", self.intended_at_playback)
        _w("asr_recovered_at_stop.txt", self.asr_recovered_at_stop)
        (out_dir / "metrics.json").write_text(
            json.dumps(self.metrics, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        (out_dir / "commit.json").write_text(
            json.dumps(self.commit, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        events_path = out_dir / "events.jsonl"
        with events_path.open("w", encoding="utf-8") as fh:
            for ev in self.events:
                fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
        _w("README-run.txt", self._readme(out_dir))

    def _readme(self, out_dir: Path) -> str:
        return f"""assistant-self-asr TTS↔TTS eval_only artifacts
================================================

label: {self.metrics.get("label", "eval_only")}
mode: tts-tts (synthetic dual-role TTS clocks)
out_dir: {out_dir}
primary_cut_source: {self.primary_cut_source}

Roles:
  assistant TTS (intended): {self.intended_text!r}
  user-role TTS (barge-in): {self.user_barge_text!r}

Timeline:
  barge_in / pause @ {self.pause_ms} ms
  user transcript_committed @ {self.user_stop_ms} ms
  user_speech window = {self.user_stop_ms - self.pause_ms} ms (drain window)
  assistant emitted@pause = {self.assistant_emitted_at_pause} words
  playback@pause = {self.playback_pos_at_pause} words
  drain_complete = {self.drain_complete}
  drained_pos = {self.drained_pos_words} words

Cut:
  production_cut_text = {self.production_cut_text!r}
  intended_at_playback = {self.intended_at_playback!r}
  asr_recovered_at_stop = {self.asr_recovered_at_stop!r}

Gates:
  prefix_valid={self.prefix_valid}
  overspeak_words={self.overspeak_words}
  underspeak_words={self.underspeak_words}
  pad_words={self.pad_words} (FALLBACK only when primary_cut_source=fallback)

Biases (labeled):
{chr(10).join("  - " + b for b in self.biases)}

Re-run:
  python3 scripts/assistant-self-asr-harness.py --mode tts-tts --out-dir {out_dir}

See docs/assistant-self-asr-eval.md
"""


def run_dual_clock_tts_eval(cfg: DualClockConfig) -> DualClockResult:
    """Run offline dual-clock TTS↔TTS founder-cut measurement."""
    intended = normalize_whitespace(cfg.intended_text)
    user_barge = normalize_whitespace(cfg.user_barge_text)
    if not intended:
        raise ValueError("intended_text is empty")
    if cfg.pad_words < 0:
        raise ValueError("pad_words must be >= 0")
    if cfg.user_speech_ms < 0:
        raise ValueError("user_speech_ms must be >= 0")
    if cfg.barge_in_delay_ms < 0:
        raise ValueError("barge_in_delay_ms must be >= 0")

    pause_ms = float(cfg.barge_in_delay_ms)
    user_stop_ms = pause_ms + float(cfg.user_speech_ms)
    words = tokenize_words(intended)
    total_words = len(words)

    # Assistant stream advances until pause, then freezes emit (cancel further speak).
    assistant_emitted_at_pause = mock_alignment_clock(
        intended,
        words_per_second=cfg.assistant_words_per_second,
        elapsed_ms=pause_ms,
    )
    playback_pos_at_pause = max(
        0, assistant_emitted_at_pause - max(0, int(cfg.playback_lag_words))
    )

    drained_text, drained_pos, drain_complete = mock_drain_during_user_speech(
        intended,
        emitted_pos_words_at_pause=assistant_emitted_at_pause,
        drain_words_per_second=cfg.drain_words_per_second,
        user_speech_ms=cfg.user_speech_ms,
        drain_start_lag_ms=cfg.drain_start_lag_ms,
    )
    if cfg.drain_cap_words is not None:
        cap = max(0, int(cfg.drain_cap_words))
        drained_pos = min(drained_pos, cap)
        drained_text = " ".join(words[:drained_pos]) if drained_pos else ""
        drain_complete = drained_pos >= assistant_emitted_at_pause

    use_drain = drain_complete and not cfg.force_fallback
    # Fallback last pos: partial drain progress if any, else pause emit pos.
    last_aligned = drained_pos if drained_pos > 0 else assistant_emitted_at_pause

    scored = score_three_way(
        intended,
        playback_pos_words=playback_pos_at_pause,
        nemotron_pos_words=drained_pos if use_drain else last_aligned,
        asr_recovered_at_stop=drained_text,
        pad_words=cfg.pad_words,
        drain_complete=use_drain,
        drained_asr=drained_text,
        last_aligned_pos_words=last_aligned,
    )
    # If force_fallback, score_three_way already used fallback because drain_complete=False.
    if cfg.force_fallback:
        assert scored.primary_cut_source == "fallback"

    commit = commit_truncated_assistant_message(
        scored.production_cut,
        utterance_id=cfg.utterance_id,
        primary_cut_source=scored.primary_cut_source,
    )

    biases = [
        "tts_tts_not_acoustic_echo_truth",
        "no_mic_dirt",
        "no_play_to_mic_loop",
        "dual_nemotron_clocks_are_mock_linear",
        "assistant_stream_and_user_stream_are_separate_mock_instances",
        "pad_words_is_fallback_only",
        "results_are_eval_only",
    ]

    now_ns = time.monotonic_ns()
    events: list[dict[str, Any]] = []

    def ev(payload: dict[str, Any]) -> None:
        e = dict(payload)
        e.setdefault("label", cfg.label)
        e.setdefault("diagnostic_mono_ns", now_ns)
        events.append(e)

    ev(
        {
            "event": "eval_run_started",
            "mode": "tts-tts",
            "roles": {
                "assistant": "tts_intended_llm_text",
                "user": "tts_or_fixture_barge_in",
            },
            "capture_locus": ["eval_playback", "eval_synthesis_stub", "eval_user_tts_stub"],
            "intended_word_count": total_words,
            "user_barge_word_count": len(tokenize_words(user_barge)),
            "pad_words": cfg.pad_words,
            "dual_nemotron": {
                "user_stream": "Nemotron-A-shaped (mock)",
                "assistant_stream": "Nemotron-B-shaped (mock)",
                "shared_worker": False,
            },
        }
    )
    ev(
        {
            "event": "assistant_tts_started",
            "role": "assistant",
            "text_word_count": total_words,
            "words_per_second": cfg.assistant_words_per_second,
        }
    )
    ev(
        {
            "event": "user_role_tts_barge_in_scheduled",
            "role": "user",
            "barge_in_delay_ms": pause_ms,
            "user_speech_ms": cfg.user_speech_ms,
            "user_barge_text": user_barge,
        }
    )
    ev(
        {
            "event": "user_first_alphanumeric_token",
            "trigger": "transcript_token_committed",
            "action": "pause_playback",
            "pause_ms": pause_ms,
            "assistant_emitted_pos_words": assistant_emitted_at_pause,
            "playback_pos_words": playback_pos_at_pause,
            "note": (
                "Mirrors scripts/speech-out-live-session.sh: first alphanumeric "
                "user token cancels/pauses active speech-out."
            ),
        }
    )
    ev(
        {
            "event": "speech_out_barge_in",
            "trigger": "transcript_token_committed",
            "pause_ms": pause_ms,
            "eval_playback": True,
            "cancel_further_speak": True,
        }
    )
    ev(
        {
            "event": "assistant_self_asr_drain_started",
            "stream": "Nemotron-B",
            "emitted_pos_words": assistant_emitted_at_pause,
            "drain_words_per_second": cfg.drain_words_per_second,
            "drain_start_lag_ms": cfg.drain_start_lag_ms,
            "note": "Drain only already-emitted audio; free during user speech window.",
        }
    )
    ev(
        {
            "event": "user_transcript_committed",
            "t0": "user_stop",
            "user_stop_ms": user_stop_ms,
            "user_speech_ms": cfg.user_speech_ms,
            "action": "finalize_truncated_assistant",
            "drained_pos_words": drained_pos,
            "drain_complete": drain_complete,
            "force_fallback": cfg.force_fallback,
        }
    )
    ev(commit)
    ev(
        {
            "event": "late_self_asr_drain_eval_only",
            "diagnostic_only": True,
            "revises_commit": False,
            "asr_recovered_at_stop": scored.asr_recovered_at_stop,
            "committed_production_cut_text": commit["production_cut_text"],
            "primary_cut_source": scored.primary_cut_source,
        }
    )
    ev(
        {
            "event": "eval_run_completed",
            "primary_cut_source": scored.primary_cut_source,
            "prefix_valid": scored.prefix_valid,
            "overspeak_words": scored.overspeak_words,
            "underspeak_words": scored.underspeak_words,
        }
    )

    metrics: dict[str, Any] = {
        "label": cfg.label,
        "mode": "tts-tts",
        "primary_cut_source": scored.primary_cut_source,
        "production_cut_text": scored.production_cut,
        "intended_at_playback": scored.intended_at_playback,
        "asr_recovered_at_stop": scored.asr_recovered_at_stop,
        "prefix_valid": scored.prefix_valid,
        "overspeak_words": scored.overspeak_words,
        "underspeak_words": scored.underspeak_words,
        "pad_words": scored.pad_words,
        "founder_cut_rule": {
            "pause_on": "first_user_alphanumeric_token",
            "finalize_on": "user_stop_transcript_committed",
            "primary": "drain_during_user_speech_force_aligned_to_intended",
            "fallback": "last_aligned_pos_plus_pad_words_from_intended",
            "pad_words": cfg.pad_words,
            "pad_source": "intended_llm_text",
            "pad_is_fallback_only": True,
            "commit_once": True,
            "late_self_asr": "diagnostic_only",
            "dual_nemotron": True,
        },
        "roles": {
            "assistant": {
                "kind": "tts",
                "text": intended,
                "stream": "Nemotron-B-shaped",
            },
            "user": {
                "kind": "tts_or_fixture",
                "text": user_barge,
                "stream": "Nemotron-A-shaped",
                "barge_in_delay_ms": pause_ms,
                "user_speech_ms": cfg.user_speech_ms,
            },
        },
        "timing_ms": {
            "pause_ms": pause_ms,
            "user_stop_ms": user_stop_ms,
            "user_speech_ms": cfg.user_speech_ms,
            "drain_start_lag_ms": cfg.drain_start_lag_ms,
            "note": (
                "Synthetic dual-role offsets from assistant playback start. "
                "Not a shared multi-clock reconstruction (inquisitor R5 open)."
            ),
        },
        "alignment": {
            "capture_locus_playback": "eval_playback",
            "capture_locus_synthesis": "eval_synthesis_stub",
            "capture_locus_user": "eval_user_tts_stub",
            "assistant_emitted_at_pause": assistant_emitted_at_pause,
            "playback_pos_words_at_pause": playback_pos_at_pause,
            "nemotron_pos_at_stop": drained_pos if use_drain else last_aligned,
            "drained_pos_words": drained_pos,
            "drain_complete": drain_complete,
            "force_fallback": cfg.force_fallback,
            "assistant_words_per_second_mock": cfg.assistant_words_per_second,
            "drain_words_per_second_mock": cfg.drain_words_per_second,
            "playback_lag_words_config": cfg.playback_lag_words,
            "total_intended_words": total_words,
            "alignment_clock": "dual_mock_linear_words_per_second",
            "todo_live_synth": [
                "Wire assistant speech-out TTS (daemon play or say) for intended text",
                "Wire user-role TTS or fixture WAV after barge_in_delay_ms",
                "Instrument play samples/chunks for eval_playback clock",
                "Wire assistant-tracking Nemotron-B drain on emitted PCM",
                "User stream remains separate Nemotron-A (dual instance)",
                "Do not change speech-core-protocol event vocabulary without approval",
            ],
        },
        "three_way": scored.to_metrics_dict(),
        "biases_labeled": biases,
    }

    return DualClockResult(
        config=cfg,
        intended_text=intended,
        user_barge_text=user_barge,
        pause_ms=pause_ms,
        user_stop_ms=user_stop_ms,
        assistant_emitted_at_pause=assistant_emitted_at_pause,
        playback_pos_at_pause=playback_pos_at_pause,
        drained_text=drained_text,
        drained_pos_words=drained_pos,
        drain_complete=drain_complete and not cfg.force_fallback,
        primary_cut_source=scored.primary_cut_source,
        production_cut_text=scored.production_cut,
        intended_at_playback=scored.intended_at_playback,
        asr_recovered_at_stop=scored.asr_recovered_at_stop,
        prefix_valid=scored.prefix_valid,
        overspeak_words=scored.overspeak_words,
        underspeak_words=scored.underspeak_words,
        pad_words=scored.pad_words,
        commit=commit,
        metrics=metrics,
        events=events,
        biases=biases,
    )


def emit_live_synth_checklist(out_dir: Path) -> dict[str, Any]:
    """Document optional live/synth path (assistant TTS + user-role TTS)."""
    checklist = {
        "label": "eval_only",
        "mode": "live_synth_stub",
        "status": "not_wired",
        "roles": {
            "assistant": "speech-out TTS of intended LLM text",
            "user": "second TTS or fixture WAV barge-in after controlled delay",
        },
        "founder_cut_rule": {
            "primary": "drain_during_user_speech_force_aligned_to_intended",
            "fallback": "last_pos_plus_pad_words",
            "pad_words_default": DEFAULT_PAD_WORDS,
            "pad_is_fallback_only": True,
            "dual_nemotron": True,
        },
        "todo": [
            {
                "id": "assistant_tts",
                "desc": (
                    "Start speech-out daemon + play intended text; capture "
                    "eval_playback samples/chunks and optional eval_synthesis PCM."
                ),
            },
            {
                "id": "user_role_tts",
                "desc": (
                    "After barge_in_delay_ms, synthesize/play user barge utterance "
                    "(speech-out say/play or fixture WAV). Not a real mic path."
                ),
            },
            {
                "id": "assistant_drain_b",
                "desc": (
                    "Nemotron-B drains already-emitted assistant audio during "
                    "user-role speech window; dual instance, not shared worker."
                ),
            },
            {
                "id": "finalize_at_user_commit",
                "desc": (
                    "At user transcript_committed (or mock stop), apply "
                    "apply_production_cut: drain primary, pad fallback."
                ),
            },
            {
                "id": "artifact_dir",
                "desc": (
                    "Write metrics.json with primary_cut_source, production_cut_text, "
                    "intended_at_playback, asr_recovered_at_stop, prefix_valid, "
                    "overspeak/underspeak, pad_words, label=eval_only."
                ),
            },
        ],
        "biases_labeled": [
            "tts_tts_not_acoustic_echo_truth",
            "no_mic_dirt",
            "no_play_to_mic_loop",
            "results_are_eval_only",
        ],
        "forbidden": [
            "speech-core-protocol shared event vocabulary changes",
            "speech-core-daemon shared event vocabulary changes",
            "editing scripts/barge-in-* (other track)",
            "treating TTS↔TTS metrics as acoustic-echo production evidence",
        ],
        "offline_path": (
            "python3 scripts/assistant-self-asr-harness.py --mode tts-tts"
        ),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "live_synth_wiring_gap.json").write_text(
        json.dumps(checklist, indent=2) + "\n", encoding="utf-8"
    )
    return checklist


def run_self_check() -> None:
    """Unit assertions for dual-clock drain-primary path."""
    # Fast drain over long user speech → primary=drain, cut == emitted@pause
    cfg = DualClockConfig(
        intended_text="one two three four five six seven eight nine ten",
        user_barge_text="wait stop",
        pad_words=2,
        assistant_words_per_second=3.0,
        barge_in_delay_ms=1000.0,  # emitted = 3 words
        user_speech_ms=1000.0,
        drain_words_per_second=20.0,  # drains all 3 easily
        playback_lag_words=1,
        force_fallback=False,
    )
    r = run_dual_clock_tts_eval(cfg)
    assert r.assistant_emitted_at_pause == 3, r.assistant_emitted_at_pause
    assert r.drain_complete is True
    assert r.primary_cut_source == "drain", r.primary_cut_source
    assert r.production_cut_text == "one two three", r.production_cut_text
    assert r.intended_at_playback == "one two", r.intended_at_playback
    assert r.prefix_valid is True
    assert r.metrics["label"] == "eval_only"
    assert r.metrics["primary_cut_source"] == "drain"
    assert "tts_tts_not_acoustic_echo_truth" in r.biases

    # Force fallback → pad path
    cfg2 = DualClockConfig(
        intended_text="one two three four five six seven eight nine ten",
        pad_words=2,
        assistant_words_per_second=3.0,
        barge_in_delay_ms=1000.0,
        user_speech_ms=1000.0,
        drain_words_per_second=20.0,
        playback_lag_words=1,
        force_fallback=True,
    )
    r2 = run_dual_clock_tts_eval(cfg2)
    assert r2.primary_cut_source == "fallback", r2.primary_cut_source
    # last_aligned from full drain pos 3 + pad 2 → five words
    assert r2.production_cut_text == "one two three four five", r2.production_cut_text

    # Incomplete drain (partial progress then cap) → fallback
    # user_speech long enough that mock drain would complete; cap freezes at 2.
    cfg3 = DualClockConfig(
        intended_text="one two three four five six seven eight nine ten",
        pad_words=2,
        assistant_words_per_second=3.0,
        barge_in_delay_ms=2000.0,  # emitted = 6
        user_speech_ms=2000.0,
        drain_words_per_second=20.0,
        drain_cap_words=2,
        playback_lag_words=0,
        force_fallback=False,
    )
    r3 = run_dual_clock_tts_eval(cfg3)
    assert r3.primary_cut_source == "fallback", r3.primary_cut_source
    assert r3.drained_pos_words == 2, r3.drained_pos_words
    # last_aligned=2 + pad 2 → four words
    assert r3.production_cut_text == "one two three four", r3.production_cut_text


if __name__ == "__main__":
    run_self_check()
    print("assistant_self_asr_tts_eval self-check: PASS")
