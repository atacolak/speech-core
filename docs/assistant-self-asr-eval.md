# Assistant self-ASR eval harness (eval_only)

**Branch track:** `feature/assistant-self-asr-eval-tts` (synthetic TTS↔TTS)  
**Label:** every artifact and metric from this harness is **`eval_only`**.  
**Contract:** `/tmp/organism-control-plane/speech-to-speech-contract.md` rev 4  
**Checkpoints:** 009 (drain free latency), 010 (dual Nemotron), 011 (TTS↔TTS track)  
**Risk register:** `/tmp/inquisitor-speech-to-speech-review.md` (do not ignore biases)

This is a **deterministic harness-only** prototype. It does **not** change
`speech-core-protocol` or `speech-core-daemon` shared event vocabulary.

## Founder cut rule (explicit — rev 3/4)

When the user barges in and then stops talking:

1. **Pause** playback on the first user **alphanumeric** token  
   (existing `scripts/speech-out-live-session.sh` behavior on
   `transcript_token_committed`).
2. **Immediately** start/continue **assistant self-ASR drain** on audio already
   emitted (Nemotron-B). Cancel further speak. Drain runs during the user speech
   window and is free on the critical path under dual-Nemotron topology.
3. **Finalize** the truncated assistant message at user stop /
   `transcript_committed` (T0):
   - **PRIMARY:** drained assistant prefix, force-aligned to a prefix of the
     **intended LLM text**.
   - **FALLBACK only** (drain incomplete / lag / starve): intended prefix at
     last known alignment position + `pad_words` (~2) from intended text.
4. **Commit once** as a truncated assistant message concept; late self-ASR
   must **not** revise the commit.

Error preference: accurate drained+aligned prefix when available within the
user-speech window. Fallback may short-overspeak *intended* words (+pad), never
invent non-intended words. Production cut must remain a prefix of normalized
intended text.

`pad_words=2` is **FALLBACK only**, not the happy path.

## Dual Nemotron (rev 4)

| Stream | Role | Eval mock |
|--------|------|-----------|
| Nemotron-A | User ASR / user-role barge-in clock | user TTS or fixture timing |
| Nemotron-B | Assistant self-ASR drain | assistant emit + drain clock |

They do **not** share one serialized worker in the intended topology. Eval
offline path uses two independent mock linear clocks.

## Three-way metrics

| Field | Meaning |
|-------|---------|
| `primary_cut_source` | `drain` (primary) or `fallback` (pad path) |
| `intended_at_playback` | Intended-text prefix at playback progress (eval_playback) |
| `asr_recovered_at_stop` | Assistant drain recovered prefix at user stop (diagnostic) |
| `production_cut_text` | Drain-aligned intended prefix, or fallback `pos + pad_words` |
| `prefix_valid` | production cut is a prefix of normalized intended text |
| `overspeak_words` | words in production cut not in intended_at_playback |
| `underspeak_words` | words in intended_at_playback missing from production cut |
| `pad_words` | config (default 2), **fallback only** — not a quality score |
| `label` | always `eval_only` |

Primary calibration gate: overspeak/underspeak vs `intended_at_playback` with
`prefix_valid == true`. Do **not** average synthesis and playback into one truth.

## How to run (offline dry-run / mock)

No speech-out daemon, mic, or Nemotron required:

```bash
# Unit-testable cut-rule + dual-clock assertions
python3 scripts/assistant-self-asr-harness.py --self-check

# Single-stream synthetic timeline (drain-primary)
python3 scripts/assistant-self-asr-harness.py --mode dry-run

python3 scripts/assistant-self-asr-harness.py --mode dry-run \
  --out-dir /tmp/assistant-self-asr-demo \
  --intended-text "one two three four five six seven eight nine ten" \
  --pad-words 2 \
  --pause-at-ms 1000 \
  --user-stop-at-ms 2000 \
  --words-per-second 3 \
  --drain-words-per-second 12 \
  --playback-lag-words 1

# Force FALLBACK path (pad) for calibration comparison
python3 scripts/assistant-self-asr-harness.py --mode dry-run --force-fallback
```

### TTS↔TTS dual-role path (this track)

Both roles are TTS-shaped. Assistant speaks intended LLM text; user-role TTS
(or fixture) barges in after a controlled delay. Dual clocks measure founder
cut under drain-primary.

**Barge-in trigger (clarified):** production/eval barge-in fires on
**Nemotron-transcribed user audio** (first alphanumeric token / transcript
path), same as `speech-out-live-session`. It does **not** depend on VAD
dirt/echo for this eval claim. TTS↔TTS can mimic a real conversation for that
path: user-role TTS → (mic or digital feed) → Nemotron A → barge tokens.
Acoustic echo is a **separate later suite**, not a blocker here.

```bash
python3 scripts/assistant-self-asr-harness.py --mode tts-tts

python3 scripts/assistant-self-asr-harness.py --mode tts-tts \
  --out-dir /tmp/assistant-self-asr-tts-tts \
  --intended-text "one two three four five six seven eight nine ten" \
  --user-barge-text "wait stop please" \
  --pad-words 2 \
  --pause-at-ms 1000 \
  --user-speech-ms 1000 \
  --words-per-second 3 \
  --drain-words-per-second 20 \
  --playback-lag-words 1
```

### Interactive peek TUI (product taste)

Minimum viable interactive UI inspired by `speech-out-live-session` keyboard
loop (`/dev/tty`) + debug timeline. Hear (optional) + see dual streams and the
cut decision live.

```bash
# Keypress barge-in on a real tty (b / space)
python3 scripts/assistant-self-asr-harness.py --mode tui \
  --out-dir /tmp/assistant-self-asr-tui \
  --intended-text "one two three four five six seven eight nine ten" \
  --user-barge-text "wait stop please"

# Auto barge if you want hands-free / CI-friendly timeline
python3 scripts/assistant-self-asr-harness.py --mode tui \
  --auto-barge-ms 1200 --user-speech-ms 1000

# Optional best-effort speech-out TTS (mock or real); continues if unavailable
python3 scripts/assistant-self-asr-harness.py --mode tui --play
SPEECH_OUT_PLAY_CMD='speech-out say --backend mock' \
  python3 scripts/assistant-self-asr-harness.py --mode tui --play
```

| Key | Action |
|-----|--------|
| `b` / `space` | Barge-in now (mimics first Nemotron alnum user token) |
| `u` | Finish user-role speech / `transcript_committed` early |
| `f` | Toggle force-fallback cut |
| `q` / Ctrl-C | Quit |

On barge-in the TUI freezes assistant emit, starts user-role TTS (or mock),
drains Nemotron-B during the user speech window, then shows
`primary_cut_source` + `production_cut_text` + truncated commit. Artifacts
match the tts-tts layout under `--out-dir`.

**TODO (live audio polish, not required for offline metrics):** instrumented
play sample clock, real dual Nemotron processes, speech-core-watch style
glyphs. MVP is a tight printf frame loop + keypress — not full curses TUI.

Artifacts written:

```text
assistant_intended.txt
user_barge_text.txt          # tts-tts only
production_cut_text
intended_at_playback.txt
asr_recovered_at_stop.txt
metrics.json                 # primary_cut_source, pad_words, label=eval_only
commit.json
events.jsonl
README-run.txt
```

Cut helpers (importable / unit-testable):

```text
scripts/assistant_self_asr_cut.py
  apply_production_cut(...)   # drain-primary API
  apply_founder_cut(...)      # explicit pad fallback helper
  mock_drain_during_user_speech(...)
  score_three_way(..., drain_complete=...)

scripts/assistant_self_asr_tts_eval.py
  run_dual_clock_tts_eval(DualClockConfig)
  emit_live_synth_checklist(out_dir)

scripts/assistant_self_asr_tui.py
  run_interactive_tui(TuiConfig)   # keypress barge-in peek
```

## Optional live / synth path (stub checklist)

Daemons are **not** required for the offline path. When wiring real TTS:

```bash
# Documents assistant TTS + user-role TTS barge-in checklist (no daemons contacted)
python3 scripts/assistant-self-asr-harness.py --mode live-synth --allow-live-stub

# Generic live wiring gap (playback + Nemotron-B drain)
python3 scripts/assistant-self-asr-harness.py --mode live --allow-live-stub
```

Live/synth TODO (still harness-only; no protocol edits):

1. **Assistant TTS** — speech-out daemon plays intended LLM text; instrument
   samples/chunks actually started (`eval_playback`); optional server PCM dump
   (`eval_synthesis`).
2. **User-role TTS / fixture** — after `barge_in_delay_ms`, second TTS or WAV
   fixture speaks barge-in utterance (not a real mic path).
3. **Nemotron-B drain** — align/drain already-emitted assistant audio during
   the user-role speech window; separate instance from user stream A.
4. **Finalize at user commit** — `apply_production_cut` with drain primary,
   pad fallback; write artifact dir + `metrics.json`.
5. **Reuse** existing first-alphanumeric pause in
   `scripts/speech-out-live-session.sh` when integrating with live session.

Suggested artifact layout for a future live run (same fields as offline):

```text
/tmp/assistant-self-asr-live-synth-<ts>/
  assistant_intended.txt
  user_barge_text.txt
  production_cut_text
  intended_at_playback.txt
  asr_recovered_at_stop.txt
  metrics.json
  commit.json
  events.jsonl
  README-run.txt
```

## Tests

```bash
bash tests/test-assistant-self-asr-harness.sh
```

Covers: self-check, dry-run drain-primary, force-fallback, tts-tts dual-role,
interactive tui auto-barge (no tty), live / live-synth stubs, bias labels.

## Biases / limitations (must stay labeled)

- **TTS↔TTS is not acoustic-echo truth** — both roles are synthetic; echo/VAD
  dirt is a **separate suite**. This track still validly exercises the
  **Nemotron-transcript barge-in path** (first alnum token).
- **No mic dirt required** — user barge-in may be TTS, fixture, or keypress
  stand-in for the first barge token; not a claim about acoustic isolation.
- Dry-run / tts-tts use **mock linear words/sec clocks**, not physical playback
  or multi-clock reconstruction (inquisitor R5).
- Dual-stream interference on one worker (R7) is **out of scope for v1** under
  dual-instance topology; re-measure only if streams collapse.
- Stub `asr_recovered_at_stop` offline is derived from intended text via mock
  drain, not a real second Nemotron (R4 risk if misread as truth).
- Green offline metrics are **not** production cut evidence (A9).
- `pad_words` remains config for the fallback path only.

## Related

- `docs/speech-output.md` — speech-out seam, barge-in harness notes  
- `scripts/speech-out-live-session.sh` — first alphanumeric token pause  
- `scripts/speech-out-diagnostics.py` — output-only diagnostics (no cut rule)  
- Dual-Nemotron barge-in **impl** lives on the sibling track
  (`scripts/barge-in-dual-asr-*`); this worktree owns eval harness files only.
