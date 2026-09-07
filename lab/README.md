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
| `scripts/breeze_tts_qual/playground.py` | Gradio lab playground over BreezeEngine (default E2) | lab-only; exclusive VRAM vs leftover qwentts; not a pin swap; not on the voicecat path |
| `docs/assistant-self-asr-eval.md` | eval-only track notes | |
| `docs/barge-in-dual-asr.md` | dual-ASR impl notes | |
| `docs/qualification/cosyvoice*` / qwen3 research | rollback / bakeoff receipts | keep evidence, not the map |
| `laptop-audio/` | host AEC / denoise tools | outside the daemons |
| `tests/test-*-self-asr*` / `test-barge-in-dual-asr.sh` | harness tests for the above | |


Lab playground (not production; live mouth stays leftover qwentts):

```bash
export QUAL_ROOT="${QUAL_ROOT:-$HOME/.cache/speech-out/breeze-tts-qual-sc-breeze-hybrid-81p}"
export PYTHONPATH="lab/scripts:${QUAL_ROOT}/src/breeze-tts"
"$QUAL_ROOT/venv/bin/python" lab/scripts/breeze_tts_qual/playground.py --host 127.0.0.1 --port 7860
```

`--dry-run` constructs the UI without parking leftover or loading the 3B.

`speech-out-live-session.sh` still looks here if you flip `SPEECH_OUT_ASSISTANT_SELF_ASR=1` or `SPEECH_OUT_CUPE_LIVE=1`. those flags stay **off**.

## live CTC is not here

warm worker: `scripts/barge_in_align/align_worker.py`  
unit: `ata-speech-align.service`  
sock: `$XDG_RUNTIME_DIR/discord-voice-agent/align.sock` (voicecat client default)

replacing wav2vec2 with nemotron-as-aligner is a new backend behind that JSONL sock, not a rewrite of voicecat.
