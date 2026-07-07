# speech-core current state

this is the reality as of the current working tree. if another doc disagrees, trust this file and the repo code first.

## one-line summary

`speech-core` is a rust speech runtime: laptop mic audio streams to the sfub daemon, nemotron produces low-latency transcript text, silero vad marks acoustic pauses, and smart turn v3 can semantically gate turn closure.

## current live path

```text
sfnix laptop
  speech-core-mic-adapter
    captures cpal mic audio as 16khz mono pcm_s16le
    sends websocket audio frames
      ↓
sfub
  speech-core-daemon
    validates frame/session metadata
    writes jsonl event log
    feeds nemotron streaming asr
    feeds silero vad
    buffers recent audio for smart turn v3
    on vad speech_end, runs smart turn v3 once on recent turn audio
    turn manager promotes accepted boundaries into turn_closed
      ↓
sfnix or sfub
  speech-core-watch / speech-core-live-session
    prints transcript text
    prints <EOU> when turn_closed arrives
```

## current defaults

installed daemon defaults:

```text
SPEECH_CORE_STREAM_CHUNK_MS=160
SPEECH_CORE_ATT_CONTEXT_RIGHT=1
SPEECH_CORE_VAD_THRESHOLD=0.5
SPEECH_CORE_VAD_ONSET_FRAMES=2
SPEECH_CORE_VAD_HANGOVER_FRAMES=3
SPEECH_CORE_VAD_SMOOTHING_ALPHA=0.1
SPEECH_CORE_VAD_STOP_THRESHOLD=0.2
SPEECH_CORE_VAD_FALLBACK_THRESHOLD=0.1
SPEECH_CORE_VAD_ACOUSTIC_FALLBACK_SILENCE_MS=3000
SPEECH_CORE_TURN_MIN_VAD_SPEECH_MS=600
SPEECH_CORE_TURN_VAD_CLOSE_ENABLED=true
SPEECH_CORE_SMART_TURN_MODEL_PATH=/home/sf/workspace/external/smart-turn-v3/smart-turn-v3.2-cpu.onnx
SPEECH_CORE_SMART_TURN_THRESHOLD=0.5
SPEECH_CORE_SMART_TURN_TIMEOUT_MS=250
SPEECH_CORE_SMART_TURN_CPU_COUNT=1
SPEECH_CORE_SMART_TURN_RECHECK_OFFSETS_MS=96,192,384,768,1536
SPEECH_CORE_TURN_SEMANTIC_GATE_ENABLED=true
SPEECH_CORE_TURN_SEMANTIC_GATE_CLOSE_ENABLED=true
SPEECH_CORE_TURN_HUMAN_HOLD_SILENCE_MS=12000
SPEECH_CORE_EOU_MODEL_DIR=
SPEECH_CORE_TURN_MODEL_EOU_CLOSE_ENABLED=false
```

important translation:

- nemotron runs every ~160ms of audio with ~80ms right-context.
- silero vad uses its native 512-sample inference window at 16khz, about 32ms. transport frames may still be 20ms.
- vad starts speech after 2 smoothed speech frames, roughly 64ms above threshold.
- vad ends speech after 3 smoothed stopping frames, roughly 96ms below stop threshold.
- turn manager ignores vad segments whose current vad segment duration is under 600ms.
- smart turn runs after vad speech_end. with the default 3-frame hangover the first probe is at about +96ms after the assumed end sample; if incomplete and no speech resumes, the geometric schedule preserves checks at +192ms, +384ms, +768ms, and +1536ms.
- if speech-like vad islands continue for 12s after the last committed transcript token without new tokens, the daemon emits `turn_human_hold`; this does not close the turn.
- smart turn timeout/unavailable/error fails open to vad close.
- parakeet realtime eou is disabled by default.

## what `<EOU>` means right now

`<EOU>` in the watcher means `turn_closed`.

with smart turn enabled, a normal successful semantic close is:

```text
silero vad emitted vad_speech_end
smart turn v3 classified the recent turn audio as complete
turn manager emitted turn_closed source=smart_turn degraded=false
```

fallback close is still possible:

```text
silero vad emitted vad_speech_end
smart turn timed out / was unavailable / failed
turn manager emitted turn_closed source=vad degraded=true
```

incomplete semantic decisions suppress immediate vad close. if the delayed recheck still holds and speech does not resume, acoustic fallback can close only after 3000ms of low-probability silence:

```text
vad_acoustic_fallback
turn_closed source=vad_acoustic_fallback degraded=true
```

## why smart turn v3 is different from parakeet realtime eou

parakeet realtime eou emitted raw rnnt tokens during streaming and was noisy in live laptop use.

smart turn v3 is audio-native endpoint classification:

- input: last 8 seconds of 16khz mono audio as whisper log-mel features `[1,80,800]`.
- output: one completion probability.
- invoked only on vad speech_end candidates.
- no tokenizer, no transcript sidecar, no python process.

this is less magical and less chatty. good.

## what works

- websocket audio transport from sfnix to sfub.
- native nixos build/install path for the laptop client.
- sfub systemd user service for the daemon.
- nemotron streaming transcript.
- silero vad acoustic pauses.
- smart turn v3 direct rust onnx semantic endpoint gate.
- transcript-before-`<EOU>` event ordering via `ModelProgressMap` wait.
- clean live watcher output: transcript plus `<EOU>`.
- jsonl event log for debugging.

## what is still rough

- smart turn v3 needs live laptop validation across actual conversational pauses.
- smart turn preprocessing is implemented directly in rust; parity against python is smoke-tested through the real model, not numerically golden-tested against transformers.
- cross-host capture latency is preserved but not calibrated.
- docs under `/home/sf/workspace/docs/speech-core` contain older planning/spec history; useful archaeology, not current runtime source of truth.

## useful commands

sfub daemon:

```bash
systemctl --user status speech-core-daemon.service
systemctl --user restart speech-core-daemon.service
journalctl --user -u speech-core-daemon.service -f
cat ~/.config/speech-core/daemon.env
```

sfnix laptop:

```bash
speech-core-live-session
cat ~/.config/speech-core/client.env
systemctl --user status speech-core-mic-adapter.service
```

repo:

```bash
cargo test --workspace
SPEECH_CORE_SMART_TURN_MODEL_PATH=/home/sf/workspace/external/smart-turn-v3/smart-turn-v3.2-cpu.onnx \
  cargo test -p speech-core-daemon real_model_smoke_when_env_set -- --nocapture
./scripts/install-sfub-daemon.sh
./scripts/sfnix-sync-build-adapter.sh
```
