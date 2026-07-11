# Assistant self-ASR eval harness (eval_only)

**Branch:** `feature/assistant-self-asr`  
**Label:** every artifact and metric from this harness is **`eval_only`**.  
**Contract:** `/tmp/organism-control-plane/speech-to-speech-contract.md` rev 2  
**Risk register:** `/tmp/inquisitor-speech-to-speech-review.md` (do not ignore biases)

This is a **deterministic harness-only** prototype. It does **not** change
`speech-core-protocol` or `speech-core-daemon` shared event vocabulary.

## Founder cut rule (explicit)

When the user barges in and then stops talking:

1. **Pause** playback on the first user **alphanumeric** token  
   (existing `scripts/speech-out-live-session.sh` behavior on
   `transcript_token_committed`).
2. **Finalize** the truncated assistant message at user stop /
   `transcript_committed` (T0) — not at first token alone.
3. **Base position** = assistant-tracking Nemotron alignment position at T0
   (progress through the known intended text / spoken assistant audio).
4. **Pad** = `+pad_words` (default **2**) from the **intended LLM text** the
   speaker layer is faking — never free-form ASR invention.
5. **Commit once** as a truncated assistant message concept; late self-ASR
   drain is **diagnostic only** and must not revise the commit.

Error preference: short overspeak of *intended* words (+pad) over waiting for
full ASR drain. Production cut must remain a prefix of normalized intended text.

## Three-way metrics

| Field | Meaning |
|-------|---------|
| `intended_at_playback` | Intended-text prefix at playback progress (eval_playback) |
| `asr_recovered_at_stop` | Second ASR recovered prefix after drain (diagnostic) |
| `production_cut_text` | `intended_prefix(nemotron_pos_at_stop) + pad_words` |
| `prefix_valid` | production cut is a prefix of normalized intended text |
| `overspeak_words` | words in production cut not in intended_at_playback |
| `underspeak_words` | words in intended_at_playback missing from production cut |
| `pad_words` | config (default 2), not a quality score |

Primary calibration gate: overspeak/underspeak vs `intended_at_playback` with
`prefix_valid == true`. Do **not** average synthesis and playback into one truth.

## How to run (offline dry-run)

No speech-out daemon, mic, or Nemotron required:

```bash
# Unit-testable cut-rule assertions
python3 scripts/assistant-self-asr-harness.py --self-check

# Synthetic timeline → artifacts under /tmp (or --out-dir)
python3 scripts/assistant-self-asr-harness.py --mode dry-run

python3 scripts/assistant-self-asr-harness.py --mode dry-run \
  --out-dir /tmp/assistant-self-asr-demo \
  --intended-text "one two three four five six seven eight nine ten" \
  --pad-words 2 \
  --pause-at-ms 1000 \
  --user-stop-at-ms 2000 \
  --words-per-second 3 \
  --playback-lag-words 1 \
  --asr-lag-words 2
```

Artifacts written:

```text
assistant_intended.txt
production_cut_text
intended_at_playback.txt
asr_recovered_at_stop.txt
metrics.json
commit.json
events.jsonl
README-run.txt
```

Cut helpers (importable / unit-testable):

```text
scripts/assistant_self_asr_cut.py
```

## Live wiring gap (stub)

```bash
python3 scripts/assistant-self-asr-harness.py --mode live --allow-live-stub
```

Writes a checklist only. Still TODO:

1. **Instrumented play path** — log samples/chunks actually started
   (`eval_playback`); optional WAV dump. Prefer local adapter instrumentation
   over protocol changes.
2. **Assistant Nemotron stream** — alignment position at
   `transcript_committed` as production cut base.
3. **Optional record-only client** — second websocket consumer of speech-out
   binary WAV chunks for `eval_synthesis` second ASR later.
4. **Reuse** existing first-alphanumeric pause in
   `scripts/speech-out-live-session.sh`.

## Biases / limitations (must stay labeled)

- Dry-run uses a **mock linear words/sec clock**, not a physical playback or
  multi-clock reconstruction (inquisitor R5).
- No acoustic play→mic loop; no echo leak metrics (A4).
- No dual-stream model-worker interference measurement (R7).
- Stub `asr_recovered_at_stop` is derived from intended text with a lag knob,
  not a real second Nemotron (R4 risk if misread as truth).
- Green dry-run metrics are **not** production cut evidence (A9).

## Related

- `docs/speech-output.md` — speech-out seam, barge-in harness notes  
- `scripts/speech-out-live-session.sh` — first alphanumeric token pause  
- `scripts/speech-out-diagnostics.py` — output-only diagnostics (no cut rule)
