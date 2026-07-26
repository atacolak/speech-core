# Speech Core charter

**status:** draft 0 — requires operator ratification  
**scope:** `atacolak/speech-core` repository and its city rig  
**authority:** the operator ratifies and amends this charter

## 1. Purpose

Speech Core develops and maintains the components that make spoken interaction real-time, interruptible, observable, and testable.

Its product path includes:

- speech input, transcription evidence, and user-turn commitment;
- routing explicit assistant speech;
- synthesis, playback, cancellation, and interruption coordination;
- adapters to external transports and reasoning runtimes;
- structured events, traces, replay, evaluation, and operator diagnostics.

Speech Core exists to make the spoken path causally legible. Every significant state transition must have a named owner, observable evidence, and a bounded failure outcome.

## 2. Repository and component boundary

The Speech Core rig may build and maintain several runtime components. Repository ownership does not make every component authoritative for every fact.

Each component must declare only state it can directly establish. It must not infer or silently substitute state owned by another component or external system.

Current external seams include:

- **Discord and other transports:** own connection, channel, and transport-delivery state. They do not own transcript commitment, reasoning state, synthesis state, or playback truth.
- **Pi and Talker reasoning runtime:** own model runs, tool execution, prompt or steer acceptance, and reasoner lifecycle. Speech Core does not invent acceptance or replace a lost reasoning target.
- **Synthesis backends:** own model loading, synthesis progress, emitted audio, cancellation acknowledgement, and backend provenance. Speech Core owns the policy and contracts by which synthesis participates in the spoken path.
- **Project memory service:** owns storage and retrieval mechanics for reviewed historical knowledge. It does not own current repository, test, trace, charter, or bead truth.

An external dependency does not require its own city rig merely because Speech Core integrates with it. A separate repository becomes a separate rig only when the city needs to govern work in that repository.

## 3. Authority model

Different systems are authoritative for different claims:

| Source | Authority |
|---|---|
| This charter | purpose, repository boundary, permanent invariants, prohibited compromises, and amendment authority |
| Accepted architecture decisions and evolution plan | the active design selected to satisfy the charter |
| Repository, tests, and traces | what currently exists and what has been empirically demonstrated |
| Beads | current authorized obligations, ownership, dependency, blockage, and completion state |
| Project memory bank | reviewed historical knowledge, decisions, failure lessons, and generated perspectives |
| Pi profile instructions | role behavior and available capabilities |

A lower source cannot silently amend a higher source. The sources also cannot substitute for one another:

- a bead does not prove that code exists;
- a test does not amend the charter;
- an architecture decision does not prove implementation;
- memory does not prove current repository or work state;
- a role prompt does not create product authority.

When project memory conflicts with the charter, repository evidence, tests, traces, or live bead state, memory loses and must be corrected through reviewed evidence.

## 4. Product invariants

1. **Owned truth:** each component declares only facts it can directly establish.
2. **Committed user turn:** `transcript_committed` is the authoritative committed user-turn snapshot unless this charter is explicitly amended.
3. **Failure-domain separation:** speech input and speech output remain separable processes and failure domains.
4. **Bounded operations:** every cross-component operation has bounded queues or resource limits, explicit backpressure behavior, and one terminal outcome.
5. **Clock and sample provenance:** timing claims identify comparable monotonic clock domains. Audio-sensitive claims identify sample domains and ranges.
6. **Qualified audibility:** audible and heard states are estimates or classifications with evidence and uncertainty, never unqualified facts.
7. **Fail-loud dependency loss:** target or dependency loss is explicit. Speech Core does not silently retarget, create a fallback reasoner, resume cancelled output, or substitute independent state.
8. **Speakable content boundary:** only explicit user-directed assistant text may become speech. Hidden reasoning, tool names, calls, arguments, results, and lifecycle events are not automatically narrated.
9. **Evidence before replacement:** deployed paths remain preserved until a replacement passes its acceptance gates and rollback has been demonstrated.
10. **Current versus proposed:** proposed architecture, accepted design, implemented behavior, and verified behavior remain explicitly distinguishable.
11. **Repository remains usable without memory:** memory failure may degrade orientation and historical recall, but must not block repository inspection, tests, ordinary work, review, or rollback.
12. **Independent testability:** each accepted delivery slice leaves the repository independently testable.

## 5. Prohibited compromises and anti-patterns

Speech Core must not:

- turn a detailed proposal into present implementation truth;
- let an adapter become authoritative merely because it observes another component;
- infer hidden runtime state from logs, process output, timing guesses, or UI presentation when a structured contract is required;
- introduce a second steering queue or silently replace Pi's prompt, steer, run, or tool authority;
- claim that submitted audio was heard without qualified playback evidence;
- wait for retrospective alignment before performing a safety-critical playback stop;
- treat JSONL or another audit sink as the real-time transport by default;
- combine refactoring and new behavior in one implementation obligation when doing so obscures causality;
- convert transcripts, scratchpads, hypotheses, ordinary progress, or failed review into canonical project memory;
- import old beads, priorities, custody roles, worker counts, or governance merely because they already exist;
- create architecture, agents, or layers without an identified consumer, authority boundary, and failure mode;
- weaken acceptance gates to conceal known baseline defects.

## 6. Operating model

Speech Core is intended to be a city-managed rig with a designated manager-equivalent role.

The rig manager may:

- interpret operator intent within this charter;
- maintain the active evolution plan and accepted architecture decisions;
- inspect repository, test, trace, service, and bead state;
- identify knowns, unknowns, conflicts, and required evidence;
- propose bounded work and route approved obligations;
- request research, implementation, and independent review;
- evaluate evidence and report acceptance, rejection, or uncertainty;
- record reviewed closures into the project memory bank;
- refresh and validate project-memory perspectives;
- propose charter amendments when evidence exposes a boundary conflict.

The rig manager may not:

- amend or ratify this charter;
- invent product authority not granted here;
- implement product code as part of its manager role;
- replace independent review with its own assertion;
- treat the evolution plan, a bead, or memory as proof of implementation;
- create an uncontrolled worker swarm;
- manipulate unrelated rigs or city infrastructure except through authorized city interfaces.

The manager is justified by the decision and evaluation artifacts it produces. Persistence and worker capacity are operating choices, not charter requirements.

## 7. Architecture decisions and evolution plan

The active evolution plan and architecture decision records describe the architecture selected to satisfy this charter. They are binding on managed work until they are superseded through an explicit decision.

They may specify, among other things:

- synthesis backend and model pins;
- package and process topology;
- progressive audio and playback contracts;
- Pi integration and exact-session shadowing;
- interruption and history policy;
- Discord acceptance paths;
- evaluation stages and release gates;
- migration and removal sequencing.

These decisions are not disposable suggestions. They are subordinate to the charter only in the precise sense that they may be revised without changing Speech Core's purpose or permanent invariants.

The current evolution packet under `docs/evolution/` is a candidate active plan until reviewed and ratified. Its proposed bead graph is planning material, not authorization to create all proposed work.

## 8. Project memory

Speech Core uses one repository-specific project memory bank shared across its authorized profiles.

Project memory exists to preserve reviewed historical knowledge across sessions and roles. It must not become a second repository, work ledger, charter, or runtime state store.

### Memory authority

- only reviewed, evidence-backed closure knowledge becomes canonical memory;
- canonical writes use deterministic work-item identities and are safe to retry;
- generic transcript retention is disabled;
- raw generic retention is not exposed to ordinary workers or reviewers;
- the manager-equivalent role is the only canonical closure writer;
- every retained closure cites its review verdict and repository evidence;
- accepted closure records remain exportable outside the memory service so the bank is not the sole durable copy;
- memory outages are visible but non-blocking.

### Memory missions

**Retain:** extract only durable, evidence-backed architecture, contracts, accepted or superseded decisions, verified failure modes, demonstrated mitigations, and stable constraints from reviewed closure records.

**Reflect:** act as an evidence-grounded project historian. Distinguish implemented, accepted, historical, proposed, rejected, superseded, and unknown claims. Surface contradictions and evidence.

**Consolidate:** preserve recurring stable knowledge, temporal change, and disagreement without inventing acceptance, implementation, or current work state.

### Project perspectives

The bank maintains three generated, disposable perspectives:

1. **current architecture** — implemented components, boundaries, protocols, topology, state authority, lifecycle, cancellation, partial implementations, and explicit unknowns;
2. **accepted decisions** — active accepted product and architectural decisions, rationale, evidence, affected areas, and supersession history;
3. **known failure modes** — verified symptoms, confirmed or uncertain causes, diagnostics, failed fixes, demonstrated mitigations, and evidence.

A perspective is injected only when it is fresh, substantive, and supported by provenance. Empty, stale, malformed, or provenance-free projections are omitted.

## 9. City relationships

A future city Mayor may route intent among rigs, identify ownership, request clarification, and summarize cross-rig outcomes. The Mayor does not amend this charter or override repository evidence.

A future Familiar may advise the operator from a city-level world model of rigs, charters, decisions, status, and reviewed outcomes. The Familiar is advisory: it does not route work, implement changes, certify repository truth, or amend charters.

Cross-rig work requires explicit ownership at each seam. Speech Core declares its side of an interface; it does not need to specify the internal governance of every external dependency.

## 10. Amendment and decision protocol

### Charter amendments

The operator is the sole ratifying authority for this charter.

Any participant may identify pressure for an amendment. The rig manager normally prepares the proposal with:

- the exact current text;
- the proposed replacement;
- the evidence and failure mode motivating the change;
- affected architecture decisions, beads, memory perspectives, and interfaces;
- migration and rollback consequences;
- unresolved dissent or uncertainty.

A charter amendment becomes active only after explicit operator approval and a committed diff. The manager then updates or supersedes affected architecture decisions and refreshes project-memory perspectives. Historical charter versions remain in Git.

### Architecture decisions

The operator and rig manager make architecture decisions together when the choice materially affects product behavior, authority, safety, compatibility, or the evolution plan.

The manager may maintain implementation-level decisions independently only when they:

- remain within this charter and already accepted architecture;
- do not alter an external contract or authority boundary;
- do not weaken an acceptance or rollback gate;
- are supported by repository evidence and normal review.

Material decisions are recorded as versioned architecture decision records. Supersession is explicit; old decisions are retained as history rather than rewritten as though they never existed.

### Emergency behavior

A safety or availability emergency may justify stopping or rolling back a service before deliberation. It does not authorize a silent charter amendment. The event, evidence, temporary action, and required follow-up decision must be recorded.

## 11. Ratification checklist

This draft becomes the binding Speech Core charter only after the operator has:

- reviewed the architecture boundary;
- accepted or modified every invariant;
- accepted or modified the prohibited compromises;
- accepted the manager's authority and limits;
- accepted the memory missions and perspectives;
- accepted the amendment and architecture-decision protocol;
- reviewed the diff from the candidate evolution charter;
- explicitly approved ratification.
