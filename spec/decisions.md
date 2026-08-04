# decisions.md — the why-log

One entry per decision: **choice / why / alternatives considered / revisit when**.
Newest at the top. Never delete; supersede by adding a new entry that says it does.

Pre-existing ADRs in `../docs/decisions/` (ADR-001 … ADR-006) remain valid records;
over time they get indexed here so there is one place to look.

---

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
