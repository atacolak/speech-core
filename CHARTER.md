# Speech Core charter

**status:** draft — becomes binding only after explicit operator ratification
**domain:** the Speech Core product and the `atacolak/speech-core` repository
**ratifying authority:** the operator

Until ratified, this file is advisory. Direct operator instructions and repository evidence govern current work.

## 1. Purpose

Speech Core exists to make spoken human-agent interaction real-time, interruptible, observable, testable, and causally legible.

Its product domain includes:

- speech input, transcription evidence, and user-turn commitment;
- routing explicit assistant speech toward synthesis and playback;
- synthesis, playback, cancellation, and interruption coordination;
- adapters to transports and reasoning runtimes;
- structured events, traces, replay, evaluation, and operator diagnostics.

Each significant runtime transition must have a declared owner, observable evidence, and a bounded failure outcome. Component ownership is declared in current architecture and accepted decision records; undeclared ownership is a defect, not permission to infer.

## 2. Product boundary

Speech Core may build and maintain several runtime components. Building or hosting a component does not make it authoritative for every fact it observes.

Each component may declare only state it can directly establish. It must not infer or silently substitute state owned by another component or external system.

Speech Core integrates through explicit authority seams:

- transports own connection, channel, and transport-delivery state;
- the reasoning runtime owns model runs, tool execution, prompt or steer acceptance, and reasoner lifecycle;
- speech input owns audio-ingress, ASR, VAD, turn evidence, and committed user-turn state;
- synthesis owns model loading, synthesis progress, emitted audio, cancellation acknowledgement, and backend provenance;
- playback owns device submission, buffering, stop/flush behavior, and qualified cursor or audibility evidence;
- session control, when present, owns conversational-history mutation and interruption policy;
- event and audit sinks preserve structured evidence but are not the real-time control path by default.

Project memory is not part of the spoken runtime path. It preserves reviewed historical knowledge and never owns current runtime, repository, test, trace, charter, or work state.

## 3. Claim-scoped authority

Different sources govern different kinds of claims:

| Source | Governs |
|---|---|
| This charter | product purpose, domain boundary, permanent invariants, prohibited compromises, and amendment authority |
| Accepted decision records | selected architecture and policy within this charter |
| Repository | artifacts currently present |
| Tests and traces | behavior demonstrated under identified conditions |
| City work ledger | currently authorized obligations, ownership, dependency, blockage, and closure state |
| Project memory | reviewed history and generated interpretation |
| Role mandates and profile instructions | delegated role authority, execution behavior, and available capabilities |

No source may substitute for another source's kind of claim:

- a plan or decision does not prove implementation;
- a work item does not prove code exists or works;
- a test does not amend this charter;
- memory does not prove current repository or work state;
- a profile or tool capability does not create product authority.

The documents under `docs/evolution/` are plans and target-design specifications, not a second charter. If they conflict with this charter, this charter wins and the conflicting document must be amended, rejected, or superseded.

## 4. Product invariants

1. **Owned truth:** each component declares only facts it can directly establish.
2. **Committed user turn:** `transcript_committed` is the authoritative committed user-turn snapshot. Later diagnostic finalization does not silently revise it.
3. **Failure-domain separation:** speech input and speech output remain separable processes and failure domains.
4. **Bounded operations:** every cross-component operation has bounded queues or resource limits, explicit backpressure behavior, and one terminal outcome.
5. **Clock and sample provenance:** timing claims identify comparable monotonic clock domains. Audio-sensitive claims identify sample domains and ranges.
6. **Qualified audibility:** audible and heard states are estimates or classifications with method, evidence, and uncertainty, never unqualified facts.
7. **Fail-loud dependency loss:** target or dependency loss is explicit. Speech Core does not silently retarget, create a fallback reasoner, resume cancelled output, or substitute independent state.
8. **Speakable-content boundary:** only explicit user-directed assistant text may become speech. Hidden reasoning, tool names, calls, arguments, results, and lifecycle events are not automatically narrated.
9. **Evidence before replacement:** a deployed path remains preserved until its replacement passes accepted gates and rollback has been demonstrated.
10. **Current versus proposed:** proposed architecture, accepted design, implemented behavior, and verified behavior remain explicitly distinguishable.
11. **Repository works without memory:** memory failure may degrade orientation and historical recall, but must not block inspection, tests, ordinary work, review, or rollback.
12. **Independent testability:** each accepted delivery slice leaves the repository independently testable.
13. **History authority:** conversational history may be mutated only by its explicitly designated owner; adapters and observers cannot bypass it.
14. **Safety-critical stop:** physical playback stop must not wait for retrospective alignment or historical classification.

## 5. Prohibited compromises

Speech Core must not:

- turn a detailed proposal into present implementation truth;
- let an adapter become authoritative merely because it observes another component;
- infer hidden runtime state from logs, process output, timing guesses, or UI presentation when a structured contract is required;
- introduce a second steering queue or silently replace the reasoning runtime's prompt, steer, run, or tool authority;
- claim that submitted audio was heard without qualified playback evidence;
- treat JSONL or another audit sink as the real-time transport by default;
- combine refactoring and new behavior in one implementation obligation when doing so obscures causality;
- convert transcripts, scratchpads, hypotheses, ordinary progress, or failed review into canonical project memory;
- import old work items, priorities, roles, worker counts, or governance merely because they already exist;
- create architecture, agents, offices, or layers without an identified consumer, authority boundary, and failure mode;
- weaken acceptance gates to conceal known baseline defects;
- let a plan, memory, role prompt, or tool permission authorize implementation;
- allow an author, manager, or implementer to self-certify a material change requiring independent review.

## 6. City interface and delegated roles

Speech Core participates in the city through authorized interfaces. City offices may route intent, request evidence, coordinate cross-rig obligations, and report outcomes. They may not amend this charter, replace Speech Core's local technical authority, certify repository truth without evidence, or silently alter authorized obligations.

Detailed manager, worker, and reviewer responsibilities belong in `governance/ROLES.md`, not in this charter. A profile or agent session occupies a role; it is not the source of that role's authority.

The rig manager may prepare charter or material decision proposals, but only the operator may ratify them. Independent review must come from a session distinct from the implementer and from the manager that records closure.

## 7. Project-memory boundary

Speech Core may use one repository-scoped project-memory facility shared by authorized roles. Memory is an optional orientation and learning facility, not a merge gate or work-authorisation surface.

Permanent requirements are:

- memory never outranks this charter, repository evidence, tests, traces, or live work state;
- only post-review, evidence-backed closure knowledge may become canonical project memory;
- canonical writes are idempotent under a stable work-item identity;
- ordinary workers and reviewers do not receive generic retention authority;
- one designated writer role records canonical closures from independent verdicts and cited evidence;
- canonical source records remain recoverable outside generated memory projections;
- transcripts, scratchpads, hypotheses, ordinary progress, and failed reviews are not canonical memory;
- profile-local memory may preserve role technique, not Speech Core repository truth;
- private or personal context is never exposed to engineering roles merely because another office possesses it;
- memory outages and stale projections are explicit and bounded.

The current bank technology, missions, projections, refresh policy, schemas, version pins, context budgets, and adapter behavior belong in `docs/memory/hindsight-v1.md` or a superseding memory specification.

## 8. Amendment and decision authority

The operator is the sole ratifying authority for this charter and for material decisions that change product scope, external contracts, authority boundaries, production backends, transport commitments, reasoner integration, or safety and rollback gates.

Any participant may propose an amendment. A proposal must contain:

- exact current text and proposed replacement;
- motivating evidence and failure mode;
- affected decisions, work items, interfaces, memory projections, and migrations;
- rollback consequences;
- unresolved dissent or uncertainty.

An amendment becomes active only after explicit, attributable operator approval of the exact diff and a committed change. Project memory cannot be the sole record of approval. Historical versions remain available through Git.

A manager may record implementation-level decisions without separate constitutional ratification only when they remain inside this charter and already accepted decisions, change no external contract or authority boundary, weaken no acceptance or rollback gate, and pass normal independent review.

Accepted decisions are versioned. Draft decisions have no authority. Supersession is explicit; prior records remain history rather than being rewritten as though they never existed.

A safety or availability emergency may justify immediate stop, isolation, or rollback. It does not create permanent authority or silently amend this charter. The event, evidence, temporary action, and required follow-up decision must be recorded.

## 9. Ratification

This draft becomes binding only after the operator has reviewed the final diff, resolved any stated dissent, and explicitly approved ratification.
