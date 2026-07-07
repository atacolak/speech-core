# turn detection and `<EOU>` policy

this doc explains exactly when the current runtime prints `<EOU>`.

## key point

turn detection is now a two-stage path when smart turn is enabled:

```text
silero vad finds an acoustic speech_end candidate
smart turn v3 decides whether the recent turn audio sounds complete
turn manager emits or suppresses turn_closed
```

silero is still the clock. smart turn is the semantic gate.

## silero vad frame mechanics

silero runs on 20ms frames:

```text
sample_rate = 16000 hz
frame_samples = 320
frame_ms = 20
```

current threshold:

```text
SPEECH_CORE_VAD_THRESHOLD=0.5
SPEECH_CORE_VAD_SMOOTHING_ALPHA=0.1
SPEECH_CORE_VAD_STOP_THRESHOLD=0.2
SPEECH_CORE_VAD_FALLBACK_THRESHOLD=0.1
```

## speech start trigger

current start config:

```text
SPEECH_CORE_VAD_ONSET_FRAMES=3
SPEECH_CORE_VAD_PRE_SPEECH_FRAMES=8
```

meaning:

1. wait for 3 consecutive smoothed speech frames.
2. emit `vad_speech_start`.
3. report the start sample with up to 8 frames / 160ms of pre-roll.

## speech end trigger

current end config:

```text
SPEECH_CORE_VAD_HANGOVER_FRAMES=10
SPEECH_CORE_VAD_ACOUSTIC_FALLBACK_SILENCE_MS=3000
```

meaning:

1. once in speech, watch for consecutive frames where smoothed probability is below `SPEECH_CORE_VAD_STOP_THRESHOLD`.
2. after 10 stopping frames, emit `vad_speech_end`.
3. `end_sample` is the first stopping frame.
4. `decision_sample` is 10 frames / ~200ms later.
5. if smart-turn keeps holding, a separate acoustic fallback can close only after 3000ms of post-end silence with smoothed vad at or below `SPEECH_CORE_VAD_FALLBACK_THRESHOLD`.

math:

```text
10 frames * 20ms = 200ms
```

## smart turn semantic gate

current smart-turn defaults:

```text
SPEECH_CORE_SMART_TURN_MODEL_PATH=/home/sf/workspace/external/smart-turn-v3/smart-turn-v3.2-cpu.onnx
SPEECH_CORE_SMART_TURN_THRESHOLD=0.5
SPEECH_CORE_SMART_TURN_TIMEOUT_MS=250
SPEECH_CORE_SMART_TURN_RECHECK_INTERVAL_MS=0
SPEECH_CORE_SMART_TURN_RECHECK_MAX_ATTEMPTS=0
SPEECH_CORE_SMART_TURN_RECHECK_OFFSETS_MS=800,1600
SPEECH_CORE_TURN_SEMANTIC_GATE_ENABLED=true
SPEECH_CORE_TURN_SEMANTIC_GATE_CLOSE_ENABLED=true
```

when vad emits `vad_speech_end`, the detector worker runs smart turn on recent audio up to `decision_sample`. with 10 hangover frames this first probe is roughly 200ms after the assumed end of speech. if smart-turn remains below threshold and no new vad speech-start arrives, delayed silence rechecks run at +800ms and +1600ms after the assumed end sample. resumed speech cancels pending probes.

smart turn sees:

```text
last up to 8 seconds of the current buffered audio
16khz mono pcm
whisper log-mel features [1,80,800]
```

then it emits:

```text
smart_turn_candidate
smart_turn_decision  # or smart_turn_timeout
turn_semantic_decision
smart_turn_recheck_scheduled | smart_turn_recheck_cancelled | smart_turn_recheck_exhausted
```

## turn close trigger

current turn config:

```text
SPEECH_CORE_TURN_VAD_CLOSE_ENABLED=true
SPEECH_CORE_TURN_MIN_VAD_SPEECH_MS=600
SPEECH_CORE_TURN_SEMANTIC_GATE_ENABLED=true
SPEECH_CORE_TURN_SEMANTIC_GATE_CLOSE_ENABLED=true
```

when `vad_speech_end` arrives, turn manager checks cumulative open-turn duration. if too short:

```text
turn_eou_suppressed reason=vad_too_short
```

if long enough and smart turn says complete:

```text
turn_eou source=smart_turn degraded=false reason=smart_turn_complete_after_vad_speech_end
turn_closed source=smart_turn degraded=false
```

if smart turn says incomplete:

```text
turn_eou_suppressed source=semantic reason=semantic_incomplete
```

then the turn remains open while waiting for either resumed speech, the delayed smart-turn recheck, or the conservative acoustic fallback:

```text
vad_acoustic_fallback
turn_closed source=vad_acoustic_fallback degraded=true reason=vad_acoustic_fallback_low_probability_silence
```

that fallback requires both long silence and smoothed vad <= `SPEECH_CORE_VAD_FALLBACK_THRESHOLD`.

if smart turn is unavailable, errors, or exceeds `SPEECH_CORE_SMART_TURN_TIMEOUT_MS`, the system fails open to vad:

```text
turn_closed source=vad degraded=true reason=smart_turn_timeout_vad_fallback
```

or:

```text
turn_closed source=vad degraded=true reason=smart_turn_unavailable_vad_fallback
```

## event ordering

model and detector workers are separate threads. current code uses `ModelProgressMap` so turn manager waits for nemotron to catch up to the vad `end_sample` before emitting `turn_closed`.

that preserves the important user-facing ordering:

```text
final transcript update before <EOU>
```

## tuning knobs

### reduce premature eou

smart turn should already reduce mid-sentence pause closures. if it is still too eager:

do not paper over this first by raising the smart-turn threshold. keep semantic threshold at `0.5` unless a reference-model comparison proves calibration drift. first raise vad hangover:

```bash
SPEECH_CORE_VAD_HANGOVER_FRAMES=24 ./scripts/install-sfub-daemon.sh   # ~480ms
```

### reduce end delay

lower vad hangover:

```bash
SPEECH_CORE_VAD_HANGOVER_FRAMES=12 ./scripts/install-sfub-daemon.sh   # ~240ms
```

or lower smart turn timeout:

```bash
SPEECH_CORE_SMART_TURN_TIMEOUT_MS=100 ./scripts/install-sfub-daemon.sh
```

but be real: if smart turn often times out, lowering timeout only makes it behave like vad again.

### log-only smart turn

run smart turn but do not let incomplete decisions suppress vad:

```bash
SPEECH_CORE_TURN_SEMANTIC_GATE_ENABLED=true \
SPEECH_CORE_TURN_SEMANTIC_GATE_CLOSE_ENABLED=false \
./scripts/install-sfub-daemon.sh
```

## event names to inspect

```text
vad_speech_start
vad_speech_end
smart_turn_candidate
smart_turn_decision
smart_turn_timeout
turn_semantic_decision
turn_eou_candidate
turn_eou_suppressed
turn_eou
turn_closed
transcript_update
```

quick log inspection:

```bash
python3 - <<'PY'
import json, pathlib, collections
p = pathlib.Path.home() / '.local/state/speech-core/logs/events.jsonl'
sessions = collections.OrderedDict()
for line in p.open(errors='replace'):
    try:
        o = json.loads(line)
    except Exception:
        continue
    sid = o.get('stream_session_id')
    if sid and o.get('stream_id') == 'sfnix.live_mic':
        sessions.setdefault(sid, []).append(o)
if sessions:
    sid, evs = next(reversed(sessions.items()))
    print('session', sid)
    for e in evs:
        if e.get('event') in {'vad_speech_start','vad_speech_end','smart_turn_decision','smart_turn_timeout','turn_eou_suppressed','turn_closed'}:
            print(e)
PY
```
