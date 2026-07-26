# ADR-001 — CosyVoice-only replacement production path

**status:** accepted direction; exact runtime pin pending qualification
**ratified by:** operator in charter workshop, 2026-07-26
**implementation:** not implied by this record

## Decision

The new production speech-out contract is designed for pinned CosyVoice progressive PCM. Supertonic is not a supported backend, fallback, compatibility constraint or required benchmark for the replacement architecture.

Deterministic fake PCM sources are retained exclusively for tests. The currently deployed Supertonic service remains untouched as rollback until candidate cutover is approved; this operational preservation does not shape the new API.

## Consequences

- No public provider-plugin framework is required.
- Model/runtime/voice/config hashes appear in provenance events.
- Missing or unhealthy CosyVoice fails closed with a typed terminal outcome.
- After accepted cutover, remove Supertonic production configuration and documentation in explicit slices.
