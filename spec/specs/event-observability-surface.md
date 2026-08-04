# Event & observability surface

| | |
|---|---|
| **Status** | ACCEPTED — 2026-08-04, by the operator ("I already checked them"); first-party recon 2026-08-04 (`a8886ef`), no prior draft |
| **Charter** | implements invariant 5 (qualified estimates — events name what they know) and the traceability half of invariant 1 (the committed transcript must be *visible* as committed) |

## 1. What is this, and why does it exist?

Every meaningful thing the daemon happens-to-know it *announces* as a JSON
event. Those events feed two very different appetites at once: **live
consumers** (the `watch` tool, future controllers) that need events the moment
they exist, and **the durable log** (jsonl files on disk) that needs to survive
restarts, be replayable, and not drown in high-frequency noise.

This chapter is the contract between those appetites: one funnel, two
destinations, and the exact rules for which events are durable, which are
live-only, and how consumers are expected to name and version what they read.

## 2. What does it do?

### events.1 — one funnel, live-first
All daemon events serialize as single JSON lines through one funnel
(`JsonlLogger::write`). Live subscribers receive the line **before** any
durable I/O runs — the ordering guarantee is in a comment on purpose:
"Preserve immediate live semantics".
*WHEN* any event is emitted *THEN* broadcast delivery precedes disk durability
for that line. *(OBSERVED: `jsonl_logger.rs:68-84` `write`/`write_serialized`,
ordering comment at `:75`)*

### events.2 — two event vocabularies coexist
**Connection events** — the protocol's typed `ServerEvent` enum, 7 variants
(`stream_start`, `hello_ack`, `audio_frame_ingested`, `audio_gap`,
`audio_sample_gap`, `audio_drop`, `error`) — travel typed over the audio
connection's own socket AND through the funnel. **Session/telemetry events** —
~47 string-named event types (`turn_started`, `transcript_committed`,
`vad_speech_start`, `smart_turn_decision`, `runtime_provenance`, …) — are
daemon-internal typed structs serialized through the funnel, visible to
broadcast subscribers and the durable log; they are **not** members of the
protocol enum.
*WHEN* a consumer subscribes to the event stream *THEN* it receives the union
as untyped JSON lines; only connection events enjoy a compile-time enum.
*(OBSERVED: enum `protocol/lib.rs:308-319`; event-name vocabulary collected
from daemon `event:` literals 2026-08-04)*

### events.3 — the live-only list is exactly three names
`vad_meter`, `turn_hold`, and `audio_frame_ingested` are **broadcast-only**:
never written to the durable jsonl. Everything else that passes the funnel is
durable. The filter matches on the serialized line's `event` field.
*WHEN* any of the three filtered names is emitted *THEN* live subscribers see
it and disk never does. *(OBSERVED: `is_jsonl_filtered_event`
`jsonl_logger.rs:268-273`)*

### events.4 — durability is bounded: rotate 256 MiB × 8
The jsonl log rotates at 256 MiB per active file, keeps 8 files, and flushes on
a 1 s / 256-line cadence. Rotation and retention are config-constant, not
env-tunable today.
*WHEN* the active file would exceed 256 MiB with the next line *THEN* rotation
happens before that line is written. *(OBSERVED: defaults
`jsonl_logger.rs:42-43` `max_bytes`/`max_files`; rotate guard at `:205-209`;
flush cadence `main.rs` logger config)*

### events.5 — schema identity is announced once per runtime
At startup the daemon emits exactly one `runtime_provenance` event carrying
`event_schema_version: "speech-core.events.v1"`, runtime id, build id, and
config id. A unit test pins the event's backward-compatible serialization.
*WHEN* the daemon starts *THEN* the first durable event names the schema
version every following line belongs to. *(OBSERVED: constant
`provenance.rs:10`; emitted `main.rs:385`; compat test
`provenance.rs:541-565`)*

### events.6 — subscribing replaces; lagging sheds
The broadcast channel holds 1024 lines. A consumer that falls behind receives a
`Lagged(skipped)` error and simply continues from the newest data — the daemon
never slows down for a slow reader. Sending `SubscribeEvents` on an audio
connection switches that connection into subscriber mode, ending audio
processing on it.
*WHEN* a subscriber lags past 1024 unread lines *THEN* the skipped count is
reported and the stream continues. *(OBSERVED: channel
`main.rs:373`; `Lagged` handling `main.rs:1100-1104`; subscribe takeover
`main.rs:975-989`)*

### events.7 — `watch` is the human window
The `speech-core-watch` binary is the operator surface over this stream, with
five modes: **Transcript** (default — live committed text), **Tui** (compact
state surface), **Debug** (Tui + explanation footer), **Jsonl** (raw passthrough),
and **Inject** (type committed deltas into the focused Wayland window) — plus
`--replay-events <file>` to replay a durable log instead of connecting.
*WHEN* an operator wants eyes on a session *THEN* they attach `watch` on the
events socket or replay a rotated jsonl. *(OBSERVED: mode enum
`watch/main.rs:103-112` + default at `:57`; replay flag `:60-62`)*

## 3. What is load-bearing?

- **Live-before-durable ordering** (`jsonl_logger.rs:75-76`) is a deliberate
  contract, currently protected by a comment, not a test. Any refactor that
  reorders broadcast behind disk I/O changes real-time consumer behavior.
- **Event names are untyped contract strings.** The ~47 session-event names are
  literal strings scattered through daemon source; consumers (`watch`, jsonl
  filter, future controllers) match on the text. Renaming a name compiles fine
  and silently breaks consumers — the enum half (events.2) is safe, the string
  half is convention-policed.
- **The funnel is the only naming authority.** Every writer goes through
  `JsonlLogger::write`; bypassing it (printing, ad-hoc broadcast) would produce
  observability the contract can't describe.
- **`runtime_provenance` is the schema anchor.** Its version string is what a
  future consumer uses to know which event vocabulary a rotated log speaks.

## 4. What is undecided?

- **Versioning policy for the string-named events** — the schema version is
  pinned and backward-compat-tested, but no written policy governs adding/
  renaming fields or event names — UNRESOLVED (revisit when an external
  consumer (controller, city telemetry) depends on field stability).
- **`AudioDrop` has no emitter** — carried over from ingress chapter: variant
  exists, nothing constructs it — UNRESOLVED (same trigger).
- **Rotation/retention tunability** — 256 MiB × 8 is compiled-in —
  UNRESOLVED (revisit when deployment sizing or compliance asks for
  different retention).
- **Whether connection events and session events should ever unify** — two
  vocabularies is *present* truth, not known intent — UNRESOLVED (revisit
  when a second consumer class reasons about which channel an event lives
  on).

## Evidence & reconciliation notes

*Recon and verification 2026-08-04 against `crates/` @ `spec/recon-001`
(`a8886ef`); no prior draft — first-party chapter.*
The event-name count (~47) is a dated snapshot of `event:` literals in the
daemon crate; names were collected mechanically on this date, and churn is
expected as detectors grow.

Suggested probes: attach `watch --mode jsonl` during a live session (live-only
filter visible: `vad_meter` flows, missing from disk); fill a log to rotation
(replay boundary continuity); subscribe and artificially lag >1024 lines
(`Lagged` path).
