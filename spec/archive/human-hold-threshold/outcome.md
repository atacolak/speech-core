# Outcome — human-hold-threshold

**Shipped 2026-08-04.** First change ever to complete the full loop:
sentence → accepted intent → managed delivery → independent verification →
integration → steward merge.

## The voyage

| Leg | Artifact | Result |
|---|---|---|
| Ruling | operator sentence, 2026-08-04 ("7500ms… the final fallback, measured by no words coming into the system") | logged `spec/decisions.md` #3 |
| Accepted intent | this folder's proposal + delta | chapter A untouched while code was false to it |
| Implementation | bead `sc-rfi`, worker `rf__worker-otv-e8qb2`, commit `a8886ef` | `turn.rs` default 12000→7500 + regression test; one file, scope-clean |
| Independent verification | bead `sc-9li`, verifier `rf__verifier-otv-7rhyn` | **PASS** — distinct session, re-ran tests, re-classified all `12000` literals, zero mutations |
| Integration | bead `sc-p5s`, fast-forward `752906d → a8886ef` | branch of record `spec/recon-001` converged; typed closure `closure:sc-p5s` |
| Steward Road-1 | this office | diff re-read by hand, regression test re-run (`1 passed`), literal classification reproduced — all delta requirements matched |

## Process findings (for the town, not the spec)

- The *integration* leg instantiated the full managed-delivery formula around
  a one-command merge: 15 beads minted the same day, including duplicated
  review/close cards under mid-flight loop repair. Substance sound; ceremony
  heavy. Selection-rule tuning is downstream work.
- Verifier correctly noted stewardship boundaries unprompted ("accepted-spec
  merge/archive… pending steward merge").
- Orphan driftwood: `sc-c47` (input convoy) remained OPEN after the family
  closed — ledger hygiene note, no behavioral effect.

## Verification evidence

- `turn.rs:52` — `human_hold_silence_ms: 7500`
- `turn.rs:2672` — `turn_manager_config_default_human_hold_silence_ms_is_7500`
- `main.rs:293-299` CLI default 7500, wired `main.rs:443`
- Remaining `12000` literals: sample/timeline fixtures + historical text only
