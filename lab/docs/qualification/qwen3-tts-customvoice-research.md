# Qwen3-TTS 0.6B CustomVoice — voice customization + cutover (2026-08-18)

**live as of 2026-08-18 (sc-o5i).** `speech-out-daemon.service` points at the qwentts worker. `qwentts-tts-server.service` owns `127.0.0.1:18091`.
cutover: [`qwentts-sc-o5i.md`](./qwentts-sc-o5i.md).

operator accepted quality. product decision: use this TTS instead of CosyVoice
for the named-speaker stream path. cutover evidence is `qwentts-sc-o5i.md`.

## 1. can we add a custom voice to 0.6B CustomVoice?

**not as a drop-in on the loaded checkpoint.** three official doors, only one
is this model:

| door | checkpoint | what it does | 0.6B CustomVoice? |
|---|---|---|---|
| named roster | CustomVoice | 9 baked codec-embedding rows | **this is the pin.** closed set. |
| 3s clone | **Base** | ECAPA-TDNN + optional ICL transcript | different gguf, unmeasured |
| invent a voice | **1.7B VoiceDesign** | free-text persona | not 0.6B, not loaded |
| official SFT | **Base only** | single-speaker fine-tune → emits a CustomVoice-shaped ckpt | not this file |

official table: 0.6B CustomVoice has **no** Instruction-Control checkmark.
1.7B CustomVoice and 1.7B VoiceDesign do. the python API still accepts
`instruct=` on 0.6B; qwentts forwards `instructions`. that is "maybe it
obeys", not a supported surface.

official fine-tune (`QwenLM/Qwen3-TTS/finetuning`):

- start from `Qwen3-TTS-12Hz-0.6B-Base` or `1.7B-Base` (script default 1.7B)
- jsonl: `{audio, text, ref_audio}` with the **same** `ref_audio` for all rows
- `sft_12hz.py --speaker_name X` writes a checkpoint with
  `tts_model_type=custom_voice` and stuffs the mean speaker embedding into
  `codec_embedding.weight[3000]`
- inference after SFT is `generate_custom_voice(..., speaker="X")`
- **single speaker only.** "Multi-speaker fine-tuning … future releases."
- 12GB 4070 + official `batch_size 32` is a fantasy; their one-click uses
  `BATCH_SIZE=2`. still a training job, not a 70ms product path.
- output is HF safetensors, **not** a qwentts.cpp GGUF. serving an SFT
  speaker on this 70ms stack means convert+quantize after train. unproven.

so: **new human tomorrow** = Base clone (unmeasured, will spend some of the
200ms) or Base SFT (days, then convert). **not** "add a 10th name to the
Q8 we are running."

## 2. how far does natural-language instruct go?

measured on **ryan**, same sentence, seed 42, qwentts.cpp Q8,
`response_format=wav` (duration only — stream clock already known):

`If you can hear this, the voice is already on the card — no cloud hop.`

| id | instruct | dur | vs baseline 5.52s |
|---|---|---:|---|
| none | — | 5.52s | — |
| fast | very fast, rushed | **4.80s** | shorter |
| deadpan | flat newsreader | **4.72s** | shorter |
| shout | loud, projected | 5.04s | slight |
| happy / angry / child | emotion / energy | 5.12s | slight |
| anchor | news formal | 5.28s | slight |
| tired | exhausted | 5.44s | ~none |
| secret | don't say this out loud | 5.60s | slight |
| slow | one word at a time | **6.40s** | longer |
| whisper | intimate, very quiet | **7.36s** | longer |
| sad | almost crying, slow | **7.36s** | longer |
| sarcastic | mocking | **7.44s** | longer |

rate and "how carefully" clearly move duration. emotion is weaker and
needs ears, not a table. official marketing for 0.6B does **not** promise
this. listen `wavs-qwen-instruct/` before wiring instruct into the product.

1.7B CustomVoice is the checkpoint that is *supposed* to do this well.
1.7B VoiceDesign invents a mouth from a paragraph. neither is this pin.

## 3. speech-out cutover (done 2026-08-18)

exact env/unit + clocks: [`qwentts-sc-o5i.md`](./qwentts-sc-o5i.md).

live:

- daemon binary still aa86b67 on `:8788`
- `SPEECH_OUT_COSYVOICE_WORKER_SCRIPT` → `qwentts_progressive_worker.py`
- inference `qwentts-tts-server.service` on `:18091`
- default instruct = informal; `voice=default` aliases to ryan
- voicecat adapters **not** edited

rollback: `$SPEECH_OUT_COSYVOICE_WORKER_SCRIPT_ROLLBACK` + stop `qwentts-tts-server`.

## 4. listen

http://127.0.0.1:18081/ — 9 speakers + ryan instruct grid on top.
cv3 clone-hunt matrix still below for A/B.

## leftover

- Base 0.6B clone first-pcm unmeasured
- SFT → GGUF unproven
- instruct obedience is ears, not a MOS
- leftover tts-server was unsupervised at first flip; now a user unit
