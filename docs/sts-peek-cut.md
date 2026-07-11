# sts-peek cut coordinator (Track C)

**Branch:** `feature/sts-peek-cut`  
**Label:** harness / `live_peek` — no shared protocol schema edits  
**Contract:** speech-to-speech-contract.md rev 4 (drain primary, pad fallback, dual Nemotron)  
**Plan:** live-sts-peek-plan.md Phase 1 Track C

This track owns the **cut coordinator** for the live STS peek surface. It
finalizes a truncated assistant prefix when the operator barges in, using the
founder cut rule:

1. **Pause** on barge / first user alphanumeric token (audio/UI tracks).
2. **Drain** Nemotron B (`assistant.self_asr`) on already-emitted audio during
   the user speech window (dual instance — free on the critical path).
3. **T0 finalize** at user `transcript_committed`:
   - **Primary:** drained B text force-aligned to a prefix of intended LLM text.
   - **Fallback:** last known alignment position + `pad_words` (~2) from intended.
4. **Commit once** (`commit.json` immutable); late self-ASR is diagnostic only.

## Package layout

```text
scripts/sts-peek/cut.py         # force-align + primary/fallback + three-way score
scripts/sts-peek/cut_coord.py   # run_dir watcher, dry-run simulator, follow mode
scripts/sts-peek/run_cut.py     # CLI entry
docs/sts-peek-cut.md            # this file
tests/test-sts-peek-cut.sh      # deterministic offline tests
```

**Do not edit** (other tracks / frozen seams):

- `scripts/sts-peek/ui*`, session/audio owned by Tracks U/L
- `scripts/assistant-self-asr*`, `scripts/assistant_self_asr_cut.py`
- `scripts/barge-in-dual-asr*`
- `crates/speech-core-protocol/**`, daemon event vocabulary

Cut math is **copied/adapted** from the frozen helpers above so Track C can
evolve independently without Phase-0 shared-module work.

## How to run

### Self-check (in-process)

```bash
python3 scripts/sts-peek/run_cut.py --self-check
```

### Offline dry-run (no daemons)

Proves **pause → drain window → cut primary=drain or fallback**.

```bash
# Primary path: B drains fully → primary_cut_source=drain
python3 scripts/sts-peek/run_cut.py --mode dry-run \
  --scenario primary-drain \
  --run-dir /tmp/sts-peek-cut-primary \
  --intended-text "one two three four five six seven eight nine ten" \
  --pad-words 2

# Fallback path: B stuck → primary_cut_source=fallback (last_pos + pad)
python3 scripts/sts-peek/run_cut.py --mode dry-run \
  --scenario fallback-incomplete \
  --run-dir /tmp/sts-peek-cut-fallback \
  --intended-text "one two three four five six seven eight nine ten" \
  --pad-words 2

# Fail-closed: no B stream at all → fallback + metrics flag
python3 scripts/sts-peek/run_cut.py --mode dry-run \
  --scenario fallback-b-missing \
  --run-dir /tmp/sts-peek-cut-b-missing
```

### Follow mode (live run_dir from Track L/U)

```bash
python3 scripts/sts-peek/run_cut.py --mode follow \
  --run-dir /path/to/session \
  --intended-text "..." \
  --pad-words 2 \
  --wait-timeout-s 30
```

Polls until `user_transcript_committed` (events or control file), then writes
cut artifacts. Exit 3 on timeout. If B stream never appears, still finalizes
with **fallback** and `fail_closed_b_stream_missing=true`.

### Offline tests

```bash
./tests/test-sts-peek-cut.sh
```

## run_dir integration contract

Shared with Track L (audio session) and Track U (UI). Track C **only reads**
inputs and **writes** cut outputs.

### Inputs (written by L/U; tolerate missing while waiting)

| Path | Role |
|------|------|
| `assistant_intended.txt` | Intended LLM text (optional if CLI `--intended-text`) |
| `events.jsonl` | Append-only event stream (see names below) |
| `control/barge.json` | Optional barge mark `{emitted_words_at_pause, b_pos_at_pause, playback_pos_words, ...}` |
| `control/drain.json` | Optional drain snapshot `{drained_asr_text, drain_complete, b_pos_words, last_aligned_pos_words, emitted_words_at_pause}` |
| `control/drain_complete` | Empty marker file → treat drain as complete |
| `control/user_commit.json` | Finalize mark `{user_transcript, ...}` |
| `drained_asr_text.txt` | Optional plain drain text |
| `assistant_self_asr.txt` | Alias for drain text |

### Event names recognized in `events.jsonl`

| Kind | Names |
|------|-------|
| Barge / pause | `barge`, `speech_out_barge_in`, `user_first_alphanumeric_token`, `pause_playback`, `sts_peek_barge` |
| Drain (B) | `assistant_self_asr_drain_started`, `assistant_self_asr_drain_progress`, `assistant_self_asr_drain_complete`, `nemotron_b_drain`, `sts_peek_drain` |
| User commit (T0) | `user_transcript_committed`, `transcript_committed`, `sts_peek_user_commit` |

Drain progress fields (any subset): `drained_text` / `drained_asr_text`,
`b_pos_words`, `drain_complete`, `emitted_words` / `target_emitted_words`,
`last_aligned_pos_words`.

### Outputs (owned by Track C)

| Path | Content |
|------|---------|
| `production_cut_text` | Final truncated assistant prefix (plain text) |
| `metrics.json` | `primary_cut_source`, `production_cut_text`, `prefix_valid`, `overspeak_words`, `underspeak_words`, `pad_words`, three-way fields, topology, fail-closed flag |
| `commit.json` | Immutable truncated assistant concept (`immutable: true`, `late_self_asr_revises: false`) |
| `cut_decision.json` | Full decision record (aligned prefix, confidence, fallback text) |
| `events.jsonl` | Coordinator appends `sts_peek_cut_decision`, commit event, `sts_peek_cut_finalized` |

### Sequence (happy path)

```text
L/U: user_first_alphanumeric_token / barge
L:   assistant_self_asr_drain_started → drain_progress (B separate Nemotron)
L/U: user_transcript_committed
C:   production_cut (drain|fallback) → metrics.json + commit.json (once)
```

## Metrics fields (acceptance)

| Field | Meaning |
|-------|---------|
| `primary_cut_source` | `"drain"` or `"fallback"` |
| `production_cut_text` | Chosen cut (always a prefix of intended when `prefix_valid`) |
| `prefix_valid` | production_cut is a word-prefix of normalized intended |
| `overspeak_words` | words in cut beyond intended_at_playback shared prefix |
| `underspeak_words` | words in intended_at_playback missing from cut |
| `pad_words` | config (default 2); used only on fallback path |
| `fail_closed_b_stream_missing` | true if no B drain evidence was seen |

## Relation to other packages

| Package | Purpose | Ownership |
|---------|---------|-----------|
| `scripts/assistant_self_asr_cut.py` | Eval harness cut helpers | frozen (import ideas only) |
| `scripts/barge-in-dual-asr/cut.py` | Dual-live cut helpers | frozen (import ideas only) |
| `scripts/sts-peek/cut*.py` | Peek coordinator | **Track C** |

No protocol changes. Dual-Nemotron topology: `shared_worker=false`.

## Related

- `docs/barge-in-dual-asr.md` — dual-live path (frozen)
- `docs/assistant-self-asr-eval.md` — eval-only track
- `scripts/speech-out-live-session.sh` — first alphanumeric pause semantics
