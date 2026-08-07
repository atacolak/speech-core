# Outcome — spec-map-parked-capabilities

**Shipped 2026-08-07.** Process-class map honesty: `spec/README.md` now carries
a **not yet accepted (parked / direction)** TOC so the speech-in flow pointer
to “speech-out parked on ADR-001 — see spec/README map” resolves to a real row.

## Transaction trail

| Act | Bead | Commit / verdict |
|---|---|---|
| Operator accept | phrase `alr lgtm!!` (msg `3db05d20`) | rev 2 freeze; digest `sha256:4579621a…d6db9` |
| Steward-accept mint | sc-9c9 product root | Policy B mint from execution-request |
| Implement | sc-cp0 (`rf__worker-otv-ychbt`) | `84acb81` — README-only +11 |
| Independent review | sc-po4 (`rf__verifier-otv-qok6g`) | **PASS** — distinct session |
| Graph close | sc-6ve (manager) | implementation-closed-steward-pending |
| Steward convergence | control `sc-scv-38d9a38358582b220483` | this archive + `receipt.md` (`converged`) |

## What landed (acceptance re-check)

- Parked/direction table sits after accepted TOC and before archive TOC. **OBSERVED** `spec/README.md:80-89` at `84acb81`.
- `speech-out` row → ADR-001 + `docs/evolution/` progressive-audio / interruption, status **parked**. **OBSERVED** exact proposal row.
- Other evolution drafts marked **direction only**, not `spec/specs/` truth. **OBSERVED**.
- Honesty paragraph under the table (navigational only; chapter via normal change loop). **OBSERVED**.
- Diff is README-only (`git diff f4a0ea4..84acb81 --name-only` → `spec/README.md`). **OBSERVED**.
- Flow pointer `spec/flows/speech-in.md:34-35` now resolves against the map. **OBSERVED**.
- No chapter opened, no ADR ratified, no runtime/charter/validator/flow edit. **OBSERVED**.
- Charter sweep: map honesty only; no invariant or prohibited-compromise engagement. **INFERRED** from diff scope.

## Fingerprints

- intent_digest (city freeze / packet / sc-9c9 binding): `sha256:4579621adeb675b938db58849f023e60f1b08257f9d2772e82f75f85e92d6db9`
- intent_revision: 2
- implement: `84acb81dbf4892db6f67b0c48ef7d843ca8acc5b`
- baseline parent: `f4a0ea4a9f53949c04954cccc3a104c16ac62da3`
- review: sc-po4 PASS
- control: `sc-scv-38d9a38358582b220483` (fingerprint `38d9a38358582b220483…`)

## Process finding (not a product defect)

Two digest procedures currently disagree on the same folder:

1. **City freeze** (`commands/execution-request digest` / `ata.execution-request` canonical meaning) hashes intent-normalized `change.toml` + `proposal.md` + optional `tasks.md`. This produced the bound digest `4579621a…` used by mint and sc-9c9.
2. **Local G6** (`scripts/check-spec-contract.py` / `spec/change-toml.md` intent_rev 1) hashes only `proposal.md` + `tasks.md` + `specs/**`, and recomputes `sha256:f8f6d08f97716f12dd8aee8ce46f2c4841f72a1acd0951a79869f4e2d502815f` for the same proposal/tasks bytes.

Proposal and tasks bytes are byte-identical to the freeze-time write (session `019fcff2-eff0-75b6-acb1-f57cc1b489e0`). The G6 “stale” finding on the untracked active package was therefore a **procedure mismatch**, not intent drift, and not introduced by implement commit `84acb81`. G6 applies to active `changes/` only; archiving removes the active-package check. Aligning the two procedures is town/tooling follow-up — not this change’s product scope.

## Steward verdict

`converged` — see `receipt.md` (`ata.spec-convergence/v1`).
