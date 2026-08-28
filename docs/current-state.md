# Speech Core current state

**status:** maintained current-implementation guide
**authority:** repository code and installed configuration establish current reality; this document explains them

If this file disagrees with code, tests, traces, or installed configuration, treat the mismatch as a documentation defect. Do not average conflicting values.

## one-line summary

`speech-core` is the spoken substrate: voicecat (or a diagnostic mic adapter) sends 16 kHz mono PCM in; nemotron (CPU) transcribes; silero + smart-turn close the turn; leftover qwentts (GPU) speaks. the live *call* is sibling voicecat + headed omp TUI, not `speech-out-live-session`.

## current live path

```text
voicecat desk /ws-phone
  16 kHz mono PCM
    ↓
ata-speech-core.service :8765
  nemotron ASR (CPU, transcribe.cpp ggml-cpu)
  silero vad
  smart-turn v3
  transcript_committed → turn_closed
    ↓
voicecat collab guest "desk"     headed omp TUI (brain)
    ↓
ata-speech-out.service :8788     leftover speak/append
  ata-speech-tts.service :18091  qwentts.cpp CUDA
    ↓
voicecat browser_sink / phone_sink

barge cut (async, not on the stop path)
  voicecat retains leftover PCM
  ata-speech-align.service       wav2vec2 CTC (CPU unix sock)
  barge.jsonl → barge-cut sidecar for the TUI row
```

laptop `speech-core-mic-adapter` + `speech-core-watch` still exist as a **diagnostic** loop. they are not the desk.

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
SPEECH_CORE_VAD_ACOUSTIC_FALLBACK_SILENCE_MS=2500
SPEECH_CORE_TURN_MIN_VAD_SPEECH_MS=400
SPEECH_CORE_TURN_VAD_CLOSE_ENABLED=true
SPEECH_CORE_SMART_TURN_MODEL_PATH=~/workspace/external/smart-turn-v3/smart-turn-v3.2-cpu.onnx
SPEECH_CORE_SMART_TURN_THRESHOLD=0.5
SPEECH_CORE_SMART_TURN_TIMEOUT_MS=250
SPEECH_CORE_SMART_TURN_CPU_COUNT=1
SPEECH_CORE_SMART_TURN_RECHECK_OFFSETS_MS=96,192,384,768,1536
SPEECH_CORE_TURN_SEMANTIC_GATE_ENABLED=true
SPEECH_CORE_TURN_SEMANTIC_GATE_CLOSE_ENABLED=true
SPEECH_CORE_TURN_HUMAN_HOLD_SILENCE_MS=7500
SPEECH_CORE_TURN_TRANSCRIPT_SILENCE_CLOSE_MS=700
SPEECH_CORE_EOU_MODEL_DIR=
SPEECH_CORE_TURN_MODEL_EOU_CLOSE_ENABLED=false
```

important translation:

- nemotron runs every ~160ms of audio with ~80ms right-context.
- silero vad uses its native 512-sample inference window at 16khz, about 32ms. transport frames may still be 20ms.
- vad starts speech after 2 smoothed speech frames, roughly 64ms above threshold.
- vad ends speech after 3 smoothed stopping frames, roughly 96ms below stop threshold.
- turn manager ignores vad segments whose current vad segment duration is under 400ms.
- smart turn runs after vad speech_end. with the default 3-frame hangover the first probe is at about +96ms after the assumed end sample; if incomplete and no speech resumes, the geometric schedule preserves checks at +192ms, +384ms, +768ms, and +1536ms.
- if speech-like audio continues for 7500ms after the last committed transcript token without new tokens, the daemon emits `turn_human_hold` and immediately performs one degraded `source=human_hold` close after close-time model drain/alignment.
- smart turn timeout/unavailable/error fails open to vad close.
- parakeet realtime eou is disabled by default.

## devices

| job | pin | device | unit |
|---|---|---|---|
| user ASR | `nemotron-speech-streaming-en-0.6b-Q4_K_M.gguf` | CPU (`libggml-cpu`) | `ata-speech-core.service` |
| VAD | `silero_vad_v4.onnx` | CPU | same |
| turn close | `smart-turn-v3.2-cpu.onnx` | CPU, 1 thread | same |
| barge cut | torchaudio `WAV2VEC2_ASR_BASE_960H` | CPU | `ata-speech-align.service` |
| TTS | qwentts.cpp 0.6B CustomVoice Q8 | GPU (`GGML_BACKEND=CUDA0`) | `ata-speech-tts.service` |

the 4070 is the mouth. do not load CosyVoice next to live qwentts. GPU nemotron is a future pin change, not current.

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

incomplete semantic decisions suppress immediate vad close. if the delayed recheck still holds and speech does not resume, acoustic fallback can close only after 2500ms of low-probability silence (installed profile; code-default is 3500ms):

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

- websocket audio transport from laptop to server.
- native nixos build/install path for the laptop client.
- server systemd user service for the daemon.
- nemotron streaming transcript.
- silero vad acoustic pauses.
- smart turn v3 direct rust onnx semantic endpoint gate.
- transcript-before-`<EOU>` event ordering via `ModelProgressMap` wait.
- clean live watcher output: transcript plus `<EOU>`.
- jsonl event log for debugging.

## what is still rough

- smart turn v3 needs broader live laptop validation across actual conversational pauses.
- smart turn preprocessing is implemented directly in Rust; parity against Python is smoke-tested through the real model, not numerically golden-tested against Transformers.
- cross-host capture latency is preserved but not calibrated.
- `docs/evolution/` still names CosyVoice as a *selected target*. that is direction archive. **live mouth is qwentts.** CosyVoice is rollback only.
- speech-out **pcm out** streams. qwentts HTTP text-in is one complete `input`. leftover **append** is the voicecat hop on `:8788` (`speak` then `append`). first-clause flush is also call-side (voicecat SENTENCE), not an engine missing-feature on `:18091`.
- WordVoice (CosyVoice3 word-level tags, [arXiv:2607.06461](https://arxiv.org/abs/2607.06461)) is **not** a streamer and **not** loaded. research: [`lab/docs/qualification/wordvoice-research.md`](../lab/docs/qualification/wordvoice-research.md). parked with the rest of CosyVoice lab.

## manual testing commands

live call: sibling voicecat (`python/.venv/bin/sdc-pipecat-webrtc`). these commands are **substrate diagnostics**, not the desk.

### speech-in only

```bash
speech-core-live-session --debug-tui
```

laptop CPAL mic → daemon → watch TUI. not voicecat `/desk`.

### speech-in + speech-out dogfood

```bash
speech-out-live-session \
  --response-text "Lorem Ipsum is simply dummy text..." \
  --steps 5 \
  --speed 1.3 \
  --voice M1 \
  --play-command pw-play \
  --trace-vad
```

canned-reply harness with optional greying. parked dual-Nemotron / CUPE flags stay off. parked helpers live under `lab/scripts/`.

### golden tests

The golden suite validates recording, assertion, and harness machinery with synthetic/mocked data. It is not a live microphone test. You have not needed it yet. When you do:

```bash
scripts/speech-core-golden.py --help
scripts/speech-core-golden-assert.py --help
```

### session artifacts

Every live session writes a directory under `~/.local/state/speech-core/session/`:

```text
mic.wav           captured microphone audio
watch.jsonl       raw daemon events
ui-events.jsonl   harness/TUI events
trigger.log       speech-out dispatch decisions
params.env        exact flags/env used
```

These are the exports. They let you replay or inspect a session after it ends.

## useful commands

server daemon:

```bash
systemctl --user status ata-speech-core.service
systemctl --user restart ata-speech-core.service
journalctl --user -u ata-speech-core.service -f
cat ~/.config/speech-core/daemon.env
```

the laptop:

```bash
speech-core-live-session
cat ~/.config/speech-core/client.env
systemctl --user status speech-core-mic-adapter.service
```

repo:

```bash
cargo test --workspace
SPEECH_CORE_SMART_TURN_MODEL_PATH=~/workspace/external/smart-turn-v3/smart-turn-v3.2-cpu.onnx \
  cargo test -p speech-core-daemon real_model_smoke_when_env_set -- --nocapture
./scripts/install-speech-core-daemon.sh
./scripts/speech-core-sync-build-adapter.sh
```


## speech-out live pin (2026-08-18)

**live mouth is qwentts.cpp Q8 CustomVoice.** CosyVoice is **not** the production pin. it is rollback + historical qual only.

```text
voicecat CosyVoiceTTSService  (name is a lie; SENTENCE-aggregates the whole reply)
  ws://127.0.0.1:8788/ws/speech-out
    ata-speech-out.service   (aa86b67 *binary*, handshake still says progressive-cosyvoice)
      qwentts_progressive_worker.py
        POST http://127.0.0.1:18091/v1/audio/speech
          ata-speech-tts.service   (tts-server, 24 kHz s16 pcm stream)
```

| unit | role |
| --- | --- |
| `ata-speech-out.service` | ws `:8788`, speak/cancel/pcm frames |
| `ata-speech-tts.service` | inference `:18091` |
| voice | `default`/`m1`/`informal` → **ryan**, instruct = informal lock |
| VRAM | **~2438 MiB** (unit pid after leftover adopt) |

clocks (do **not** subtract domains):

- isolated http first-usable p50 **~70 ms** informal/crisp/clip
- clause vs full paragraph first-usable: **71.0 vs 71.3 ms** (`bench/ttfp-oneshot-vs-clause.json`). extra text does **not** delay first audio.
- live `:8788` first-pcm (first codec frame, often hush) warm **~33 ms**
- cancel ack **~41 ms**

evidence: [`docs/qualification/qwentts-sc-o5i.md`](qualification/qwentts-sc-o5i.md), [`docs/qualification/qwentts-stream-leftover.md`](qualification/qwentts-stream-leftover.md).

**one-shot vs stream:** pcm **out** already streams (`response_format=pcm`, `--codec-chunk-dur 0.08`). text **in** is one complete `input` string. empty / `append` / missing `input` → HTTP 400. daemon `ClientMessage` is `Speak` with `text: String` — no `append` / `text_delta`. `stream:true` on the HTTP body is ignored.

the hundreds-of-ms win is: **flush the first speakable clause as soon as the agent emits it**, then more `speak`s. that hop is voicecat (`TextAggregationMode.SENTENCE` waits for the whole reply). do not invent CosyVoice token-bistream (`sc-01p` closed). do not pretend qwentts can chew tokens while pcm is already leaving.

rollback: `SPEECH_OUT_COSYVOICE_WORKER_SCRIPT=$SPEECH_OUT_COSYVOICE_WORKER_SCRIPT_ROLLBACK`, stop `ata-speech-tts.service`, restart the daemon. ~6 GB CosyVoice3 TRT. do **not** load both engines on this 12 GB card.

isolated CosyVoice3 RL lab (clone / instruct / tags / export, **not** live): `~/workspace/cosyvoice_playground`. launch `./run.sh --port 7861`. do **not** load it beside live qwentts on this 12 GB card.

historical CosyVoice2 Unet (~225 ms finished-sentence) is an isolated container, not this pin.

voicecat adapters are a `:8788` client. do not retune them from an `sc` bead.




## manual tui convention

For substrate-contact testing, `speech-core-live-session --debug-tui` is the diagnostic surface. the live conversation UI is the headed omp TUI via voicecat. `speech-out-live-session` greying is dogfood-only.
