# Delta — specs/speech-in-turn-lifecycle

## MODIFIED Requirements

### turn.6 — close sources and the degraded flag
The human-hold close SHALL use a threshold of **7500 ms of speech-like audio
without committed tokens**, single-sourced across all configuration surfaces.

#### Scenario: one value, one home
WHEN any code path constructs the turn-manager configuration — CLI defaults,
`TurnManagerConfig::default()`, or a test fixture — THEN the human-hold
threshold is 7500 ms or an explicit override, and no surface carries a
conflicting literal.

#### Scenario: the final fallback
WHEN speech-like audio (voice or ambient noise) persists for 7500 ms while the
model commits zero tokens — i.e. detection evidence never matures into words —
THEN the turn closes as `human_hold`, committing possibly-empty text in the
guaranteed close order (turn.3).

## Open questions — REMOVED
- ~~human-hold default conflict~~ — ruled 7500 ms by the operator 2026-08-04;
  rationale and revisit-trigger in `spec/decisions.md`.
