# 04 — Talker shadow session

**status:** accepted target specification; implementation partial
**authority:** subordinate to `CHARTER.md`; not proof of implementation

## Product behavior

Talker normally shadows one exact Pi agent session until explicit disconnect. It is a low-latency spoken/operator front layer, not a substitute reasoner.

## Direct Pi routing

When `transcript_committed` arrives:

```text
shadow target idle    -> Pi prompt(message)
shadow target running -> Pi steer(message)
```

Speech-core sends native Pi RPC/SDK commands directly. Pi owns steer queuing and safe delivery. Speech-core does not implement a second steering queue or wait for a separately invented safe boundary.

The system records submission, Pi acceptance/rejection and reflected Pi queue/run events. A TUI or Discord status is a projection of Pi state, not a competing scheduler.

## Intermediate assistant speech

The speakable source is ordinary explicit assistant text emitted during agent work.

Example:

```text
assistant: "I'll check the active service first."
tool call and result
assistant: "That route failed, so I'm checking the installed unit directly."
tool call and result
assistant final: "The service is running, but the audio device is inaccessible..."
```

Rules:

- no progress-emission tool;
- no automatic speech derived from tool lifecycle events;
- no tool arguments/results converted to speech;
- no hidden thinking/reasoning blocks spoken;
- explicit user-directed assistant text may be segmented and spoken immediately;
- prompts encourage a brief natural opening before likely long/tool-heavy work, but infrastructure does not fabricate one.

## Message classification

At stream time, the system may not yet know whether an assistant message will be intermediate or final. Use a causally honest lifecycle:

```text
assistant_message_started
assistant_text_delta
assistant_speakable_segment_committed
assistant_message_completed
assistant_message_classified(intermediate|final)
```

Speech may begin before classification. A message followed by tool use or another model turn is classified `intermediate`; the terminal assistant message at agent completion is `final`.

The shadowed Pi session retains both intermediate and final assistant messages in normal history. The speech ledger separately records which portions were synthesized, submitted, estimated audible or interrupted.

## Binding state machine

```text
DISCONNECTED -> CONNECTING -> CONNECTED
CONNECTED -> DISCONNECTING -> DISCONNECTED
CONNECTED -> LOST
LOST -> CONNECTING only for an explicit/exact reattachment attempt
LOST -> DISCONNECTED on explicit disconnect/reset
```

### Disconnected mode

Expose only concise discovery/connection instructions and the minimum tools needed to select an exact agent/session. Never invent or silently choose a target.

### Connected mode

Project a terse prompt:

```text
You are shadowing agent <agent-id>, session <session-id>. User turns are
routed to that exact session. Do not switch or fall back. Disconnect only
when explicitly requested.
```

Remove discovery/connect tools from the active Pi tool set. Retain only a short `disconnect_shadow` control if model-mediated disconnect is required. Ordinary forwarding is infrastructure and is not a model tool.

Pi supports changing active tools and rebuilding the effective system prompt for the next turn. Runtime binding state remains authoritative; prompt text is only its projection.

### Lost mode

If the exact target dies or becomes unavailable:

- Talker remains alive;
- new user messages are not answered independently;
- no similarly named or fresh session is selected;
- Discord, speech and TUI state clearly report target loss;
- reattachment may target only the same durable identity unless the operator explicitly selects a new one.

Suggested user message:

> The agent session I was shadowing is no longer available. I have not switched to another session.

## Binding record

At minimum record:

```text
shadow_agent_id
shadow_session_id
shadow_session_path or durable locator
shadow_process_instance_id
binding_epoch
connected_at_mono_ns
last_confirmed_alive_mono_ns
```

Events from stale binding epochs cannot affect the current connection.

## Initial implementation boundary

Prefer a persistent Pi RPC/SDK adapter over parsing human CLI stdout. Pin the supported Pi protocol/version and translate native Pi events into speech-core events. Plain-text compatibility may exist for diagnostics but is not the critical path.

## Acceptance essentials

- direct native steer during an active run;
- no destructive cancellation merely to deliver steer;
- first explicit assistant text can reach synthesis before tool completion;
- correct intermediate/final classification;
- dynamic prompt/tool shrink after connection;
- fail-loud exact-target loss;
- no silent fallback;
- complete Discord connect/work/steer/final/disconnect scenario.
