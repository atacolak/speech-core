# sts-peek UI (Track U) — live observability

Thin operator surface for the live STS peek. It **composes** `speech-core-watch`
for real energy / RMS / VAD / smart-turn glyphs and overlays harness status from
a Track L `run_dir`. It does **not** reimplement the watch TUI or edit the
`speech-core-watch` crate.

## What is real vs mock

| Surface | Source | When |
|--------|--------|------|
| Energy / VAD bars, smart-turn glyphs (`◖◗①②③④◆◇`) | `speech-core-watch --mode debug` (or `tui`) | Live attach or `--replay-events` |
| Barge mark, human-mode, user-stop | Keys → `run_dir/control/*` | Always when `--run-dir` set |
| Intended text | `run_dir/intended.txt` (or `assistant_intended.txt`) | When Track L writes it |
| Drain / cut line, `primary_cut_source` | `run_dir/cut/metrics.json` (+ optional `production_cut_text`) | When Track C writes it |
| Fake meters | Internal `MockMeters` | `--mock` only (offline tests) |

## Files

```text
scripts/sts-peek/ui.py       # UI loop, watch composer, run_dir overlay
scripts/sts-peek/run_ui.py   # entrypoint
scripts/sts-peek/keys.py     # /dev/tty + scripted keys
docs/sts-peek-ui.md          # this file
tests/test-sts-peek-ui.sh    # offline mock acceptance
```

## Keys (`/dev/tty`)

| Key | Action | Control file |
|-----|--------|--------------|
| `b` / space | Barge | touch `run_dir/control/barge.now` |
| `u` | User-stop (optional) | touch `run_dir/control/user_stop.now` (also barges if not yet) |
| `h` | Human-mode toggle | write `run_dir/control/human.mode` (`1` / `0`) |
| `q` / Ctrl-C | Quit | — |

Track L should poll `control/barge.now` (mtime or existence) and cancel
assistant speech-out when it appears.

## Pointing at a Track L run_dir

Track L owns the session directory. Minimal layout the UI understands:

```text
$RUN_DIR/
  intended.txt              # optional; also accepts assistant_intended.txt
  watch.jsonl               # optional; used for --replay-events default
  ui-events.jsonl           # UI appends barge/human/user-stop events
  control/
    barge.now               # UI writes on b/space
    user_stop.now           # UI writes on u
    human.mode              # UI writes 1/0 on h
    state.txt               # optional harness phase string (Track L)
  cut/
    metrics.json            # Track C: primary_cut_source, production_cut_text, …
    production_cut_text     # optional plain-text cut
```

### Live attach (preferred)

With daemons up and Track L running into `$RUN_DIR`:

```bash
python3 scripts/sts-peek/run_ui.py \
  --run-dir "$RUN_DIR" \
  --stream-id "${SPEECH_CORE_STREAM_ID:-laptop.live_mic}" \
  --stream-session-id "$SPEECH_CORE_STREAM_SESSION_ID" \
  --watch-mode debug
```

This launches `speech-core-watch --mode debug` with the same stream filters the
live-session harnesses use. The UI paints watch stdout (glyphs / live vad) and
the harness overlay (barge, cut, intended).

Env passthrough:

- `SPEECH_CORE_WS_URL`
- `SPEECH_CORE_STREAM_ID`
- `SPEECH_CORE_STREAM_SESSION_ID`

### Replay / offline attach to a recorded session

```bash
python3 scripts/sts-peek/run_ui.py \
  --run-dir "$RUN_DIR" \
  --replay-events "$RUN_DIR/watch.jsonl" \
  --watch-mode debug
```

If `--watch-jsonl` / `--replay-events` is omitted but `$RUN_DIR/watch.jsonl`
exists, the UI will prefer replaying that file when starting watch.

### Offline mock (no daemons)

```bash
python3 scripts/sts-peek/run_ui.py --mock --key-script 'sleep:0.1,b,sleep:0.2,h,q'
# or
python3 scripts/sts-peek/ui.py --self-check
./tests/test-sts-peek-ui.sh
```

Mock mode draws synthetic RMS / energy / VAD bars and smart-turn glyphs so key
handling and control-file wiring can be tested in CI.

## speech-core-watch composition (no crate edits)

The UI shells out to the existing binary:

```text
speech-core-watch \
  --mode debug \
  [--stream-id ID] \
  [--stream-session-id SID] \
  [--url WS] \
  [--replay-events PATH]
```

Binary resolution order: `--watch-bin` → `$PATH` → `target/debug/speech-core-watch`.

If watch cannot be launched, live mode degrades to mock meters and surfaces
`watch_error` in the summary line — it does not edit `crates/speech-core-watch`.

## Acceptance

1. Offline mock UI test passes (scripted keys).
2. Docs explain live attach + barge control file (this page).
3. No crate edits.
4. Commit on `feature/sts-peek-ui` only.

## Related

- Plan: `/tmp/organism-control-plane/live-sts-peek-plan.md` (Track U)
- Manual watch modes: `docs/current-state.md` (`--debug-tui`, speech-out-live-session)
- Existing printf peek (frozen): `scripts/assistant_self_asr_tui.py`
