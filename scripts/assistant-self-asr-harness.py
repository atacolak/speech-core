#!/usr/bin/env python3
"""Eval-only assistant self-ASR / founder-cut harness.

Measures the founder cut rule from speech-to-speech-contract.md rev 2:

  pause on first user alphanumeric token
  finalize at user stop / transcript_committed
  base = Nemotron alignment position at that moment
  pad = +pad_words (default 2) from intended LLM text
  commit once; late self-ASR is diagnostic only

Modes
-----
  dry-run (default): offline synthetic timeline, no daemons required.
  live (stub): documents wiring gaps; refuses unless --allow-live-stub.

Artifacts (under --out-dir, all labeled eval_only)
-------------------------------------------------
  assistant_intended.txt
  production_cut_text
  metrics.json
  events.jsonl
  commit.json                 # truncated assistant message concept (log only)
  asr_recovered_at_stop.txt   # diagnostic
  intended_at_playback.txt
  README-run.txt

Capture locus labels follow the contract:
  eval_playback  — mock or instrumented samples/chunks played
  eval_synthesis — optional server PCM / second ASR (stubbed in dry-run)

Do NOT treat green metrics as production evidence (see inquisitor R1–R7).
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
    commit_truncated_assistant_message,
    is_alphanumeric_token,
    mock_alignment_clock,
    normalize_whitespace,
    score_three_way,
    tokenize_words,
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
            "Default mode is offline dry-run."
        )
    )
    p.add_argument(
        "--mode",
        choices=("dry-run", "live"),
        default="dry-run",
        help="dry-run: synthetic offline. live: stub only unless wired.",
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
        "--pad-words",
        type=int,
        default=DEFAULT_PAD_WORDS,
        help=f"Safety pad from intended text (default {DEFAULT_PAD_WORDS}).",
    )
    p.add_argument(
        "--pause-at-ms",
        type=float,
        default=1200.0,
        help="Dry-run: wall offset when first alphanumeric user token arrives.",
    )
    p.add_argument(
        "--user-stop-at-ms",
        type=float,
        default=2200.0,
        help="Dry-run: wall offset of user stop / transcript_committed (T0).",
    )
    p.add_argument(
        "--words-per-second",
        type=float,
        default=3.0,
        help="Dry-run mock alignment clock rate for intended words.",
    )
    p.add_argument(
        "--playback-lag-words",
        type=int,
        default=1,
        help=(
            "Dry-run: how many words playback trails the mock Nemotron clock "
            "at T0 (models synthesis-ahead-of-playback bias, inquisitor R1)."
        ),
    )
    p.add_argument(
        "--asr-lag-words",
        type=int,
        default=2,
        help=(
            "Dry-run: how many words mock second-ASR recovery trails Nemotron "
            "pos at stop (diagnostic asr_recovered_at_stop)."
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
        help="Permit --mode live which currently only documents the wiring gap.",
    )
    p.add_argument(
        "--self-check",
        action="store_true",
        help="Run built-in unit assertions on the cut rule, then exit.",
    )
    return p


def run_self_check() -> int:
    """Deterministic unit-testable path without writing artifacts."""
    intended = "one two three four five six seven eight"
    cut = apply_founder_cut(intended, nemotron_pos_words=3, pad_words=2)
    assert cut == "one two three four five", cut
    cut_end = apply_founder_cut(intended, nemotron_pos_words=7, pad_words=2)
    assert cut_end == intended
    cut_zero = apply_founder_cut(intended, nemotron_pos_words=0, pad_words=2)
    assert cut_zero == "one two", cut_zero
    assert is_alphanumeric_token("hello")
    assert is_alphanumeric_token("A")
    assert not is_alphanumeric_token("...")
    assert not is_alphanumeric_token(",")

    scored = score_three_way(
        intended,
        playback_pos_words=3,
        nemotron_pos_words=3,
        asr_recovered_at_stop="one two",
        pad_words=2,
    )
    assert scored.production_cut == "one two three four five"
    assert scored.intended_at_playback == "one two three"
    assert scored.prefix_valid is True
    assert scored.overspeak_words == 2  # pad beyond playback
    assert scored.underspeak_words == 0
    assert scored.label == "eval_only"

    # Non-prefix free-form ASR must not be used as production cut.
    free = "totally invented words"
    from assistant_self_asr_cut import is_prefix_of_intended

    assert not is_prefix_of_intended(free, intended)

    # Mock clock clamps.
    pos = mock_alignment_clock(intended, words_per_second=2.0, elapsed_ms=1500)
    assert pos == 3, pos

    print("self-check: PASS")
    print(f"  sample production_cut: {scored.production_cut!r}")
    print(f"  sample metrics: {json.dumps(scored.to_metrics_dict(), sort_keys=True)}")
    return 0


def run_dry_run(args: argparse.Namespace) -> int:
    if args.intended_file is not None:
        intended = args.intended_file.read_text(encoding="utf-8")
    else:
        intended = args.intended_text
    intended = normalize_whitespace(intended)
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
    # T0 / user_stop: transcript_committed -> finalize cut.
    pause_ms = float(args.pause_at_ms)
    stop_ms = float(args.user_stop_at_ms)

    _append_event(
        events_path,
        {
            "event": "eval_run_started",
            "mode": "dry-run",
            "capture_locus": ["eval_playback", "eval_synthesis_stub"],
            "intended_word_count": total_words,
            "pad_words": args.pad_words,
            "words_per_second": args.words_per_second,
        },
    )

    # Mock assistant stream progress uses the linear alignment clock.
    # TODO(live): instrument speech-out play path for samples/chunks played;
    # TODO(live): optional second record-only client for server PCM → second ASR.
    nemotron_at_pause = mock_alignment_clock(
        intended, words_per_second=args.words_per_second, elapsed_ms=pause_ms
    )
    nemotron_at_stop = mock_alignment_clock(
        intended, words_per_second=args.words_per_second, elapsed_ms=stop_ms
    )

    # Playback may trail synthesis/Nemotron (R1). Clamp >= 0.
    playback_at_stop = max(0, nemotron_at_stop - max(0, int(args.playback_lag_words)))
    asr_pos = max(0, nemotron_at_stop - max(0, int(args.asr_lag_words)))
    asr_recovered = " ".join(words[:asr_pos]) if asr_pos else ""

    _append_event(
        events_path,
        {
            "event": "user_first_alphanumeric_token",
            "trigger": "transcript_token_committed",
            "action": "pause_playback",
            "pause_ms": pause_ms,
            "mock_nemotron_pos_words": nemotron_at_pause,
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
        },
    )
    _append_event(
        events_path,
        {
            "event": "user_transcript_committed",
            "t0": "user_stop",
            "user_stop_ms": stop_ms,
            "action": "finalize_truncated_assistant",
            "mock_nemotron_pos_words": nemotron_at_stop,
        },
    )

    scored = score_three_way(
        intended,
        playback_pos_words=playback_at_stop,
        nemotron_pos_words=nemotron_at_stop,
        asr_recovered_at_stop=asr_recovered,
        pad_words=args.pad_words,
    )
    commit = commit_truncated_assistant_message(
        scored.production_cut, utterance_id=args.utterance_id
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
        },
    )

    metrics: dict[str, Any] = {
        "label": "eval_only",
        "mode": "dry-run",
        "founder_cut_rule": {
            "pause_on": "first_user_alphanumeric_token",
            "finalize_on": "user_stop_transcript_committed",
            "base": "nemotron_alignment_pos_words_at_stop",
            "pad_words": args.pad_words,
            "pad_source": "intended_llm_text",
            "commit_once": True,
            "late_self_asr": "diagnostic_only",
        },
        "timing_ms": {
            "pause_ms": pause_ms,
            "user_stop_ms": stop_ms,
            "note": (
                "Dry-run offsets from synthetic playback start. "
                "Not a shared multi-clock reconstruction (inquisitor R5 open)."
            ),
        },
        "alignment": {
            "capture_locus_playback": "eval_playback",
            "capture_locus_synthesis": "eval_synthesis_stub",
            "nemotron_pos_at_stop": nemotron_at_stop,
            "playback_pos_words_at_stop": playback_at_stop,
            "words_per_second_mock": args.words_per_second,
            "playback_lag_words_config": args.playback_lag_words,
            "asr_lag_words_config": args.asr_lag_words,
            "total_intended_words": total_words,
            "alignment_clock": "mock_linear_words_per_second",
            "todo_live": [
                "Wire instrumented speech-out play samples/chunks for playback clock",
                "Wire assistant-tracking Nemotron stream position at transcript_committed",
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
            "no_dual_stream_interference",
            "asr_recovered_is_stubbed_from_intended_with_lag",
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

Founder cut rule applied:
  1. pause_playback @ first alphanumeric user token ({pause_ms} ms synthetic)
  2. finalize @ user stop / transcript_committed ({stop_ms} ms synthetic)
  3. base nemotron_pos_words @ stop = {nemotron_at_stop}
  4. pad_words = {args.pad_words} from intended text
  5. commit once (see commit.json); late self-ASR diagnostic only

Three-way sample:
  intended_at_playback: {scored.intended_at_playback!r}
  asr_recovered_at_stop: {scored.asr_recovered_at_stop!r}
  production_cut_text:   {scored.production_cut!r}

Gates:
  prefix_valid={scored.prefix_valid}
  overspeak_words={scored.overspeak_words}
  underspeak_words={scored.underspeak_words}

Re-run:
  python3 scripts/assistant-self-asr-harness.py --mode dry-run --out-dir {out_dir}

Live wiring gap (not implemented in this milestone):
  - instrumented speech-out play samples/chunks
  - live Nemotron assistant stream position
  - optional second record-only client for server PCM
  - no protocol schema edits

See docs/assistant-self-asr-eval.md
"""
    _write_text(out_dir / "README-run.txt", run_readme)

    _append_event(
        events_path,
        {
            "event": "eval_run_completed",
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
    print(f"  nemotron_pos@stop:    {nemotron_at_stop}")
    print(f"  playback_pos@stop:    {playback_at_stop}")
    print(f"  pad_words:            {args.pad_words}")
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


def run_live_stub(args: argparse.Namespace) -> int:
    if not args.allow_live_stub:
        print(
            "error: --mode live is a documented stub. "
            "Pass --allow-live-stub to emit the wiring checklist, "
            "or use --mode dry-run.",
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
            "base": "nemotron_alignment_pos_at_stop",
            "pad_words_default": DEFAULT_PAD_WORDS,
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
                "id": "assistant_nemotron",
                "desc": (
                    "Second / assistant-tracking Nemotron alignment position at "
                    "transcript_committed. Production cut base. Dual-stream "
                    "interference (inquisitor R7) must be measured."
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
            "treating dry-run metrics as production evidence",
        ],
    }
    (out_dir / "live_wiring_gap.json").write_text(
        json.dumps(checklist, indent=2) + "\n", encoding="utf-8"
    )
    print("assistant-self-asr live stub (eval_only)")
    print(f"  wrote wiring gap checklist: {out_dir / 'live_wiring_gap.json'}")
    print("  no live daemons contacted; use --mode dry-run for metrics artifacts.")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.self_check:
        return run_self_check()
    if args.mode == "live":
        return run_live_stub(args)
    return run_dry_run(args)


if __name__ == "__main__":
    raise SystemExit(main())
