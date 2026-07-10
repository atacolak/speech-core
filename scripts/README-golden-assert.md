# speech-core-golden-assert — Semantic Golden Assertion Engine

## Ownership (per spec `docs/golden-suite-spec.md`)

This module owns:
- **Capture/event subscription**: Direct WebSocket subscription, terminal marker waiting, event stream persistence
- **Validity**: Fail-closed capture validity contract
- **Assertion DSL v1**: require/forbid/count/order/partial-order/numeric/transcript/invariants evaluation
- **Reporting**: Human-readable + JSON reports, exit codes
- **Command integration surface**: `capture`, `assert`, `run`, `validate-events`, `test`

Sibling `golden_cli` owns: `validate-manifest`, `record`, `synth`, `promote` and main manifest/profile fixtures.

## Quickstart

```bash
# Run all deterministic mock tests (no daemon/model/WAV needed)
python3 scripts/speech-core-golden-assert.py test -v

# Capture events from live daemon
python3 scripts/speech-core-golden-assert.py capture \
  --url ws://127.0.0.1:8765/ws/audio-ingress \
  --stream-session-id "run-$(date +%s)" \
  --out tests/golden-runs/my-run

# Validate captured events
python3 scripts/speech-core-golden-assert.py validate-events \
  --scenario-dir tests/golden-runs/my-run \
  --stream-session-id "run-12345"

# Run assertions against captured artifacts
python3 scripts/speech-core-golden-assert.py assert \
  --scenario-dir tests/golden-runs/my-run \
  --assertion-dsl tests/golden/fixtures/my-scenario/assertion.dsl.json
```

## Interface for sibling CLI

The `golden_cli` tool can import and delegate assertion work:

```python
import sys
sys.path.insert(0, 'scripts')

from _golden.capture import capture_events, MockEventTransport, WebsocketEventTransport
from _golden.validity import validate_capture_artifacts
from _golden.assert_engine import run_assertions, AssertionResult
from _golden.report import print_human_report, write_json_report
from _golden.constants import (
    EXIT_PASS, EXIT_ASSERTION_FAILED, EXIT_CAPTURE_INCOMPLETE,
    DEFAULT_TERMINAL_MARKERS, SAMPLE_RATE,
)
```

### Capture delegation

```python
# Sibling CLI orchestrates:
# 1. Generate stream_session_id
# 2. Start daemon/adapter if needed
# 3. Call capture_events()
# 4. Handle exit codes

exit_code, events, validity = await capture_events(
    transport=WebsocketEventTransport(),
    url="ws://127.0.0.1:8765/ws/audio-ingress",
    stream_session_id=stream_session_id,
    out_dir=run_dir,
    timeout_ms=30_000,
)
```

### Assertion delegation

```python
# After capture completes, run assertions:
exit_code, result = run_assertions(events, dsl_dict, scenario_dir)

# Produce reports:
print_human_report(exit_code, result, validity_record)
write_json_report(exit_code, result, validity_record, report_path)
```

### Mock testing (no daemon/model/WAV)

```python
# For deterministic CI testing:
raw_events = [json.dumps(evt) for evt in fixture_events]
transport = MockEventTransport(raw_events, close_cleanly=True)
exit_code, events, validity = await capture_events(
    transport=transport, url="mock://",
    stream_session_id="test-session",
    out_dir=tmp_dir,
)
```

## Architecture

```
scripts/speech-core-golden-assert.py   # CLI entry point
scripts/_golden/
  __init__.py          # Package marker
  constants.py         # Exit codes, timing constants, terminal markers, blacklists
  capture.py           # WebSocket transport, terminal marker tracker, capture loop
  validity.py          # Fail-closed validity checks (pre-assertion gate)
  assert_engine.py     # DSL v1 evaluator (require/forbid/order/numeric/transcript/...)
  report.py            # Human-readable and JSON report generation
```

## Exit Codes (spec §13)

| Code | Name |
|------|------|
| 0 | PASS |
| 1 | ASSERTION_FAILED |
| 2 | MANIFEST_INVALID |
| 5 | DEPENDENCY_MISSING |
| 6 | DAEMON_UNREACHABLE |
| 8 | CAPTURE_TIMEOUT |
| 9 | TERMINAL_MARKER_MISSING |
| 10 | ARTIFACT_HASH_MISMATCH |
| 12 | CONFIG_MISMATCH |
| 14 | SCENARIO_NOT_FOUND |
| 17 | WAV_FORMAT_INVALID |
| 18 | EVENT_SCHEMA_INVALID |
| 20 | INTERNAL_ERROR |
| 21 | CAPTURE_INCOMPLETE |

## Deterministic Mock Fixtures

24 mock tests cover:

- **Normal one-close**: VAD start → turn start → VAD end → smart turn complete → close
- **Duplicate close**: session_end follow-up after normal close
- **Late revision**: transcript token after turn close
- **Gap/drop**: declared audio gaps
- **Wrong session**: mixed stream_session_id → fails validity
- **Missing terminal**: vad_session_end missing → fails capture
- **Malformed JSON**: unparseable events → EXIT_EVENT_SCHEMA_INVALID
- **Empty stream**: no events → fails
- **Timeout**: no terminal markers by deadline → fails
- **Validity**: empty file, stale session, wrong session id
- **Assertion DSL**: require, forbid, order, partial order, numeric tolerance, balanced turns, sample windows, transcript, ownership, unstable field warnings

## Protocol Assumptions

1. **Direct WebSocket subscription**: Uses `SubscribeEvents` control message to subscribe to daemon events filtered by `stream_session_id`.
2. **Terminal markers**: Required set `{vad_session_end, turn_session_end, smart_turn_session_end, model_chunk_processed(is_final=true)}` for `golden-mvp` profile.
3. **Fail-closed**: Capture fails on empty/stale/malformed/wrong-session/error/incomplete evidence.
4. **Filtered diagnostics**: `vad_meter`, `turn_hold` events are separated to `filtered-live-diagnostics.jsonl`.
5. **Transport abstraction**: `EventTransport` interface supports real WebSocket and deterministic mock.

## Residual Live Integration Limitations

1. **WebSocket transport**: Uses stdlib-only WebSocket implementation. No support for WSS/TLS in MVP.
2. **Adapter process spawning**: Basic subprocess management; production may need more robust lifecycle.
3. **Clock calibration**: Mock transport has no clock simulation; real transport relies on daemon monotonic clock.
4. **WAV validation**: Basic hash check only; full WAV format validation (RIFF header, sample rate, format) is deferred to sibling `golden_cli` or quality module.
5. **Live smoke**: Requires running daemon and real model; only mock tests are CI-safe.
6. **YAML DSL**: Requires optional PyYAML dependency for YAML assertion files.

## Migration from raw JSONL diff

The old approach compared raw JSONL files byte-for-byte. The new approach:

1. Subscribes directly to daemon events via WebSocket (never tails global log)
2. Waits for explicit terminal markers (never sleeps for fixed duration)
3. Evaluates semantic assertions (not raw JSON equality)
4. Blacklists unstable fields (IDs, timestamps, floats, durations)
5. Uses tolerances for sample-domain comparisons
6. Produces structured human+JSON reports with exact exit codes
