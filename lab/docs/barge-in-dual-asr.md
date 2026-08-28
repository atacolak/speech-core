# Dual-Nemotron barge-in (implementation track)

**Branch:** `feature/assistant-self-asr-dual-live`  
**Label:** harness / `eval_only` — no shared protocol schema edits  
**Contract:** `/tmp/organism-control-plane/speech-to-speech-contract.md` rev 4  
**Checkpoints:** 009 (drain during user speech), 010 (dual Nemotron locked), 011 (file ownership)

This track implements the **production dual-stream barge-in path** as harness
scripts. It is separate from the synthetic TTS↔TTS eval harness
(`scripts/assistant-self-asr-*`, owned by the eval track).

## Topology (locked)

```text
 OPERATOR mic ──► speech-in ──► Nemotron A (user stream)
                                      │
                                      │ first alnum token → PAUSE playback
                                      │ transcript_committed → FINALIZE cut
                                      ▼
                              coordinator (this package)
                                      ▲
 speech-out PCM ──► record-only client ──► file-adapter ──► Nemotron B
                  (already-emitted only)   stream_id=
                                           assistant.self_asr
```

| Instance | Role | Stream | Notes |
|----------|------|--------|-------|
| **Nemotron A** | User ASR | existing mic / `laptop.live_mic` | Do not break speech-in |
| **Nemotron B** | Assistant self-ASR | `assistant.self_asr` | Separate process/stream |

**Not one shared worker.** Drain-during-user-speech is free on the critical path
because B cannot steal A’s compute. Optimizations (shared process, lighter
second model, batching) are later.

## Production cut sequence

1. **Pause** playback on first user **alphanumeric** token  
   (same evidence rule as `scripts/speech-out-live-session.sh`).
2. **Immediately** drain Nemotron B on audio **already emitted**  
   (cancel further synthesis; do not treat unplayed tail as heard).
3. **T0 finalize** at user `transcript_committed`:
   - **Primary:** drained B text, **force-aligned** to a prefix of the
     **intended LLM text**.
   - **Fallback** (drain incomplete / lag): intended prefix at last known
     alignment position + `pad_words` (~2) from intended text.
4. **Commit once** as truncated assistant message (harness `commit.json`);
   late self-ASR is diagnostic only and must not revise.

Error preference: accurate drained+aligned prefix when available inside the
user-speech window; fallback may short-overspeak *intended* words only.

## Package layout

```text
scripts/barge-in-dual-asr.py          # entry
scripts/barge-in-dual-asr/
  cut.py                              # force-align + primary/fallback cut
  simulator.py                        # dual-stream dry-run
  live_wiring.py                      # probes + fail-closed live checklist
  record_client.py                    # optional record-only speech-out WS client
  feed_assistant_asr.py               # file-adapter feed as assistant.self_asr
docs/barge-in-dual-asr.md             # this file
tests/test-barge-in-dual-asr.sh       # deterministic offline tests
```

**Do not edit** (other track / frozen seams):

- `scripts/assistant-self-asr-*`, `scripts/assistant_self_asr_cut.py`
- `docs/assistant-self-asr-eval.md`
- `crates/speech-core-protocol/**`
- daemon shared event vocabulary

## How to run

### Offline dry-run (no daemons)

Proves: **pause → drain window → cut primary=drain or fallback**.

```bash
# Built-in unit assertions
python3 scripts/barge-in-dual-asr.py --self-check

# Primary path: B drains fully → cut_source=drain
python3 scripts/barge-in-dual-asr.py --mode dry-run \
  --scenario primary-drain \
  --out-dir /tmp/barge-in-dual-primary

# Fallback path: B stuck → cut_source=fallback (last_pos + pad)
python3 scripts/barge-in-dual-asr.py --mode dry-run \
  --scenario fallback-incomplete \
  --out-dir /tmp/barge-in-dual-fallback

# Custom knobs
python3 scripts/barge-in-dual-asr.py --mode dry-run \
  --intended-text "one two three four five six seven eight nine ten" \
  --emitted-words-at-pause 7 \
  --b-pos-at-pause 5 \
  --drain-complete \
  --pad-words 2 \
  --pause-at-ms 800 \
  --user-stop-at-ms 2200
```

Artifacts:

```text
assistant_intended.txt
production_cut_text
drained_asr_text.txt
cut_decision.json
commit.json                 # immutable truncated assistant concept
metrics.json
events.jsonl                # pause → drain → commit timeline
README-run.txt
```

### Live mode (fail closed)

```bash
# Probes speech-core + speech-out TCP and local binaries.
# Exit 2 if not ready (unless --allow-live-stub).
python3 scripts/barge-in-dual-asr.py --mode live --out-dir /tmp/barge-in-live

# Checklist only when daemons are down
python3 scripts/barge-in-dual-asr.py --mode live --allow-live-stub
```

When daemons are reachable, `live_runbook.json` lists operator steps. Full
automatic dual-session orchestration stays harness-driven (no protocol edits).

#### Operator live wiring (Nemotron B)

1. Start speech-core daemon and speech-out daemon (server).
2. Build client tools:

   ```bash
   cargo build -p speech-out -p speech-core-file-adapter \
     -p speech-core-watch -p speech-core-mic-adapter
   ```

3. Keep **user** path on existing mic / live-session (Nemotron A).
4. **Record-only** speech-out client (synthesis PCM, no play):

   ```bash
   # requires: pip install websockets
   python3 scripts/barge-in-dual-asr/record_client.py \
     --url "${SPEECH_OUT_WS_URL:-ws://127.0.0.1:8788/ws/speech-out}" \
     --out-dir /tmp/assistant-pcm
   ```

5. On barge-in pause, feed **already-emitted** WAV into B:

   ```bash
   python3 scripts/barge-in-dual-asr/feed_assistant_asr.py \
     /tmp/assistant-pcm/merged_or_chunk.wav \
     --url "${SPEECH_CORE_WS_URL:-ws://127.0.0.1:8765/ws/audio-ingress}"
   ```

   Uses `stream_id=assistant.self_asr` (separate from user mic).

6. At user `transcript_committed`, force-align B text to intended via
   `production_cut()` (see `cut.py`); write `commit.json`.

## What is live vs stubbed

| Piece | Status |
|-------|--------|
| Dual-stream cut rule (primary drain / fallback pad) | **Live logic** in `cut.py` |
| Dry-run dual-stream simulator (pause → drain → cut) | **Live** offline |
| Force-align drained ASR → intended prefix | **Live** heuristic |
| Record-only speech-out WS client | **Implemented**; needs `websockets` + running speech-out |
| file-adapter feed as `assistant.self_asr` | **Implemented**; needs binary + speech-core |
| Automatic dual-daemon coordinator loop | **Runbook / probe**; operator-driven until wired in session harness |
| Shared protocol / daemon event changes | **Out of scope** (frozen) |
| Acoustic echo / VAD dirt | **Not** claimed (synthetic path) |

## Relation to eval harness

| Track | Worktree ownership | Purpose |
|-------|--------------------|---------|
| Eval (TTS↔TTS) | `scripts/assistant-self-asr-*` | Calibrate pad / three-way metrics |
| Impl (this) | `scripts/barge-in-dual-asr*` | Dual Nemotron production cut path |

Eval may still use a second process for B; production interference (R7) is out
of scope for v1 **because** topology is dual-instance.

## Related

- `docs/speech-output.md` — speech-out WS binary WAV chunks  
- `scripts/speech-out-live-session.sh` — first alphanumeric pause  
- `docs/seams.md` — SCF1 / file-adapter ingress  
- `docs/assistant-self-asr-eval.md` — eval-only track (do not edit here)
