# session handoff

use this file when starting a fresh assistant session.

## current repo

```text
/home/sf/workspace/speech-core
```

this repo is git-tracked. current runtime source of truth is this repo, especially:

```text
README.md
docs/current-state.md
docs/turn-detection.md
docs/smart-turn-v3.md
docs/seams.md
```

older planning docs live at:

```text
/home/sf/workspace/docs/speech-core
```

they contain useful history, but some sections are stale because implementation moved to rust and parakeet realtime eou was retired.

## current runtime summary

```text
sfnix mic adapter -> sfub daemon -> nemotron transcript + silero vad acoustic boundary + smart-turn-v3 semantic gate -> watcher prints transcript + <EOU>
```

parakeet realtime eou is disabled by default.

smart turn v3 is direct rust onnx, no python sidecar. default artifact:

```text
/home/sf/workspace/external/smart-turn-v3/smart-turn-v3.2-cpu.onnx
```

## recent decisions to know

- parakeet realtime eou was retired from default runtime because live laptop evidence was noisy.
- `ModelProgressMap` was added so `turn_closed` waits for nemotron model catch-up before `<EOU>` is emitted.
- `SPEECH_CORE_TURN_MIN_VAD_SPEECH_MS` is now 300ms, not 700ms.
- smart turn v3 runs on vad speech_end candidates and can suppress vad closure when it predicts incomplete.
- smart turn timeout/unavailable/error fails open to vad closure to protect latency.

## if the user asks “why is `<EOU>` firing while i am still speaking?”

answer:

- `<EOU>` is printed on `turn_closed`.
- a clean semantic close is `source=smart_turn`.
- fallback close is `source=vad` if smart turn is unavailable, errors, or times out.
- inspect `turn_closed.source`, `turn_closed.reason`, and nearby `smart_turn_decision` / `turn_eou_suppressed` events before guessing.

quick command:

```bash
python3 - <<'PY'
import json, pathlib, collections
p = pathlib.Path.home() / '.local/state/speech-core/logs/events.jsonl'
sessions = collections.OrderedDict()
for line in p.open(errors='replace'):
    try: o = json.loads(line)
    except Exception: continue
    sid = o.get('stream_session_id')
    if sid and o.get('stream_id') == 'sfnix.live_mic':
        sessions.setdefault(sid, []).append(o)
if sessions:
    sid, evs = next(reversed(sessions.items()))
    print('session', sid)
    for e in evs:
        if e.get('event') in {'vad_speech_end','smart_turn_decision','smart_turn_timeout','turn_eou_suppressed','turn_closed'}:
            print(e)
PY
```

## useful verification

```bash
cargo check -p speech-core-daemon
cargo test --workspace
SPEECH_CORE_SMART_TURN_MODEL_PATH=/home/sf/workspace/external/smart-turn-v3/smart-turn-v3.2-cpu.onnx \
  cargo test -p speech-core-daemon real_model_smoke_when_env_set -- --nocapture
```

## do not do this blindly

- do not re-enable parakeet realtime eou by default.
- do not remove vad; smart turn is designed to run after vad silence candidates, not continuously.
- do not add a python smart-turn sidecar. user explicitly wanted rust onnx directly.
- do not assume cross-host monotonic clocks are comparable.
- do not treat transcript text as sample-accurate unless token timestamps or alignment prove it.
