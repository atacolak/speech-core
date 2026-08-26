# Qwen3-TTS-0.6B isolated candidate (2026-08-18)

**lost isolated path.** vllm-omni lost to qwentts.cpp. live pin is qwentts (sc-o5i), not this serve and not CosyVoice.

## what shipped

official stack, not a random hf wrapper:

- `vllm==0.26.0` + `vllm-omni==0.26.0` (tag `v0.26.0`, not main `0.1.dev`)
- weights local:
  - `Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice` (2.4G)
  - `Qwen/Qwen3-TTS-12Hz-0.6B-Base` (2.4G, clone — **not served this pass**)
  - `Qwen/Qwen3-TTS-Tokenizer-12Hz` (651M)
- deploy: `~/.cache/speech-out/qwen3-tts-0.6b-20260817/qwen3_tts_4070.yaml`
  - official `async_chunk: true` + `initial_codec_chunk_frames: 1`
  - talker cudagraph on, `gpu_memory_utilization: 0.22` / stage, `max_num_seqs: 4`
- serve: `vllm-omni serve … --omni` on `127.0.0.1:18091`
- env needed on this box: `VLLM_USE_FLASHINFER_SAMPLER=0` (flashinfer JIT wants `/usr/local/cuda`; nvcc 13.3 vs torch cu130 headers fight)

layout: `~/.cache/speech-out/qwen3-tts-0.6b-20260817/`

## measured (CustomVoice, warm, stream pcm)

clock = first TCP byte of `/v1/audio/speech` with `stream=true, stream_format=audio, response_format=pcm`.

| voice | lang | n | first-byte p50 | e2e | out |
|---|---|---:|---:|---:|---|
| Ryan | en | 5 | **342 ms** | ~0.76–0.85 s | 24 kHz mono s16, ~5.3–5.7 s |
| Vivian | zh | 3 | **343 ms** | ~1.40–1.56 s | longer zh line |

cold first request 786 ms (ryan). after abort-at-16kB the server still answers 200. vram **10857 / 12282 MiB** (stage0 2766 + stage1 7700). cannot coexist with cv3 trt (~6 GB) on this 4070.

wav: `qwen3-tts-0.6b-20260817/wavs/ryan_en_stream_probe.wav`

## vs parked cv3 (aa86b67, 2512 **base** not RL)

cv3 live-ws matched p50 **298 ms**, isolated trt p50 ~270–282 ms, ~6.2 GB. qwen3-tts 0.6B customvoice is **~40–70 ms slower first-byte** and **+4.6 GB**. official h200 number (64 ms) is a different gpu + full flashinfer. we are not that.

0.6B-Base clone serve is wired (`run_server_base.sh`) but **not launched** — one model per server. restart with Base to clone the new CV3-Eval refs.

## leftover

- this vllm-omni pin LOST. measured faster path: `docs/qualification/qwen3-tts-qwentts-q8.md`
  (qwentts.cpp Q8_0: first-byte p50 32 ms / usable ~70 ms / 2.4–3.1 GB).
- flashinfer sampler still off in the omni stack; irrelevant now.
- nano-qwen3tts-vllm flash-attn ABI-miss on torch 2.11; not measured.
- Base clone not measured. not a speech-out worker; no pin swap.
