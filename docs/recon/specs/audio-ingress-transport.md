# audio ingress transport contract

Living-spec draft (phase-1 recon). Scope B — see docs/recon/scopes.md. Labels:
OBSERVED (file:line) / INFERRED (reasoning) / UNRESOLVED (operator question).

## observable behavior

OBSERVED (protocol/src/lib.rs, daemon main.rs): adapters and daemon exchange over one
websocket connection, in order:

1. adapter sends `ControlMessage::Hello` (lib.rs enum) — declares adapter_id,
   stream_id, stream_session_id, source_kind, sample_rate_hz, channels, format,
   timestamp_provenance.
2. daemon replies `stream_start`, then `hello_ack` with clock-comparability
   (Hello arm, main.rs:914-975). Binary audio BEFORE hello is rejected with an
   `error` event (main.rs:861-868). Duplicate hello on one connection is rejected
   (main.rs:923-934).
3. adapter sends binary `SCF1` frames (`AudioFrame::encode/decode`,
   lib.rs:224-266); daemon is SILENT per frame by default (no per-frame ack;
   `audio_frame_ingested` is broadcast-only and filtered from durable jsonl,
   jsonl_logger.rs `is_jsonl_filtered_event`).
4. sequence gaps → `audio_gap` event; sample-clock discontinuities → `audio_sample_gap`
   event; both sent to client + jsonl (main.rs:877-891) and trigger a reset of model +
   detector state (`AudioGapReset` fan-out, main.rs:766-775).
5. connection close (or `Drain`) ends the session; model finalize + turn close run
   (cleanup after ws loop, main.rs:1018-1024 → `end_session` main.rs:607).

OBSERVED frame metadata is validated against the hello on every frame —
`validate_frame_against_hello` (main.rs:1161): adapter/stream/session ids, source_kind,
format, rate, channels, AND the full timestamp provenance tuple. Mismatch = `error`
event, frame dropped.

## load-bearing structure

- **Envelope**: magic `SCF1`, version byte 1, u32 json-header length, JSON header,
  payload (lib.rs:10-12). Payload length MUST equal `sample_count × channels ×
  bytes_per_sample` or decode fails (`AudioFrame::validate_payload_len`). Header cap
  64 KiB.
- **Session registry**: `DaemonState.sessions` keyed by `stream_session_id`
  (main.rs:557-606); duplicate active session id → hello rejected; generation counter
  prevents stale cleanup from killing a newer session (`end_session` generation check,
  main.rs:607-647).
- **Clock provenance rules**: `ClockComparability` ∈ {SameClock, EstimatedOffset,
  Uncalibrated}; `capture_to_ingress_latency` returns None (status Uncalibrated/
  TimestampQualityInsufficient/TimestampSemanticsUnsupported) unless comparable +
  SourceCapture + FirstSample (lib.rs latency fns). Cross-host adapters default to
  Uncalibrated → latency is honestly unknown, never fabricated. OBSERVED.
- **Gap reset fan-out**: `AudioGapReset` is derived from sample gaps and dropped-frame
  queue-full events (`AudioGapReset::from_dropped_frame`, main.rs) and applied to BOTH
  model worker and detector worker before the next frame is ingested (main.rs:773-784).
- **Backpressure**: model queue 256 frames / detector queue 512 frames
  (`SPEECH_CORE_MODEL_QUEUE_FRAMES`, `SPEECH_CORE_DETECTOR_QUEUE_FRAMES`); on `Full`,
  the frame is dropped, a `model_error`/`detector_error` event is logged, and an
  `AudioGapReset` is queued (model.rs ingest_frame, mod.rs ingest_frame). Ingress
  itself never blocks on the queues.

## evidence and unknowns

OBSERVED: protocol unit tests (lib.rs tests: round-trip, latency statuses, payload
length rejection); daemon ws integration tests (main.rs tests: silent-ingest default,
duplicate-hello cleanup, gap reporting, metadata rejection, partial-startup rollback);
mic/file adapters both implement hello-then-frames.

INFERRED: `ingress_queue_depth_frames: 0` ("transport v1 has no downstream queue yet",
main.rs comment) is an honest placeholder — the async ws receive loop is the real
bottleneck and has no depth accounting. Stated from code comment + absence of queue.

UNRESOLVED:
1. HTTP path `/ws/audio-ingress` is client-side only (accept_async, main.rs:839, does
   not route on path). Is a pathless ws endpoint the contract, or should the daemon
   reject non-conforming paths? (README documents the path as if it were server-side.)
2. `Drain` control message is defined in the protocol but NOT handled in
   `handle_connection` (falls into the `Ok(control) =>` debug arm, main.rs:991).
   Dead contract field or unimplemented feature?
3. `AudioDrop` server event exists in the enum (lib.rs) but OBSERVED never emitted —
   drops are reported as `*_error` events instead. Is `audio_drop` reserved for a
   future queue-aware transport?
4. Bind default is `127.0.0.1:8765` (loopback) while README says `ws://host:8765` —
   install script writes `SPEECH_CORE_DAEMON_BIND=$bind`; is remote-ingress supported
   or intentionally local-only?
5. Latency `value_ms` is only meaningful under SameClock/EstimatedOffset; default
   adapters are Uncalibrated so live latency columns are None. Is that acceptable for
   the operator's observability goals, or should the mic adapter do clock estimation?

## next probes

- Send a `Drain` message against a live daemon; verify it is ignored (expect: no
  session end, connection stays open).
- Send a `SubscribeEvents` and then binary frames on the SAME connection; verify the
  subscriber loop takes over and audio stops being processed (main.rs:1000-1005 breaks
  out of the audio loop — OBSERVED behavior, unverified intent).
- Replay with file-adapter across a forced sequence gap (file-adapter has no gap
  injection today); confirm `audio_gap` + reset semantics end-to-end.
