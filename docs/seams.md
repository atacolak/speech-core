# speech-core seams

this is the map for future work. a seam is a boundary where one component promises a shape to another component.

## 1. adapter seam

producer:

```text
crates/speech-core-mic-adapter
crates/speech-core-file-adapter
```

consumer:

```text
crates/speech-core-daemon websocket ingress
```

contract:

- adapter sends `hello` first.
- adapter sends binary audio frames using the `SCF1` envelope.
- every frame has stream/session ids, sequence number, sample offset, sample count, format, rate, channels, timing provenance, and pcm payload.
- current live mic format is `pcm_s16le`, 16khz, mono.

important ids:

```text
stream_id             stable logical source, e.g. sfnix.live_mic
stream_session_id     one capture run/process/session
adapter_id            capture implementation/device identity
seq                   frame sequence inside the session
source_sample_start   adapter-side sample clock
```

files:

```text
crates/speech-core-protocol/src/lib.rs
crates/speech-core-mic-adapter/src/main.rs
crates/speech-core-file-adapter/src/main.rs
```

## 2. daemon ingress seam

producer:

```text
websocket handler in crates/speech-core-daemon/src/main.rs
```

consumers:

```text
jsonl logger
nemotron model worker
detector worker
```

contract:

- reject binary audio before `hello`.
- validate frame metadata against the session hello.
- preserve sequence gaps and sample gaps.
- stamp daemon-side ingress timings.
- broadcast/log jsonl events.

important event names:

```text
stream_start
hello_ack
audio_frame_ingested
audio_gap
audio_sample_gap
```

## 3. model worker seam

producer:

```text
daemon ingress sends audio frames to model worker
```

consumer/backend:

```text
transcribe.cpp via crates/speech-core-daemon/native/transcribe_shim.cpp
```

contract:

- model worker accepts only 16khz mono `pcm_f32le` or `pcm_s16le` converted to f32.
- one transcribe.cpp stream per `stream_session_id`.
- worker serializes backend compute; websocket receive must not run inference inline.
- text pointers from transcribe.cpp must be copied before stream mutation.

current model:

```text
nemotron-speech-streaming-en-0.6b-Q4_K_M.gguf
stream_chunk_ms=160
att_context_right=1
```

events:

```text
model_session_start
model_chunk_processed
transcript_token_committed
transcript_update
model_error
```

files:

```text
crates/speech-core-daemon/src/model.rs
crates/speech-core-daemon/native/transcribe_shim.cpp
crates/speech-core-daemon/native/transcribe_shim.h
```

## 4. detector seam

producer:

```text
daemon ingress sends audio frames to DetectorWorker
```

consumers:

```text
SileroVadDetector
SmartTurnDetector
ParakeetEouDetector, disabled by default
TurnManager
```

contract:

- detectors see normalized per-frame audio.
- detectors emit `DetectorSignal` values, not final policy decisions.
- turn manager is the only component that promotes detector evidence into turn closure.
- detectors can receive `DetectorAction` feedback from the turn manager.

current default detectors:

```text
silero vad: enabled
smart turn v3: enabled when SPEECH_CORE_SMART_TURN_MODEL_PATH exists
parakeet realtime eou: disabled
```

smart turn detail:

- it buffers recent audio in the detector worker.
- it runs direct rust onnx inference on vad `speech_end` candidates.
- it emits semantic evidence before the turn manager handles the vad end.
- it does not run as a python sidecar and does not consume transcript text.

files:

```text
crates/speech-core-daemon/src/detectors/mod.rs
crates/speech-core-daemon/src/detectors/vad.rs
crates/speech-core-daemon/src/detectors/smart_turn.rs
crates/speech-core-daemon/src/detectors/turn.rs
crates/speech-core-daemon/src/detectors/parakeet_eou.rs
```

## 5. turn policy seam

producer:

```text
TurnManager
```

consumer:

```text
watcher / future apps / future agent runtime
```

contract:

- `turn_eou_candidate` means some detector proposed a boundary.
- `turn_eou_suppressed` means the turn manager rejected it.
- `turn_eou` means accepted boundary evidence.
- `turn_closed` means the turn is closed for consumers.

current accepted sources:

```text
source=smart_turn
degraded=false
reason=smart_turn_complete_after_vad_speech_end
```

or fallback:

```text
source=vad
degraded=true
reason=smart_turn_timeout_vad_fallback | smart_turn_unavailable_vad_fallback | vad_speech_end
```

smart turn incomplete decisions suppress vad closure with `turn_eou_suppressed source=semantic reason=semantic_incomplete` and leave the turn open.

## 6. event subscription seam

producer:

```text
JsonlLogger in speech-core-daemon
```

consumer:

```text
speech-core-watch compact tui
future apps/agents
```

contract:

- events are written to jsonl.
- events are also broadcast over websocket when a client sends `SubscribeEvents`.
- subscriber can filter by stream/session.

watch modes:

```text
speech-core-watch --mode tui          # compact symbolic turn surface, default for live sessions
speech-core-watch --mode debug        # tui plus recent seam explanations
speech-core-watch --mode transcript   # clean transcript + <EOU>
speech-core-watch --mode jsonl        # raw event stream
speech-core-watch --verbose           # legacy debug in transcript mode
```

## 7. install/runtime seam

sfub daemon:

```text
scripts/install-sfub-daemon.sh
~/.config/speech-core/daemon.env
~/.config/systemd/user/speech-core-daemon.service
~/.local/state/speech-core/logs/events.jsonl
```

sfnix client:

```text
scripts/sfnix-sync-build-adapter.sh
scripts/install-sfnix-client.sh
~/.config/speech-core/client.env
~/.local/bin/speech-core-live-session
```

installed defaults should be inspected from env files, not guessed.
