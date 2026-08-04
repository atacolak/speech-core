# ata.spec-change/v1 — the change.toml manifest schema

Every accepted (or shippable-intent) change folder carries a `change.toml` —
the machine-readable spine the city binds to. The Markdown around it remains
the human's copy; **this file is the deterministic identity**. States describe
artifacts, never conversations.

## Required fields

| field | meaning |
|---|---|
| `schema` | literal `"ata.spec-change/v1"` |
| `id` | the folder name |
| `class` | `behavior` (product behavior moves) | `tooling` | `process` |
| `status` | `proposed` | `accepted` (operator has ruled) |
| `canonical_base_branch` | branch of record (ruling: decisions.md) |
| `canonical_base_commit` | HEAD the change was frozen against — convention: the commit *before* the manifest's own landing commit (a file cannot hash its own commit) |
| `intent_revision` | integer; bumps whenever the semantic input changes |
| `intent_digest` | `sha256:` over the semantic input (procedure below) |
| `affected_capabilities` | chapter names the delta touches (`[]` allowed for tooling) |

## Optional fields

| field | meaning |
|---|---|
| `affected_requirements` | requirement IDs the delta ADDs/MODIFIES/REMOVEs |
| `affected_charter_invariants` | charter invariant numbers engaged |
| `exclusions` | what this change explicitly does NOT do |
| `operator_authorization` | verbatim acceptance quote + date |
| `implementation_root` | bead id, once dispatched |
| `implementation_target_branch` | integration target |
| `implementation_commit` | filled at reconcile time |

## The digest (intent_rev 1 procedure)

The semantic input = `proposal.md`, `tasks.md`, and every file under `specs/`
(the delta). Reproducible command from the change folder:

```sh
{ find . -maxdepth 1 -name 'proposal.md' -o -maxdepth 1 -name 'tasks.md';
  find ./specs -type f 2>/dev/null; } | sed 's|^\./||' | LC_ALL=C sort \
  | xargs sha256sum | sha256sum
```

Any semantic change → different digest → beads bound to the older digest are
**stale**. Prose-only churn that changes the bytes still bumps the digest;
 bumping `intent_revision` marks *meaningful* revisions — the digest is the
warden, the revision is the narrative.

## What changes to a frozen change mean

- **`operator_authorization` / acceptance edits** → re-freeze: bump
  `intent_revision`, recompute digest, log the reason in `decisions.md`.
- **`implementation_*` fields** → filled by steward/reconciliation during the
  transaction; they do not bump the digest (they are evidence, not intent).
