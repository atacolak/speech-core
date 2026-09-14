# lab — parked, not live

not on the voicecat path. do not treat anything here as the product.

live ear/mouth stays at repo root: daemons, `scripts/barge_in_align/`, qwentts pin, `spec/`.

## what is here

| path | was | why parked |
|---|---|---|
| `scripts/assistant-self-asr*` | dual-Nemotron B eval / cut helpers | off by default; voicecat does not run a second ASR |
| `scripts/barge-in-dual-asr*` | dual-stream drain cut | same |
| `scripts/speech_talker_session.py` | `pi --profile talker` dogfood | headed omp TUI is the brain now |
| `scripts/cosyvoice_qual/` | CosyVoice GPU qualification | live mouth is qwentts |
| `scripts/breeze_tts_qual/` | Breeze TTS 2 hybrid-int8 + CUDA-graph 4070 qual (sc-breeze-hybrid-81p) | not on the voicecat path; live mouth stays leftover qwentts |
| `scripts/breeze_tts_qual/playground.py` | shim → `tts/playground` (E2 lab) | lab-only; exclusive VRAM vs leftover qwentts; not a pin swap |
| `docs/assistant-self-asr-eval.md` | eval-only track notes | |
| `docs/barge-in-dual-asr.md` | dual-ASR impl notes | |
| `docs/qualification/cosyvoice*` / qwen3 research | rollback / bakeoff receipts | keep evidence, not the map |
| `laptop-audio/` | host AEC / denoise tools | outside the daemons |
| `tests/test-*-self-asr*` / `test-barge-in-dual-asr.sh` | harness tests for the above | |


Lab playground (not production; live mouth stays leftover qwentts).
The TTS laboratory lives in `tts/`. E2 is the selected Breeze runtime.

```bash
export QUAL_ROOT="${QUAL_ROOT:-${XDG_DATA_HOME:-$HOME/.local/share}/speech-out/breeze-tts-2-e2}"
export PYTHONPATH=".:lab/scripts:${QUAL_ROOT}/src/breeze-tts"
"$QUAL_ROOT/venv/bin/python" tts/playground/app.py --host 127.0.0.1 --port 7860
```

The shim `lab/scripts/breeze_tts_qual/playground.py` still launches the same app.

`--dry-run` constructs the UI without parking leftover or loading the 3B.

Transcribe fills **Clone transcript** from the uploaded Clone audio using
the existing `transcribe.cpp` CLI on CPU with
`parakeet-tdt-0.6b-v2-Q8_0.gguf`. WAV is read directly; MP3 and other
non-WAV uploads are decoded with ffmpeg on CPU. It does not take Breeze
VRAM. Override with `TRANSCRIBE_CLI` / `PARAKEET_GGUF` if needed.

`speech-out-live-session.sh` still looks here if you flip `SPEECH_OUT_ASSISTANT_SELF_ASR=1` or `SPEECH_OUT_CUPE_LIVE=1`. those flags stay **off**.

## live CTC is not here

warm worker: `scripts/barge_in_align/align_worker.py`  
unit: `ata-speech-align.service`  
sock: `$XDG_RUNTIME_DIR/discord-voice-agent/align.sock` (voicecat client default)

replacing wav2vec2 with nemotron-as-aligner is a new backend behind that JSONL sock, not a rewrite of voicecat.
