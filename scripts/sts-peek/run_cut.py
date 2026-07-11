#!/usr/bin/env python3
"""CLI for sts-peek cut coordinator (Track C).

Usage:
  python3 scripts/sts-peek/run_cut.py --run-dir DIR --intended-text "..." \\
      [--pad-words 2] [--mode dry-run|follow]

Modes:
  dry-run  — synthetic barge→drain→user_commit timeline (no daemons).
             Default scenario proves primary drain; use --scenario fallback
             for pad fallback. Writes production_cut_text / metrics / commit.
  follow   — poll run_dir events/control files written by audio/UI tracks;
             finalize cut on user transcript_committed. Tolerates missing
             files while waiting; times out with a clear error.

Self-check:
  python3 scripts/sts-peek/run_cut.py --self-check
"""
from __future__ import annotations

import argparse
import json
import sys
import types
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parent


def _ensure_imports() -> None:
    """Make scripts/sts-peek/ importable as sibling modules (cut, cut_coord)."""
    pkg_name = "sts_peek"
    if pkg_name not in sys.modules:
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [str(_PKG_DIR)]  # type: ignore[attr-defined]
        pkg.__file__ = str(_PKG_DIR / "__init__.py")
        sys.modules[pkg_name] = pkg
    # Ensure package dir is on sys.path so `from cut import ...` works when
    # cut_coord is loaded as a top-level module via runpy / importlib.
    path_str = str(_PKG_DIR)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)


def _load_coord():
    _ensure_imports()
    import importlib.util

    # Load cut first so cut_coord's `from cut import ...` resolves.
    cut_path = _PKG_DIR / "cut.py"
    if "cut" not in sys.modules:
        spec = importlib.util.spec_from_file_location("cut", cut_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load {cut_path}")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["cut"] = mod
        spec.loader.exec_module(mod)

    coord_path = _PKG_DIR / "cut_coord.py"
    if "cut_coord" not in sys.modules:
        spec = importlib.util.spec_from_file_location("cut_coord", coord_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load {coord_path}")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["cut_coord"] = mod
        spec.loader.exec_module(mod)
    return sys.modules["cut_coord"]


DEFAULT_INTENDED = "one two three four five six seven eight nine ten"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run_cut.py",
        description="sts-peek cut coordinator (drain-primary / pad-fallback)",
    )
    p.add_argument(
        "--run-dir",
        type=Path,
        default=None,
        help="Session / run directory for events and cut artifacts",
    )
    p.add_argument(
        "--intended-text",
        type=str,
        default=None,
        help="Intended LLM text (else read run_dir/assistant_intended.txt)",
    )
    p.add_argument(
        "--pad-words",
        type=int,
        default=None,
        help="Fallback pad words (default 2)",
    )
    p.add_argument(
        "--mode",
        choices=("dry-run", "follow"),
        default="dry-run",
        help="dry-run synthetic timeline, or follow run_dir events (default dry-run)",
    )
    p.add_argument(
        "--scenario",
        choices=("primary-drain", "fallback-incomplete", "fallback-b-missing"),
        default="primary-drain",
        help="dry-run scenario (default primary-drain)",
    )
    p.add_argument(
        "--label",
        type=str,
        default="live_peek",
        help="metrics/commit label (default live_peek)",
    )
    p.add_argument(
        "--wait-timeout-s",
        type=float,
        default=30.0,
        help="follow mode: max seconds to wait for user commit (default 30)",
    )
    p.add_argument(
        "--poll-interval-s",
        type=float,
        default=0.05,
        help="follow mode: poll interval seconds (default 0.05)",
    )
    p.add_argument(
        "--self-check",
        action="store_true",
        help="Run in-process unit assertions and exit",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Print result summary as JSON to stdout",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    coord = _load_coord()
    cut = sys.modules["cut"]

    if args.self_check:
        coord.self_check()
        return 0

    if args.run_dir is None:
        print("error: --run-dir is required (unless --self-check)", file=sys.stderr)
        return 2

    run_dir = args.run_dir.expanduser().resolve()
    pad = (
        int(args.pad_words)
        if args.pad_words is not None
        else cut.DEFAULT_PAD_WORDS
    )
    if pad < 0:
        print("error: --pad-words must be >= 0", file=sys.stderr)
        return 2

    if args.mode == "dry-run":
        intended = (
            cut.normalize_whitespace(args.intended_text)
            if args.intended_text
            else DEFAULT_INTENDED
        )
        if args.scenario == "primary-drain":
            scenario = coord.scenario_primary_drain(intended, pad_words=pad)
        elif args.scenario == "fallback-incomplete":
            scenario = coord.scenario_fallback_incomplete(intended, pad_words=pad)
        else:
            scenario = coord.scenario_fallback_b_missing(intended, pad_words=pad)

        # Allow CLI intended override to reshape scenario text.
        scenario.intended_text = intended
        scenario.pad_words = pad

        result = coord.run_synthetic_timeline(
            scenario, run_dir, label=args.label
        )
        _print_result(result, as_json=args.json)
        return 0

    # follow mode
    try:
        intended = coord.load_intended_text(run_dir, args.intended_text)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    cfg = coord.CutCoordConfig(
        run_dir=run_dir,
        intended_text=intended,
        pad_words=pad,
        label=args.label,
        wait_timeout_s=args.wait_timeout_s,
        poll_interval_s=args.poll_interval_s,
    )
    try:
        result = coord.follow_run_dir(cfg)
    except coord.FollowTimeoutError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    except Exception as exc:  # noqa: BLE001 — CLI boundary
        print(f"error: follow failed: {exc}", file=sys.stderr)
        return 1

    _print_result(result, as_json=args.json)
    return 0


def _print_result(result: dict, *, as_json: bool) -> None:
    summary = {
        "out_dir": result.get("out_dir") or result.get("metrics", {}).get("out_dir"),
        "scenario": result.get("scenario"),
        "primary_cut_source": result.get("primary_cut_source"),
        "production_cut_text": result.get("production_cut_text"),
        "prefix_valid": result.get("prefix_valid"),
        "fail_closed_b_stream_missing": result.get("fail_closed_b_stream_missing"),
        "immutable": (result.get("commit") or {}).get("immutable"),
        "late_self_asr_revises": (result.get("commit") or {}).get(
            "late_self_asr_revises"
        ),
    }
    # Prefer run_dir from metrics path if present
    metrics = result.get("metrics") or {}
    if summary["out_dir"] is None and metrics:
        summary["primary_cut_source"] = summary["primary_cut_source"] or metrics.get(
            "primary_cut_source"
        )
        summary["production_cut_text"] = summary[
            "production_cut_text"
        ] or metrics.get("production_cut_text")
        summary["prefix_valid"] = (
            summary["prefix_valid"]
            if summary["prefix_valid"] is not None
            else metrics.get("prefix_valid")
        )
        summary["fail_closed_b_stream_missing"] = (
            summary["fail_closed_b_stream_missing"]
            if summary["fail_closed_b_stream_missing"] is not None
            else metrics.get("fail_closed_b_stream_missing")
        )

    if as_json:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return

    print("sts-peek cut coordinator")
    if summary.get("scenario"):
        print(f"  scenario:              {summary['scenario']}")
    if summary.get("out_dir"):
        print(f"  out_dir:               {summary['out_dir']}")
    print(f"  primary_cut_source:    {summary.get('primary_cut_source')}")
    print(f"  production_cut_text:   {summary.get('production_cut_text')!r}")
    print(f"  prefix_valid:          {summary.get('prefix_valid')}")
    print(f"  commit.immutable:      {summary.get('immutable')}")
    print(f"  late_self_asr_revises: {summary.get('late_self_asr_revises')}")
    if summary.get("fail_closed_b_stream_missing"):
        print("  fail_closed:           B stream missing → fallback")


if __name__ == "__main__":
    sys.exit(main())
