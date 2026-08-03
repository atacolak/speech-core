# speech-in turn lifecycle

Living-spec draft (phase-1 recon). Scope A — see docs/recon/scopes.md. Labels:
OBSERVED (file:line) / INFERRED (reasoning) / UNRESOLVED (operator question).

## observable behavior

OBSERVED (turn.rs, daemon unit tests, current-state.md): during one websocket audio
session, the daemon emits, in order:

1. `turn_started` (turn.rs:1296 `start_turn`, source `vad` | `transcript` | `model`)
2. `turn_eou_candidate` / `turn_eou_suppressed` (turn.rs:106 `handle_signal` paths)
3. close-time model alignment (`turn_close_alignment`, turn.rs `align_model_for_close`)
4. `turn_eou` (turn.rs:1372)
5. `transcript_committed` — the authoritative per-turn snapshot (turn.rs:1434)
6. `turn_closed` — terminal for consumers (turn.rs:1449)

Invariant (charter #1 + code): `transcript_committed` is emitted BEFORE `turn_closed`
and carries `is_degraded` + `close_source` (turn.rs:1434-1449). Late ASR revisions
appear only in diagnostic `transcript_finalized` (model.rs end_session path), never in
a re-commit. OBSERVED per-turn text is sliced from shared token snapshots by
turn-start token-count baseline + sample boundaries (`per_turn_committed_snapshot`,
model.rs:357).

## load-bearing structure

- **Evidence-only detectors**: `AudioDetector` trait (mod.rs:962) emits `DetectorSignal`
  values; ONLY TurnManager promotes evidence into `turn_eou`/`turn_closed`
  (mod.rs doc comment + turn.rs). OBSERVED.
- **Close sources** (OBSERVED turn.rs `handle_signal`): `vad` (degraded, requires
  `turn_vad_close_enabled`), `smart_turn` (non-degraded, `smart_turn_complete_direct` or
  `_after_vad_speech_end`), `model_eou` (disabled by default), `human_hold`
  (speech-like audio, no tokens, 7500ms default), `transcript_silence`, `vad_acoustic_fallback`,
  `session_end`, `audio_gap`.
- **Semantic gating**: smart-turn `complete=false` suppresses VAD closure only when
  `semantic_gate_enabled && semantic_gate_close_enabled`; timeouts/unavailable FAIL OPEN
  to VAD (turn.rs VadSegmentEnd arm, reasons `smart_turn_timeout_vad_fallback` /
  `smart_turn_unavailable_vad_fallback`). OBSERVED.
- **Close-time model alignment**: preferred path = deterministic finalize — drain real
  audio + `CLOSE_FINALIZE_PADDING_MS=320` synthetic silence → `sc_transcribe_finalize`
  → `rebegin` (model.rs:1166 `finalize_turn_stream`); fallback = progress wait +
  token quiescence (60ms) with trailing-token extension ≤320ms (turn.rs:1879, 2014).
  OBSERVED.
- **Ghost-turn guard**: tokens whose end_sample ≤ last closed decision sample are
  dropped; late orphan tokens without new VAD start are suppressed (turn.rs
  TranscriptTokenCommitted arm, `transcript_token_late_orphan`).
- **Snapshot ring freeze**: after commit, tokens at/before freeze sample are dropped
  until next turn opens (`freeze_tokens_through`, model.rs); punctuation-only tokens
  never open turns (model.rs `record_token_snapshot` guard; `is_speech_evidence_text`).
- **Shared state owner**: `ModelProgressMap` (model.rs) is the single synchronization
  point between model worker thread and turn manager; `current_turn_id` tagging.

## evidence and unknowns

OBSERVED: ordering `transcript_committed` → `turn_closed` and commit text construction
(turn.rs:1434-1449; model.rs:357-455). Human-hold close path (turn.rs:466 VadSpeechPresence).
Acoustic fallback (turn.rs VadAcousticFallback arm). 69 daemon unit tests including
turn-policy tests in turn.rs `mod tests` (event-order assertions).

INFERRED: the "degraded" bit is the code's operationalization of charter's "qualified
estimates, not unqualified facts" — a VAD-only close is `degraded=true`, smart-turn close
`degraded=false`. Stated reasoning only; charter does not name `degraded`.

UNRESOLVED:
1. Turn id format `{stream_session_id}:turn:{index}` (turn.rs:1293) — is `turn_id`
   stable across daemon restarts / reconnects? (index resets per session; sessions are
   per websocket connection.) Controller identity semantics need an operator answer.
2. Multiple turns can close within one websocket session (barge-in). What is the
   documented contract for `turn_id` reuse across `Drain`/reconnect?
3. `transcript_committed` for a turn whose text is empty (e.g. pure human_hold close,
   no tokens) — OBSERVED possible (per_turn_committed_snapshot unwrap_or_default
   yields empty string, turn.rs:1425). Is empty-text commit acceptable product behavior?
4. Alignment timeout default 3000ms (`turn_model_alignment_timeout_ms`) vs finalize
   hard bound 800ms (`CLOSE_FINALIZE_TIMEOUT_MS`) — which number is authoritative for
   the operator's latency budget?
5. What should consume `transcript_committed` (controller)? Nothing in the workspace
   does today (README "Later: Controller").

## next probes

- Run `tests/test-barge-in-dual-asr.sh` and confirm turn-policy event order on the
  current commit.
- File-adapter replay of a recorded `mic.wav` through a fresh daemon; assert exact
  event sequence for a known-close wav.
- Decide empty-text commit question (probe: feed pure noise ≥ human_hold threshold).
