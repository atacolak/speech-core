# ADR-002 — Pi-native steering

**status:** accepted direction
**ratified by:** operator in charter workshop, 2026-07-26
**implementation:** not implied by this record

## Decision

When a committed user turn arrives while the exact shadowed Pi session is active, speech-core sends the native Pi `steer` command directly. Pi owns queueing and delivery to the agent loop. Speech-core does not implement another steering queue or wait for an invented safe boundary.

When the target is idle, speech-core sends a normal Pi prompt. If the run completes before routing, session control starts a new run. If the exact target is lost, delivery fails loudly.

## Consequences

- Native Pi acceptance/rejection and queue/run events are mirrored for observability.
- Tool work is not destructively cancelled merely to steer.
- Idempotency correlates `turn_id`, `agent_run_id`, `steer_id` and `binding_epoch`.
