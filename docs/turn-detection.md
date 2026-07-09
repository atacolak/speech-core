# turn detection and `<EOU>` policy

this is the operational policy for speech-core turn-taking. keep the tui out of the normal loop; the operator surface should be transcript text plus `<EOU>`.

## invariant

silero vad is an acoustic boundary sensor. smart-turn v3 is the semantic gate.

```text
audio transport frames may be 20ms
silero inference windows are 512 samples at 16khz, about 32ms
```

we previously tried 20ms / 320-sample silero inference frames. that was wrong for this silero v4 binding/model: it did not crash, but probabilities stayed near silence even for loud speech. use the native 512-sample window.

## current vad config

```text
frame_samples = 512
frame_ms ≈ 32
SPEECH_CORE_VAD_THRESHOLD=0.5
SPEECH_CORE_VAD_ONSET_FRAMES=2          # ~64ms
SPEECH_CORE_VAD_HANGOVER_FRAMES=3       # ~96ms
SPEECH_CORE_VAD_PRE_SPEECH_FRAMES=5     # ~160ms preroll
SPEECH_CORE_VAD_SMOOTHING_ALPHA=0.1
SPEECH_CORE_VAD_STOP_THRESHOLD=0.2
SPEECH_CORE_VAD_FALLBACK_THRESHOLD=0.1
SPEECH_CORE_VAD_ACOUSTIC_FALLBACK_SILENCE_MS=3000
SPEECH_CORE_TURN_MIN_VAD_SPEECH_MS=400
```

meaning:

1. vad emits speech start after 2 consecutive smoothed speech frames.
2. vad emits speech end after 3 consecutive smoothed low-probability frames.
3. tiny vad islands under 400ms are not allowed to become semantic closure candidates.
4. if smart-turn holds incomplete and speech never resumes, low acoustic probability for 3s can close as degraded fallback.

## smart-turn config

```text
SPEECH_CORE_SMART_TURN_MODEL_PATH=~/workspace/external/smart-turn-v3/smart-turn-v3.2-cpu.onnx
SPEECH_CORE_SMART_TURN_THRESHOLD=0.5
SPEECH_CORE_SMART_TURN_TIMEOUT_MS=250
SPEECH_CORE_SMART_TURN_CPU_COUNT=1
SPEECH_CORE_SMART_TURN_MAX_AUDIO_SECS=8
SPEECH_CORE_SMART_TURN_PRE_SPEECH_MS=500
SPEECH_CORE_SMART_TURN_RECHECK_INTERVAL_MS=0
SPEECH_CORE_SMART_TURN_RECHECK_MAX_ATTEMPTS=0
SPEECH_CORE_SMART_TURN_RECHECK_OFFSETS_MS=96,192,384,768,1536
SPEECH_CORE_TURN_SEMANTIC_GATE_ENABLED=true
SPEECH_CORE_TURN_SEMANTIC_GATE_CLOSE_ENABLED=true
SPEECH_CORE_TURN_HUMAN_HOLD_SILENCE_MS=12000
```

when vad emits `vad_speech_end`, smart-turn runs on recent audio up to the decision sample. with the default 3-frame hangover, that initial decision is roughly +96ms after the acoustic end sample. if it says complete, the turn closes. if it says incomplete, immediate vad closure is suppressed; delayed semantic probes follow the geometric schedule at +192ms, +384ms, +768ms, and +1536ms unless speech resumes.

## close policy

successful semantic close:

```text
vad_speech_end
smart_turn_decision complete=true
turn_closed source=smart_turn degraded=false
```

semantic hold:

```text
vad_speech_end
smart_turn_decision complete=false
turn_eou_suppressed reason=semantic_incomplete
```

conservative acoustic fallback:

```text
vad_acoustic_fallback
turn_closed source=vad_acoustic_fallback degraded=true
```

smart-turn unavailable/error timeout fail-open:

```text
turn_closed source=vad degraded=true reason=smart_turn_unavailable_vad_fallback
```

human filler / thinking-noise event:

```text
turn_human_hold reason=speech_like_audio_without_tokens
```

this fires when vad keeps seeing speech-like audio but nemotron has not committed any new token for `SPEECH_CORE_TURN_HUMAN_HOLD_SILENCE_MS`. it is intentionally non-closing; consumers can use it later for a tiny tts nudge like "you good?" or a listening-status indicator.

## operator surface

normal use:

```bash
speech-core-live-session --mode transcript
```

or just:

```bash
speech-core-live-session
```

because transcript mode is the default now.

avoid the tui for normal agent conversation. it is diagnostic-only and has already wasted enough blood sugar.

## diagnostics if it feels wrong

recording is on by default and writes `mic.wav` in the session run dir. inspect these events around a bad boundary:

```text
vad_session_start
vad_meter
vad_speech_start
vad_speech_end
smart_turn_candidate
smart_turn_decision
smart_turn_recheck_scheduled
smart_turn_recheck_cancelled
vad_acoustic_fallback
turn_human_hold
turn_eou_suppressed
turn_closed
transcript_update
```

quick latest-session sketch:

```bash
python3 scripts/analyze-session-timeline.py --latest
```

## tuning rules

1. do not set silero inference back to 320 samples.
2. do not tune by staring at the tui.
3. do one live recording, inspect audio plus event timeline, then change one knob.
4. if starts are missed, lower threshold slightly or reduce smoothing.
5. if mid-sentence pauses close too early, increase hangover frames or fallback silence, not smart-turn threshold first.
6. if closing is too slow, reduce recheck offsets or acoustic fallback silence after confirming smart-turn is actually holding incomplete.
