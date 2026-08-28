# Speech Core evolution

**status:** 2026-07 operator-selected *target*; **not live law**. live mouth is qwentts; live call is voicecat. see [`../current-state.md`](../current-state.md) and [`../../README.md`](../../README.md).
**current implementation truth:** [`../current-state.md`](../current-state.md) and repository evidence
**work authorization:** city work ledger, not this directory

This directory describes a selected target architecture from 2026-07 and the sequence that was meant to reach it. It is not a second charter, a current-state report, or a work ledger. CosyVoice / Discord-first / BFA in these files are **not** the installed pin.

## Reading order

1. [`ACTIVE.md`](ACTIVE.md) — concise status, dependencies, next delivery slices, and evidence links.
2. [`../decisions/`](../decisions/) — selected architectural decisions.
3. [`02-architecture-and-ownership.md`](02-architecture-and-ownership.md) — target component boundaries and authority.
4. [`03-event-and-identity-contract.md`](03-event-and-identity-contract.md) — target event, identity, clock, and terminal semantics.
5. [`04-talker-shadow-session.md`](04-talker-shadow-session.md) — Pi-native routing and exact-session shadowing.
6. [`05-progressive-audio-contract.md`](05-progressive-audio-contract.md) — progressive PCM, synthesis, playback, cancellation, and bounds.
7. [`06-interruption-and-history.md`](06-interruption-and-history.md) — interruption transaction and history policy.
8. [`07-evaluation-and-release-gates.md`](07-evaluation-and-release-gates.md) — evaluator tiers and acceptance gates.
9. [`08-delivery-plan.md`](08-delivery-plan.md) — dependency-ordered candidate slices.

## Status semantics

```text
draft       proposed; no decision authority
accepted    operator-selected design; not proof of implementation
partial     some accepted behavior exists; named gaps remain
implemented repository artifacts exist
verified    accepted evidence demonstrates the claim under stated conditions
superseded  replaced by a named later record
rejected    deliberately not selected
```

Every document in this directory describes a target unless it cites repository evidence for an implemented or verified claim. `ACTIVE.md` is the only evolution status summary; do not create parallel progress notes.

## Completion rule

A behavior is complete only when the same delivery slice provides:

1. an authoritative state transition and structured event;
2. stable identifiers, ordering, and clock/sample provenance;
3. an operator representation where relevant;
4. an evaluator assertion and replay or runtime evidence;
5. independent review linked from the city work item.

## Preservation rule

The deployed speech-in and speech-out paths remain available until a replacement candidate passes its accepted gates and rollback is demonstrated. Planning documents do not change services, profiles, or work state.
