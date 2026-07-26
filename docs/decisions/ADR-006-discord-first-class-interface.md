# ADR-006 — Discord is a first-class operator interface

**status:** accepted direction
**ratified by:** operator in charter workshop, 2026-07-26
**implementation:** not implied by this record

## Decision

Discord is a primary current and near-term interface. It is included in product acceptance rather than deferred to optional nightly testing.

The core remains transport-neutral, with Discord implemented as an adapter to session control. Discord does not own agent, TTS, player or history authority.

## Consequences

- End-to-end acceptance includes exact-session connect/disconnect, intermediate speech, native steer, final speech, barge, loss notification and reconnect.
- Discord-specific latency and buffering are measured separately.
- Local deterministic and physical tests remain mandatory because Discord alone cannot isolate component failures.
