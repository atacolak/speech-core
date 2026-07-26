# Speech Core project memory — Hindsight v1

**status:** accepted implementation target; not yet implemented
**scope:** one repository bank for `atacolak/speech-core`
**authority:** implementation specification subordinate to `CHARTER.md`

## 1. Objective

Implement one trustworthy learning loop:

```text
orient from reviewed project memory
→ complete one authorized city work item
→ independently review repository evidence
→ retain the verified closure
→ refresh bounded project perspectives
→ orient a later role from the result
```

Memory assists orientation. It does not replace the charter, accepted decisions, repository, tests, traces, or city work ledger.

## 2. Fixed v1 decisions

| Concern | v1 decision |
|---|---|
| Backend | pinned local Hindsight server; initial target `0.8.5` |
| Pi integration | adapt installed `pi-hindsight`; initial client package `0.10.0` |
| Scope | one Speech Core repository bank |
| Writer | rig manager only, through a constrained closure operation |
| Readers | manager, workers, reviewers, and researchers may receive bounded read-only orientation |
| Retention point | after independent review passes |
| Candidate memory | not retained |
| Transcript retention | disabled |
| Repository ingestion | disabled |
| Profile-local repository memory | disabled |
| Perspectives | three stable Hindsight mental models |
| Refresh | manual, full, after accepted durable closure |
| Tags | none in v1 |
| Availability | visible degradation; repository work continues |

Do not enable stock `pi-hindsight` lifecycle behavior for Speech Core roles. In particular, do not grant automatic transcript-delta retention or unbounded automatic recall/mental-model injection.

## 3. Terminology

A **perspective** is Speech Core's product term for a bounded, reusable view of reviewed project knowledge.

In v1 each perspective is backed by one Hindsight **mental model**: a server object with a stable ID, source query, generated content, token bound, refresh operation, and history.

A perspective is not silently injected. The adapter fetches and validates it, then may include it in an explicit, bounded orientation block returned by `memory_orient`.

The classifications `implemented`, `verified`, `accepted`, `proposed`, `rejected`, `superseded`, `historical`, and `unknown` are Speech Core semantics. Hindsight does not enforce them natively. The closure schema, source queries, adapter validation, and tests must preserve them.

## 4. Logical adapter

```text
memory_status()
memory_orient(work_item, task_summary?)
memory_reflect(question, work_item?)
memory_record_verified_closure(record)     # manager only
memory_refresh_all()                       # manager/internal only
```

`memory_orient` returns a bounded structure:

```text
Repository-memory status
Authority warning
Current architecture perspective
Accepted decisions perspective
Known failure modes perspective
Task-specific reflection
Freshness and provenance summary
```

Every section has an independent token bound and may be omitted. The adapter never mutates a role's base prompt with an unbounded bank dump.

## 5. Missions

### Retain

> Extract only durable, evidence-backed project knowledge from reviewed closure records: implemented architecture or contracts, accepted/rejected/superseded decisions and their rationale, verified failure modes and demonstrated mitigations, and stable engineering constraints. Preserve dates and evidence references. Ignore plans, active work status, raw logs, agent conversation, ordinary progress, and unverified hypotheses.

### Reflect

> Act as an evidence-grounded project historian. Distinguish implemented, verified, accepted, historical, proposed, rejected, superseded, and unknown claims. Surface contradictions, dates, and supporting evidence. Never represent bank knowledge as authoritative live repository, test, trace, charter, or work state.

### Consolidate

> Preserve recurring stable architecture, decision, and failure knowledge. Preserve temporal change and disagreement. Do not invent acceptance, implementation, verification, or active work state.

## 6. Perspectives

All three mental models use stable IDs, no tags, bounded output, full manual refresh, no cron, no refresh after consolidation, and exclusion of other mental models as source evidence.

### `current-architecture`

> Describe the currently implemented Speech Core architecture: major components, ownership boundaries, public protocols and events, runtime topology, state authority, lifecycle and cancellation semantics, and known partial implementations. State unknowns explicitly. Exclude proposed future architecture and active work state.

### `accepted-decisions`

> Describe active operator-accepted Speech Core product and architectural decisions. For each, state the decision, rationale, acceptance evidence, affected area, and any earlier decision it supersedes. Distinguish rejected and superseded decisions as history. Exclude draft or unaccepted proposals.

### `known-failure-modes`

> Describe verified significant or recurring Speech Core failure modes. Include symptoms, confirmed or explicitly uncertain causes, reliable diagnostics, failed attempted fixes, demonstrated mitigations, and evidence. Do not promote hypotheses to verified causes.

## 7. Orientation protocol

At the start of managed work:

1. resolve the Speech Core bank;
2. read `CHARTER.md` and `governance/ROLES.md`;
3. read the current city work item;
4. inspect live Git state and relevant repository evidence;
5. fetch all three perspectives with freshness and provenance details;
6. reject stale, empty, malformed, or provenance-free content;
7. ask one task-specific reflection that distinguishes history from claims requiring live verification;
8. return the bounded orientation block;
9. give workers only the subset relevant to their obligation.

The current work item, repository, tests, and traces are always read directly. Memory cannot assert that code exists, tests pass, a work item is open or closed, a proposal is implemented, or the charter changed.

## 8. Closure record

Only the manager can submit this record, and only after an independent pass verdict.

```yaml
schema: ata.repo-closure/v1
repository: atacolak/speech-core
work_item: <city work item id>
occurred_at: <timestamp>
review:
  verdict: pass
  reference: <verdict artifact or work-item record>
commits:
  - <sha>
summary: <demonstrably completed outcome>
affected_components: []
verified_facts:
  - claim: <fact present in verdict/evidence>
    evidence: []
decisions:
  - status: accepted | rejected | superseded
    statement: <operator-ratified decision, when applicable>
    evidence: []
failure_learnings:
  - symptom: <observed symptom>
    cause: <verified or explicitly uncertain>
    diagnostic: <how established>
    mitigation: <demonstrated mitigation>
    evidence: []
```

Required identity:

```text
document_id = "closure:<work-item-id>"
update_mode = replace/upsert
```

Retrying the same closure must not duplicate knowledge. A closure may restate only claims present in the review verdict and cited evidence. New architectural authority cannot appear first in memory.

The exact closure record or an equivalent primary record remains recoverable through Git or the city work ledger so Hindsight is never the sole durable copy.

## 9. Refresh and inclusion validation

After a retained closure becomes query-visible, refresh all three mental models.

A perspective may be included in orientation only when:

- refresh reached a successful terminal state;
- content is non-empty and substantive;
- supporting provenance is present;
- it is not stale under the adapter's configured threshold;
- its stable ID and source query match the versioned bank template.

Otherwise omit it, report degraded memory status, and proceed from charter, decisions, work item, repository, tests, and traces.

Pin tested server/client versions. Never deploy this v1 against a floating `latest` tag.

## 10. Failure behavior

| Failure | Required behavior |
|---|---|
| Hindsight unavailable | continue work; report degraded memory |
| Perspective stale or malformed | omit it |
| Direct reflection ungrounded | omit it |
| Retention fails | report sync failure; retry with same document ID |
| Refresh fails | do not include failed result |
| Memory conflicts with live authority | live authority wins; retain a reviewed correction later |
| Review fails | retain nothing canonical |
| No durable learning | close work without retention |

Memory-sync failure does not invalidate otherwise reviewed repository work, but it must not be reported as successful.

## 11. Operator surface

Provide one status command showing, without credentials:

- detected repository and logical role;
- endpoint reachability;
- bank ID;
- server and client versions;
- mental-model IDs, refresh times, and staleness;
- latest closure and refresh operation;
- latest memory error;
- whether automatic retention and automatic injection are disabled.

## 12. Acceptance

V1 is complete only when demonstrated:

1. bank template validates and imports twice without duplicate models;
2. manager, reviewer, and worker resolve the same bank;
3. ordinary roles lack generic retain and refresh tools;
4. one real city work item completes worker → reviewer → manager closure;
5. retrying its deterministic closure creates no duplicate knowledge;
6. all three perspectives refresh with substantive provenance;
7. a later session retrieves the verified learning;
8. stale or malformed projections are omitted;
9. a simulated outage does not block normal repository work;
10. no repository outcome enters profile-local memory;
11. status reports identity, freshness, disabled automatic behavior, and degradation.

## 13. Outside v1

- operator or psychological memory;
- the Familiar's personal or city world model;
- role-specific project banks;
- multiple-bank composition;
- automated work creation;
- autonomous charter amendment;
- whole-repository indexing;
- automatic transcript retention;
- automatic or cron mental-model refresh;
- custom tag taxonomy;
- per-component models;
- hostile multi-tenant authorization;
- a general multi-repository framework.
