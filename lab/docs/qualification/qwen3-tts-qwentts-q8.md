# Qwen3-TTS-0.6B via qwentts.cpp Q8_0 (2026-08-18)

**live as of 2026-08-18 (sc-o5i).** `speech-out-daemon.service` speaks this stack via the qwentts worker. isolated process became `qwentts-tts-server.service` on `127.0.0.1:18091`.
cutover evidence: [`qwentts-sc-o5i.md`](./qwentts-sc-o5i.md).

## what shipped

real quantized CUDA stack, not another vllm-omni yaml tweak:

- runtime: [ServeurpersoCom/qwentts.cpp](https://github.com/ServeurpersoCom/qwentts.cpp) `a8a7716` (2026-08-07)
- built locally against torch's cu13 nvcc (`89-real`, `CCCL_DISABLE_CTK_COMPATIBILITY_CHECK`)
  because this box has no `/usr/local/cuda` and nvcc 13.3 vs headers 13.0 fight
- weights: [Serveurperso/Qwen3-TTS-GGUF](https://huggingface.co/Serveurperso/Qwen3-TTS-GGUF)
  - `qwen-talker-0.6b-customvoice-Q8_0.gguf` (924 MB)
  - `qwen-tokenizer-12hz-Q8_0.gguf` (278 MB)
- backend: `GGML_BACKEND=CUDA0` (Ada path)
- serve: `tts-server --codec-chunk-dur 0.08` so PCM streams frame-ish, not 24 s one-shots
- layout: `~/.cache/speech-out/qwen3-tts-0.6b-20260817/`
  bins in `src/qwentts.cpp/build/`, needs `LD_LIBRARY_PATH=cuda-home/lib64`

official `:cuda13` image also pulled (`ghcr.io/serveurpersocom/qwentts.cpp:cuda13`)
and extracted to `bin/qwentts-cuda13/`. host docker has **no GPU CDI**, so that
binary was not the measured path.

## measured (CustomVoice, warm, stream pcm)

clock = first TCP byte of `POST /v1/audio/speech` `response_format=pcm` (s16le 24 kHz).
usable = first sample `|s| >= 512` in the same stream.

| voice | n | first-byte p50 | first-usable p50 | e2e | vram |
|---|---:|---:|---:|---:|---|
| vivian en | 5 | **32 ms** | ~70–148 ms | 0.54–0.63 s | 2.4–3.1 GB |
| ryan en | 5 | **31 ms** | ~68 ms | 0.49–0.74 s | same process |

cross-voice usable (one-shot after warm): vivian 148 / ryan 68 / sohee 147 / serena 32 / vivian-zh 70. **usable p50 ~70 ms**.

wav peak is real speech (vivian en_code: 5.52 s, absmax 20479, rms 3137, peak −4.1 dB, not a silence dump).

clips: `wavs/qwentts-q8-*.wav` and `matrix-host/wavs-qwentts-q8/`.

## vs isolated vllm-omni 0.26 CustomVoice (same 4070, same ryan/vivian)

| stack | first-byte p50 | vram | coexist cv3 (~6 GB)? |
|---|---:|---:|---|
| vllm-omni 0.26 (`qwen3_tts_4070.yaml`) | **342 ms** | **10.9 GB** | no |
| qwentts.cpp Q8_0 | **31–32 ms** wire / **~70 ms** usable | **2.4–3.1 GB** | yes, on paper |
| parked cv3 TRT aa86b67 | ~270–312 ms isolated | ~6.2 GB | — |

this is the first qwen3 path that beats cv3 first-pcm **and** fits next to it.

## leftover / honesty

- first TCP byte is ~32 ms because the server streams codec frames immediately; onset is often near-silent for 30–120 ms. **usable energy is the number that matters (~70 ms).**
- quality is Q8 named-speaker CustomVoice, **not** a clone of the 6 official CV3-Eval refs. listen before anyone talks pin swap.
- nano-qwen3tts-vllm installed but flash-attn wheel is `cu130torch2.13` vs our `torch 2.11.0+cu130` (`c10::ValueError` ABI miss). not measured.
- NVFP4 rejected (Hopper/Blackwell only). MLX/LiteRT/OpenVINO excluded.
- first TCP byte is not first usable energy. cutover clocks: [`qwentts-sc-o5i.md`](./qwentts-sc-o5i.md).

## how to rerun

```bash
export LD_LIBRARY_PATH=~/.cache/speech-out/qwen3-tts-0.6b-20260817/cuda-home/lib64
export GGML_BACKEND=CUDA0
~/.cache/speech-out/qwen3-tts-0.6b-20260817/src/qwentts.cpp/build/tts-server \
  --model ~/.cache/speech-out/qwen3-tts-0.6b-20260817/models/gguf/qwen-talker-0.6b-customvoice-Q8_0.gguf \
  --codec ~/.cache/speech-out/qwen3-tts-0.6b-20260817/models/gguf/qwen-tokenizer-12hz-Q8_0.gguf \
  --host 127.0.0.1 --port 18091 --lang English --alias qwen3-tts-0.6b-q8 \
  --codec-chunk-dur 0.08
```
