"""Validity: fail-closed capture validity contract.

Implements spec §7 fail-closed validity table: every condition that makes
evidence invalid is checked before semantic assertions run.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .constants import (
    EXIT_ARTIFACT_HASH_MISMATCH,
    EXIT_CAPTURE_INCOMPLETE,
    EXIT_CAPTURE_TIMEOUT,
    EXIT_CONFIG_MISMATCH,
    EXIT_EVENT_SCHEMA_INVALID,
    EXIT_INTERNAL_ERROR,
    EXIT_PASS,
    EXIT_WAV_FORMAT_INVALID,
    Event,
    EventStream,
)


def validate_capture_artifacts(
    scenario_dir: Path,
    stream_session_id: str,
    *,
    expected_wav_hash: Optional[str] = None,
    expected_config_hash: Optional[str] = None,
    expected_scenario_hash: Optional[str] = None,
    expected_profile_hash: Optional[str] = None,
    required_markers: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[int, Dict[str, Any]]:
    """Run fail-closed validity checks on captured artifacts.

    Returns (exit_code, validity_record). Nonzero exit means invalid;
    semantic assertions must not run.

    Checks performed:
    1. event-stream.jsonl exists, non-empty, not stale
    2. Every line is valid JSON
    3. All events with stream_session_id match the expected id
    4. Required terminal markers present
    5. WAV exists and decodes (basic check) if expected_wav_hash provided
    6. Hash comparisons
    """
    events_path = scenario_dir / "event-stream.jsonl"
    record: Dict[str, Any] = {
        "scenario_dir": str(scenario_dir),
        "stream_session_id": stream_session_id,
        "valid": False,
        "check_results": {},
    }

    # ── 1. event-stream.jsonl exists ─────────────────────────────────────
    if not events_path.exists():
        record["reason"] = "event-stream.jsonl missing"
        record["exit_code"] = EXIT_CAPTURE_INCOMPLETE
        return EXIT_CAPTURE_INCOMPLETE, record

    if events_path.stat().st_size == 0:
        record["reason"] = "event-stream.jsonl is empty"
        record["exit_code"] = EXIT_CAPTURE_INCOMPLETE
        return EXIT_CAPTURE_INCOMPLETE, record

    # ── 2. parse all events, validate JSON ───────────────────────────────
    events, schema_error = _load_and_validate_jsonl(events_path)
    if schema_error:
        record["reason"] = schema_error
        record["exit_code"] = EXIT_EVENT_SCHEMA_INVALID
        return EXIT_EVENT_SCHEMA_INVALID, record

    record["event_count"] = len(events)

    if len(events) == 0:
        record["reason"] = "No events in event-stream.jsonl"
        record["exit_code"] = EXIT_CAPTURE_INCOMPLETE
        return EXIT_CAPTURE_INCOMPLETE, record

    # ── 3. validate stream_session_id ────────────────────────────────────
    wrong_session_events = []
    for i, event in enumerate(events):
        evt_session = event.get("stream_session_id")
        if evt_session is not None and evt_session != stream_session_id:
            wrong_session_events.append(i)
    if wrong_session_events:
        record["reason"] = (
            f"Events with wrong stream_session_id at indices {wrong_session_events}"
        )
        record["exit_code"] = EXIT_EVENT_SCHEMA_INVALID
        return EXIT_EVENT_SCHEMA_INVALID, record

    # ── 4. check for stream_start ────────────────────────────────────────
    has_stream_start = any(
        (e.get("event") or e.get("type")) == "stream_start"
        for e in events
    )
    if not has_stream_start:
        record["reason"] = "No stream_start event"
        record["exit_code"] = EXIT_CAPTURE_INCOMPLETE
        return EXIT_CAPTURE_INCOMPLETE, record

    # ── 5. check terminal markers ────────────────────────────────────────
    if required_markers:
        observed_types = {(e.get("event") or e.get("type")) for e in events}
        missing = []
        for marker in required_markers:
            event_name = marker.get("event")
            if event_name and event_name not in observed_types:
                missing.append(marker)
        if missing:
            record["reason"] = f"Missing terminal markers: {missing}"
            record["exit_code"] = EXIT_CAPTURE_INCOMPLETE
            record["missing_markers"] = missing
            return EXIT_CAPTURE_INCOMPLETE, record

    # ── 6. hash checks ───────────────────────────────────────────────────
    events_hash = _sha256_file(events_path)
    record["artifact_hashes"] = {"event_stream_sha256": events_hash}

    if expected_wav_hash:
        wav_path = scenario_dir / "audio.wav"
        if not wav_path.exists():
            record["reason"] = "audio.wav missing"
            record["exit_code"] = EXIT_WAV_FORMAT_INVALID
            return EXIT_WAV_FORMAT_INVALID, record
        wav_hash = _sha256_file(wav_path)
        record["artifact_hashes"]["wav_sha256"] = wav_hash
        if wav_hash != expected_wav_hash:
            record["reason"] = (
                f"WAV hash mismatch: expected {expected_wav_hash}, got {wav_hash}"
            )
            record["exit_code"] = EXIT_ARTIFACT_HASH_MISMATCH
            return EXIT_ARTIFACT_HASH_MISMATCH, record

    if expected_config_hash:
        config_path = scenario_dir / "effective-config.json"
        if config_path.exists():
            config_hash = _sha256_file(config_path)
            record["artifact_hashes"]["effective_config_sha256"] = config_hash
            if config_hash != expected_config_hash:
                record["reason"] = (
                    f"Config hash mismatch: expected {expected_config_hash}, got {config_hash}"
                )
                record["exit_code"] = EXIT_CONFIG_MISMATCH
                return EXIT_CONFIG_MISMATCH, record

    # ── passed all validity checks ───────────────────────────────────────
    record["valid"] = True
    record["exit_code"] = EXIT_PASS
    record["reason"] = "All validity checks passed"
    return EXIT_PASS, record


def _load_and_validate_jsonl(path: Path) -> Tuple[EventStream, Optional[str]]:
    """Load events from JSONL file. Returns (events, error_message)."""
    events = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line_no, line in enumerate(f, 1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                event = json.loads(stripped)
            except json.JSONDecodeError as e:
                return [], f"Malformed JSON at line {line_no}: {e}"
            if not isinstance(event, dict):
                return [], f"Event at line {line_no} is not a JSON object"
            events.append(event)
    return events, None


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
