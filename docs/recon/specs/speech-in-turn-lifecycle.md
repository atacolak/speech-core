# speech-in turn lifecycle

Living-spec draft (phase-1 recon). Scope A — see docs/recon/scopes.md. Labels:
OBSERVED (file:line) / INFERRED (reasoning) / INTENT — UNRESOLVED (revisit when
<trigger>). No operator-escalation language; minutiae stay inline until a named
trigger raises them.

## observable behavior

OBSERVED (`crates/speech-core-daemon/src/detectors/turn.rs`, daemon unit tests,
`docs/current-state.md`): during one websocket audio session, the daemon emits, in
order:

1. `turn_started` (`crates/speech-core-daemon/src/detectors/turn.rs:1296` `start_turn`,
   source `vad` | `transcript` | `model`)
2. `turn_eou_candidate` / `turn_eou_suppressed`
   (`crates/speech-core-daemon/src/detectors/turn.rs:106` `handle_signal` paths)
3. close-time model alignment (`turn_close_alignment`,
   `crates/speech-core-daemon/src/detectors/turn.rs` `align_model_for_close`)
4. `turn_eou` (`crates/speech-core-daemon/src/detectors/turn.rs:1372`)
5. `transcript_committed` — the authoritative per-turn snapshot
   (`crates/speech-core-daemon/src/detectors/turn.rs:1434`)
6. `turn_closed` — terminal for consumers
   (`crates/speech-core-daemon/src/detectors/turn.rs:1449`)

Invariant (charter #1 + code): `transcript_committed` is emitted BEFORE `turn_closed`
and carries `is_degraded` + `close_source`
(`crates/speech-core-daemon/src/detectors/turn.rs:1434-1449`). Late ASR revisions
appear only in diagnostic `transcript_finalized` (`crates/speech-core-daemon/src/model.rs`
end_session path), never in a re-commit. OBSERVED per-turn text is sliced from shared
token snapshots by turn-start token-count baseline + sample boundaries
(`per_turn_committed_snapshot`, `crates/speech-core-daemon/src/model.rs:357`).

OBSERVED turn-id construction: format `{stream_session_id}:turn:{index}` via
`next_turn_id` (`crates/speech-core-daemon/src/detectors/turn.rs:1289-1293`); index is
session-local and increments in `start_turn`
(`crates/speech-core-daemon/src/detectors/turn.rs:1302-1303`). Sessions are per websocket
connection (`crates/speech-core-daemon/src/main.rs:557-605` `start_session`).
INTENT — UNRESOLVED (revisit when a controller, reconnect harness, or cross-session
consumer requires a stable turn identity contract).

OBSERVED multi-turn within one websocket session: barge-in / successive closes are
exercised by daemon turn-policy tests in
`crates/speech-core-daemon/src/detectors/turn.rs` `mod tests` (multiple
`transcript_committed` / `turn_closed` pairs per session). `Drain` is defined on the
wire (`crates/speech-core-protocol/src/lib.rs:294-298`) but is not handled in
`handle_connection` (falls through `Ok(control)` debug arm,
`crates/speech-core-daemon/src/main.rs:991-993`), so reconnect/Drain does not currently
participate in turn-id allocation.
INTENT — UNRESOLVED (revisit when Drain is implemented or a reconnect/barge-in consumer
documents turn_id reuse rules).

OBSERVED empty-text commit path: `per_turn_committed_snapshot` may yield
`unwrap_or_default()` empty string before `transcript_committed`
(`crates/speech-core-daemon/src/detectors/turn.rs:1402-1411`, write at
`crates/speech-core-daemon/src/detectors/turn.rs:1433-1447`), including pure
`human_hold` closes with no tokens
(`crates/speech-core-daemon/src/detectors/turn.rs:461-519`).
INTENT — UNRESOLVED (revisit when a controller or product acceptance criterion rejects
or requires empty-text commits).

OBSERVED close-time latency bounds (two numbers, both live): CLI/env default
`turn_model_alignment_timeout_ms = 3000`
(`crates/speech-core-daemon/src/main.rs:317-323`); hard finalize bound
`CLOSE_FINALIZE_TIMEOUT_MS = 800`
(`crates/speech-core-daemon/src/detectors/turn.rs:1829`); alignment budget uses
`timeout_ms.max(CLOSE_FINALIZE_TIMEOUT_MS)`
(`crates/speech-core-daemon/src/detectors/turn.rs:1933-1934`,
`crates/speech-core-daemon/src/detectors/turn.rs:1964`).
INTENT — UNRESOLVED (revisit when an operator latency budget or SLO must pick one
authoritative number).

OBSERVED human-hold default conflict (recorded, not resolved): CLI/env default
`turn_human_hold_silence_ms = 7500`
(`crates/speech-core-daemon/src/main.rs:293-299`) is what the running daemon wires into
`TurnManagerConfig` (`crates/speech-core-daemon/src/main.rs:443`);
`TurnManagerConfig::default()` sets `human_hold_silence_ms: 12000`
(`crates/speech-core-daemon/src/detectors/turn.rs:39-54`). Comment on the hold-progress
event says "typically 7500ms"
(`crates/speech-core-daemon/src/detectors/turn.rs:1756`). Both values exist; this draft
does not select intent.
INTENT — UNRESOLVED (revisit when tests construct TurnManagerConfig via Default, or a
config/UX change requires one hold threshold).

OBSERVED commit consumer surface: `transcript_committed` is emitted as the controller
dispatch trigger (comment at
`crates/speech-core-daemon/src/detectors/turn.rs:1387-1389`,
`crates/speech-core-daemon/src/detectors/turn.rs:1471`) and written to the event log /
broadcast path; nothing in this workspace consumes it as a controller (README "Later:
Controller").
INTENT — UNRESOLVED (revisit when a controller or external consumer is introduced).

## load-bearing structure

- **Evidence-only detectors**: `AudioDetector` trait
  (`crates/speech-core-daemon/src/detectors/mod.rs:962`) emits `DetectorSignal` values;
  ONLY TurnManager promotes evidence into `turn_eou`/`turn_closed`
  (`crates/speech-core-daemon/src/detectors/mod.rs:958-961` doc comment +
  `crates/speech-core-daemon/src/detectors/turn.rs`). OBSERVED.
- **Close sources** (OBSERVED `crates/speech-core-daemon/src/detectors/turn.rs`
  `handle_signal`): `vad` (degraded, requires `turn_vad_close_enabled`), `smart_turn`
  (non-degraded, `smart_turn_complete_direct` or `_after_vad_speech_end`), `model_eou`
  (disabled by default), `human_hold` (speech-like audio, no tokens; runtime default
  7500ms via CLI — see conflict above), `transcript_silence`, `vad_acoustic_fallback`,
  `session_end`, `audio_gap`.
- **Semantic gating**: smart-turn `complete=false` suppresses VAD closure only when
  `semantic_gate_enabled && semantic_gate_close_enabled`; timeouts/unavailable FAIL OPEN
  to VAD (`crates/speech-core-daemon/src/detectors/turn.rs` VadSegmentEnd arm, reasons
  `smart_turn_timeout_vad_fallback` / `smart_turn_unavailable_vad_fallback`). OBSERVED.
- **Close-time model alignment**: preferred path = deterministic finalize — drain real
  audio + `CLOSE_FINALIZE_PADDING_MS=320` synthetic silence → `sc_transcribe_finalize`
  → `rebegin` (`crates/speech-core-daemon/src/model.rs:1166` `finalize_turn_stream`);
  fallback = progress wait + token quiescence (60ms) with trailing-token extension
  ≤320ms (`crates/speech-core-daemon/src/detectors/turn.rs:1821-1829`,
  `crates/speech-core-daemon/src/detectors/turn.rs:1879`,
  `crates/speech-core-daemon/src/detectors/turn.rs:2014`). OBSERVED.
- **Ghost-turn guard**: tokens whose end_sample ≤ last closed decision sample are
  dropped; late orphan tokens without new VAD start are suppressed
  (`crates/speech-core-daemon/src/detectors/turn.rs` TranscriptTokenCommitted arm,
  `transcript_token_late_orphan`).
- **Snapshot ring freeze**: after commit, tokens at/before freeze sample are dropped
  until next turn opens (`freeze_tokens_through`,
  `crates/speech-core-daemon/src/model.rs`); punctuation-only tokens never open turns
  (`crates/speech-core-daemon/src/model.rs` `record_token_snapshot` guard;
  `is_speech_evidence_text`).
- **Shared state owner**: `ModelProgressMap` (`crates/speech-core-daemon/src/model.rs`)
  is the single synchronization point between model worker thread and turn manager;
  `current_turn_id` tagging.

## evidence

OBSERVED: ordering `transcript_committed` → `turn_closed` and commit text construction
(`crates/speech-core-daemon/src/detectors/turn.rs:1434-1449`;
`crates/speech-core-daemon/src/model.rs:357-455`). Human-hold close path
(`crates/speech-core-daemon/src/detectors/turn.rs:435-529` VadSpeechPresence). Acoustic
fallback (`crates/speech-core-daemon/src/detectors/turn.rs` VadAcousticFallback arm). 69
daemon unit tests including turn-policy tests in
`crates/speech-core-daemon/src/detectors/turn.rs` `mod tests` (event-order assertions).

INFERRED: the "degraded" bit is the code's operationalization of charter's "qualified
estimates, not unqualified facts" — a VAD-only close is `degraded=true`, smart-turn close
`degraded=false`. Stated reasoning only; charter does not name `degraded`.

## next probes

- Run parked `lab/tests/test-barge-in-dual-asr.sh` only if resurrecting dual-ASR.
  live turn-policy evidence is the daemon crate tests.
- File-adapter replay of a recorded `mic.wav` through a fresh daemon; assert exact
  event sequence for a known-close wav.
- Feed pure noise ≥ human_hold threshold and capture whether empty-text
  `transcript_committed` is emitted (evidence for the empty-commit marker above).
