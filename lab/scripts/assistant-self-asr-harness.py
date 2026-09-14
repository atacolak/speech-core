#!/usr/bin/env python3
"""Eval-only assistant self-ASR / founder-cut harness.

Measures the founder cut rule from speech-to-speech-contract.md rev 3/4:

  pause on first user alphanumeric token
  drain assistant self-ASR during user speech (dual Nemotron-B)
  finalize at user stop / transcript_committed
  PRIMARY  = drained+aligned intended prefix
  FALLBACK = last pos + pad_words (~2) from intended LLM text
  commit once; late self-ASR is diagnostic only

Modes
-----
  dry-run (default): offline synthetic single-stream timeline, no daemons.
  tts-tts: offline dual-role TTS↔TTS clocks (assistant TTS + user-role TTS).
  tui: interactive peek (keypress barge-in, dual transcripts, cut decision).
  live (stub): documents wiring gaps; refuses unless --allow-live-stub.
  live-synth (stub): documents assistant TTS + user TTS barge-in checklist.

Artifacts (under --out-dir, all labeled eval_only)
-------------------------------------------------
  assistant_intended.txt
  production_cut_text
  metrics.json            # primary_cut_source, pad_words, three-way, label
  events.jsonl
  commit.json             # truncated assistant message concept (log only)
  asr_recovered_at_stop.txt
  intended_at_playback.txt
  user_barge_text.txt     # tts-tts only
  README-run.txt

Capture locus labels follow the contract:
  eval_playback  — mock or instrumented samples/chunks played
  eval_synthesis — optional server PCM / second ASR (stubbed offline)

Do NOT treat green metrics as production evidence (see inquisitor R1–R7).
TTS↔TTS is not acoustic-echo truth; no mic dirt.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

# Allow `python scripts/assistant-self-asr-harness.py` without installing a package.
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from assistant_self_asr_cut import (  # noqa: E402
    DEFAULT_PAD_WORDS,
    apply_founder_cut,
    apply_production_cut,
    commit_truncated_assistant_message,
    force_align_drained_to_intended,
    is_alphanumeric_token,
    mock_alignment_clock,
    mock_drain_during_user_speech,
    normalize_whitespace,
    score_three_way,
    tokenize_words,
)
from assistant_self_asr_tts_eval import (  # noqa: E402
    DEFAULT_USER_BARGE_TEXT,
    DualClockConfig,
    emit_live_synth_checklist,
    run_dual_clock_tts_eval,
)
from assistant_self_asr_tui import (  # noqa: E402
    TuiConfig,
    run_interactive_tui,
)

DEFAULT_INTENDED = (
    "The quick brown fox jumps over the lazy dog while the assistant "
    "continues speaking about barge in calibration and pad words."
)


def _write_text(path: Path, text: str) -> None:
    path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")


def _append_event(events_path: Path, event: dict[str, Any]) -> None:
    event = dict(event)
    event.setdefault("label", "eval_only")
    event.setdefault("diagnostic_mono_ns", time.monotonic_ns())
    with events_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Eval-only founder-cut harness for assistant self-ASR calibration. "
            "Default mode is offline dry-run. Primary cut = drain-during-user-speech; "
            "pad_words is FALLBACK only."
        )
    )
    p.add_argument(
        "--mode",
        choices=("dry-run", "tts-tts", "tui", "live", "live-synth"),
        default="dry-run",
        help=(
            "dry-run: synthetic offline. tts-tts: dual-role TTS clocks. "
            "tui: interactive peek with keypress barge-in. "
            "live / live-synth: stubs only unless allowed."
        ),
    )
    p.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Artifact directory (default: /tmp/assistant-self-asr-eval-<ts>).",
    )
    p.add_argument(
        "--intended-text",
        default=DEFAULT_INTENDED,
        help="Intended LLM / TTS text the speaker layer is faking.",
    )
    p.add_argument(
        "--intended-file",
        type=Path,
        default=None,
        help="Read intended text from file (overrides --intended-text).",
    )
    p.add_argument(
        "--user-barge-text",
        default=DEFAULT_USER_BARGE_TEXT,
        help="User-role TTS barge-in utterance (tts-tts mode).",
    )
    p.add_argument(
        "--pad-words",
        type=int,
        default=DEFAULT_PAD_WORDS,
        help=(
            f"FALLBACK pad from intended text when drain incomplete "
            f"(default {DEFAULT_PAD_WORDS}). Not the happy path."
        ),
    )
    p.add_argument(
        "--pause-at-ms",
        type=float,
        default=1200.0,
        help="Dry-run / tts-tts: wall offset when first alphanumeric user token arrives.",
    )
    p.add_argument(
        "--user-stop-at-ms",
        type=float,
        default=2200.0,
        help="Dry-run: wall offset of user stop / transcript_committed (T0).",
    )
    p.add_argument(
        "--user-speech-ms",
        type=float,
        default=None,
        help=(
            "tts-tts: user barge speech duration (drain window). "
            "Default: user-stop-at-ms - pause-at-ms when both set, else 1000."
        ),
    )
    p.add_argument(
        "--words-per-second",
        type=float,
        default=3.0,
        help="Mock assistant alignment clock rate for intended words.",
    )
    p.add_argument(
        "--drain-words-per-second",
        type=float,
        default=12.0,
        help="Mock assistant Nemotron-B drain rate during user speech.",
    )
    p.add_argument(
        "--drain-start-lag-ms",
        type=float,
        default=0.0,
        help="Optional lag before drain starts after pause.",
    )
    p.add_argument(
        "--force-fallback",
        action="store_true",
        help="Force pad_words fallback path even if drain would complete.",
    )
    p.add_argument(
        "--playback-lag-words",
        type=int,
        default=1,
        help=(
            "How many words playback trails the mock emit/Nemotron clock "
            "at pause (models synthesis-ahead-of-playback bias, inquisitor R1)."
        ),
    )
    p.add_argument(
        "--asr-lag-words",
        type=int,
        default=2,
        help=(
            "Dry-run legacy: how many words mock second-ASR recovery trails "
            "when not using drain path (diagnostic only; ignored when drain runs)."
        ),
    )
    p.add_argument(
        "--utterance-id",
        default="eval-assistant-1",
        help="Utterance id for log-only commit artifact.",
    )
    p.add_argument(
        "--allow-live-stub",
        action="store_true",
        help="Permit --mode live / live-synth which currently only document wiring gaps.",
    )
    p.add_argument(
        "--auto-barge-ms",
        type=float,
        default=None,
        help=(
            "tui: auto barge-in after N ms if no keypress. "
            "Default: wait for keypress; if no tty, auto @ 1200 ms."
        ),
    )
    p.add_argument(
        "--play",
        action="store_true",
        help=(
            "tui: best-effort speech-out TTS for assistant + user-role "
            "(continues mock-only if speech-out unavailable)."
        ),
    )
    p.add_argument(
        "--play-command",
        default=None,
        help="tui: shell command for TTS (else SPEECH_OUT_PLAY_CMD or speech-out say mock).",
    )
    p.add_argument(
        "--self-check",
        action="store_true",
        help="Run built-in unit assertions on the cut rule + tts-tts, then exit.",
    )
    return p


def run_self_check() -> int:
    """Deterministic unit-testable path without writing artifacts."""
    intended = "one two three four five six seven eight"

    # FALLBACK helper still: pos 3 + pad 2
    cut = apply_founder_cut(intended, nemotron_pos_words=3, pad_words=2)
    assert cut == "one two three four five", cut
    cut_end = apply_founder_cut(intended, nemotron_pos_words=7, pad_words=2)
    assert cut_end == intended
    cut_zero = apply_founder_cut(intended, nemotron_pos_words=0, pad_words=2)
    assert cut_zero == "one two", cut_zero

    # Drain-primary production cut
    drain_cut, src = apply_production_cut(
        intended,
        drained_asr="one two three four",
        drain_complete=True,
        last_aligned_pos_words=2,
        pad_words=2,
    )
    assert src == "drain", src
    assert drain_cut == "one two three four", drain_cut

    # Incomplete drain → fallback
    fb_cut, fb_src = apply_production_cut(
        intended,
        drained_asr="one two",
        drain_complete=False,
        last_aligned_pos_words=2,
        pad_words=2,
    )
    assert fb_src == "fallback", fb_src
    assert fb_cut == "one two three four", fb_cut

    aligned, n = force_align_drained_to_intended(
        "ONE two THREE invented", intended
    )
    assert n == 3 and aligned == "one two three", (aligned, n)

    assert is_alphanumeric_token("hello")
    assert is_alphanumeric_token("A")
    assert not is_alphanumeric_token("...")
    assert not is_alphanumeric_token(",")

    # score_three_way with drain primary
    scored = score_three_way(
        intended,
        playback_pos_words=3,
        nemotron_pos_words=3,
        asr_recovered_at_stop="one two three",
        pad_words=2,
        drain_complete=True,
        drained_asr="one two three",
    )
    assert scored.primary_cut_source == "drain"
    assert scored.production_cut == "one two three"
    assert scored.intended_at_playback == "one two three"
    assert scored.prefix_valid is True
    assert scored.overspeak_words == 0
    assert scored.underspeak_words == 0
    assert scored.label == "eval_only"

    # score_three_way fallback (legacy pad path)
    scored_fb = score_three_way(
        intended,
        playback_pos_words=3,
        nemotron_pos_words=3,
        asr_recovered_at_stop="one two",
        pad_words=2,
        drain_complete=False,
    )
    assert scored_fb.primary_cut_source == "fallback"
    assert scored_fb.production_cut == "one two three four five"
    assert scored_fb.overspeak_words == 2

    # Non-prefix free-form ASR must not be used as production cut.
    free = "totally invented words"
    from assistant_self_asr_cut import is_prefix_of_intended

    assert not is_prefix_of_intended(free, intended)

    # Mock clocks
    pos = mock_alignment_clock(intended, words_per_second=2.0, elapsed_ms=1500)
    assert pos == 3, pos

    drained, dpos, complete = mock_drain_during_user_speech(
        intended,
        emitted_pos_words_at_pause=5,
        drain_words_per_second=20.0,
        user_speech_ms=500.0,
    )
    assert complete is True and dpos == 5 and drained == "one two three four five"

    # Dual-clock TTS↔TTS module self-check
    from assistant_self_asr_tts_eval import run_self_check as tts_self_check

    tts_self_check()

    from assistant_self_asr_tui import run_self_check as tui_self_check

    tui_self_check()

    print("self-check: PASS")
    print(f"  drain production_cut: {scored.production_cut!r} source={scored.primary_cut_source}")
    print(f"  fallback production_cut: {scored_fb.production_cut!r} source={scored_fb.primary_cut_source}")
    print(f"  sample metrics: {json.dumps(scored.to_metrics_dict(), sort_keys=True)}")
    return 0


def _resolve_intended(args: argparse.Namespace) -> str:
    if args.intended_file is not None:
        intended = args.intended_file.read_text(encoding="utf-8")
    else:
        intended = args.intended_text
    return normalize_whitespace(intended)


def run_dry_run(args: argparse.Namespace) -> int:
    intended = _resolve_intended(args)
    if not intended:
        print("error: intended text is empty", file=sys.stderr)
        return 2
    if args.pad_words < 0:
        print("error: --pad-words must be >= 0", file=sys.stderr)
        return 2
    if args.user_stop_at_ms < args.pause_at_ms:
        print(
            "error: --user-stop-at-ms must be >= --pause-at-ms "
            "(pause first, finalize later)",
            file=sys.stderr,
        )
        return 2

    out_dir: Path = args.out_dir or Path(
        f"/tmp/assistant-self-asr-eval-{time.strftime('%Y%m%d-%H%M%S')}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    events_path = out_dir / "events.jsonl"
    if events_path.exists():
        events_path.unlink()

    words = tokenize_words(intended)
    total_words = len(words)

    # --- Synthetic timeline (eval_only) ---------------------------------
    # T_pause: first alphanumeric user token -> pause playback (existing).
    # During user speech: drain assistant Nemotron-B on already-emitted audio.
    # T0 / user_stop: transcript_committed -> finalize cut (drain primary).
    pause_ms = float(args.pause_at_ms)
    stop_ms = float(args.user_stop_at_ms)
    user_speech_ms = stop_ms - pause_ms

    _append_event(
        events_path,
        {
            "event": "eval_run_started",
            "mode": "dry-run",
            "capture_locus": ["eval_playback", "eval_synthesis_stub"],
            "intended_word_count": total_words,
            "pad_words": args.pad_words,
            "pad_is_fallback_only": True,
            "words_per_second": args.words_per_second,
            "drain_words_per_second": args.drain_words_per_second,
        },
    )

    # Emit clock freezes at pause (cancel further speak).
    emitted_at_pause = mock_alignment_clock(
        intended, words_per_second=args.words_per_second, elapsed_ms=pause_ms
    )
    playback_at_pause = max(
        0, emitted_at_pause - max(0, int(args.playback_lag_words))
    )

    drained_text, drained_pos, drain_complete = mock_drain_during_user_speech(
        intended,
        emitted_pos_words_at_pause=emitted_at_pause,
        drain_words_per_second=args.drain_words_per_second,
        user_speech_ms=user_speech_ms,
        drain_start_lag_ms=args.drain_start_lag_ms,
    )
    use_drain = drain_complete and not args.force_fallback
    last_aligned = drained_pos if drained_pos > 0 else emitted_at_pause

    _append_event(
        events_path,
        {
            "event": "user_first_alphanumeric_token",
            "trigger": "transcript_token_committed",
            "action": "pause_playback",
            "pause_ms": pause_ms,
            "mock_emitted_pos_words": emitted_at_pause,
            "playback_pos_words": playback_at_pause,
            "note": (
                "Mirrors scripts/speech-out-live-session.sh: first alphanumeric "
                "Nemotron user token cancels/pauses active speech-out."
            ),
        },
    )
    _append_event(
        events_path,
        {
            "event": "speech_out_barge_in",
            "trigger": "transcript_token_committed",
            "pause_ms": pause_ms,
            "eval_playback": True,
            "cancel_further_speak": True,
        },
    )
    _append_event(
        events_path,
        {
            "event": "assistant_self_asr_drain_started",
            "stream": "Nemotron-B-mock",
            "emitted_pos_words": emitted_at_pause,
            "drain_words_per_second": args.drain_words_per_second,
            "user_speech_ms": user_speech_ms,
        },
    )
    _append_event(
        events_path,
        {
            "event": "user_transcript_committed",
            "t0": "user_stop",
            "user_stop_ms": stop_ms,
            "action": "finalize_truncated_assistant",
            "drained_pos_words": drained_pos,
            "drain_complete": drain_complete,
            "force_fallback": args.force_fallback,
        },
    )

    scored = score_three_way(
        intended,
        playback_pos_words=playback_at_pause,
        nemotron_pos_words=drained_pos if use_drain else last_aligned,
        asr_recovered_at_stop=drained_text,
        pad_words=args.pad_words,
        drain_complete=use_drain,
        drained_asr=drained_text,
        last_aligned_pos_words=last_aligned,
    )
    commit = commit_truncated_assistant_message(
        scored.production_cut,
        utterance_id=args.utterance_id,
        primary_cut_source=scored.primary_cut_source,
    )
    _append_event(events_path, commit)

    # Late self-ASR drain is diagnostic only — log it, do not revise commit.
    _append_event(
        events_path,
        {
            "event": "late_self_asr_drain_eval_only",
            "diagnostic_only": True,
            "revises_commit": False,
            "asr_recovered_at_stop": scored.asr_recovered_at_stop,
            "committed_production_cut_text": commit["production_cut_text"],
            "primary_cut_source": scored.primary_cut_source,
        },
    )

    metrics: dict[str, Any] = {
        "label": "eval_only",
        "mode": "dry-run",
        "primary_cut_source": scored.primary_cut_source,
        "founder_cut_rule": {
            "pause_on": "first_user_alphanumeric_token",
            "finalize_on": "user_stop_transcript_committed",
            "primary": "drain_during_user_speech_force_aligned_to_intended",
            "fallback": "last_aligned_pos_plus_pad_words_from_intended",
            "pad_words": args.pad_words,
            "pad_source": "intended_llm_text",
            "pad_is_fallback_only": True,
            "commit_once": True,
            "late_self_asr": "diagnostic_only",
            "dual_nemotron": True,
        },
        "timing_ms": {
            "pause_ms": pause_ms,
            "user_stop_ms": stop_ms,
            "user_speech_ms": user_speech_ms,
            "note": (
                "Dry-run offsets from synthetic playback start. "
                "Not a shared multi-clock reconstruction (inquisitor R5 open)."
            ),
        },
        "alignment": {
            "capture_locus_playback": "eval_playback",
            "capture_locus_synthesis": "eval_synthesis_stub",
            "nemotron_pos_at_stop": drained_pos if use_drain else last_aligned,
            "emitted_pos_words_at_pause": emitted_at_pause,
            "playback_pos_words_at_stop": playback_at_pause,
            "drained_pos_words": drained_pos,
            "drain_complete": drain_complete,
            "force_fallback": args.force_fallback,
            "words_per_second_mock": args.words_per_second,
            "drain_words_per_second_mock": args.drain_words_per_second,
            "playback_lag_words_config": args.playback_lag_words,
            "total_intended_words": total_words,
            "alignment_clock": "mock_linear_words_per_second_with_drain",
            "todo_live": [
                "Wire instrumented speech-out play samples/chunks for playback clock",
                "Wire assistant-tracking Nemotron-B drain during user speech",
                "Optional second record-only websocket client for server PCM → second ASR",
                "Do not change speech-core-protocol event vocabulary without architecture approval",
            ],
        },
        "three_way": scored.to_metrics_dict(),
        # Flatten primary gates for quick jq.
        "prefix_valid": scored.prefix_valid,
        "overspeak_words": scored.overspeak_words,
        "underspeak_words": scored.underspeak_words,
        "pad_words": scored.pad_words,
        "production_cut_text": scored.production_cut,
        "intended_at_playback": scored.intended_at_playback,
        "asr_recovered_at_stop": scored.asr_recovered_at_stop,
        "biases_labeled": [
            "mock_alignment_clock_not_physical_playback",
            "no_acoustic_loop",
            "no_mic_dirt",
            "pad_words_is_fallback_only",
            "asr_recovered_is_stubbed_drain_from_intended",
            "results_are_eval_only",
        ],
    }

    _write_text(out_dir / "assistant_intended.txt", intended)
    _write_text(out_dir / "production_cut_text", scored.production_cut)
    _write_text(out_dir / "intended_at_playback.txt", scored.intended_at_playback)
    _write_text(out_dir / "asr_recovered_at_stop.txt", scored.asr_recovered_at_stop)
    (out_dir / "metrics.json").write_text(
        json.dumps(metrics, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (out_dir / "commit.json").write_text(
        json.dumps(commit, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    run_readme = f"""assistant-self-asr eval_only dry-run artifacts
=============================================

label: eval_only
mode: dry-run
out_dir: {out_dir}
primary_cut_source: {scored.primary_cut_source}

Founder cut rule applied (rev 3/4):
  1. pause_playback @ first alphanumeric user token ({pause_ms} ms synthetic)
  2. drain assistant self-ASR during user speech ({user_speech_ms} ms window)
  3. finalize @ user stop / transcript_committed ({stop_ms} ms synthetic)
  4. PRIMARY = drained+aligned intended prefix; FALLBACK = last pos + pad_words
  5. pad_words = {args.pad_words} (FALLBACK only)
  6. commit once (see commit.json); late self-ASR diagnostic only

Drain:
  emitted@pause = {emitted_at_pause}
  drained_pos = {drained_pos}
  drain_complete = {drain_complete}
  force_fallback = {args.force_fallback}

Three-way sample:
  intended_at_playback: {scored.intended_at_playback!r}
  asr_recovered_at_stop: {scored.asr_recovered_at_stop!r}
  production_cut_text:   {scored.production_cut!r}

Gates:
  primary_cut_source={scored.primary_cut_source}
  prefix_valid={scored.prefix_valid}
  overspeak_words={scored.overspeak_words}
  underspeak_words={scored.underspeak_words}

Re-run:
  python3 scripts/assistant-self-asr-harness.py --mode dry-run --out-dir {out_dir}

TTS↔TTS dual-role path:
  python3 scripts/assistant-self-asr-harness.py --mode tts-tts

See docs/assistant-self-asr-eval.md
"""
    _write_text(out_dir / "README-run.txt", run_readme)

    _append_event(
        events_path,
        {
            "event": "eval_run_completed",
            "primary_cut_source": scored.primary_cut_source,
            "prefix_valid": scored.prefix_valid,
            "overspeak_words": scored.overspeak_words,
            "underspeak_words": scored.underspeak_words,
            "out_dir": str(out_dir),
        },
    )

    print("assistant-self-asr harness (eval_only / dry-run)")
    print(f"  out_dir:              {out_dir}")
    print(f"  intended words:       {total_words}")
    print(f"  pause_ms:             {pause_ms}")
    print(f"  user_stop_ms:         {stop_ms}")
    print(f"  emitted@pause:        {emitted_at_pause}")
    print(f"  drained_pos:          {drained_pos} complete={drain_complete}")
    print(f"  playback_pos@pause:   {playback_at_pause}")
    print(f"  primary_cut_source:   {scored.primary_cut_source}")
    print(f"  pad_words:            {args.pad_words} (fallback only)")
    print(f"  production_cut_text:  {scored.production_cut!r}")
    print(f"  intended_at_playback: {scored.intended_at_playback!r}")
    print(f"  asr_recovered@stop:   {scored.asr_recovered_at_stop!r}")
    print(
        f"  prefix_valid={scored.prefix_valid}  "
        f"overspeak_words={scored.overspeak_words}  "
        f"underspeak_words={scored.underspeak_words}"
    )
    print(f"  metrics:              {out_dir / 'metrics.json'}")
    return 0


def run_tts_tts(args: argparse.Namespace) -> int:
    intended = _resolve_intended(args)
    if not intended:
        print("error: intended text is empty", file=sys.stderr)
        return 2
    if args.pad_words < 0:
        print("error: --pad-words must be >= 0", file=sys.stderr)
        return 2

    if args.user_speech_ms is not None:
        user_speech_ms = float(args.user_speech_ms)
    else:
        # Prefer pause/stop pair when user_stop > pause; else default 1000 ms.
        if args.user_stop_at_ms >= args.pause_at_ms:
            user_speech_ms = float(args.user_stop_at_ms) - float(args.pause_at_ms)
        else:
            user_speech_ms = 1000.0

    cfg = DualClockConfig(
        intended_text=intended,
        user_barge_text=args.user_barge_text,
        pad_words=args.pad_words,
        assistant_words_per_second=args.words_per_second,
        playback_lag_words=args.playback_lag_words,
        barge_in_delay_ms=float(args.pause_at_ms),
        user_speech_ms=user_speech_ms,
        drain_words_per_second=args.drain_words_per_second,
        drain_start_lag_ms=args.drain_start_lag_ms,
        force_fallback=bool(args.force_fallback),
        utterance_id=args.utterance_id,
    )
    try:
        result = run_dual_clock_tts_eval(cfg)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    out_dir: Path = args.out_dir or Path(
        f"/tmp/assistant-self-asr-tts-tts-{time.strftime('%Y%m%d-%H%M%S')}"
    )
    result.write_artifacts(out_dir)

    print("assistant-self-asr harness (eval_only / tts-tts)")
    print(f"  out_dir:              {out_dir}")
    print(f"  roles:                assistant TTS + user-role TTS barge-in")
    print(f"  pause_ms:             {result.pause_ms}")
    print(f"  user_stop_ms:         {result.user_stop_ms}")
    print(f"  emitted@pause:        {result.assistant_emitted_at_pause}")
    print(
        f"  drained_pos:          {result.drained_pos_words} "
        f"complete={result.drain_complete}"
    )
    print(f"  primary_cut_source:   {result.primary_cut_source}")
    print(f"  pad_words:            {result.pad_words} (fallback only)")
    print(f"  production_cut_text:  {result.production_cut_text!r}")
    print(f"  intended_at_playback: {result.intended_at_playback!r}")
    print(f"  asr_recovered@stop:   {result.asr_recovered_at_stop!r}")
    print(
        f"  prefix_valid={result.prefix_valid}  "
        f"overspeak_words={result.overspeak_words}  "
        f"underspeak_words={result.underspeak_words}"
    )
    print(f"  biases:               tts_tts_not_acoustic_echo_truth, no_mic_dirt")
    print(f"  metrics:              {out_dir / 'metrics.json'}")
    return 0


def run_tui(args: argparse.Namespace) -> int:
    intended = _resolve_intended(args)
    if not intended:
        print("error: intended text is empty", file=sys.stderr)
        return 2
    if args.pad_words < 0:
        print("error: --pad-words must be >= 0", file=sys.stderr)
        return 2

    if args.user_speech_ms is not None:
        user_speech_ms = float(args.user_speech_ms)
    elif args.user_stop_at_ms >= args.pause_at_ms:
        user_speech_ms = float(args.user_stop_at_ms) - float(args.pause_at_ms)
    else:
        user_speech_ms = 1500.0

    out_dir: Path = args.out_dir or Path(
        f"/tmp/assistant-self-asr-tui-{time.strftime('%Y%m%d-%H%M%S')}"
    )
    # When pause-at-ms was left at default and auto-barge not set, prefer keypress.
    # If --pause-at-ms explicitly used as auto schedule, map it when --auto-barge-ms unset
    # only if user also passed a positive value via env-less default: keep keypress-first.
    auto_barge = args.auto_barge_ms

    cfg = TuiConfig(
        intended_text=intended,
        user_barge_text=args.user_barge_text,
        pad_words=args.pad_words,
        assistant_words_per_second=args.words_per_second,
        drain_words_per_second=args.drain_words_per_second,
        playback_lag_words=args.playback_lag_words,
        auto_barge_ms=auto_barge,
        user_speech_ms=user_speech_ms,
        play=bool(args.play),
        play_command=args.play_command,
        force_fallback=bool(args.force_fallback),
        utterance_id=args.utterance_id,
        out_dir=out_dir,
    )
    print(
        "assistant-self-asr interactive TUI (eval_only)\n"
        "  barge-in: keypress b/space mimics Nemotron first-alnum token path\n"
        "  (not VAD/echo; acoustic echo is a separate suite)\n"
        f"  out_dir: {out_dir}\n"
        f"  play: {cfg.play}\n",
        flush=True,
    )
    result = run_interactive_tui(cfg)
    if result is None:
        return 1
    print(f"  primary_cut_source:   {result.primary_cut_source}")
    print(f"  production_cut_text:  {result.production_cut_text!r}")
    print(f"  metrics:              {out_dir / 'metrics.json'}")
    return 0


def run_live_stub(args: argparse.Namespace) -> int:
    if not args.allow_live_stub:
        print(
            "error: --mode live is a documented stub. "
            "Pass --allow-live-stub to emit the wiring checklist, "
            "or use --mode dry-run / --mode tts-tts.",
            file=sys.stderr,
        )
        return 2

    out_dir: Path = args.out_dir or Path(
        f"/tmp/assistant-self-asr-eval-live-stub-{time.strftime('%Y%m%d-%H%M%S')}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    checklist = {
        "label": "eval_only",
        "mode": "live_stub",
        "status": "not_wired",
        "founder_cut_rule": {
            "pause_on": "first_user_alphanumeric_token",
            "finalize_on": "user_stop_transcript_committed",
            "primary": "drain_during_user_speech_force_aligned_to_intended",
            "fallback": "last_pos_plus_pad_words",
            "pad_words_default": DEFAULT_PAD_WORDS,
            "pad_is_fallback_only": True,
            "dual_nemotron": True,
        },
        "todo": [
            {
                "id": "play_instrumentation",
                "desc": (
                    "Instrumented play path: log samples/chunks actually started "
                    "(eval_playback) without changing shared protocol vocabulary."
                ),
            },
            {
                "id": "assistant_nemotron_b",
                "desc": (
                    "Assistant-tracking Nemotron-B drain on already-emitted audio "
                    "during user speech. Dual instance — not shared with user A."
                ),
            },
            {
                "id": "record_only_client",
                "desc": (
                    "Optional second websocket client that only records binary "
                    "WAV chunks from speech-out for eval_synthesis second ASR."
                ),
            },
            {
                "id": "existing_pause",
                "desc": (
                    "Reuse scripts/speech-out-live-session.sh pause on first "
                    "alphanumeric transcript_token_committed."
                ),
            },
        ],
        "forbidden": [
            "speech-core-protocol shared event vocabulary changes",
            "speech-core-daemon shared event vocabulary changes",
            "treating dry-run / tts-tts metrics as production evidence",
        ],
    }
    (out_dir / "live_wiring_gap.json").write_text(
        json.dumps(checklist, indent=2) + "\n", encoding="utf-8"
    )
    print("assistant-self-asr live stub (eval_only)")
    print(f"  wrote wiring gap checklist: {out_dir / 'live_wiring_gap.json'}")
    print("  no live daemons contacted; use --mode dry-run or --mode tts-tts.")
    return 0


def run_live_synth_stub(args: argparse.Namespace) -> int:
    if not args.allow_live_stub:
        print(
            "error: --mode live-synth is a documented stub. "
            "Pass --allow-live-stub to emit the synth wiring checklist, "
            "or use --mode tts-tts for offline dual-role metrics.",
            file=sys.stderr,
        )
        return 2

    out_dir: Path = args.out_dir or Path(
        f"/tmp/assistant-self-asr-live-synth-stub-{time.strftime('%Y%m%d-%H%M%S')}"
    )
    checklist = emit_live_synth_checklist(out_dir)
    print("assistant-self-asr live-synth stub (eval_only)")
    print(f"  wrote: {out_dir / 'live_synth_wiring_gap.json'}")
    print(f"  roles: {checklist['roles']}")
    print("  offline metrics path: --mode tts-tts (no daemons)")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.self_check:
        return run_self_check()
    if args.mode == "live":
        return run_live_stub(args)
    if args.mode == "live-synth":
        return run_live_synth_stub(args)
    if args.mode == "tts-tts":
        return run_tts_tts(args)
    if args.mode == "tui":
        return run_tui(args)
    return run_dry_run(args)


if __name__ == "__main__":
    raise SystemExit(main())
