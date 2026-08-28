# Speech Core documentation

[`../rig.toml`](../rig.toml) maps this repository's product surfaces and city work identity.

## Canonical product surfaces

| Question | Canonical home |
|---|---|
| How does every mechanism behave, and why? | [`../spec/`](../spec/README.md) — accepted truth |
| What is Speech Core allowed to be? | [`../CHARTER.md`](../CHARTER.md) |
| What is implemented now? | [`current-state.md`](current-state.md) plus repository inspection |
| Where are current component boundaries? | [`seams.md`](seams.md) |
| What product architecture has been selected? | [`decisions/`](decisions/) — several ADRs are **direction archive**, not live |
| What 2026-07 target still sits on disk? | [`evolution/ACTIVE.md`](evolution/ACTIVE.md) — **superseded as live law**; qwentts is the mouth |
| What work is authorized and who owns it? | the city `sc` work ledger |
| What proves a completed claim? | linked commits, tests, traces, and review evidence when required by the work item |

If sources conflict, behavior/why claims resolve toward `../spec/` (then the
charter); this docs tree is reference and evidence material, not a competing
claim home. Identify the claim type and inspect its canonical authority. Do not
blend values.

## Current implementation

- [`current-state.md`](current-state.md) — live topology, installed defaults, limits, and verification commands. **live mouth is qwentts, not CosyVoice.** live call is sibling voicecat, not `speech-out-live-session`.
- [`qualification/qwentts-sc-o5i.md`](qualification/qwentts-sc-o5i.md) — live cutover evidence.
- [`qualification/qwentts-stream-leftover.md`](qualification/qwentts-stream-leftover.md) — pcm-out streams; leftover append is the voicecat hop.
- [`seams.md`](seams.md) — current component contracts.
- [`turn-detection.md`](turn-detection.md) — VAD, smart-turn, close policy, diagnostics, and tuning.
- [`smart-turn-v3.md`](smart-turn-v3.md) — model artifact, preprocessing, runtime behavior, and verification.
- [`speech-output.md`](speech-output.md) — **stale** Supertonic/WAV essay. not the live pin.

## Selected product architecture

- [`decisions/`](decisions/) — selected Speech Core directions; acceptance is not implementation. ADR-001 still talks CosyVoice/Supertonic as 2026-07 qualification policy; live pin is qwentts (2026-08-18).
- [`evolution/README.md`](evolution/README.md) — target-document semantics. treat the numbered files as archive until a speech-out spec change is opened.
- [`evolution/ACTIVE.md`](evolution/ACTIVE.md) — 2026-07 delivery sequence. **not the live map.**

## Parked tracks

- [`../lab/README.md`](../lab/README.md) — dual-Nemotron, CUPE/BFA, CosyVoice qual, talker-session, laptop-audio.
- [`golden-suite-spec.md`](golden-suite-spec.md) — golden capture/assertion spec (synthetic; not the live mic).
- [`../scripts/README-golden-assert.md`](../scripts/README-golden-assert.md) — assertion tool usage.
