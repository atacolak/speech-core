# Speech Core

ear and mouth. sibling `voicecat` is the call. headed omp TUI is the brain.

## Start here

| Need | Read |
|---|---|
| **live map (this file)** | what is on, what is lab, devices |
| **Accepted behavior truth** | [`spec/README.md`](spec/README.md) |
| Enduring purpose / authority | [`CHARTER.md`](CHARTER.md) |
| Installed defaults and honest limits | [`docs/current-state.md`](docs/current-state.md) |
| Docs index | [`docs/README.md`](docs/README.md) |
| City identity | [`rig.toml`](rig.toml) |
| Call (desk / phone) | [`../voicecat/README.md`](../voicecat/README.md) |
| Parked experiments | [`lab/README.md`](lab/README.md) |

```text
you speak
  → voicecat desk/phone          transport + hear
  → speech-in daemon :8765       VAD / ASR / turn close   (CPU)
  → transcript_committed
  → headed omp TUI               reasoner
  → speech-out :8788 leftover    speak then append
  → qwentts.cpp :18091           TTS PCM                  (GPU)
  → voicecat sink                hear
```

barge: first alphanumeric user ASR token cancels leftover. voicecat writes `barge.jsonl` then async CTC `barge-cut`. that is a TUI sidecar row, **not** speech-core-watch greying.

## Live vs lab

| live | lab (`lab/`) |
|---|---|
| `speech-core-daemon` + protocol | dual-Nemotron self-ASR |
| `speech-out` leftover WS | CUPE / Bournemouth karaoke |
| qwentts.cpp CustomVoice | CosyVoice qualification |
| Silero + smart-turn v3 | `pi --profile talker` loop |
| `scripts/barge_in_align/` warm CTC worker | laptop AEC / denoise tools |
| voicecat as the only call | `speech-out-live-session` greying TUI |

do not start from dogfood greying, CosyVoice, or dual-ASR. those are parked.

## Devices

| job | model | device |
|---|---|---|
| user ASR | `nemotron-speech-streaming-en-0.6b-Q4_K_M.gguf` via transcribe.cpp | **CPU** (`libggml-cpu`) |
| VAD | Silero v4 ONNX | CPU |
| turn close | smart-turn v3.2 ONNX | CPU (1 thread) |
| barge cut | torchaudio `WAV2VEC2_ASR_BASE_960H` | CPU (`ata-speech-align.service`) |
| TTS | qwentts.cpp 0.6B CustomVoice Q8 | **GPU** (`GGML_BACKEND=CUDA0`) |

Parakeet realtime EOU is compiled, **off**. CosyVoice3 TRT is rollback only. do not load it next to live qwentts on this 12 GB card.

## Components

```text
live
  crates/speech-core-daemon     ASR + VAD + turn
  crates/speech-core-protocol   shared messages
  crates/speech-out             leftover TTS websocket
  scripts/barge_in_align/       warm CTC worker (unix sock)

diagnostic (optional)
  crates/speech-core-watch      event TUI; not the call UI
  crates/speech-core-mic-adapter  laptop CPAL; desk pcm comes from voicecat
  crates/speech-core-file-adapter WAV replay

units (this host)
  ata-speech-core.service
  ata-speech-out.service
  ata-speech-tts.service
  ata-speech-align.service      sock: $XDG_RUNTIME_DIR/discord-voice-agent/align.sock
```

## Core invariants

- A turn, once closed, is immutable. Late punctuation does not revise operator-visible state.
- `transcript_committed` is the authoritative per-turn snapshot. Controllers dispatch on it.
- `transcript_finalized` is diagnostic-only.
- speech-in and speech-out stay separate processes.
- Physical playback stop does not wait on CTC / alignment.
- VAD proposes; smart-turn checks semantic completion; acoustic fallback prevents hangs.

## Environment

| variable | purpose |
|---|---|
| `SPEECH_CORE_WS_URL` | `ws://host:8765/ws/audio-ingress` |
| `SPEECH_OUT_WS_URL` | `ws://host:8788/ws/speech-out` |
| `SPEECH_CORE_MODEL_PATH` | Nemotron GGUF |
| `SPEECH_CORE_VAD_MODEL_PATH` | Silero VAD ONNX |
| `SPEECH_CORE_SMART_TURN_MODEL_PATH` | smart-turn-v3 ONNX |
| `SPEECH_OUT_QWENTTS_VOICE` | live named speaker (default **ryan**) |
| `SPEECH_OUT_ALIGN_SOCK` | warm CTC unix sock (voicecat default: discord-voice-agent/align.sock) |

Install scripts write URLs/paths to `~/.config/speech-core/daemon.env` and `client.env`. full defaults: [`docs/current-state.md`](docs/current-state.md).

## Run

live call is voicecat, not this repo. daemons must already be up:

```bash
systemctl --user status ata-speech-core ata-speech-out ata-speech-tts ata-speech-align
```

speech-in only (laptop mic diagnostic, not the desk):

```bash
speech-core-live-session --debug-tui
```

events:

```bash
tail -f ~/.local/state/speech-core/logs/events.jsonl
```

## CTC plug (do not break this)

voicecat does **not** import the aligner. it speaks JSONL to the warm worker:

```text
{"cmd":"align","wav":"...","intended":"...","played_ms":1234,"speed":1.0,"backend":"ctc_forced"}
→ {ok, spoken_prefix, word_index, backend_id, ...}

{"cmd":"ping"} → {ok, preloaded}
```

a nemotron-backed cut is a new `backend` behind that sock. keep the json. keep the sock path. do not fold alignment into `speech-core-daemon` as a silent second product.

## Later (not this cleanup)

- GPU nemotron + concurrent forwards (user ASR + barge cut, one weight load)
- that is a pin change: transcribe.cpp cuda, vram vs qwentts, rollback. separate bead.
