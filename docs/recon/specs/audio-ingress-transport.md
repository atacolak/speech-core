# audio ingress transport contract

Living-spec draft (phase-1 recon). Scope B — see docs/recon/scopes.md. Labels:
OBSERVED (file:line) / INFERRED (reasoning) / INTENT — UNRESOLVED (revisit when
<trigger>). No operator-escalation language; minutiae stay inline until a named
trigger raises them.

## observable behavior

OBSERVED (`crates/speech-core-protocol/src/lib.rs`,
`crates/speech-core-daemon/src/main.rs`): adapters and daemon exchange over one
websocket connection, in order:

1. adapter sends `ControlMessage::Hello` (`crates/speech-core-protocol/src/lib.rs`
   enum) — declares adapter_id, stream_id, stream_session_id, source_kind,
   sample_rate_hz, channels, format, timestamp_provenance.
2. daemon replies `stream_start`, then `hello_ack` with clock-comparability
   (Hello arm, `crates/speech-core-daemon/src/main.rs:914-974`). Binary audio
   BEFORE hello is rejected with an `error` event
   (`crates/speech-core-daemon/src/main.rs:859-866`). Duplicate hello on one
   connection is rejected (`crates/speech-core-daemon/src/main.rs:925-932`).
3. adapter sends binary `SCF1` frames (`AudioFrame::encode/decode`,
   `crates/speech-core-protocol/src/lib.rs:224-270`); daemon is SILENT per frame
   by default (no per-frame ack; `audio_frame_ingested` is broadcast-only and
   filtered from durable jsonl, `crates/speech-core-daemon/src/jsonl_logger.rs`
   `is_jsonl_filtered_event`).
4. sequence gaps → `audio_gap` event; sample-clock discontinuities →
   `audio_sample_gap` event; both sent to client + jsonl
   (`crates/speech-core-daemon/src/main.rs:875-891`) and trigger a reset of model
   + detector state (`AudioGapReset` fan-out,
   `crates/speech-core-daemon/src/main.rs:766-774`).
5. connection close (or cleanup after ws loop) ends the session; model finalize +
   turn close run (`crates/speech-core-daemon/src/main.rs:1019-1024` →
   `end_session` `crates/speech-core-daemon/src/main.rs:607`).

OBSERVED frame metadata is validated against the hello on every frame —
`validate_frame_against_hello` (`crates/speech-core-daemon/src/main.rs:1161`):
adapter/stream/session ids, source_kind, format, rate, channels, AND the full
timestamp provenance tuple. Mismatch = `error` event, frame dropped.

OBSERVED websocket path handling: server uses
`tokio_tungstenite::accept_async` with no HTTP-path check
(`crates/speech-core-daemon/src/main.rs:839-841`). Client defaults use
`ws://127.0.0.1:8765/ws/audio-ingress`
(`crates/speech-core-mic-adapter/src/main.rs:40`,
`crates/speech-core-file-adapter/src/main.rs:28`); README documents
`ws://host:8765/ws/audio-ingress` (`README.md:66`). Path is client convention
only today.
INTENT — UNRESOLVED (revisit when an external adapter, reverse-proxy route, or
release contract requires server-side path enforcement).

OBSERVED `Drain` control message: defined in protocol
(`crates/speech-core-protocol/src/lib.rs:294-298`) but not matched in
`handle_connection`; non-Hello/non-SubscribeEvents controls fall into the
`Ok(control) =>` debug arm (`crates/speech-core-daemon/src/main.rs:991-993`).
INTENT — UNRESOLVED (revisit when a client sends Drain for graceful end-of-stream
or a transport revision implements it).

OBSERVED `AudioDrop` server event: variant exists
(`crates/speech-core-protocol/src/lib.rs:314-319`) and is named in test helpers
(`crates/speech-core-daemon/src/main.rs:1505`) but is not constructed on the
ingress drop path; queue-full drops surface as `model_error`/`detector_error`
plus `AudioGapReset` instead.
INTENT — UNRESOLVED (revisit when a queue-aware transport or subscriber needs a
dedicated drop event).

OBSERVED bind default: daemon listens on `127.0.0.1:8765`
(`crates/speech-core-daemon/src/main.rs:42-47`, env `SPEECH_CORE_DAEMON_BIND`);
README table shows `ws://host:8765/ws/audio-ingress` (`README.md:66`); install
script writes `SPEECH_CORE_DAEMON_BIND=$bind`. Loopback is the code default.
INTENT — UNRESOLVED (revisit when remote-ingress deployment or install UX requires
a non-loopback bind policy).

OBSERVED latency provenance: `capture_to_ingress_latency` returns no `value_ms`
under `ClockComparability::Uncalibrated`
(`crates/speech-core-protocol/src/lib.rs:458-463`); default mic-adapter hello uses
`Uncalibrated` (`crates/speech-core-mic-adapter/src/main.rs:494`); daemon tests
assert `value_ms is None` for that path
(`crates/speech-core-daemon/src/main.rs:1289-1292`). Live latency columns are
honestly unknown unless comparable clocks are declared.
INTENT — UNRESOLVED (revisit when operator observability or a cross-host adapter
requires estimated/same-clock latency).

## load-bearing structure

- **Envelope**: magic `SCF1`, version byte 1, u32 json-header length, JSON header,
  payload (`crates/speech-core-protocol/src/lib.rs:10-13`). Payload length MUST
  equal `sample_count × channels × bytes_per_sample` or decode fails
  (`AudioFrame::validate_payload_len`). Header cap 64 KiB.
- **Session registry**: `DaemonState.sessions` keyed by `stream_session_id`
  (`crates/speech-core-daemon/src/main.rs:557-605`); duplicate active session id →
  hello rejected; generation counter prevents stale cleanup from killing a newer
  session (`end_session` generation check,
  `crates/speech-core-daemon/src/main.rs:607-641`).
- **Clock provenance rules**: `ClockComparability` ∈ {SameClock, EstimatedOffset,
  Uncalibrated}; `capture_to_ingress_latency` returns None (status Uncalibrated/
  TimestampQualityInsufficient/TimestampSemanticsUnsupported) unless comparable +
  SourceCapture + FirstSample (`crates/speech-core-protocol/src/lib.rs` latency
  fns). Cross-host adapters default to Uncalibrated → latency is honestly
  unknown, never fabricated. OBSERVED.
- **Gap reset fan-out**: `AudioGapReset` is derived from sample gaps and
  dropped-frame queue-full events and applied to BOTH model worker and detector
  worker before the next frame is ingested
  (`crates/speech-core-daemon/src/main.rs:766-784`).
- **Backpressure**: model queue 256 frames / detector queue 512 frames
  (`SPEECH_CORE_MODEL_QUEUE_FRAMES`, `SPEECH_CORE_DETECTOR_QUEUE_FRAMES`); on
  `Full`, the frame is dropped, a `model_error`/`detector_error` event is logged,
  and an `AudioGapReset` is queued. Ingress itself never blocks on the queues.

## evidence

OBSERVED: protocol unit tests (`crates/speech-core-protocol/src/lib.rs` tests:
round-trip, latency statuses, payload length rejection); daemon ws integration
tests (`crates/speech-core-daemon/src/main.rs` tests: silent-ingest default,
duplicate-hello cleanup, gap reporting, metadata rejection, partial-startup
rollback); mic/file adapters both implement hello-then-frames.

INFERRED: `ingress_queue_depth_frames: 0` ("transport v1 has no downstream queue
yet", `crates/speech-core-daemon/src/main.rs:658-659` comment) is an honest
placeholder — the async ws receive loop is the real bottleneck and has no depth
accounting. Stated from code comment + absence of queue.

## next probes

- Send a `Drain` message against a live daemon; verify it is ignored (expect: no
  session end, connection stays open) — evidence for the Drain marker above.
- Send a `SubscribeEvents` and then binary frames on the SAME connection; verify
  the subscriber loop takes over and audio stops being processed
  (`crates/speech-core-daemon/src/main.rs:975-989` breaks out of the audio loop —
  OBSERVED behavior, intent tracked only if a multiplexed client appears).
- Replay with file-adapter across a forced sequence gap; confirm `audio_gap` +
  reset semantics end-to-end.
