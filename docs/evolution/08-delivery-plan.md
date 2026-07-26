# 08 — Delivery plan

**status:** active sequencing guide; work requires city-ledger authorization
**authority:** subordinate to `CHARTER.md`; not proof of implementation

This is the evolution-plan chunking plan. City work items may refine names and scope, but must preserve these ownership and dependency constraints.

## Chunk A — Freeze and decisions

### A1 Baseline freeze

Capture commands, versions, service topology, representative traces, current test results and known baseline defects.

### A2 Canonical architecture decisions

Accept package DAG, session-control ownership, Pi-native steering, intermediate assistant speech semantics, sticky shadow binding, audibility truth model and Discord priority.

### A3 CosyVoice/environment qualification

Pin candidate repository/model/runtime/voice and produce GPU/VRAM/startup/cancellation feasibility evidence without touching current production services.

**Parallelism:** A1 and A3 can run in parallel. A2 consumes this planning packet and can overlap investigation but gates implementation contracts.

## Chunk B — Shared contracts and fixtures

### B1 Canonical event envelope

Implement shared identities, producer ordering, clocks, terminal outcomes and compatibility fixtures.

### B2 Progressive PCM wire contract

Implement speak/cancel/audio frame/flow-control schemas and serialization fixtures.

### B3 Agent/shadow protocol adapter contract

Pin Pi RPC/SDK protocol translation, message lifecycle, native steer and binding events.

### B4 Shared reducer skeleton

Reduce the accepted event fixtures without creating behavior-specific TUI polish.

**Parallelism:** B1 is the root. B2 and B3 proceed in parallel after envelope semantics stabilize. B4 follows representative fixtures from both.

## Chunk C — Deterministic test foundations

### C1 Fake Pi event source

Generate assistant/tool/steer/run sequences, including intermediate/final classification races.

### C2 Fake progressive PCM source

Generate sample-exact utterances, delays, gaps and cancellation races.

### C3 Fake player/sink

Provide deterministic buffer, cursor, stop, fade and flush behavior.

### C4 Conversation evaluator runner

Combine fixtures into replayable scenarios and machine-readable summaries.

**Parallelism:** C1, C2 and C3 are highly parallel after B contracts; C4 integrates them.

## Chunk D — Talker and shadow binding

### D1 Persistent Pi RPC/SDK adapter

Replace one-shot human stdout parsing in the candidate path.

### D2 Exact-session binding state machine

Implement disconnected/connecting/connected/lost/disconnecting with binding epochs.

### D3 Dynamic prompt and active-tool projection

Shrink connected mode to exact binding context and minimal disconnect control.

### D4 Direct prompt/steer routing

Use native Pi prompt while idle and native Pi steer while active; reflect Pi queue state.

### D5 Assistant text stream and classification

Translate explicit assistant deltas, commit speakable spans, and classify completed messages intermediate/final.

### D6 Fail-loud loss and reconnect

Reject silent fallback and permit only explicit exact-target reattachment.

**Parallelism:** D1 precedes most work. D2 and D5 can develop against C1 fixtures. D3 follows D2. D4 follows D1/D2. D6 follows D2/D4.

## Chunk E — Audio implementation

### E1 Targeted speech-out extraction

Extract accepted protocol seams without behavior change.

### E2 Internal player with fake sink

Implement bounded queue, sample ledger and cancellation against C3.

### E3 Real PipeWire player

Add real device ownership after fake-sink semantics pass and device ACLs are available.

### E4 CosyVoice worker spike

Prove pinned model load and progressive PCM under B2 without production cutover.

### E5 CosyVoice lifecycle and cancellation

Add bounded worker ownership, cancellation, cleanup and provenance.

### E6 Egress queue and backpressure

Serialize utterances, prioritize final over stale intermediate, and enforce bounds.

### E7 CosyVoice-to-player integration

Integrate only after E2/E5/E6 pass their isolated gates.

**Parallelism:** E1, E2 and E4 can run concurrently in isolated worktrees after B2. E3 follows E2 and environment access. E5 follows E4. E6 can use C2 before E4 completes. E7 is an integration join.

## Chunk F — Segmentation and session control

### F1 Speakable-unit segmenter

Punctuation/clause/timeout segmentation with immutable source spans.

### F2 Session-control skeleton

Map turn/run/response/message/segment/utterance identities and own history policy.

### F3 Intermediate/final scheduler

Serialize speech and supersede stale intermediate audio when final speech begins.

### F4 End-to-end first-speech path

Join D5, F1/F2/F3 and E6/E7.

**Parallelism:** F1 and F2 can proceed against contracts/fixtures while D/E run. F3 follows both. F4 is an integration join.

## Chunk G — Interruption and heard-prefix truth

### G1 Coordinated barge transaction

Correlate player, egress, Pi steer/new-run and provisional history legs.

### G2 PCM egress planner

Score natural stop candidates within a measured configurable deadline.

### G3 Provisional audible-prefix classifier

Intersect segment/audio mapping with player cursor estimate and uncertainty.

### G4 Persistent BFA service

Warm alignment service with typed classification, timeout and stale-result protection.

### G5 Monotonic history refinement

Apply at most one safe refinement and reject results from newer turns/bindings.

### G6 Integrated interruption semantics

Join physical stop, cancellation, direct Pi steering and history classification.

**Parallelism:** G2, G3 and G4 can run in parallel after E/F foundations. G1 requires player/egress/session control. G5 follows G3/G4. G6 joins all.

## Chunk H — Operator surfaces

### H1 Shared console model and panels

Add event-driven input, agent, output and performance lanes.

### H2 Discord shadow control

Connect/disconnect exact sessions and render fail-loud state.

### H3 Discord live speech path

Intermediate/final streaming, steer during active work, barge and reconnect.

### H4 Replay parity

Replay one canonical fixture through summaries, local TUI and Discord-state projection.

**Parallelism:** H1 can evolve alongside behaviors using shared reducer slices. H2 can begin after D2. H3 requires D/F/E. H4 follows stable event families.

## Chunk I — Quality and optimization

### I1 Shared pronunciation ledger

Only after synthesis and alignment consumers exist.

### I2 CUPE boundary hints

Only if evaluator evidence shows PCM-only egress needs them.

### I3 Stage-level latency campaign

Optimize measured stages independently with before/after evaluator reports.

### I4 Privacy, retention and endpoint hardening

Can start early as policy; final gates cover the integrated deployment.

### I5 Cutover, rollback and Supertonic removal

Candidate deployment, Discord dogfood, rollback rehearsal, operator approval, then explicit old-path removal.

## Expected parallel execution shape

After Chunk B stabilizes, four main tracks can run concurrently:

```text
Talker/shadow: D
Audio:         E
Control/text:  F
Evaluator/UI:  C + H1
```

They join at F4, then interruption track G and Discord integration H3. Environment qualification A3 and physical-player E3 remain external critical-path risks.

## Bead sizing rule

A bead should normally deliver one independently rejectable artifact such as:

- one schema and its fixtures;
- one state-machine transition family;
- one runtime adapter behavior;
- one cancellation guarantee;
- one reducer panel and replay assertion;
- one benchmark/gate.

Avoid beads named only `implement CosyVoice`, `rewrite speech-out`, `add steering`, or `build evaluator`. Split refactoring from behavior and implementation from independent verification.
