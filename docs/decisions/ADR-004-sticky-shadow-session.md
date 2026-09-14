# ADR-004 — Multi-profile addressing with durable identity and explicit focus

**status:** proposed — pending exact Founder ratification
**ratified by:** none
**implementation:** not implied by this record

## Decision

Every **live Herdr-exposed profile** is potentially addressable by Talker, subject to that profile's own authority and any gateway authorization checks. Permanent universal stickiness to one exact Pi session is **not** product law.

**Durable target/session identity** is the routing authority. Workspace, window, pane, and profile **labels are presentation metadata** only. Routing and reattachment key on durable identity, never on label similarity.

Talker conversational **focus** and **subscriptions** are distinct:

- one profile may hold conversational focus (sticky for the spoken thread until explicit switch or disconnect);
- multiple active subscriptions may coexist so background work can still deliver explicit operator-facing turns (ADR-003);
- **one-shot ask does not switch focus**;
- **explicit switch/focus does persist** until a later explicit switch, disconnect, or authorized focus policy;
- switching focus does **not** automatically cancel background work or other subscriptions.

Connected, disconnected, switching, and lost are distinct operator-visible states projected from runtime route/binding state (not from prompt wording alone). After an explicit focus bind, discovery/connect affordances may shrink in the Talker profile projection; ordinary message routing remains gateway infrastructure rather than an unbounded free-form retarget tool.

If the durable target is lost:

- Talker remains alive;
- delivery and new routed turns for that binding fail loudly;
- Talker does not answer as a silent substitute reasoner for the lost target;
- no replacement is created and no similarly labeled session is attached;
- reattachment may target the same durable identity; selecting a different target requires explicit operator action (or an explicitly authorized routing decision recorded as such).

Route/binding epochs reject stale events and late results from prior bindings.

## Consequences

- Runtime route and subscription state, not prompt text, is authoritative.
- Operators can work across the real multi-profile topology without the system guessing.
- Single-target sticky focus remains a supported mode, not the only mode.
- Discord, speech, and TUI (or successors) should expose selected, switching, lost, and multi-subscription summary states when those surfaces are authorized; this record does not implement them.
- This record grants no runtime implementation authority until exact Founder ratification of this diff.

## Plain-language consequence

Talker can address any live profile it is allowed to see, keep one in the conversational foreground, and still listen for work it started elsewhere. Names on windows are labels, not identity. If the real session dies, Talker says so and waits — it does not quietly grab another.

## Reversal cost

Low while unratified. Moderate after route identity, subscription IDs, and epochs become protocol fields. High if clients hard-code either universal single-session stickiness or implicit dynamic retargeting.

## Current implementation status

- OBSERVED: prior ADR assumed one sticky exact shadow session until disconnect; that universal assumption is replaced here by multi-profile addressing with explicit focus.
- OBSERVED: current Talker accepts optional `--pi-session` and otherwise starts a local talker profile; it does not yet implement multi-subscription routing (`lab/scripts/speech_talker_session.py`, parked).
- INFERRED: Herdr is the live exposure surface for addressable profiles in the intended topology; non-Herdr targets are out of scope until separately authorized.
- UNKNOWN: exact authorization matrix for which Talker instances may address which profiles.

## Ownership

| Authority | Owns |
|-----------|------|
| Hybrid gateway | durable target identity, route/binding epoch, subscription records, loss detection |
| Talker | focus intent, explicit switch/ask choices, operator-visible explanation of loss |
| Target profile | whether it accepts work under its authority; its native session continuity |
| Presentation layers | labels (workspace/window/pane/name) only — never routing truth |
