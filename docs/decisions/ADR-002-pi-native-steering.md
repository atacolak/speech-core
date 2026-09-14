# ADR-002 — Talker Pi-profile tools with hybrid exact-routing gateway

**status:** proposed — pending exact Founder ratification
**ratified by:** none
**implementation:** not implied by this record

## Decision

Talker is a Pi profile. It may answer simple operator questions itself and may use profile tools to work with live Herdr-exposed profiles. Tools expose at least:

- discovery of addressable profiles/sessions;
- one-shot ask of a chosen target without changing focus;
- persistent switch/focus onto a chosen target;
- follow-up / steer into in-flight work on a bound target;
- status inspection for targets, work, and subscriptions;
- cancellation of work the operator has authority to stop.

Speech Core uses a **hybrid** controller/gateway path, not pure direct-steer as the complete product law and not a universal second reasoner queue:

- the **hybrid gateway** owns exact durable target identity, work IDs, subscription IDs, routing decisions, lifecycle correlation, target-loss handling, and asynchronous delivery facts;
- each **target profile** retains native authority over its own history, reasoning, tool execution, prompt/steer acceptance, and run lifecycle;
- **Talker** owns conversational intent, Pi-profile tool use, spoken-thread adaptation, and whether/when to ask, switch, follow up, or cancel.

Speech Core does not invent a competing steering queue inside a target runtime, silently retarget, fabricate reasoner state, or mutate a profile's conversational history outside that profile's designated owner. Idempotency correlates operator turn, work/subscription identity, target binding epoch, and gateway delivery attempt.

If the exact durable target is lost before or during delivery, the gateway fails loudly. It does not substitute another session by label similarity or invent a replacement.

## Consequences

- Direct native prompt/steer to one session may remain a supported path when the gateway selects that exact target; it is not the universal product assumption.
- Gateway and runtime responsibilities stay named separately: work/subscription identity and async delivery vs native run/tool/history authority.
- Native reasoner acceptance, rejection, and lifecycle events remain required evidence where the selected runtime supports them.
- Tool work on a target is not destructively cancelled merely to deliver a follow-up; cancellation is an explicit operator (or authorized policy) act.
- This record grants no runtime implementation authority until exact Founder ratification of this diff.

## Plain-language consequence

Talker becomes a spoken operator front that can chat lightly and drive real profile tools, while a hybrid gateway keeps exact routing and async work honest. Target agents keep their own brains and history. Nothing guesses a new session when the real one disappears.

## Reversal cost

Low while unratified and unimplemented. Moderate after gateway work/subscription IDs and routing epochs become public contracts. High if clients hard-depend on either pure direct-steer or a fully opaque controller without native target authority.

## Current implementation status

- OBSERVED: repository ADRs previously stated direct native Pi steer and single sticky shadow as accepted direction; those assumptions are superseded here as proposed product law pending ratification.
- OBSERVED: current Talker scripts accept an optional exact Pi session and do not yet implement the full hybrid gateway tool surface (`lab/scripts/speech_talker_session.py`, parked).
- INFERRED: Herdr-exposed live profiles are the intended addressable set once gateway discovery is authorized; exact discovery API is not fixed by this record.
- UNKNOWN: final wire schema for work/subscription IDs and tool payloads.

## Ownership

| Authority | Owns |
|-----------|------|
| Talker (Pi profile) | conversational intent, tool choice, spoken thread, simple direct answers |
| Hybrid gateway | exact target identity, work/subscription IDs, routing, async delivery, target-loss |
| Target profile | native history, reasoning, tools, prompt/steer/run authority |
| Speech scheduling | presentation order only (see ADR-003); not routing truth |
