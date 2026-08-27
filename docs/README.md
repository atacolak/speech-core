# Speech Core documentation

[`../rig.toml`](../rig.toml) maps this repository's product surfaces and city work identity.

## Canonical product surfaces

| Question | Canonical home |
|---|---|
| How does every mechanism behave, and why? | [`../spec/`](../spec/README.md) — accepted truth |
| What is Speech Core allowed to be? | [`../CHARTER.md`](../CHARTER.md) |
| What is implemented now? | [`current-state.md`](current-state.md) plus repository inspection |
| Where are current component boundaries? | [`seams.md`](seams.md) |
| What product architecture has been selected? | [`decisions/`](decisions/) |
| What target and sequence are active? | [`evolution/ACTIVE.md`](evolution/ACTIVE.md) |
| What work is authorized and who owns it? | the city `sc` work ledger |
| What proves a completed claim? | linked commits, tests, traces, and review evidence when required by the work item |

If sources conflict, behavior/why claims resolve toward `../spec/` (then the
charter); this docs tree is reference and evidence material, not a competing
claim home. Identify the claim type and inspect its canonical authority. Do not
blend values.

## Current implementation

- [`current-state.md`](current-state.md) — live topology, installed defaults, limits, and verification commands. **live mouth is qwentts, not CosyVoice.** isolated CosyVoice3 RL playground: `~/workspace/cosyvoice_playground`.
- [`qualification/qwentts-sc-o5i.md`](qualification/qwentts-sc-o5i.md) — live cutover evidence.
- [`qualification/qwentts-stream-leftover.md`](qualification/qwentts-stream-leftover.md) — pcm-out streams; text-in is one-shot.
- [`seams.md`](seams.md) — current component contracts.
- [`turn-detection.md`](turn-detection.md) — VAD, smart-turn, close policy, diagnostics, and tuning.
- [`smart-turn-v3.md`](smart-turn-v3.md) — model artifact, preprocessing, runtime behavior, and verification.
- [`speech-output.md`](speech-output.md) — **stale** Supertonic/WAV essay. not the live pin.

## Selected product architecture

- [`decisions/`](decisions/) — selected Speech Core directions; acceptance is not implementation.
- [`evolution/README.md`](evolution/README.md) — target-document semantics and reading order.
- [`evolution/ACTIVE.md`](evolution/ACTIVE.md) — concise target, gap, and sequence.
- numbered evolution documents — detailed Speech Core target contracts and gates.

## Evaluation and focused tracks

- [`golden-suite-spec.md`](golden-suite-spec.md) — golden capture, assertion, and release specification.
- [`../scripts/README-golden-assert.md`](../scripts/README-golden-assert.md) — assertion tool usage.
- [`barge-in-dual-asr.md`](barge-in-dual-asr.md) — dual-Nemotron implementation track.
- [`assistant-self-asr-eval.md`](assistant-self-asr-eval.md) — evaluation-only assistant self-ASR track.

## Platform support

- [`../laptop-audio/README.md`](../laptop-audio/README.md) — laptop audio hygiene, AEC, and denoising outside the core runtime.
