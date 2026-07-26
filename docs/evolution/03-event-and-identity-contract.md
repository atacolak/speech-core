# 03 — Event and identity contract

**status:** accepted target specification; schema not yet implemented
**authority:** subordinate to `CHARTER.md`; not proof of implementation

This document defines planning requirements, not a final serialization schema.

## Canonical envelope requirements

Every cross-component event must carry:

- `schema_name` and `schema_version`;
- globally unique `event_id`;
- stable `event_type`;
- `producer` and `producer_instance_id`;
- monotonically increasing producer-local `producer_seq`;
- relevant domain identities;
- `causation_id` and `correlation_id` where the event is part of a transaction;
- source monotonic clock identity/value and comparability/uncertainty;
- payload validated for the event version.

Illustrative shape:

```json
{
  "schema_name": "speech-core.event",
  "schema_version": 1,
  "event_id": "evt-...",
  "event_type": "speech.player.cursor",
  "producer": "speech-player",
  "producer_instance_id": "player-...",
  "producer_seq": 184,
  "session_id": "session-...",
  "causation_id": "evt-...",
  "correlation_id": "interrupt-...",
  "source_clock": {
    "clock_id": "host-monotonic:machine-a",
    "mono_ns": 123456789,
    "comparability": "same_clock",
    "uncertainty_ns": 0
  },
  "payload": {}
}
```

## Identity hierarchy

Use only identities relevant to an event, but preserve the hierarchy:

```text
session_id
  turn_id
    agent_run_id
      assistant_response_id
        assistant_message_id
          speakable_segment_id
            utterance_id
              audio_chunk_id
```

Additional transaction identities:

- `stream_session_id` — one capture or playback stream lifetime;
- `tool_call_id` — native Pi tool invocation identity;
- `steer_id` — one committed user-turn delivery attempt to an active run;
- `interruption_id` — one coordinated audible-barge transaction;
- `playback_operation_id` — one player submission/terminal lifecycle;
- `binding_epoch` — one exact Talker-to-agent-session attachment epoch;
- `calibration_id` — player/audibility calibration used by an estimate.

## Audio sample domains

An event carrying audio offsets must name the sample domain and format. Do not compare offsets from different domains without an explicit mapping event.

At minimum distinguish:

- generated utterance samples;
- egress stream samples;
- player-submitted samples;
- device-consumed estimate;
- captured/loopback samples;
- BFA alignment audio samples.

An audio range contains:

```text
sample_domain_id
sample_rate_hz
channels
format
source_sample_start
source_sample_end
```

## Terminal semantics

Every operation has exactly one terminal outcome:

```text
completed | cancelled | failed | superseded | rejected | lost
```

Retries receive new operation IDs linked to the failed operation. A transport disconnect is not automatically a cancellation acknowledgement. A synthesis completion is not a playback completion. A playback completion is not proof of human hearing.

## Ordering and duplicate rules

Consumers must tolerate and visibly account for:

- duplicate events;
- late events;
- gaps in producer sequence;
- unknown future event types;
- reconnects with new producer instance IDs;
- results from stale `binding_epoch` values.

Reducers use producer sequence for local ordering and domain correlation for cross-producer transactions. Cross-clock latency is emitted only when clock comparability permits it; otherwise it is marked unavailable or estimated with uncertainty.

## Event families required

### Speech input

Preserve existing stream, frame, VAD, turn, transcript and committed-turn events under the canonical envelope.

### Agent and shadow binding

- shadow connect requested/accepted/rejected;
- shadow connected/disconnected/lost;
- agent run started/completed/failed;
- assistant message started/delta/completed/classified;
- native tool lifecycle mirrored for observability only;
- steer submitted/accepted/rejected and Pi queue-state reflection.

### Egress and player

- utterance accepted/rejected/queued/superseded;
- synthesis started/first PCM/chunk/completed/cancelled/failed;
- playback accepted/started/cursor/ducked/fade-planned/stopped/flushed/completed/failed;
- bounded-queue and backpressure events.

### Interruption and alignment

- interruption pre-armed/confirmed;
- stop transaction legs and outcomes;
- provisional audible-prefix classification;
- BFA requested/completed/failed/timed-out;
- history committed/refined/rejected-as-stale.

## Compatibility gate

Before replacing existing event production, fixtures must prove representative current speech-in, speech-out, Talker and barge JSONL can be translated or decoded without silently losing lifecycle state.
