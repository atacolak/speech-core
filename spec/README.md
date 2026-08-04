# spec/ — Speech Core's source of truth

Everything spec-related lives in this one folder. If you want to know how any
mechanism behaves, start here. You should never need to read code to understand
the system — the code is accountable to these files, and when they disagree that
is a *finding*, not something to silently fix.

Stewarded by the **steward**. Nothing outside `spec/` is theirs to touch.

## Layout

| path | what it is |
|---|---|
| `specs/` | accepted truth — one file per capability |
| `changes/` | proposals under discussion (uncommitted drafts; conversation in progress) |
| `archive/` | shipped history — how the truth grew |
| `decisions.md` | the why-log: every choice, why, alternatives, revisit-trigger |

## How to read a spec

Each file covers **one capability** and answers four questions, in order:

1. **What is this, and why does it exist?** — one plain paragraph.
2. **What does it do?** — behavior as numbered requirements, each with
   WHEN → THEN scenarios. Evidence labels: `OBSERVED` (verified in code, file:line),
   `INFERRED` (reasoning stated), `UNRESOLVED` (undecided, has a revisit trigger).
3. **What is load-bearing?** — the structure a change must respect.
4. **What is undecided?** — open questions, each with its trigger. Honesty over completeness.

## How a change works (the loop)

```
you have intent  →  we draft a delta in changes/  →  you accept or amend
→  implementation happens (beads/workers; the steward never writes code)
→  result comes back  →  steward verifies code vs spec
   ├─ matches  →  merge delta into specs/, move change to archive/
   └─ differs  →  finding presented with evidence; YOU rule: amend the spec
                or send the code back
```

The operator is the acceptance and reconciliation authority. Nothing enters
`specs/` without your word; no mismatch is ever silently resolved in either
direction. That is what makes the spec steer the project: every divergence
between intent and reality passes through you.

### Delta grammar (changes/<name>/specs/…)

| section | meaning |
|---|---|
| `## ADDED Requirements` | brand-new behavior |
| `## MODIFIED Requirements` | existing behavior changing — must carry every surviving scenario |
| `## REMOVED Requirements` | behavior going away — name + why |
| `## RENAMED Requirements` | name-only change, FROM:/TO: pairs |

Grammar discipline is load-bearing: a real change mislabeled as ADDED produces
two competing truths; new behavior mislabeled as MODIFIED has nothing to replace.

## Table of contents

| spec | capability | status |
|---|---|---|
| `specs/speech-in-turn-lifecycle.md` | how speech input becomes a committed turn | **accepted 2026-08-04** |
| `specs/audio-ingress-transport.md` | the wire contract between adapters and daemon | *converging from recon draft B* |

The charter (`../CHARTER.md`) is the constitution of this rig: purpose, authority
boundaries, invariants. Specs must never contradict it; when one would, the
charter wins and we bring the conflict to the operator. The steward sweeps every
accepted change against charter invariants and flags anything — code behavior or
an operator's own sentence — that implies a new invariant, a prohibition, or a
conflict, always as an exact proposed diff. Amendment is the operator's alone.
