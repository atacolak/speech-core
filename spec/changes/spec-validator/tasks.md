# tasks.md — spec-validator

Downstream implementation; spawn as a bead only on the operator's word.

- [ ] Implement `scripts/check-spec-contract.py` enforcing guarantees 1–4 from
      `proposal.md` (headers parsed from the chapter status table + `### <id> —`
      requirement headers in chapters and deltas)
- [ ] Include a self-test sanity fixture or at minimum: run against today's
      `spec/` and report zero findings
- [ ] Report findings in plain human language with file paths (the reader is
      the operator, not a CI parser)
- [ ] Steward: verify the four guarantees against the real tree, then archive
      this change
