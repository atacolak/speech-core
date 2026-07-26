# Speech Core documentation map

Start at the repository [`README.md`](../README.md). This page is the stable map for humans and agents who need more detail.

## Canonical homes

| Question | Canonical home |
|---|---|
| What is Speech Core and what may never silently change? | [`CHARTER.md`](../CHARTER.md) |
| What should I read first and how do I run it? | [`README.md`](../README.md) |
| What is implemented and usable now? | [`current-state.md`](current-state.md) plus repository inspection |
| Where are current component boundaries? | [`seams.md`](seams.md) |
| What architecture has the operator selected? | [`decisions/`](decisions/) |
| What target are we building and in what order? | [`evolution/ACTIVE.md`](evolution/ACTIVE.md) |
| What work is authorized and who owns it? | the city work ledger; never a planning document |
| What evidence proves a work item? | its commits, tests, traces, and independent review verdict |
| How do managed roles behave? | [`../governance/ROLES.md`](../governance/ROLES.md) |
| How will repository memory work? | [`memory/hindsight-v1.md`](memory/hindsight-v1.md) |

If two sources appear to conflict, do not blend them. Identify the claim type, inspect its canonical source, and surface the conflict.

## Current runtime

- [`current-state.md`](current-state.md) — live topology, installed defaults, honest limits, and verification commands.
- [`seams.md`](seams.md) — current component contracts and boundaries.
- [`turn-detection.md`](turn-detection.md) — VAD, smart-turn, close policy, diagnostics, and tuning.
- [`smart-turn-v3.md`](smart-turn-v3.md) — model artifact, preprocessing, runtime behavior, and verification.
- [`speech-output.md`](speech-output.md) — current speech-out protocol, playback, and cancellation behavior.

Defaults that affect behavior originate in code and installed configuration. Documentation explains them; tests should detect drift.

## Target architecture and decisions

- [`decisions/`](decisions/) — operator-accepted directions. Acceptance is not implementation.
- [`evolution/README.md`](evolution/README.md) — target-document semantics and reading order.
- [`evolution/ACTIVE.md`](evolution/ACTIVE.md) — one concise target/status/sequence summary.
- numbered evolution specifications — detailed target contracts and gates.

There is no second charter under `docs/evolution/`, and no proposed bead graph in the repository. The rig's city-managed `.beads/` ledger contains only authorized work; it is not a second planning or memory system.

## Evaluation and focused tracks

- [`golden-suite-spec.md`](golden-suite-spec.md) — golden capture, assertion, and release specification.
- [`../scripts/README-golden-assert.md`](../scripts/README-golden-assert.md) — assertion tool usage.
- [`barge-in-dual-asr.md`](barge-in-dual-asr.md) — dual-Nemotron implementation track.
- [`assistant-self-asr-eval.md`](assistant-self-asr-eval.md) — evaluation-only assistant self-ASR track.

Focused-track documents describe their own scope and must not be treated as whole-product current state.

## Platform support

- [`../laptop-audio/README.md`](../laptop-audio/README.md) — laptop audio hygiene, AEC, and denoising tools outside the core runtime.

## Cold-entry reading order

A new managed agent reads:

1. [`../README.md`](../README.md);
2. [`../CHARTER.md`](../CHARTER.md);
3. [`../governance/ROLES.md`](../governance/ROLES.md);
4. its city work item;
5. [`current-state.md`](current-state.md) and relevant code/tests;
6. applicable decision records and target specifications;
7. bounded project-memory orientation, when healthy.

A worker does not need to read the entire repository before starting one bounded work item. The manager supplies the relevant subset and the worker verifies current claims against live sources.
