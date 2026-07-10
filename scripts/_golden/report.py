"""Reporting: human-readable plus JSON reports.

Implements spec §13: produces concise diff-style reports for failing
assertions and JSON machine-readable reports.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from .assert_engine import AssertionResult
from .constants import EXIT_NAMES


def print_human_report(
    exit_code: int,
    assertion_result: Optional[AssertionResult] = None,
    validity_record: Optional[Dict[str, Any]] = None,
    *,
    file=None,
) -> None:
    """Print a human-readable report to stdout/stderr."""
    if file is None:
        file = sys.stdout

    code_name = EXIT_NAMES.get(exit_code, str(exit_code))
    status = "PASS" if exit_code == 0 else "FAIL"

    file.write("=" * 60 + "\n")
    file.write(f"  GOLDEN ASSERTION REPORT: {status}\n")
    file.write(f"  Exit code: {exit_code} ({code_name})\n")
    file.write("=" * 60 + "\n\n")

    # Validity summary
    if validity_record:
        file.write("── Validity ──\n")
        file.write(f"  Valid: {validity_record.get('valid', False)}\n")
        file.write(f"  Session: {validity_record.get('stream_session_id', '?')}\n")
        file.write(f"  Events: {validity_record.get('event_count', 0)}\n")
        file.write(f"  Turn closes: {validity_record.get('turn_closed_count', 0)}\n")
        reason = validity_record.get("reason", "")
        if reason:
            file.write(f"  Reason: {reason}\n")
        hashes = validity_record.get("artifact_hashes", {})
        if hashes:
            file.write("  Hashes:\n")
            for k, v in hashes.items():
                file.write(f"    {k}: {v}\n")
        file.write("\n")

    # Assertion results
    if assertion_result:
        _print_assertion_result(file, assertion_result)


def _print_assertion_result(file, ar: AssertionResult) -> None:
    file.write("── Assertions ──\n")
    file.write(f"  Passed: {len(ar.passed)}\n")
    file.write(f"  Violations: {len(ar.violations)}\n")
    file.write(f"  Warnings: {len(ar.warnings)}\n\n")

    if ar.violations:
        file.write("── Violations ──\n")
        for v in ar.violations:
            file.write(f"  ✗ {v.rule}\n")
            file.write(f"    {v.detail}\n")
            if v.evidence:
                file.write(f"    Evidence ({len(v.evidence)} events):\n")
                for ev in v.evidence[:3]:
                    evt = ev.get("event") or ev.get("type", "?")
                    # Extract key fields for concise diff
                    fields = _extract_key_fields(ev)
                    file.write(f"      [{evt}] {json.dumps(fields)}\n")
                if len(v.evidence) > 3:
                    file.write(f"      ... and {len(v.evidence) - 3} more\n")
            file.write("\n")

    if ar.warnings:
        file.write("── Warnings ──\n")
        for w in ar.warnings:
            file.write(f"  ⚠ {w}\n")
        file.write("\n")


def _extract_key_fields(event: Dict[str, Any]) -> Dict[str, Any]:
    """Extract semantically meaningful fields for diff display."""
    stable_keys = [
        "event", "type", "source", "degraded", "reason", "complete",
        "timed_out", "text", "seq", "sample_start", "sample_count",
        "source_sample_start", "start_sample", "end_sample", "decision_sample",
        "probability", "threshold", "confidence",
    ]
    return {k: event[k] for k in stable_keys if k in event}


def write_json_report(
    exit_code: int,
    assertion_result: Optional[AssertionResult],
    validity_record: Optional[Dict[str, Any]],
    path: Path,
    *,
    scenario_id: Optional[str] = None,
) -> None:
    """Write a machine-readable JSON report."""
    report: Dict[str, Any] = {
        "exit_code": exit_code,
        "exit_name": EXIT_NAMES.get(exit_code, str(exit_code)),
        "scenario_id": scenario_id,
        "passed": exit_code == 0,
    }
    if validity_record:
        report["validity"] = validity_record
    if assertion_result:
        report["assertions"] = assertion_result.to_dict()

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False, default=str)
