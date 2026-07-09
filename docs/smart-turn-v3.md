# smart turn v3 endpointing

`speech-core` can use `pipecat-ai/smart-turn-v3` as a semantic gate on top of silero vad.

plainly: silero still finds acoustic silence quickly. smart turn then looks at the recent turn audio and decides whether the speaker sounds complete or merely paused.

## model artifact

current default path used by the server scripts:

```text
~/workspace/external/smart-turn-v3/smart-turn-v3.2-cpu.onnx
```

source:

```text
https://huggingface.co/pipecat-ai/smart-turn-v3/resolve/main/smart-turn-v3.2-cpu.onnx
```

observed sha256:

```text
2bb026316b14a660486a75b1733cd3fbab8c2fd0314dc9af7be49f8cca967e4f
```

model contract:

```text
input:  input_features f32 [1, 80, 800]
output: logits         f32 [1, 1]
```

`logits` is already a completion probability in practice. default threshold is `0.5`.

## preprocessing

smart turn v3 is audio-native. no tokenizer. no transcript sidecar. no python.

runtime preprocessing in rust mirrors whisper feature extraction:

- 16khz mono pcm audio.
- keep the last 8 seconds, or left-pad shorter audio with zeros.
- zero-mean/unit-variance normalize the waveform.
- centered stft with reflect padding.
- `n_fft=400`, `hop_length=160`, periodic hann window.
- 80-bin slaney mel filterbank, 0hz-8khz.
- log10 mel, clamp to max dynamic range of 8.0, scale with `(x + 4.0) / 4.0`.

## runtime policy

important: smart turn does not replace vad frame processing.

flow:

```text
audio frame
  -> silero vad sees speech_start/speech_end
  -> smart turn buffers recent audio
  -> on vad_speech_end, smart turn runs once on recent audio through decision_sample
  -> turn manager receives the semantic decision before handling the vad end
  -> turn manager waits for nemotron model progress before visible close
```

when semantic gate close is enabled:

- `complete=true`: close turn with `source=smart_turn`, `degraded=false`.
- `complete=false`: suppress vad close with `reason=semantic_incomplete`; keep the turn open.
- unavailable/error/timeout: fail open to vad close so latency does not get worse than the previous path.

## config

```bash
SPEECH_CORE_SMART_TURN_MODEL_PATH=~/workspace/external/smart-turn-v3/smart-turn-v3.2-cpu.onnx
SPEECH_CORE_SMART_TURN_THRESHOLD=0.5
SPEECH_CORE_SMART_TURN_TIMEOUT_MS=250
SPEECH_CORE_SMART_TURN_CPU_COUNT=1
SPEECH_CORE_SMART_TURN_MAX_AUDIO_SECS=8
SPEECH_CORE_SMART_TURN_PRE_SPEECH_MS=500
SPEECH_CORE_TURN_SEMANTIC_GATE_ENABLED=true
SPEECH_CORE_TURN_SEMANTIC_GATE_CLOSE_ENABLED=true
```

`install-speech-core-daemon.sh` and `start-speech-core-daemon.sh` default the model path and semantic gate to enabled. set `SPEECH_CORE_TURN_SEMANTIC_GATE_CLOSE_ENABLED=false` to log decisions without letting them suppress vad closure.

## events

new events:

```text
smart_turn_session_start
smart_turn_candidate
smart_turn_decision
smart_turn_timeout
smart_turn_session_end
turn_semantic_decision
```

accepted smart-turn closure still emits the normal:

```text
turn_eou
turn_closed
```

with:

```text
source=smart_turn
detector=pipecat_smart_turn_v3
degraded=false
reason=smart_turn_complete_after_vad_speech_end
```

## verification

compile/tests:

```bash
cargo check -p speech-core-daemon
cargo test --workspace
```

real onnx smoke:

```bash
SPEECH_CORE_SMART_TURN_MODEL_PATH=~/workspace/external/smart-turn-v3/smart-turn-v3.2-cpu.onnx \
  cargo test -p speech-core-daemon real_model_smoke_when_env_set -- --nocapture
```
