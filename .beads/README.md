# beads (speech-core)

issue ledger for this repo. prefix **`sc`**.

**2026-08-17:** ledger is **empty on purpose**. Last committed jsonl was
Voice House `vh-*` (deleted in `821e021`). Do not restore it. New work is
new `sc-*` seam beads after BOARD names them.

## tools

| | |
|---|---|
| **`br`** | create / ready / claim / close / deps |
| **`mg`** | board UI on the jsonl mirror |


```bash
cd /path/to/speech-core
br ready --json
br create "…" --json
br update sc-xxx --claim --assignee=worker --json
br close sc-xxx -r "done" --json
br sync --flush-only          # refresh issues.jsonl if the board looks stale
mg -path .beads/issues.jsonl
```

storage lives under `.beads/` (`beads.db` + `issues.jsonl`). see `br robot-docs guide` for the full command surface.

orchestration with omp: skill **`beads-ready-front`** (`skill://beads-ready-front`).
