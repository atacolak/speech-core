# decisions.md — the why-log

One entry per decision: **choice / why / alternatives considered / revisit when**.
Newest at the top. Never delete; supersede by adding a new entry that says it does.

Pre-existing ADRs in `../docs/decisions/` (ADR-001 … ADR-006) remain valid records;
over time they get indexed here so there is one place to look.

---

## 2026-08-04 — spec compartmentalization: layers and lenses, not one book

Choice: the spec system is compartmentalized along two axes: (1) **capability
chapters** (vertical seams — one per mechanism), each with four fixed internal
layers (story §1 / contract §2 / mechanism §3 / undecided §4); (2) **flow
lenses** (`spec/flows/` — horizontal end-to-end walks, e.g. "a spoken sentence
through the system") that cite chapters and are re-checked whenever a chapter
they touch changes. Reader paths in the README tell each altitude which layers
to read. Multi-representation is the design, not a failure of one.
Why: operator correction — "one book" was never the requirement; the
requirement is *right compartmentalization*. One representation cannot house
the complexity of a system; the operator should be able to read a seam (or a
flow) without auditing the whole.
Alternatives: single monolithic "book" (rejected: overfit to a misread of the
operator's ask); OpenSpec-style three-file split per capability
(spec/design/evidence) (deferred: our four-layer chapter already separates the
same concerns inside one readable seam; revisit if chapters grow unreadable).
Revisit when: any chapter exceeds a comfortable single sitting; a second rig
adopts this layout; flows multiply past ~5 and need their own index.

## 2026-08-04 — the front door: spec/ becomes the rig's actual entrance

Choice: `spec/README.md` is the rig's authoritative entrance for "how this
system works". Root `README.md`, `rig.toml`, `docs/README.md`, and
`scripts/check-doc-contract.py` were re-pointed/ratified accordingly; old doc
surfaces (`docs/current-state.md`, `docs/decisions/`, `docs/evolution/`)
become **reference/evidence material**, not canonical-claim homes.
Why: an independent review found two authority maps (old docs vs spec/) — two
maps is how an operator loses the plot. One door, many rooms.
Alternatives: deprecate old docs (rejected: they hold recon gold and ADRs);
generated views (deferred: no generator yet).
Execution note: performed by the steward under explicit operator permission
("I agree with Q2. You can do that. I give you permission.") as a scoped
one-off; the steward's no-code/config hard rule stands for the general case.
Revisit when: a generator exists to produce doc views; a new rig bootstraps
and this pattern is templated.

## 2026-08-04 — spec validator commissioned (guarantee list)

Choice: commission a small rig-local validator giving mechanical teeth to four
guarantees: (1) `spec/specs/` contains only ACCEPTED-status chapters; (2)
delta MODIFIED/REMOVED targets exist in accepted chapters before a change is
applied, ADDED targets do not; (3) a change may archive only with all tasks
checked, proposal status shipped, and an outcome record; (4) requirement IDs
cited in changes resolve to live requirements. Drafted as
`spec/changes/spec-validator/`; implementation is execution-side (a bead on
the operator's word), never steward-authored code.
Why: the loop today is constitutionally strong and mechanically hand-applied;
the guarantees are cheap to check and catch self-contradictions early (today's
PROPOSED-in-specs/ lapse would have been caught by guarantee 1).
Alternatives: adopt OpenSpec CLI wholesale (rejected for now: grammar cost
outweighs parser benefit; our four-layer chapters and labels stay); no
validator (rejected: hand-application already produced one lapse).
Revisit when: the validator exists and its false-positive rate is known;
OpenSpec custom schemas mature.

## 2026-08-04 — archive closure conventions + upstream facts refresh

Choice: (a) archive folders are dated `archive/YYYY-MM-DD-<name>/`; (b)
archiving requires completed tasks.md, proposal status flipped to shipped, and
an outcome.md; (c) deltas may REMOVE open questions via a named
`## Open questions — REMOVED` section (codified in README's grammar); (d)
evidence cites prefer stable symbols (fn/test names) in requirement bodies;
dated line-level receipts live in evidence appendices. Also factual refresh:
Spec Kit now ships a `converge` pass and documents three persistence models;
our entry #2 characterization updated accordingly.
Why: independent review found the archive "narratively closed but structurally
open" and line-cites rotting (test-count drift on day one).
Revisit when: the validator can enforce (a)-(c) mechanically.

## 2026-08-04 — formula arsenal direction (town-side, recorded here for continuity)

Direction noted: operator leans toward the manager choosing among **multiple
curated formulas** (a small arsenal) rather than two hardcoded ones, and wants
the public `tdupu/gascity-packs` collection inventoried for candidates and
spec-writing conventions. Constitutional floor for any formula (generated or
curated): independent review, bounded scope, board truth, typed memory — a
generated formula failing the floor never runs.
Status: DIRECTION, not commissioned work; recon of the packs repo pending;
execution is manager/town territory.
Revisit when: packs inventory exists; the two-formula arsenal measurably
constrains a real work shape.

## 2026-08-04 — reconciliation contract: how implementation converges back onto the spec

**Choice:** three convergence roads, and no others: (1) happy path — work closing under an
accepted `changes/<name>/` gets *verified against code by the steward* and only then the
delta merges; (2) reality-mismatch — shipped code differs from the delta → evidence table →
operator rules which side moves → then merge; (3) delta-less drift — sweeps re-walk OBSERVED
citations and flag stale claims for operator ruling. Beads carry `spec-change: <name>`
metadata so closed work name-matches change folders; a city order may later ping the steward
on matching `bead.closed` events. Automation may flag, never fix; verification always
precedes archive; the spec is only ever updated by an accepted delta, never by code directly.

**Why:** implemented truth must flow into accepted truth through a *gate with memory*, not
through trust in status reports. Verify-before-merge keeps `specs/` reality-anchored; the
ruling step keeps steering human; the sweep net catches what bindings miss.

**Alternatives:** manager self-reports completion merged as said — rejected: unverifiable
trust-transfer. Full autonomy of reconcile (auto-amend on mismatch) — rejected: mismatch
ruling is where the operator steers the project. Cron-only sweeps without bead linkage —
deferred-not-rejected: bead linkage is the precise trigger, sweeps remain the net.

**Revisit when:** the managed loop runs against speech-core for real and the conversational
trigger proves too manual, or `bead.closed`-pinged reconciliation shows false positives.

---

## 2026-08-04 — charter stewardship: the steward watches, only the operator amends

**Choice:** the steward holds the charter as the permanent ceiling: every accepted
change is swept against charter invariants, and any operator statement or finding
that implies a new invariant, a prohibition, or a conflict is surfaced immediately
with an exact proposed diff. The charter is *never* amended by inference, drift,
or side effect — only by the operator ratifying the exact diff.

**Why:** ratification lives with the operator (CHARTER amendment clause), but
charters go stale through *unnoticed* erosion — casual conversation introduces
de facto invariants, code learns behaviors the charter forbids, neither gets
recorded. A designated watchdog makes staleness visible instead of silent.

**Alternatives:** steward amends directly — rejected: concentration of authority
against the rig's own constitution. Review charter on a schedule — rejected:
event-driven catching beats calendar-driven, and the events are right here.

**Revisit when:** a formal steward city-office mandate is written, or steward
responsibilities split across multiple agents.

---

## 2026-08-04 — human-hold close threshold is 7500 ms

**Choice:** human-hold fires after 7500 ms of speech-like audio with no committed
tokens. The conflict between CLI default (7500) and `TurnManagerConfig::default()`
(12000) is resolved in favor of the CLI value; code converges via
`changes/human-hold-threshold`.

**Why:** operator ruling: "12 seconds might be too long. the 7.5sec fallback is
optimal in cases where our voice detection cannot fall under a certain detection
threshold, either due to human making noise or external noise. it is sort of like
the final fallback, measured by no words coming into the system." This accepts
runtime reality as intent; the 12000 default was latent, not operative.

**Alternatives:** 12000 ms — rejected: would declare the live runtime *too eager*
and retune behavior without evidence that 7.5 s is mistimed in practice.

**Revisit when:** dogfood sessions produce measured evidence of premature or tardy
human-hold closes (too eager: mid-thought cutoffs; too tardy: dead-air drag).

---

## 2026-08-04 — protocol: living-spec spine + clarify gates + evidence labels

**Choice:** run this rig's spec area on OpenSpec's living-spec protocol
(`specs/` accepted truth, `changes/` deltas in ADDED/MODIFIED/REMOVED/RENAMED
grammar, merge-to-`archive/` on ship), borrowing spec-kit's constitution-as-gate
and clarify-interview ideas, extended with evidence labels
(OBSERVED / INFERRED / UNRESOLVED-with-trigger) and this why-log. The operator
is the acceptance and reconciliation authority: intent → operator-accepted delta
→ implementation → verification → operator-ruled reconciliation. Every spec/code
mismatch is surfaced with evidence and ruled on, never silently resolved.

**Why:** both source methodologies are published practice, but neither solves
this rig's needs alone: OpenSpec covers keeping standing truth true over an
existing moving codebase; spec-kit covers extracting and gating intent before
building; neither tracks *evidence status* (needed because our truth is tested
against a real codebase) nor records *why* durably. The operator steers the
project through the acceptance/reconciliation gates, so the spec loop — not
automation — is where authority lives.

**Alternatives:** adopt spec-kit wholesale including "code as regenerated output"
— rejected: brownfield reality; existing code is observed truth, not regenerable
output. Use unlabeled requirements — rejected: stale OBSERVED claims must be
machine-identifiable. Let managed-city automation update specs post-change without
operator ruling — rejected: retained as a future *proposal* mechanism only; a
city order may later require a spec delta per behavior bead, and auto-mark stale
claims, but acceptance stays human.

**Revisit when:** the city rigs-management layer activates and the automation
boundary (propose-vs-accept) gets real pressure, or the delta grammar proves
over-ceremonial for small fixes.

---

## 2026-08-04 — adopt a single-folder spec area as Speech Core's ground truth

**Choice:** all spec material lives in one top-level folder `spec/`, containing
`specs/` (accepted truth, one file per capability), `changes/` (proposals under
discussion), `archive/` (shipped history), and this `decisions.md` (why-log).
Specs are the operator's primary interface to the system, updated after every
behavior change, written to be read and browsed easily rather than dug out of code.

**Why:** the operator operates at the spec abstraction, not the code abstraction.
The spec must be true after every change and readable enough that both the
operator and small assistant models can answer project questions from it alone.
A single folder keeps repo root tidy and makes the spec area unmistakable.

**Alternatives:** separate top-level folders (`specs/`, `changes/`, `archive/`,
`decisions.md`) — rejected: operator prefers one folder for everything spec-related.
Keep the recon layout (`docs/recon/specs/`) — rejected: it described an
investigation, not a standing source of truth. Migrate ADRs into decisions.md —
deferred: churn for no information gain; index them instead.

**Revisit when:** the city defines a cross-rig spec-area standard, or the operator
finds the four-question file shape fails the "easy to read" test in practice.
