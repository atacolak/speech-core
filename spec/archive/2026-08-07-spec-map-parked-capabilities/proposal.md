# Add parked-capabilities row to the speech-core spec map

| | |
|---|---|
| **Status** | SHIPPED 2026-08-07 — impl sc-cp0 `84acb81`; independent PASS sc-po4; steward convergence archive + receipt `converged` |
| **Class** | process — map/docs only; no product runtime behavior |
| **Touches** | `spec/README.md` (table of contents only) |
| **Change id** | `spec-map-parked-capabilities` |

## Objective

Make the `spec/README.md` table of contents honestly show capabilities that are
not yet accepted chapters — so the existing flow pointer to “speech-out parked
on ADR-001 — see spec/README map” resolves to a real map row, without opening
any new accepted chapter or claiming product behavior.

## Acceptance

- `spec/README.md` contains a **not yet accepted (parked / direction)** table
  placed after the accepted-chapters TOC and before the archive TOC.
- Table includes a `speech-out` parked row pointing at ADR-001 +
  `docs/evolution/` (progressive-audio / interruption), marked **parked**.
- Table notes other `docs/evolution/*.md` drafts as **direction only**, not
  `spec/specs/` truth.
- One honesty sentence under the table states parked names are navigational
  only and become chapters only through the normal change loop.
- Diff is README-only (no `spec/specs/`, runtime, charter, ADR, flow, or
  validator edits).
- `scripts/check-spec-contract.py` still reports zero findings on the tree
  after the edit (if run).

## Why

The reading lens `spec/flows/speech-in.md` already tells the reader where speech
goes next:

> chapter speech-out *(parked on ADR-001 — see spec/README map)*

But the README map today lists only **accepted** chapters and **archives**.
There is no parked / not-yet-accepted row. The pointer is already true in the
flow and already false in the map — a one-screen documentation gap with zero
runtime blast radius.

## What changes

Add a short third table to `spec/README.md`, immediately after the accepted
chapters table and before the archive table, titled **not yet accepted
(parked / direction)**. Exact rows (already-true pointers only):

| path / name | what it would cover | current home | status |
|---|---|---|---|
| `speech-out` (chapter not opened) | synthesis, playback, cancel, qualified audibility | ADR-001 + `docs/evolution/` progressive-audio / interruption specs | **parked** — do not invent an accepted chapter until operator opens a real change |
| other evolution drafts | Talker/Pi binding, event identity target, delivery plan, eval gates | `docs/evolution/*.md` | **direction only** — not `spec/specs/` truth; acceptance is per future change |

One plain sentence under the table:

> Parked names are navigational honesty, not promises. Nothing in this table is
> accepted behavior. A parked row becomes a real chapter only through the normal
> change loop (proposal → operator accept → implementation → steward merge).

## What does not change

- No file under `spec/specs/` (no requirement ADDED/MODIFIED/REMOVED).
- No charter text, no ADR ratification, no runtime/code/config.
- No new capability invented; no speech-out behavior claimed.
- No archive moves; no validator guarantee changes.
- Flow `speech-in.md` text stays as-is (it already points here).

## Why this shape for dogfood

Tiny, docs-only, already-true, single file, reversible by deleting a table.
Enough spine for `change.toml` + emit packet without touching product behavior.
