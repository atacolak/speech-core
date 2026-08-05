# Outcome — spec-validator

**Shipped 2026-08-05.** Validator `scripts/check-spec-contract.py`
(guarantees 1–8) + 13 focused tests live on `feature/assistant-self-asr`.

## Transaction trail

| Act | Bead | Commit / verdict |
|---|---|---|
| Dispatch | sc-cq9 root (+ decompose sc-3np/sc-915) | binding byte-exact to this manifest |
| Implement (attempt 1) | sc-o6j | `9d78b2a` |
| Review (attempt 1) | sc-6yu | **FAIL** — G2 global ID resolution (cross-chapter probe slipped), G7 no focused tests |
| Remediate | sc-p6d | `285cc1c` — chapter-scoped G2 targets + G7 fixtures |
| Review (attempt 2) | sc-qm1 | **PASS** — `verification:sc-qm1-g2-g7-remediation-review` |
| Steward convergence | — | this archive; guarantees 1–8 re-run live, 13 tests green |

The review machinery proved itself: an independent reviewer caught real
under-enforcement, drove bounded remediation, and re-verified. The root
closed the round transacted, not trusted.

## Dogfood note

This archive is itself the second manifest-rules dogfood: `change.toml`
carries `implementation_commit`, review receipts, and digest stayed stable
(8 semantic files throughout — no intent drift during remediation).

## Carried advisories (steward notes for future tranches)

- Multi-line `FROM`/`TO` RENAMED bullet pairs not parsed (pre-existing gap).
- G7 positive-path test is ROOT-sensitive under fixtures (manual probe OK).
- G4 no-leak largely redundant with G2 ADDED-exists; revisit if grammar changes.

## Fingerprints

- candidate: `285cc1c95a4a56325d13e566821508b0fc07a92a`
- reviews: `verification:sc-qm1-g2-g7-remediation-review` (PASS),
  `verification:sc-6yu-spec-validator-impl-review` (FAIL, remediated)
- digest: `sha256:e96dece…689e17` (recomputed match by reviewer)
