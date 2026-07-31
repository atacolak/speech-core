# ADR-001 — Reversible voice-backend qualification hypothesis

**status:** ratified — reversible CosyVoice qualification policy
**ratified by:** Founder, 2026-07-31
**implementation:** not implied by this record

## Decision

The replacement speech-out contract remains backend-neutral. CosyVoice is the current preferred qualification candidate to test seriously, not an exclusive permanent backend and not an exclusive production commitment. Preference is reversible: another backend may be adopted later if evidence warrants it.

Original reasons for preferring CosyVoice are currently unknown and must be recovered from evidence rather than invented. Until recovered, they are not treated as established selection criteria.

Supertonic is not the intended future backend, but the deployed Supertonic path remains available as dogfood and rollback until a separately approved replacement passes accepted gates and a separate cutover decision is made. Its current behavior does not define the replacement wire contract.

Before any CosyVoice adoption decision, qualification must establish:

- progressive audio streaming behavior and first-PCM latency;
- end-to-end latency and real-time factor at useful settings;
- bounded cancellation and control behavior, including proof of worker cleanup;
- audio sample rate, channel, sample-range, timing, frequency and discontinuity observability;
- provenance for repository, model, runtime, voice/reference assets and configuration;
- coexistence with speech input, alignment and current services;
- failure isolation and demonstrated rollback.

Deterministic fake PCM sources remain available for contract tests. Qualification evidence may support a later adoption decision but does not itself authorize adoption, integration, cutover, service/config mutation, or reuse/closure of failed sc-e71.4 evidence. Failed sc-e71.4 evidence remains failed.

## Consequences

- Public speech-out commands and PCM/event contracts do not name or require one synthesis backend.
- A broad provider-plugin marketplace is not required; backend neutrality means the contract does not encode CosyVoice-only assumptions.
- Any qualified backend emits exact model/runtime/voice/config provenance and typed terminal outcomes.
- CosyVoice-specific code, if later authorized, remains behind the backend-neutral contract and must fail explicitly.
- Supertonic removal requires a later, explicit cutover and removal decision.

## Plain-language consequence

This record replaces “CosyVoice-only” product law with a testable, reversible qualification policy. Speech Core may investigate CosyVoice as the current preferred candidate without forcing protocol consumers to depend on it and without treating one qualification report as adoption. Current Supertonic remains recoverable until a later cutover decision. Unknown historical preference reasons must be recovered from evidence, not restated as fact.

## Reversal cost

Low before implementation: another exact ADR amendment. Moderate after a backend-neutral contract is published because changing public schemas would require compatibility work. High only after a separately approved production integration/cutover; rollback must therefore be proven before adoption.

## Current implementation status

- OBSERVED: current speech-out uses Supertonic HTTP, pragmatic text chunks, binary WAV chunks, playback flow control, and explicit cancellation (`docs/speech-output.md`; `crates/speech-out/src/main.rs`). It is not backend-neutral internally.
- OBSERVED: current Supertonic behavior buffers within each text chunk; it is not the target sample-addressed progressive PCM contract.
- OBSERVED: CosyVoice is not integrated. sc-e71.4 is blocked after verifier FAIL and cannot be treated as accepted qualification; that failed evidence remains failed.
- OBSERVED: current code emits some chunk byte counts, daemon monotonic timestamps, latency and cancellation events, but does not establish the complete sample/timing/frequency observability proposed here.
- OBSERVED: original reasons for CosyVoice preference are not established in this record and must be recovered separately.

## Evidence needed

A fresh, isolated qualification must provide exact code/model/dependency/voice hashes; GPU proof; cold/warm startup; first PCM; full latency and RTF distributions; emitted format/sample rate; sample-domain and frequency/discontinuity observability; cancel-before/after-first-PCM behavior; no post-cancel output; cleanup/VRAM recovery; concurrency/coexistence with ASR/alignment; failure isolation; legal/license assessment; and rollback proof. Evidence must have a changed fingerprint before any new review of sc-e71.4. Qualification success is not adoption, integration, or cutover authority.
