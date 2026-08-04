# Proposal — spec-validator (tooling-class change)

| | |
|---|---|
| **Status** | PROPOSED — guarantee list ratified 2026-08-04 (decisions.md); implementation is execution-side |
| **Class** | tooling — validates the spec area itself; changes no product behavior |
| **Manifest** | `change.toml` in this folder (first dogfood of the transaction spine) |

## Why

The loop's four guarantees are currently hand-applied by the steward. A
36-hour-old system already produced one self-contradiction they would have
caught (PROPOSED chapters inside `specs/`). Hand-application scales badly;
mechanical checks don't.

## Guarantees (the whole commission)

1. **Placement** — every file in `spec/specs/` carries status ACCEPTED in its
   header table; vice versa, no file with status PROPOSED exists under
   `spec/specs/`.
2. **Delta targets resolve** — in `spec/changes/*/specs/**`, every MODIFIED /
   REMOVED / RENAMED requirement header matches a live requirement in the
   target accepted chapter; every ADDED requirement ID is new there.
3. **Archive completeness** — a folder in `spec/archive/` is dated
   `YYYY-MM-DD-<name>`, contains proposal.md with status *shipped*, a tasks.md
   with no unchecked boxes, and an outcome.md.
4. **No leaks** — no requirement ID introduced by an unarchived change appears
   in `spec/specs/` (deltas apply only via steward merge).

## Tranche 2 (transaction-spine checks, adopted 2026-08-04)

5. **Manifest presence** — every accepted change folder carries a valid
   `change.toml` (`ata.spec-change/v1`) with required fields.
6. **Digest current** — recomputing the intent digest from the folder's
   semantic input yields `change.toml`'s recorded `intent_digest`.
7. **Archive transaction completion** — an archived change's `change.toml`
   carries `implementation_commit`, and the commit exists.
8. **Lens freshness** — each `spec/flows/` lens records which chapters it
   cites; a lens citing a chapter whose revision moved after the lens'
   recorded check date is flagged `stale` (report only, not a failure).

Non-goals: lint prose style, parse WHEN/THEN bodies, judge content quality.
That judgment stays human/steward; the validator guards *placement and
closure*, not taste.

## Suggested shape

Single script `scripts/check-spec-contract.py` (sibling of
`scripts/check-doc-contract.py`), exit 0/1 with human-readable findings.
Run by hand today; a CI/order hook is a later town decision.
