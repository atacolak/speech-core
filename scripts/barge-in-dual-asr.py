#!/usr/bin/env python3
"""Dual-Nemotron barge-in entrypoint (harness-only).

Topology (speech-to-speech-contract.md rev 4):
  Nemotron A = user mic path (existing speech-in; do not break)
  Nemotron B = assistant self-ASR on speech-out synthesized PCM (separate)

Cut rule (rev 3 happy path; pad is FALLBACK):
  1. Pause playback on first user alphanumeric token
  2. Drain B on already-emitted audio during user speech
  3. At user transcript_committed: drained + force-aligned intended prefix
  4. Fallback if drain incomplete: last pos + pad_words(~2) from intended
  5. Commit truncated assistant message once; never revise

Modes:
  dry-run  — offline dual-stream simulator (default; no daemons)
  live     — probe wiring; fail closed with steps unless ready / --allow-live-stub
  self-check — unit assertions on cut + sim paths

Does NOT edit speech-core-protocol, daemon event vocabulary, or eval harness files.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

# Allow `python3 scripts/barge-in-dual-asr.py` without installing a package.
# The on-disk dir is hyphenated (scripts/barge-in-dual-asr/); we expose it as
# the importable package name barge_in_dual_asr so relative imports work.
_SCRIPTS_DIR = Path(__file__).resolve().parent
_PKG_DIR = _SCRIPTS_DIR / "barge-in-dual-asr"

import importlib.util
import types


def _ensure_package() -> None:
    pkg_name = "barge_in_dual_asr"
    if pkg_name in sys.modules:
        return
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [str(_PKG_DIR)]  # type: ignore[attr-defined]
    pkg.__file__ = str(_PKG_DIR / "__init__.py")
    sys.modules[pkg_name] = pkg


def _load_pkg_module(name: str):
    """Load scripts/barge-in-dual-asr/<name>.py as barge_in_dual_asr.<name>."""
    _ensure_package()
    full_name = f"barge_in_dual_asr.{name}"
    if full_name in sys.modules:
        return sys.modules[full_name]
    path = _PKG_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(
        full_name,
        path,
        submodule_search_locations=[str(_PKG_DIR)],
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    # Required for relative imports inside the submodule.
    mod.__package__ = "barge_in_dual_asr"
    sys.modules[full_name] = mod
    spec.loader.exec_module(mod)
    return mod


# Load cut first so simulator's `from .cut import ...` resolves.
_cut = _load_pkg_module("cut")
_sim = _load_pkg_module("simulator")
_live = _load_pkg_module("live_wiring")

DEFAULT_PAD_WORDS = _cut.DEFAULT_PAD_WORDS
DEFAULT_INTENDED = (
    "The quick brown fox jumps over the lazy dog while the assistant "
    "continues speaking about barge in calibration and dual nemotron drain."
)


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Dual-Nemotron barge-in coordinator (harness-only). "
            "Default mode is offline dry-run dual-stream simulator."
        )
    )
    p.add_argument(
        "--mode",
        choices=("dry-run", "live"),
        default="dry-run",
        help="dry-run: dual-stream simulator. live: probe + wiring (fail closed).",
    )
    p.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Artifact directory (default under /tmp).",
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
        help=f"Fallback pad from intended text (default {DEFAULT_PAD_WORDS}).",
    )
    p.add_argument(
        "--emitted-words-at-pause",
        type=int,
        default=6,
        help="Dry-run: intended words already in emit/play path at pause.",
    )
    p.add_argument(
        "--b-pos-at-pause",
        type=int,
        default=4,
        help="Dry-run: Nemotron B word position at pause (may lag emitted).",
    )
    p.add_argument(
        "--drain-complete",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Dry-run: whether B fully drains emitted audio by user commit.",
    )
    p.add_argument(
        "--b-pos-at-user-commit",
        type=int,
        default=None,
        help="Dry-run: force B position at user commit (incomplete drain).",
    )
    p.add_argument(
        "--drained-asr-text",
        default=None,
        help="Dry-run: override drained ASR text (else intended prefix at B pos).",
    )
    p.add_argument(
        "--pause-at-ms",
        type=float,
        default=800.0,
        help="Dry-run: wall offset of first alnum user token / pause.",
    )
    p.add_argument(
        "--user-stop-at-ms",
        type=float,
        default=2200.0,
        help="Dry-run: wall offset of user transcript_committed (T0).",
    )
    p.add_argument(
        "--user-transcript",
        default="wait stop talking please",
        help="Dry-run: simulated user turn text.",
    )
    p.add_argument(
        "--utterance-id",
        default="dual-assistant-1",
        help="Utterance id for log-only commit artifact.",
    )
    p.add_argument(
        "--scenario",
        choices=("primary-drain", "fallback-incomplete", "custom"),
        default="custom",
        help=(
            "Dry-run preset. primary-drain: complete drain → cut_source=drain. "
            "fallback-incomplete: stuck B → cut_source=fallback. "
            "custom: use explicit flags."
        ),
    )
    p.add_argument(
        "--core-url",
        default=_live.DEFAULT_CORE_WS,
        help="Live: speech-core audio-ingress websocket URL.",
    )
    p.add_argument(
        "--out-url",
        default=_live.DEFAULT_OUT_WS,
        help="Live: speech-out websocket URL.",
    )
    p.add_argument(
        "--allow-live-stub",
        action="store_true",
        help="Permit --mode live when daemons are unreachable (writes checklist).",
    )
    p.add_argument(
        "--self-check",
        action="store_true",
        help="Run built-in unit assertions, then exit.",
    )
    p.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="Repository root for binary discovery (live mode).",
    )
    return p


def _resolve_intended(args: argparse.Namespace) -> str:
    if args.intended_file is not None:
        return args.intended_file.read_text(encoding="utf-8")
    return args.intended_text


def run_self_check() -> int:
    intended = "one two three four five six seven eight nine ten"

    # Fallback cut: pos 3 + pad 2 → five words.
    fb = _cut.apply_fallback_cut(intended, last_pos_words=3, pad_words=2)
    assert fb == "one two three four five", fb

    # Force-align exact drain of first 6 words.
    aligned, n, conf = _cut.force_align_to_intended(
        "one two three four five six", intended
    )
    assert aligned == "one two three four five six", aligned
    assert n == 6, n
    assert conf > 0.5, conf

    # Primary path: drain complete + aligned → source drain.
    d = _cut.production_cut(
        intended,
        drained_asr_text="one two three four five six",
        last_pos_words=6,
        pad_words=2,
        drain_complete=True,
    )
    assert d.source == "drain", d
    assert d.production_cut_text == "one two three four five six", d.production_cut_text
    assert d.prefix_valid is True

    # Fallback path: drain incomplete.
    d2 = _cut.production_cut(
        intended,
        drained_asr_text="one two",
        last_pos_words=2,
        pad_words=2,
        drain_complete=False,
    )
    assert d2.source == "fallback", d2
    assert d2.production_cut_text == "one two three four", d2.production_cut_text
    assert d2.prefix_valid is True

    # Alignment with light ASR noise still maps to intended prefix.
    aligned2, n2, _ = _cut.force_align_to_intended(
        "one two three, four five", intended
    )
    assert n2 >= 4, (aligned2, n2)

    # Alnum pause mirror.
    assert _cut.is_alphanumeric_token("wait")
    assert not _cut.is_alphanumeric_token("...")

    # Simulator primary-drain scenario (tmp dir).
    import tempfile

    with tempfile.TemporaryDirectory(prefix="barge-in-self-check-") as td:
        out = Path(td) / "primary"
        cfg = _sim.DualStreamSimConfig(
            intended_text=intended,
            emitted_words_at_pause=6,
            b_pos_at_pause=4,
            drain_complete=True,
            pad_words=2,
        )
        summary = _sim.run_dual_stream_simulation(cfg, out)
        assert summary["cut_source"] == "drain", summary
        assert summary["production_cut_text"] == "one two three four five six"
        assert (out / "commit.json").is_file()
        assert (out / "metrics.json").is_file()
        commit = json.loads((out / "commit.json").read_text(encoding="utf-8"))
        assert commit["immutable"] is True
        assert commit["late_self_asr_revises"] is False
        assert commit["cut_source"] == "drain"

        out2 = Path(td) / "fallback"
        cfg2 = _sim.DualStreamSimConfig(
            intended_text=intended,
            emitted_words_at_pause=6,
            b_pos_at_pause=2,
            drain_complete=False,
            b_pos_at_user_commit=2,
            pad_words=2,
        )
        summary2 = _sim.run_dual_stream_simulation(cfg2, out2)
        assert summary2["cut_source"] == "fallback", summary2
        assert summary2["production_cut_text"] == "one two three four"

    print("self-check: PASS")
    print(f"  primary cut:  {summary['production_cut_text']!r} (source=drain)")
    print(f"  fallback cut: {summary2['production_cut_text']!r} (source=fallback)")
    return 0


def _apply_scenario(args: argparse.Namespace) -> None:
    if args.scenario == "primary-drain":
        args.emitted_words_at_pause = 6
        args.b_pos_at_pause = 4
        args.drain_complete = True
        args.b_pos_at_user_commit = None
        args.drained_asr_text = None
    elif args.scenario == "fallback-incomplete":
        args.emitted_words_at_pause = 6
        args.b_pos_at_pause = 2
        args.drain_complete = False
        args.b_pos_at_user_commit = 2
        args.drained_asr_text = None


def run_dry_run(args: argparse.Namespace) -> int:
    _apply_scenario(args)
    intended = _cut.normalize_whitespace(_resolve_intended(args))
    if not intended:
        print("error: intended text is empty", file=sys.stderr)
        return 2
    if args.pad_words < 0:
        print("error: --pad-words must be >= 0", file=sys.stderr)
        return 2
    if args.user_stop_at_ms < args.pause_at_ms:
        print(
            "error: --user-stop-at-ms must be >= --pause-at-ms",
            file=sys.stderr,
        )
        return 2

    out_dir: Path = args.out_dir or Path(
        f"/tmp/barge-in-dual-asr-{time.strftime('%Y%m%d-%H%M%S')}"
    )
    cfg = _sim.DualStreamSimConfig(
        intended_text=intended,
        emitted_words_at_pause=args.emitted_words_at_pause,
        b_pos_at_pause=args.b_pos_at_pause,
        drain_complete=bool(args.drain_complete),
        b_pos_at_user_commit=args.b_pos_at_user_commit,
        drained_asr_override=args.drained_asr_text,
        pad_words=args.pad_words,
        pause_at_ms=args.pause_at_ms,
        user_stop_at_ms=args.user_stop_at_ms,
        user_transcript=args.user_transcript,
        utterance_id=args.utterance_id,
    )
    summary = _sim.run_dual_stream_simulation(cfg, out_dir)

    print("barge-in dual-Nemotron (eval_only / dry-run)")
    print(f"  out_dir:              {out_dir}")
    print(f"  scenario:             {args.scenario}")
    print(f"  topology:             Nemotron A=user | Nemotron B=assistant.self_asr")
    print(f"  shared_worker:        False")
    print(f"  pause_ms:             {cfg.pause_at_ms}")
    print(f"  user_stop_ms:         {cfg.user_stop_at_ms}")
    print(f"  drain_window_ms:      {cfg.user_stop_at_ms - cfg.pause_at_ms}")
    print(f"  emitted@pause:        {cfg.emitted_words_at_pause}")
    print(f"  drain_complete:       {cfg.drain_complete}")
    print(f"  cut_source:           {summary['cut_source']}")
    print(f"  production_cut_text:  {summary['production_cut_text']!r}")
    print(f"  prefix_valid:         {summary['prefix_valid']}")
    print(f"  commit:               {out_dir / 'commit.json'}")
    print(f"  metrics:              {out_dir / 'metrics.json'}")
    return 0


def run_live(args: argparse.Namespace) -> int:
    out_dir: Path = args.out_dir or Path(
        f"/tmp/barge-in-dual-asr-live-{time.strftime('%Y%m%d-%H%M%S')}"
    )
    intended = _cut.normalize_whitespace(_resolve_intended(args))
    code, checklist = _live.run_live_coordinator_stub(
        args.repo_root.resolve(),
        out_dir,
        core_ws=args.core_url,
        out_ws=args.out_url,
        allow_stub=args.allow_live_stub,
        intended_text=intended,
    )
    status = checklist.get("status")
    ready = checklist.get("probes", {}).get("ready_for_live")
    print("barge-in dual-Nemotron LIVE")
    print(f"  out_dir:         {out_dir}")
    print(f"  status:          {status}")
    print(f"  ready_for_live:  {ready}")
    print(f"  wiring:          {out_dir / 'live_wiring.json'}")
    if code != 0:
        print(
            "error: live environment not ready. "
            "Start speech-core + speech-out, build adapters, re-run; "
            "or pass --allow-live-stub for checklist-only; "
            "or use --mode dry-run.",
            file=sys.stderr,
        )
        print(
            "  see docs/barge-in-dual-asr.md and live_wiring.json wiring_steps",
            file=sys.stderr,
        )
    elif ready:
        print("  daemons reachable — see live_runbook.json for operator steps")
        print(
            "  Nemotron B feed: python3 -m not used; "
            "scripts/barge-in-dual-asr/feed_assistant_asr.py <wav>"
        )
    else:
        print("  live stub checklist written (daemons not reachable)")
    return code


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.self_check:
        return run_self_check()
    if args.mode == "live":
        return run_live(args)
    return run_dry_run(args)


if __name__ == "__main__":
    raise SystemExit(main())
