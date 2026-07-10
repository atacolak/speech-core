#!/usr/bin/env python3
"""speech-core-golden-assert — semantic golden capture, validity, assertion, and reporting.

Owns: capture/event subscription, validity, assertion DSL/evaluation, reporting.
Delegates: validate-manifest, record, synth, promote → sibling golden_cli.

Usage:
  speech-core-golden-assert capture [...]    Subscribe and capture events
  speech-core-golden-assert assert [...]     Run assertion DSL
  speech-core-golden-assert run [...]        Combined capture + assert
  speech-core-golden-assert test             Run deterministic mock tests
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Optional

# Ensure the parent scripts dir is importable for _golden package
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from _golden.capture import (
    MockEventTransport,
    TerminalMarkerTracker,
    WebsocketEventTransport,
    capture_events,
)
from _golden.validity import validate_capture_artifacts
from _golden.assert_engine import run_assertions
from _golden.report import print_human_report, write_json_report
from _golden.constants import (
    DEFAULT_TERMINAL_MARKERS,
    EXIT_PASS,
    EXIT_NAMES,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="speech-core-golden-assert — semantic golden test runner"
    )
    sub = parser.add_subparsers(dest="command", help="subcommand")

    # ── capture ──────────────────────────────────────────────────────────
    cap = sub.add_parser("capture", help="Subscribe and capture event stream")
    cap.add_argument("--url", default="ws://127.0.0.1:8765/ws/audio-ingress",
                     help="Daemon websocket URL")
    cap.add_argument("--stream-session-id", required=True,
                     help="Unique stream session id")
    cap.add_argument("--out", required=True, type=Path,
                     help="Output directory for event-stream.jsonl")
    cap.add_argument("--timeout-ms", type=int, default=30000,
                     help="Capture timeout in ms")
    cap.add_argument("--adapter-cmd", nargs="*",
                     help="Optional adapter command to spawn")
    cap.add_argument("--adapter-cwd", type=Path,
                     help="Working dir for adapter")

    # ── assert ───────────────────────────────────────────────────────────
    asp = sub.add_parser("assert", help="Run assertion DSL against captured events")
    asp.add_argument("--scenario-dir", required=True, type=Path,
                     help="Directory with event-stream.jsonl and assertion YAML")
    asp.add_argument("--assertion-dsl", type=Path,
                     help="Path to assertion DSL YAML/JSON file")
    asp.add_argument("--stream-session-id",
                     help="Expected stream session id for validity check")
    asp.add_argument("--wav-hash", help="Expected WAV SHA-256")
    asp.add_argument("--config-hash", help="Expected config SHA-256")

    # ── run ──────────────────────────────────────────────────────────────
    runp = sub.add_parser("run", help="Combined capture + assert")
    runp.add_argument("--url", default="ws://127.0.0.1:8765/ws/audio-ingress")
    runp.add_argument("--stream-session-id", required=True)
    runp.add_argument("--out", required=True, type=Path)
    runp.add_argument("--timeout-ms", type=int, default=30000)
    runp.add_argument("--assertion-dsl", type=Path,
                      help="Assertion DSL file (YAML or JSON)")
    runp.add_argument("--wav-hash")
    runp.add_argument("--config-hash")

    # ── test ─────────────────────────────────────────────────────────────
    testp = sub.add_parser("test", help="Run deterministic mock tests")
    testp.add_argument("--verbose", "-v", action="store_true")

    # ── validate ─────────────────────────────────────────────────────────
    valp = sub.add_parser("validate-events", help="Validate captured events")
    valp.add_argument("--scenario-dir", required=True, type=Path)
    valp.add_argument("--stream-session-id", required=True)

    args = parser.parse_args()

    if args.command == "capture":
        return asyncio.run(_cmd_capture(args))
    elif args.command == "assert":
        return _cmd_assert(args)
    elif args.command == "run":
        return asyncio.run(_cmd_run(args))
    elif args.command == "test":
        return _cmd_test(args)
    elif args.command == "validate-events":
        return _cmd_validate(args)
    else:
        parser.print_help()
        return 0


# ── command implementations ──────────────────────────────────────────────────

async def _cmd_capture(args) -> int:
    transport = WebsocketEventTransport(timeout=args.timeout_ms / 1000.0)
    try:
        exit_code, events, validity = await capture_events(
            transport=transport,
            url=args.url,
            stream_session_id=args.stream_session_id,
            out_dir=args.out,
            timeout_ms=args.timeout_ms,
            adapter_command=args.adapter_cmd,
            adapter_cwd=args.adapter_cwd,
        )
    except ConnectionError as e:
        print(f"DAEMON_UNREACHABLE: {e}", file=sys.stderr)
        return 6
    except Exception as e:
        print(f"INTERNAL_ERROR: {e}", file=sys.stderr)
        return 20

    print_human_report(exit_code, validity_record=validity)
    write_json_report(exit_code, None, validity, args.out / "capture-report.json")
    return exit_code


def _cmd_assert(args) -> int:
    scenario_dir = args.scenario_dir
    if not scenario_dir.exists():
        print(f"SCENARIO_NOT_FOUND: {scenario_dir}", file=sys.stderr)
        return 14

    # Load events
    events_path = scenario_dir / "event-stream.jsonl"
    if not events_path.exists():
        print(f"CAPTURE_INCOMPLETE: {events_path} not found", file=sys.stderr)
        return 21

    events = []
    with open(events_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError as e:
                    print(f"EVENT_SCHEMA_INVALID: {e}", file=sys.stderr)
                    return 18

    # Load DSL
    dsl = {}
    dsl_path = args.assertion_dsl or (scenario_dir / "assertion.dsl.json")
    if dsl_path.exists():
        with open(dsl_path, "r", encoding="utf-8") as f:
            dsl = json.load(f)
    else:
        # Try YAML
        yaml_path = scenario_dir / "assertion.dsl.yaml"
        if yaml_path.exists():
            try:
                import yaml as _yaml
                with open(yaml_path, "r") as f:
                    dsl = _yaml.safe_load(f)
            except ImportError:
                print("DEPENDENCY_MISSING: PyYAML needed for YAML DSL", file=sys.stderr)
                return 5

    if not dsl:
        print("No assertion DSL found", file=sys.stderr)
        return 20

    # Run validity first
    validity_exit, validity_record = validate_capture_artifacts(
        scenario_dir=scenario_dir,
        stream_session_id=args.stream_session_id or "unknown",
        expected_wav_hash=args.wav_hash,
        expected_config_hash=args.config_hash,
    )

    if validity_exit != 0:
        print_human_report(validity_exit, validity_record=validity_record,
                           file=sys.stderr)
        write_json_report(validity_exit, None, validity_record,
                          scenario_dir / "assert-report.json")
        return validity_exit

    # Run assertions
    exit_code, result = run_assertions(events, dsl, scenario_dir)

    print_human_report(exit_code, result, validity_record)
    write_json_report(exit_code, result, validity_record,
                      scenario_dir / "assert-report.json")
    return exit_code


async def _cmd_run(args) -> int:
    """Combined capture + assert."""
    transport = WebsocketEventTransport(timeout=args.timeout_ms / 1000.0)
    try:
        exit_code, events, validity = await capture_events(
            transport=transport,
            url=args.url,
            stream_session_id=args.stream_session_id,
            out_dir=args.out,
            timeout_ms=args.timeout_ms,
        )
    except ConnectionError as e:
        print(f"DAEMON_UNREACHABLE: {e}", file=sys.stderr)
        return 6
    except Exception as e:
        print(f"INTERNAL_ERROR: {e}", file=sys.stderr)
        return 20

    if exit_code != 0:
        print_human_report(exit_code, validity_record=validity, file=sys.stderr)
        write_json_report(exit_code, None, validity, args.out / "assert-report.json")
        return exit_code

    # Load DSL
    dsl = {}
    dsl_path = args.assertion_dsl or (args.out / "assertion.dsl.json")
    if dsl_path.exists():
        with open(dsl_path, "r", encoding="utf-8") as f:
            dsl = json.load(f)

    if dsl:
        exit_code, result = run_assertions(events, dsl, args.out)
        print_human_report(exit_code, result, validity)
        write_json_report(exit_code, result, validity, args.out / "assert-report.json")
        return exit_code

    print_human_report(exit_code, validity_record=validity)
    write_json_report(exit_code, None, validity, args.out / "assert-report.json")
    return exit_code


def _cmd_validate(args) -> int:
    """Validate captured events without assertion DSL."""
    exit_code, record = validate_capture_artifacts(
        scenario_dir=args.scenario_dir,
        stream_session_id=args.stream_session_id,
    )
    print_human_report(exit_code, validity_record=record)
    return exit_code


# ── deterministic mock tests ─────────────────────────────────────────────────

def _cmd_test(args) -> int:
    """Run deterministic mock tests for capture, validity, and assertion engine."""
    import traceback

    failures = []
    test_funcs = [
        test_capture_basic_one_close,
        test_capture_duplicate_close,
        test_capture_session_end_close,
        test_capture_late_revision,
        test_capture_gap_drop,
        test_capture_reused_id_wrong_session,
        test_capture_missing_terminal,
        test_capture_malformed_json,
        test_capture_empty_stream,
        test_validity_empty_file,
        test_validity_stale_session,
        test_validity_wrong_session_id,
        test_assert_require_forbid,
        test_assert_order,
        test_assert_partial_order,
        test_assert_numeric_tolerance,
        test_assert_balanced_turns,
        test_assert_sample_window,
        test_assert_unstable_field_warning,
        test_assert_transcript,
        test_terminal_marker_tracker,
        test_capture_timeout,
        test_assert_ownership_exactly_one,
        test_assert_ownership_no_late_mutation,
    ]

    for test_fn in test_funcs:
        name = test_fn.__name__
        try:
            test_fn()
            if args.verbose:
                print(f"  PASS  {name}")
        except AssertionError as e:
            failures.append((name, str(e), traceback.format_exc()))
            print(f"  FAIL  {name}: {e}")
        except Exception as e:
            failures.append((name, str(e), traceback.format_exc()))
            print(f"  ERROR {name}: {e}")
            if args.verbose:
                traceback.print_exc()

    print(f"\n{'='*50}")
    print(f"  Results: {len(test_funcs) - len(failures)}/{len(test_funcs)} passed")
    if failures:
        print(f"  Failures:")
        for name, msg, _ in failures:
            print(f"    - {name}: {msg}")
    print(f"{'='*50}")

    return 1 if failures else 0


# ── mock fixtures ────────────────────────────────────────────────────────────

def _make_event(event_type: str, **kwargs) -> dict:
    """Create a mock event dict."""
    e = {"event": event_type, **kwargs}
    return e


def _fixture_normal_one_close() -> list:
    """Normal one-close scenario: VAD start, turn start, VAD end, smart turn complete, EOU, close."""
    return [
        _make_event("stream_start", stream_session_id="session-1",
                     stream_id="test", adapter_id="mock", sample_rate_hz=16000),
        _make_event("vad_session_start", stream_session_id="session-1",
                     frame_ms=32, threshold=0.5),
        _make_event("smart_turn_session_start", stream_session_id="session-1",
                     threshold=0.5, timeout_ms=250),
        _make_event("turn_session_start", stream_session_id="session-1"),
        _make_event("vad_speech_start", stream_session_id="session-1",
                     start_sample=5000, decision_sample=5500, confidence=0.9),
        _make_event("turn_started", stream_session_id="session-1",
                     turn_id="turn-1", start_sample=5000),
        _make_event("vad_speech_end", stream_session_id="session-1",
                     start_sample=5000, end_sample=12000, decision_sample=13536),
        _make_event("smart_turn_candidate", stream_session_id="session-1",
                     end_sample=12000),
        _make_event("smart_turn_decision", stream_session_id="session-1",
                     end_sample=12000, decision_sample=13536,
                     complete=True, probability=0.85, threshold=0.5),
        _make_event("turn_semantic_decision", stream_session_id="session-1",
                     complete=True),
        _make_event("turn_eou", stream_session_id="session-1",
                     source="smart_turn"),
        _make_event("turn_closed", stream_session_id="session-1",
                     turn_id="turn-1", source="smart_turn", degraded=False,
                     reason="semantic_complete", end_sample=12000, decision_sample=13536),
        _make_event("vad_session_end", stream_session_id="session-1"),
        _make_event("turn_session_end", stream_session_id="session-1"),
        _make_event("smart_turn_session_end", stream_session_id="session-1"),
        _make_event("model_chunk_processed", stream_session_id="session-1",
                     is_final=True),
    ]


def _fixture_duplicate_close() -> list:
    """Duplicate close: session_end follow-up."""
    return [
        _make_event("stream_start", stream_session_id="session-1"),
        _make_event("vad_session_start", stream_session_id="session-1"),
        _make_event("smart_turn_session_start", stream_session_id="session-1"),
        _make_event("turn_session_start", stream_session_id="session-1"),
        _make_event("vad_speech_start", stream_session_id="session-1"),
        _make_event("turn_started", stream_session_id="session-1", turn_id="turn-1"),
        _make_event("vad_speech_end", stream_session_id="session-1"),
        _make_event("smart_turn_decision", stream_session_id="session-1", complete=True),
        _make_event("turn_closed", stream_session_id="session-1",
                     turn_id="turn-1", source="smart_turn", degraded=False),
        _make_event("turn_closed", stream_session_id="session-1",
                     turn_id="turn-1", source="session_end", degraded=True),
        _make_event("vad_session_end", stream_session_id="session-1"),
        _make_event("turn_session_end", stream_session_id="session-1"),
        _make_event("smart_turn_session_end", stream_session_id="session-1"),
        _make_event("model_chunk_processed", stream_session_id="session-1", is_final=True),
    ]


def _fixture_gap_drop() -> list:
    """Audio gap due to dropped frames."""
    return [
        _make_event("stream_start", stream_session_id="session-1"),
        _make_event("vad_session_start", stream_session_id="session-1"),
        _make_event("smart_turn_session_start", stream_session_id="session-1"),
        _make_event("turn_session_start", stream_session_id="session-1"),
        _make_event("audio_frame_ingested", stream_session_id="session-1",
                     seq=0, sample_start=0, sample_count=320),
        _make_event("audio_frame_ingested", stream_session_id="session-1",
                     seq=1, sample_start=320, sample_count=320),
        _make_event("audio_gap", stream_session_id="session-1",
                     expected_seq=2, observed_seq=3, missing_frames=1),
        _make_event("audio_frame_ingested", stream_session_id="session-1",
                     seq=3, sample_start=960, sample_count=320),
        _make_event("vad_session_end", stream_session_id="session-1"),
        _make_event("turn_session_end", stream_session_id="session-1"),
        _make_event("smart_turn_session_end", stream_session_id="session-1"),
        _make_event("model_chunk_processed", stream_session_id="session-1", is_final=True),
    ]


def _fixture_wrong_session() -> list:
    """Event with wrong stream_session_id mixed in."""
    return [
        _make_event("stream_start", stream_session_id="session-1"),
        _make_event("vad_session_start", stream_session_id="session-1"),
        _make_event("vad_speech_start", stream_session_id="session-1"),
        _make_event("turn_started", stream_session_id="session-1", turn_id="turn-1"),
        _make_event("vad_speech_end", stream_session_id="session-2"),  # WRONG
        _make_event("turn_closed", stream_session_id="session-1", turn_id="turn-1"),
        _make_event("vad_session_end", stream_session_id="session-1"),
        _make_event("turn_session_end", stream_session_id="session-1"),
        _make_event("smart_turn_session_end", stream_session_id="session-1"),
        _make_event("model_chunk_processed", stream_session_id="session-1", is_final=True),
    ]


def _fixture_incomplete() -> list:
    """No turn_closed at all."""
    return [
        _make_event("stream_start", stream_session_id="session-1"),
        _make_event("vad_session_start", stream_session_id="session-1"),
        _make_event("vad_speech_start", stream_session_id="session-1"),
        _make_event("turn_started", stream_session_id="session-1", turn_id="turn-1"),
        _make_event("vad_speech_end", stream_session_id="session-1"),
        # Missing: turn_closed, session ends
        _make_event("vad_session_end", stream_session_id="session-1"),
        _make_event("turn_session_end", stream_session_id="session-1"),
        _make_event("smart_turn_session_end", stream_session_id="session-1"),
        _make_event("model_chunk_processed", stream_session_id="session-1", is_final=True),
    ]


def _fixture_missing_terminal() -> list:
    """Missing vad_session_end terminal marker."""
    return [
        _make_event("stream_start", stream_session_id="session-1"),
        _make_event("turn_session_start", stream_session_id="session-1"),
        _make_event("smart_turn_session_start", stream_session_id="session-1"),
        _make_event("vad_speech_start", stream_session_id="session-1"),
        _make_event("turn_started", stream_session_id="session-1", turn_id="turn-1"),
        _make_event("vad_speech_end", stream_session_id="session-1"),
        _make_event("smart_turn_decision", stream_session_id="session-1", complete=True),
        _make_event("turn_closed", stream_session_id="session-1", turn_id="turn-1"),
        # Missing vad_session_end
        _make_event("turn_session_end", stream_session_id="session-1"),
        _make_event("smart_turn_session_end", stream_session_id="session-1"),
        _make_event("model_chunk_processed", stream_session_id="session-1", is_final=True),
    ]


def _fixture_late_revision() -> list:
    """Token revision arrives after turn_closed."""
    return [
        _make_event("stream_start", stream_session_id="session-1"),
        _make_event("vad_session_start", stream_session_id="session-1"),
        _make_event("smart_turn_session_start", stream_session_id="session-1"),
        _make_event("turn_session_start", stream_session_id="session-1"),
        _make_event("vad_speech_start", stream_session_id="session-1"),
        _make_event("turn_started", stream_session_id="session-1", turn_id="turn-1"),
        _make_event("transcript_token_committed", stream_session_id="session-1",
                     turn_id="turn-1", text="hello"),
        _make_event("vad_speech_end", stream_session_id="session-1"),
        _make_event("smart_turn_decision", stream_session_id="session-1", complete=True),
        _make_event("turn_closed", stream_session_id="session-1",
                     turn_id="turn-1", source="smart_turn", degraded=False),
        _make_event("transcript_token_committed", stream_session_id="session-1",
                     turn_id="turn-2", text="world"),  # late, new turn
        _make_event("vad_session_end", stream_session_id="session-1"),
        _make_event("turn_session_end", stream_session_id="session-1"),
        _make_event("smart_turn_session_end", stream_session_id="session-1"),
        _make_event("model_chunk_processed", stream_session_id="session-1", is_final=True),
    ]


# ── test implementations ─────────────────────────────────────────────────────

def _run_mock_capture(events: list, session_id: str = "session-1",
                       timeout_ms: int = 10000, *, close_cleanly: bool = True,
                       transport_events: Optional[list] = None) -> tuple:
    """Helper: run capture with mock transport, return (exit_code, events, validity)."""
    import tempfile
    if transport_events is not None:
        raw = transport_events
    else:
        raw = [json.dumps(e) for e in events]
    transport = MockEventTransport(raw, close_cleanly=close_cleanly)
    with tempfile.TemporaryDirectory() as tmp:
        return asyncio.run(
            capture_events(
                transport=transport,
                url="mock://",
                stream_session_id=session_id,
                out_dir=Path(tmp),
                timeout_ms=timeout_ms,
            )
        )


def test_capture_basic_one_close():
    """Normal one-close scenario captures successfully."""
    events = _fixture_normal_one_close()
    exit_code, captured, validity = _run_mock_capture(events)
    assert exit_code == 0, f"Expected 0, got {exit_code}: {validity.get('reason')}"
    assert validity["valid"] is True
    assert validity["turn_closed_count"] == 1
    assert len(captured) > 0


def test_capture_duplicate_close():
    """Duplicate close (session_end follow-up) should still capture but assertion should catch it."""
    events = _fixture_duplicate_close()
    exit_code, captured, validity = _run_mock_capture(events)
    assert exit_code == 0  # Capture succeeds (all terminal markers present)
    # But there should be 2 turn_closed events
    closed = [e for e in captured if e.get("event") == "turn_closed"]
    assert len(closed) == 2


def test_capture_session_end_close():
    """session_end close in normal scenario — captured but assertion DSL should forbid."""
    events = _fixture_duplicate_close()
    exit_code, captured, validity = _run_mock_capture(events)
    assert exit_code == 0
    # Run forbid assertion
    dsl = {
        "forbid": [
            {"event": "turn_closed", "where": {"source": "session_end"}}
        ]
    }
    exit_code, result = run_assertions(captured, dsl)
    assert exit_code != 0, "Should fail because session_end close is present"
    assert not result.all_passed


def test_capture_late_revision():
    """Late transcript after close."""
    events = _fixture_late_revision()
    exit_code, captured, validity = _run_mock_capture(events)
    assert exit_code == 0


def test_capture_gap_drop():
    """Audio gap events captured."""
    events = _fixture_gap_drop()
    exit_code, captured, validity = _run_mock_capture(events)
    assert exit_code == 0
    gap_events = [e for e in captured if e.get("event") == "audio_gap"]
    assert len(gap_events) >= 1


def test_capture_reused_id_wrong_session():
    """Event with wrong session id mixed in fails validity."""
    events = _fixture_wrong_session()
    exit_code, captured, validity = _run_mock_capture(events)
    # Should fail because event with wrong session was mixed in
    assert exit_code != 0


def test_capture_missing_terminal():
    """Missing terminal marker fails."""
    events = _fixture_missing_terminal()
    exit_code, captured, validity = _run_mock_capture(events)
    assert exit_code != 0, f"Expected nonzero for missing terminal, got {exit_code}"


def test_capture_malformed_json():
    """Malformed JSON in event stream should fail."""
    import tempfile
    transport = MockEventTransport(
        ['{"event": "valid"}', 'NOT JSON', '{"event": "also_valid"}'],
        close_cleanly=True,
    )
    with tempfile.TemporaryDirectory() as tmp:
        exit_code, _, validity = asyncio.run(
            capture_events(
                transport=transport, url="mock://",
                stream_session_id="session-1",
                out_dir=Path(tmp),
                timeout_ms=10000,
            )
        )
    assert exit_code != 0, "Malformed JSON should fail capture"


def test_capture_empty_stream():
    """Empty event stream should fail."""
    import tempfile
    transport = MockEventTransport([], close_cleanly=True)
    with tempfile.TemporaryDirectory() as tmp:
        exit_code, _, validity = asyncio.run(
            capture_events(
                transport=transport, url="mock://",
                stream_session_id="session-1",
                out_dir=Path(tmp),
                timeout_ms=5000,
            )
        )
    assert exit_code != 0, "Empty stream should fail"


def test_capture_timeout():
    """Capture timeout before terminal markers."""
    import tempfile
    # Events without terminal markers — should timeout
    events = [
        _make_event("stream_start", stream_session_id="session-1"),
        _make_event("vad_session_start", stream_session_id="session-1"),
        _make_event("vad_speech_start", stream_session_id="session-1"),
        _make_event("turn_started", stream_session_id="session-1", turn_id="turn-1"),
        _make_event("vad_speech_end", stream_session_id="session-1"),
        # No terminal markers
    ]
    raw = [json.dumps(e) for e in events]
    transport = MockEventTransport(raw + [None], close_cleanly=True)
    with tempfile.TemporaryDirectory() as tmp:
        exit_code, _, validity = asyncio.run(
            capture_events(
                transport=transport, url="mock://",
                stream_session_id="session-1",
                out_dir=Path(tmp),
                timeout_ms=1000,
            )
        )
    # With timeout + clean close, the code returns TERMINAL_MARKER_MISSING (9)
    # because the stream ended cleanly but markers were missing.
    # With timeout before clean close, it returns CAPTURE_TIMEOUT (8).
    # Either is acceptable.
    assert exit_code in (8, 9, 21), f"Expected timeout/incomplete/missing, got {exit_code}"


def test_validity_empty_file():
    """Validity check on nonexistent file fails."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        exit_code, record = validate_capture_artifacts(
            scenario_dir=Path(tmp),
            stream_session_id="session-1",
        )
        assert exit_code != 0


def test_validity_stale_session():
    """Validity check catches stale/missing stream."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        # Write empty file
        (Path(tmp) / "event-stream.jsonl").write_text("")
        exit_code, record = validate_capture_artifacts(
            scenario_dir=Path(tmp),
            stream_session_id="session-1",
        )
        assert exit_code != 0


def test_validity_wrong_session_id():
    """Events with wrong stream_session_id fail validity."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        events = _fixture_wrong_session()
        with open(Path(tmp) / "event-stream.jsonl", "w") as f:
            for e in events:
                f.write(json.dumps(e) + "\n")
        exit_code, record = validate_capture_artifacts(
            scenario_dir=Path(tmp),
            stream_session_id="session-1",
        )
        assert exit_code != 0, f"Wrong session should fail, got {exit_code}"


# ── assertion DSL tests ──────────────────────────────────────────────────────

def test_assert_require_forbid():
    """require/forbid assertions."""
    events = _fixture_normal_one_close()
    dsl = {
        "require": [
            {"event": "turn_closed", "count": 1},
            {"event": "vad_speech_start"},
        ],
        "forbid": [
            {"event": "turn_closed", "where": {"source": "session_end"}},
        ],
    }
    exit_code, result = run_assertions(events, dsl)
    assert exit_code == 0, f"Expected pass, got {exit_code}: {[v.detail for v in result.violations]}"


def test_assert_order():
    """Order constraint between causally related events."""
    events = _fixture_normal_one_close()
    dsl = {
        "order": [
            ["vad_speech_start", "turn_started"],
            ["vad_speech_end", "smart_turn_candidate", "smart_turn_decision"],
        ],
    }
    exit_code, result = run_assertions(events, dsl)
    assert exit_code == 0, f"Order should pass: {[v.detail for v in result.violations]}"


def test_assert_partial_order():
    """Partial order with same-session matching."""
    events = _fixture_normal_one_close()
    dsl = {
        "partial_order": [
            {
                "before": {"event": "turn_semantic_decision"},
                "after": {"event": "turn_closed"},
                "match": "same_session",
            }
        ],
    }
    exit_code, result = run_assertions(events, dsl)
    assert exit_code == 0, f"Partial order should pass"


def test_assert_numeric_tolerance():
    """Numeric assertions with tolerance."""
    events = _fixture_normal_one_close()
    dsl = {
        "numeric": [
            {
                "event": "vad_speech_end",
                "field": "decision_sample",
                "expected_samples": 13536,
                "tolerance_samples": 512,
            }
        ],
    }
    exit_code, result = run_assertions(events, dsl)
    assert exit_code == 0


def test_assert_balanced_turns():
    """Balanced turns invariant."""
    events = _fixture_normal_one_close()
    dsl = {"invariants": ["balanced_turns"]}
    exit_code, result = run_assertions(events, dsl)
    assert exit_code == 0


def test_assert_sample_window():
    """Sample window assertions."""
    events = _fixture_normal_one_close()
    dsl = {
        "sample_window": [
            {
                "name": "close_after_acoustic_end",
                "event": "turn_closed",
                "field": "decision_sample",
                "min_from": "event(vad_speech_end).end_sample",
                "max_from": "event(vad_speech_end).end_sample + samples(2500ms)",
            }
        ],
    }
    exit_code, result = run_assertions(events, dsl)
    assert exit_code == 0, f"Sample window should pass: {[v.detail for v in result.violations]}"


def test_assert_unstable_field_warning():
    """Assertion using unstable fields should warn."""
    from _golden.assert_engine import AssertionEvaluator
    dsl = {
        "require": [
            {"event": "turn_closed", "where": {"daemon_mono_ns": 12345}},
        ]
    }
    evaluator = AssertionEvaluator(dsl, [])
    evaluator._check_unstable_fields_in_dsl()
    assert len(evaluator.result.warnings) > 0, "Should warn about unstable field"


def test_assert_transcript():
    """Transcript assertion."""
    events = _fixture_late_revision()
    dsl = {
        "transcript": {
            "normalize": "lowercase_strip_punctuation_whitespace",
            "require_any": ["hello"],
        }
    }
    exit_code, result = run_assertions(events, dsl)
    assert exit_code == 0


def test_assert_ownership_exactly_one():
    """Ownership: exactly one close per started turn."""
    events = _fixture_normal_one_close()
    dsl = {
        "ownership": {
            "key": "turn_id",
            "require_exactly_one_close_per_started_turn": True,
        }
    }
    exit_code, result = run_assertions(events, dsl)
    assert exit_code == 0

    # With duplicate close, should fail
    events_dup = _fixture_duplicate_close()
    exit_code, result = run_assertions(events_dup, dsl)
    assert exit_code != 0


def test_assert_ownership_no_late_mutation():
    """Ownership: no late mutation after close."""
    events = _fixture_normal_one_close()
    dsl = {
        "ownership": {
            "key": "turn_id",
            "forbid_late_mutation_after_close": True,
        }
    }
    exit_code, result = run_assertions(events, dsl)
    assert exit_code == 0


def test_terminal_marker_tracker():
    """TerminalMarkerTracker correctly tracks markers."""
    tracker = TerminalMarkerTracker(DEFAULT_TERMINAL_MARKERS)
    assert not tracker.all_observed
    assert tracker.required_count == 4

    tracker.feed({"event": "vad_session_end"})
    assert tracker.observed_count == 1
    assert not tracker.all_observed

    tracker.feed({"event": "turn_session_end"})
    tracker.feed({"event": "smart_turn_session_end"})
    tracker.feed({"event": "model_chunk_processed", "is_final": True})
    assert tracker.all_observed


# ── entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    sys.exit(main())
