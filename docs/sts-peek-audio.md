# sts-peek audio + session layer (Track L)

**Branch:** `feature/sts-peek-audio`  
**Label:** harness / `live_peek` — no shared protocol schema edits  
**Plan:** `/tmp/organism-control-plane/live-sts-peek-plan.md` Phase 1 Track L  
**Contract:** speech-to-speech-contract.md rev 4 (dual Nemotron)

This document is the **audio/session contract** for sibling tracks:

| Track | Owner paths | Consumes |
|-------|-------------|----------|
| **L (this)** | `scripts/sts-peek/{session,audio,run_audio}.py` | speech-out play/cancel |
| **U** | `scripts/sts-peek/ui*` | `run_dir`, `stream_session_id`, `control/barge.now`, `events.jsonl` |
| **C** | `scripts/sts-peek/cut*` | same + cancel timestamps / intended text |

## Goal

Headless (or UI-driven) live audio session that:

1. Speaks **assistant intended text** via speech-out and plays audio.
2. On **barge** (control file or `--barge-after-ms`): cancels assistant playback.
3. Speaks **user-role TTS** with a distinct voice when possible, **or**
4. **`--human`:** after barge, do **not** play user TTS; leave mic path for operator.

## How to run

### Offline smoke (no daemons)

```bash
python3 scripts/sts-peek/run_audio.py --mock-audio \
  --intended-text "hello from the assistant for barge testing" \
  --user-tts-text "wait stop" \
  --barge-after-ms 200 \
  --out-dir /tmp/sts-peek-mock

# Human mode stub (no user TTS after barge)
python3 scripts/sts-peek/run_audio.py --mock-audio --human \
  --intended-text "hello" \
  --barge-after-ms 150 \
  --out-dir /tmp/sts-peek-human

# Layout contract only
python3 scripts/sts-peek/run_audio.py --print-layout
```

Tests:

```bash
./tests/test-sts-peek-audio.sh
```

### Live (daemons up)

```bash
# Server: speech-out daemon on :8788 (and speech-core if feeding ASR later)
# Client:
SPEECH_OUT_WS_URL=ws://127.0.0.1:8788/ws/speech-out \
SPEECH_OUT_PLAY_COMMAND=pw-play \
python3 scripts/sts-peek/run_audio.py \
  --intended-text "Long assistant utterance you can barge into mid sentence." \
  --user-tts-text "wait stop please" \
  --assistant-voice M1 \
  --user-voice F1 \
  --barge-after-ms 1500 \
  --out-dir /tmp/sts-peek-live

# Manual barge instead of schedule:
#   run without --barge-after-ms, then in another shell:
#   touch /tmp/sts-peek-live/control/barge.now
```

If daemons are down and `--mock-audio` is **not** set, the CLI exits **2** with a clear error and still writes `run_dir` + `probes.json`.

### Optional synthesis record

```bash
python3 scripts/sts-peek/run_audio.py \
  --record-synthesis \
  --intended-text "..." --barge-after-ms 800
# Spawns scripts/barge-in-dual-asr/record_client.py as subprocess into run_dir/record/
# (does not modify barge-in-dual-asr sources)
```

## Published IDs

| Field | Value / source |
|-------|----------------|
| `stream_session_id` | `sts-peek-<epoch>-<hex>` (override with `--stream-session-id`) |
| `user_stream_id` | `laptop.live_mic` or `SPEECH_CORE_STREAM_ID` |
| `assistant_stream_id` | `assistant.self_asr` (Nemotron B feed later) |
| `core_ws` | `SPEECH_CORE_WS_URL` / `--core-url` |
| `out_ws` | `SPEECH_OUT_WS_URL` / `--out-url` |

Also written to `run_dir/session.json` and `run_dir/params.env` for UI/cut attach.

## run_dir layout

```text
run_dir/
  params.env                 # shell-sourceable knobs + stream ids
  session.json               # full meta + path map + barge contract
  assistant_intended.txt
  user_text.txt              # empty when --human
  events.jsonl               # harness-local session log (not protocol)
  probes.json                # binary + TCP readiness
  audio_result.json          # sequence summary
  README-run.txt
  control/
    barge.now                # UI: touch this on keypress to barge
  pids/
    assistant_play.pid
    user_play.pid
    record_client.pid
  speech_out/
    assistant_events.jsonl   # speech-out play stdout/stderr stream
    user_events.jsonl
  cancel/
    assistant_cancel.json    # cancel timestamp, reason, play_pid
  record/                    # optional record_client capture
  mic/                       # human-mode stub + launch-mic-adapter.sh
```

### Barge contract (UI attach)

- **Prefer:** UI touches `run_dir/control/barge.now` on `b` / space.
- **Headless:** `--barge-after-ms N` writes the same file after N ms.
- Audio loop polls the flag; on observe it cancels assistant `speech-out play` (process-tree TERM→KILL, same idea as `speech-out-live-session.sh`).

### events.jsonl (harness-local)

Not a protocol change. Typical sequence:

```text
session_created
probes
assistant_speak_started
assistant_play_pid
barge_watch_started
barge_flag_written | barge_flag_observed
assistant_cancel_requested
assistant_playback_cancelled
user_speak_started | user_path_human
user_speak_finished (TTS path)
audio_sequence_complete
```

Each line includes `diagnostic_mono_ns`, `diagnostic_clock_origin=harness_local_monotonic`, `stream_session_id`, `label`.

## Voices

| Role | CLI | Default env | Default |
|------|-----|-------------|---------|
| Assistant | `--assistant-voice` | `STS_PEEK_ASSISTANT_VOICE` / `SPEECH_OUT_VOICE` | `M1` |
| User TTS | `--user-voice` | `STS_PEEK_USER_VOICE` / `SPEECH_OUT_USER_VOICE` | `F1` |

**Single-voice limitation:** If Supertonic only has one working voice (or F1 is missing), both roles may sound the same. The harness still tags roles separately in events and `session.json`; it does not invent protocol fields for role.

## Human mode

`--human` / `STS_PEEK_HUMAN_MODE=1`:

- After barge: **no** user-role TTS.
- Writes `mic/human_mode.json` documenting the operator mic path.
- If `speech-core-mic-adapter` is on PATH / target/{debug,release}, writes executable `mic/launch-mic-adapter.sh` with `stream_id` + `stream_session_id` already set.
- Operator (or Track U) starts mic; user `transcript_committed` remains on the speech-in path (Nemotron A). Track C cut coordinator attaches separately.

## Manual live checklist

1. Start speech-out daemon (server) on `:8788`.
2. Optional: speech-core for later ASR feed / human mic.
3. `cargo build -p speech-out` (and mic/file adapters if needed).
4. Run `run_audio.py` with real `--out-url` (no `--mock-audio`).
5. Hear assistant TTS; wait for scheduled barge or `touch …/control/barge.now`.
6. Confirm playback stops; hear user TTS (or mic open in `--human`).
7. Inspect `cancel/assistant_cancel.json`, `events.jsonl`, PIDs cleared.

## What this track does **not** do

- No cut decision / `commit.json` (Track C).
- No watch TUI / keyboard loop (Track U) — only the barge file contract.
- No edits to `scripts/barge-in-dual-asr*`, `scripts/assistant-self-asr*`, or `crates/**`.
- No speech-core-protocol / daemon event vocabulary changes.
- No automatic dual-Nemotron B drain loop (record_client is optional capture only).

## Related

- `docs/speech-output.md` — speech-out WS play / cancel semantics  
- `docs/barge-in-dual-asr.md` — dual Nemotron record/feed helpers  
- `scripts/speech-out-live-session.sh` — kill_tree + first-alnum pause patterns  
