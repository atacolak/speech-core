# tasks.md — spec-validator

Downstream implementation; spawn as a bead only on the operator's word.

- [x] Implement `scripts/check-spec-contract.py` enforcing guarantees 1–4 from
      `proposal.md` (headers parsed from the chapter status table + `### <id> —`
      requirement headers in chapters and deltas)
- [x] Include a self-test sanity fixture or at minimum: run against today's
      `spec/` and report zero findings
- [x] Report findings in plain human language with file paths (the reader is
      the operator, not a CI parser)
- [x] Tranche 2: manifest presence, digest recomputation, archive transaction
      completion, lens-freshness flags (see `proposal.md` §Tranche 2)
- [x] Steward: verify all guarantees against the real tree, then archive
      this change — archiving this specific change is ALSO the dogfood archive
      under the manifest rules (eating our own cooking)
