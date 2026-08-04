# Audio ingress transport contract

| | |
|---|---|
| **Status** | ACCEPTED — 2026-08-04, by the operator ("I already checked them"); converged from recon draft B, verified against code 2026-08-04 (`a8886ef`) — zero divergence |
| **Charter** | implements invariants 3 (bounded resources) and 5 (qualified estimates — latency is honestly unknown, never fabricated) |

## 1. What is this, and why does it exist?

Speech Core's daemon does not own a microphone. Audio arrives from **adapters** —
separate processes (the mic adapter, the file adapter) that connect over one
websocket and push framed audio. This chapter is the contract on that wire: who
speaks first, what a frame looks like, what the daemon checks, and what happens
when the stream stutters.

It matters because this wire is where *time* enters the system. Every latency
number the product will ever report depends on what the adapter honestly
declares about its clock — so this seam carries a charter-grade honesty rule:
unknown is reported as unknown, never invented.

One websocket connection = one audio session; the turn lifecycle (chapter
`speech-in-turn-lifecycle`) lives downstream of everything written here.

## 2. What does it do?

### ingress.1 — hello speaks first
The adapter SHALL open with a `Hello` control message declaring adapter/stream/
session ids, source kind, sample rate, channels, format, and **timestamp
provenance**. Binary audio before hello is rejected with an `error` event; a
second hello on one connection is rejected the same way. The daemon replies
`stream_start` then `hello_ack` with clock comparability.
*WHEN* any binary frame arrives before a hello *THEN* it is dropped with an
error and the stream does not start. *(OBSERVED: `main.rs:859-866` pre-hello
rejection, `main.rs:914-974` hello arm, dup rejection at `main.rs:925-932`)*

### ingress.2 — the wire envelope (SCF1)
Audio travels as binary frames: magic `SCF1`, version byte `1`, a u32
JSON-header length, a JSON header, then the payload. Header length is capped at
64 KiB. Payload length MUST equal `sample_count × channels × bytes_per_sample`
or decode fails outright.
*WHEN* a frame's payload size contradicts its header *THEN* the frame fails
decoding rather than being reinterpreted. *(OBSERVED: constants
`protocol/lib.rs:10-13`; `encode`/`decode`/`validate_payload_len`
`protocol/lib.rs:203-270`)*

### ingress.3 — every frame is checked against the hello
The daemon SHALL validate each frame's full metadata — adapter/stream/session
ids, source kind, format, rate, channels, **and the timestamp-provenance tuple**
— against the accepted hello. Mismatch produces an `error` event and the frame
is dropped.
*WHEN* any frame field disagrees with the hello *THEN* the frame never reaches
model or detector. *(OBSERVED: `validate_frame_against_hello` `main.rs:1161`)*

### ingress.4 — the daemon is silent per frame by default
There is no per-frame ack. `audio_frame_ingested` exists but is broadcast-only
and filtered out of the durable jsonl log. Silence on the wire is the healthy
state; events appear only when something is noteworthy.
*WHEN* frames flow normally *THEN* the daemon sends nothing back.
*(OBSERVED: broadcast-only handling; `is_jsonl_filtered_event`
`jsonl_logger.rs:70,88`)*

### ingress.5 — gaps surface as events, then everything resets
A sequence-number gap yields an `audio_gap` event; a sample-clock
discontinuity yields `audio_sample_gap`. Both go to the client and the durable
log, and both trigger an `AudioGapReset` fanned out to **both** the model
worker and the detector worker *before the next frame is ingested*.
*WHEN* the stream stutters *THEN* consumers learn of the gap and downstream
state is reset in order, never left half-stale. *(OBSERVED: gap events
`main.rs:875-891`; reset fan-out `main.rs:766-784`)*

### ingress.6 — backpressure drops loudly, never blocks
Model and detector queues are bounded (256 and 512 frames; env-tunable). When a
queue is full, the frame is dropped, a `model_error`/`detector_error` event is
recorded, and an `AudioGapReset` is queued so consumers see a gap rather than
secretly slowed audio. The ingress reader itself never blocks on the queues.
*WHEN* a consumer falls behind *THEN* the stream stays real-time and the loss
is made visible, not smoothed over. *(OBSERVED: queue defaults
`main.rs:90`, `main.rs:246`; `Full`-arm behavior per draft, unchanged)*

### ingress.7 — sessions are guarded by generation
Sessions register by `stream_session_id`; a duplicate active id rejects the
hello. A generation counter ensures an old connection's cleanup cannot kill a
newer session that reused the id. Connection close ends the session: model
finalize and turn close run to completion first.
*WHEN* a reconnect races an old connection's teardown *THEN* the newer session
survives. *(OBSERVED: `start_session` `main.rs:557`; `end_session` generation
guard `main.rs:607`; finalize path `main.rs:1019-1024`)*

### ingress.8 — latency is honestly unknown until clocks are comparable
`capture_to_ingress_latency` returns **no value** when timestamp provenance is
`Uncalibrated` (the mic adapter's default). Values exist only under
`SameClock`/`EstimatedOffset` with `SourceCapture` + `FirstSample` semantics.
*WHEN* comparable clocks are not declared *THEN* latency columns report
unknown — never an estimate dressed as a measurement. *(OBSERVED:
`protocol/lib.rs:458-463`; mic default `mic-adapter/main.rs:494`; daemon test
asserts `None` `main.rs:1289-1292`)*

### ingress.9 — the URL path is convention, not enforcement
The server accepts the websocket upgrade with **no HTTP-path check**; the
documented `/ws/audio-ingress` path is client-side convention only. Bind
default is loopback `127.0.0.1:8765` (`SPEECH_CORE_DAEMON_BIND`).
*WHEN* a client connects on any path *THEN* the daemon serves the audio
protocol identically. *(OBSERVED: `accept_async` `main.rs:839-841`; bind
`main.rs:42-47`; adapter defaults `mic-adapter/main.rs:40`,
`file-adapter/main.rs:28`; README:66)*

## 3. What is load-bearing?

- **The SCF1 envelope is stable wire reality.** Magic/version/header-length/
  payload (`protocol/lib.rs:10-13`). Any change here is a protocol revision,
  not a tweak — adapters in the field speak this byte-for-byte.
- **Timestamp provenance is a first-class contract field**, validated per frame
  (ingress.3). Silently widening what counts as a "measurement" would dissolve
  charter invariant 5.
- **Reset-before-next-frame ordering** (`main.rs:766-784`): reset fan-out must
  precede ingestion of the following frame; reordering it reintroduces the
  half-stale-state bug class the fan-out exists to kill.
- **The session generation guard** (`main.rs:607`) is the only thing standing
  between reconnect churn and torn-down live sessions.

## 4. What is undecided?

- **Drain semantics** — `Drain` is defined in the protocol
  (`protocol/lib.rs:294-298`) but falls through the connection handler
  (`main.rs:991-993`, debug-logged only) — UNRESOLVED (revisit when a client
  needs graceful end-of-stream; shared marker with chapter A).
- **`AudioDrop` has no emitter** — the event variant exists
  (`protocol/lib.rs:314-319`) and test helpers name it, but no code path
  constructs it; queue-full drops surface as error events + gap resets instead —
  UNRESOLVED (revisit when a subscriber needs a dedicated drop signal).
- **Server-side path enforcement** — UNRESOLVED (revisit when an external
  adapter, a reverse proxy, or a release contract requires it).
- **Non-loopback bind policy** — UNRESOLVED (revisit when remote ingress or
  install UX requires it).
- **Subscribe-then-audio multiplexing** — a `SubscribeEvents` on an audio
  connection breaks out of the audio loop (`main.rs:975-989`); audio stops
  being processed on that connection — UNRESOLVED (revisit when a multiplexed
  client appears; OBSERVED today, intent unpicked).
- **Ingress queue depth is a placeholder** — `ingress_queue_depth_frames: 0`
  with the comment "transport v1 has no downstream queue yet"
  (`main.rs:658-659`); the ws receive loop has no depth accounting — INFERRED
  honest placeholder. UNRESOLVED (revisit when observability of the ingress
  bottleneck is specified).

## Evidence & reconciliation notes

*Verified 2026-08-04 against `crates/` @ `spec/recon-001` (`a8886ef`).*
Converged from recon draft B (`docs/recon/specs/audio-ingress-transport.md`).
**Zero divergence found**: all cited line numbers and behavioral claims match
current code; the draft's three INTENT markers carried forward as section-4
questions above.

Suggested probes (from the draft, still valid): live `Drain` message (expect
ignored, connection stays open); forced sequence gap via file-adapter replay
(end-to-end gap + reset); `SubscribeEvents`-then-binary on one connection.
