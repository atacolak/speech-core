# Speech Core current state

**status:** maintained current-implementation guide
**authority:** repository code and installed configuration establish current reality; this document explains them

If this file disagrees with code, tests, traces, or installed configuration, treat the mismatch as a documentation defect. Do not average conflicting values.

## one-line summary

`speech-core` is the spoken substrate: voicecat (or a diagnostic mic adapter) sends 16 kHz mono PCM in; nemotron (CPU) transcribes; silero + smart-turn close the turn; leftover mouth daemon (`:8788`) hops to breeze-tts-2 E2 (GPU). the live *call* is sibling voicecat + headed omp TUI, not `speech-out-live-session`.
the short shared picture of the same system: [`core-picture.md`](core-picture.md).

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
ata-speech-out.service :8788     leftover mouth daemon (speak/append/cancel)
  hop POST :7861/internal/leftover/v1/audio/speech
    breeze-tts-2 E2 worker (GPU)
  ata-speech-tts.service :18091  parked qwentts.cpp (rollback only)
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
| TTS | breeze-tts-2 E2 | GPU | `lab-web-7861` / `tts.lab.backend.runtime.worker` |
| parked TTS rollback | qwentts.cpp 0.6B CustomVoice Q8 | GPU if started | `ata-speech-tts.service` (disabled; not Wants= from the mouth daemon) |

the GPU holds one synthesizer. live occupant is breeze-tts-2 E2 (~8720 MiB). do not start parked qwentts or CosyVoice beside it. GPU nemotron is a future pin change, not current.

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
- `docs/evolution/` still names CosyVoice as a *selected target*. that is direction archive. **live synthesizer is breeze-tts-2 E2** via leftover mouth hop `:8788`. parked qwentts.cpp and CosyVoice3 TRT are rollback only.
- leftover **pcm out** streams from the breeze hop. leftover **append** is the voicecat hop on `:8788` (`speak` then `append`). first-clause flush is call-side (voicecat SENTENCE). parked qwen `:18091` is not the live path.
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


## speech-out live pin (2026-09-11)

**live synthesizer is breeze-tts-2 E2.** leftover `:8788` is the mouth daemon. qwentts.cpp and CosyVoice3 TRT are rollback only. frozen env names (`SPEECH_OUT_PROGRESSIVE_BACKEND=cosyvoice`, `SPEECH_OUT_COSYVOICE_*`, `SPEECH_OUT_QWENTTS_URL`, handshake `progressive-cosyvoice`) still describe the leftover hop, not the model on the GPU.

```text
voicecat SpeechOutTTSService
  ws://127.0.0.1:8788/ws/speech-out
    ata-speech-out.service   (speech-out-append-v1; handshake still says progressive-cosyvoice)
      qwentts_progressive_worker.py   (leftover hop client; vestigial name)
        POST SPEECH_OUT_QWENTTS_URL
          http://10.77.67.147:7861/internal/leftover/v1/audio/speech
            breeze-tts-2 E2 worker (GPU, ~8720 MiB)
  ata-speech-tts.service :18091  parked qwentts.cpp (disabled; not Wants= from the mouth)
```

| unit | role |
| --- | --- |
| `ata-speech-out.service` | leftover mouth daemon, ws `:8788`, speak/append/cancel/pcm frames |
| `lab-web-7861` | breeze-tts-2 E2, hop `:7861/internal/leftover/v1/audio/speech` |
| `ata-speech-tts.service` | parked qwentts.cpp rollback on `:18091`; must not start beside E2 |
| talker voice | lab sqlite `active_voice_id`; leftover hop uses that profile |
| VRAM | **~8720 MiB** breeze worker; parked qwen ~2240 MiB if started |

clocks (do **not** subtract domains):

- isolated http first-usable p50 **~70 ms** informal/crisp/clip
- clause vs full paragraph first-usable: **71.0 vs 71.3 ms** (`bench/ttfp-oneshot-vs-clause.json`). extra text does **not** delay first audio.
- live `:8788` first-pcm (first codec frame, often hush) warm **~33 ms**
- cancel ack **~41 ms**

evidence: [`docs/qualification/qwentts-sc-o5i.md`](qualification/qwentts-sc-o5i.md), [`docs/qualification/qwentts-stream-leftover.md`](qualification/qwentts-stream-leftover.md).

**unit ceiling (leftover daemon, not the hop):** `speech-out-append-v1` fails a unit at **720000 samples** (1500 frames × 20 ms PCM = 30.000 s) with `speech_out_failed` / `error:max_samples_per_utterance exceeded`. that string reaches the wire (`ErrorFrame(speech_out_terminal:failed:max_samples_per_utterance exceeded)`). not silent. ceiling is **per unit**, not per utterance — four shorter units can outlast 30s. hop `POST /internal/leftover/v1/audio/speech` has no HTTP duration cap. desk (`SpeechOutWholeResponseTTSService`, voicecat `b66bb19`) splits at ~420 chars / ~22s on sentence boundaries so a long reply is not clipped mid-word. do not raise this on the hop.

**engine ceiling (hop 200, truncated PCM):** leftover hop is not lossless on a long single POST. E2 `FastStreamingConfig` defaults `max_new_tokens=750`, `max_seq_len=1024` (graphs warmed there; official breeze API is 1500/2048). generation stops at EOS, at 750 new tokens, or when `prefill_len + step >= 1023`, then the hop still returns **200**. two-regime (one POST each, raw pcm seconds, overseer #48294): 315→10.880s, 789→27.600, **1105→36.640 peak**, 1421→31.200, 1737→25.760, 2053→20.320, 2527→12.160. below the peak, ~36.6s is `max_new_tokens=750` at ~20.5 tok/s (48.9 ms/token). above it `max_seq_len` binds and duration falls: 1 audio token lost per 0.352 input chars (~2.84 chars/prefill token), `prefill ~= 0.352*chars - 115`. predicted transition `0.352*c - 115 + 750 <= 1023` is `c <= 1102`; measured peak 1105. stay under **~1100 chars** for full fidelity; above it you lose ~49 ms of audio per extra 2.8 characters, silently, with a 200. do not raise 750/1024 without a new E2 warmup. desk 420-char units are ~148 prefill tokens, under both caps.

**one-shot vs stream:** leftover hop pcm **out** streams (`response_format=pcm`). hop text **in** is one complete `input` string per POST — no append flag. `:8788` leftover v1 **does** accept `speak` / `append` / `finish` / `hold_open`; each of those is a fresh hop POST / Breeze synth. empty hop `input` → HTTP 400. `stream:true` on the hop body is ignored. non-streamed desk replies coalesce into one speak so Breeze keeps paragraph breath (~360 ms/boundary). streamed first clause stays speak, then one append.

the hundreds-of-ms win is: **flush the first speakable clause as soon as the agent emits it**. do not invent CosyVoice token-bistream (`sc-01p` closed). do not pad hop zeros at append boundaries — that fakes a pause and misses intonation.

rollback: `SPEECH_OUT_COSYVOICE_WORKER_SCRIPT=$SPEECH_OUT_COSYVOICE_WORKER_SCRIPT_ROLLBACK`, stop `ata-speech-tts.service`, restart the daemon. ~6 GB CosyVoice3 TRT. do **not** load two synthesizers on this 12 GB card.

isolated CosyVoice3 RL lab (clone / instruct / tags / export, **not** live): `~/workspace/cosyvoice_playground`. launch `./run.sh --port 7861`. do **not** load it beside live breeze-tts-2 E2 on this 12 GB card.

historical CosyVoice2 Unet (~225 ms finished-sentence) is an isolated container, not this pin.

voicecat adapters are a `:8788` client. do not retune them from an `sc` bead.




## manual tui convention

For substrate-contact testing, `speech-core-live-session --debug-tui` is the diagnostic surface. the live conversation UI is the headed omp TUI via voicecat. `speech-out-live-session` greying is dogfood-only.
