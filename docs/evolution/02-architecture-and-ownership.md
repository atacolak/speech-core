# 02 — Architecture and ownership

**status:** accepted target specification; implementation partial
**authority:** subordinate to `CHARTER.md`; not proof of implementation

## Dependency direction

```text
speech-core-protocol            speech-core-events
  ingress audio/control              shared event envelope
          |                                  |
          v                                  v
speech-core-daemon       talker-runtime / shadow binding
          |                         |
          +-----------+-------------+
                      v
             speech-session-control
               |       |        |
               v       v        v
        speech-egress  player   BFA results
               |        ^
               v        |
            CosyVoice --+ progressive PCM

Discord adapter and local operator adapters connect to session control.
Shared console/reducer consumes events only.
```

## Authoritative owners

### `speech-core-daemon`

Owns user-audio ingestion, ASR/VAD/turn state and `transcript_committed`. It does not own assistant playback or agent steering policy.

### `speech-core-events`

Owns the canonical event envelope, common identities, ordering, clock provenance and terminal conventions. It has no dependency on runtime implementations or UI.

### Talker runtime adapter

Owns the persistent Pi RPC/SDK connection, translation of Pi events, exact shadow binding, and dynamic prompt/tool projection. It does not own playback, BFA or final history classification.

### `speech-session-control`

Owns:

- conversation and run coordination;
- idle prompt versus active-run steer routing;
- response/message/segment/utterance mapping;
- egress priority and supersession policy;
- interruption transaction orchestration;
- cancellation fan-out;
- provisional and refined heard-prefix state;
- conversation-history commit and repair;
- shadow-target loss policy.

This is the only component allowed to mutate conversational history.

### `speech-egress-protocol`

Owns speak/cancel commands, progressive PCM framing, flow control and terminal outcomes. Egress and player depend on this protocol rather than on one another's implementations.

### `speech-egress`

Owns utterance queueing, CosyVoice lifecycle, synthesis cancellation, PCM emission and backend provenance. It has no audio-device ownership.

### `speech-player`

Owns the audio device, ordered playback, buffering, cursor/uncertainty estimates, duck/fade/stop/flush and playback terminal outcomes. It has no TTS-provider knowledge.

### `speech-bfa`

Consumes immutable text/audio identities and player stop evidence, then emits asynchronous alignment classifications. It is never on the physical stop critical path.

### Shared console/reducer

Consumes canonical events and produces state for local TUI and diagnostics. Producer packages never depend on it.

### Discord adapter

Owns Discord voice/text transport and channel lifecycle. It is a first-class adapter but not the authority for conversation, agent, TTS or playback state.

## Runtime cycles prohibited

- Talker does not ask player/BFA what should enter history.
- Egress and player communicate through a shared protocol, not mutual package imports.
- Egress does not ask Talker to invent progress speech.
- Player and BFA do not steer the agent.
- Playback stop does not await BFA.
- Runtime behavior does not depend on a TUI being present.
- Discord transport state does not silently substitute agent-session state.

## Migration structure

The existing `speech-out` command may temporarily act as a compatibility facade while new egress/player components are introduced. The facade has explicit removal criteria and does not impose Supertonic/WAV semantics on new contracts.

## Package extraction rule

Create the new seams alongside the replacement behavior. Do not spend a long preliminary phase polishing the old Supertonic monolith. A refactoring slice must preserve behavior; a behavior slice must use already accepted seams.
