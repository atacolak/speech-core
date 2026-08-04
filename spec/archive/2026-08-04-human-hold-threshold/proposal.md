# Proposal — human-hold-threshold

| | |
|---|---|
| **Status** | **SHIPPED 2026-08-04** — implemented (`sc-rfi` → `a8886ef`), independently verified (`sc-9li` PASS), integrated (`sc-p5s`), steward-merged; see `outcome.md` |
| **Touches** | `specs/speech-in-turn-lifecycle.md` (open question), daemon config default |

## Why

Chapter A surfaced a real conflict: the human-hold close threshold was 7500 ms
via the daemon CLI default but 12000 ms in `TurnManagerConfig::default()`. Two
values existed; no intent was recorded.

## Ruling (operator, 2026-08-04)

**7500 ms.** Reasoning, in the operator's words: "12 seconds might be too long.
the 7.5sec fallback is optimal in cases where our voice detection cannot fall
under a certain detection threshold, either due to human making noise or
external noise. it is sort of like the final fallback, measured by no words
coming into the system."

## What changes

- The human-hold threshold is **7500 ms by intent**, single-sourced.
- Code must converge: `TurnManagerConfig::default()` (currently 12000) aligns to
  7500, or the duplicate default is removed so one home defines the value.
- Chapter A's open question is cleared; the settled value is stated in
  `turn.6` close sources.

## Impact

- Behavior: none change at runtime (the daemon already runs 7500 via CLI).
  The stray 12000 default was latent only, but dangerous precisely because it
  was latent — any test or consumer constructing `TurnManagerConfig::default()`
  would silently get 4.5 extra seconds of "listening to noise."
- Downstream work: a one-line-class code task (`tasks.md`), spawned only when
  the operator triggers it. The steward never writes code.
