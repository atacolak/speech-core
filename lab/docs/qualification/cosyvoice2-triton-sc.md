# CosyVoice2 Triton + TRT-LLM (Unet path B) — sc-cv2-triton-trtllm-vw8

Not AutoModel. Not `inference_bistream`. Official Unet is **full `target_text` in + Decoupled audio-out**. First emit is hop=15 + lookahead=3 (18 tokens). Clock zero is `T_text_done` (full text into serve / first non-error gRPC callback after `async_stream_infer`). L20 README streaming first-chunk (cache=False, conc=1): avg 220.43 ms / p50 218.07 ms. The 150 ms figure is AutoModel path A marketing — not this bead.

Live `speech-out-daemon` stays PARKED.

## Host

- GPU: RTX 4070 12282 MiB. At start of this wave: ~300–410 MiB used (Xorg/compositor), ~11.4 GiB free.
- Docker: no nvidia runtime / no CDI. `--gpus all` fails (`failed to discover GPU vendor from CDI`).
- Proven CUDA path: `docker_gpu_args.sh` manual `--device /dev/nvidia*` + bind-mount `libcuda`/`libnvidia-ml` + host `nvidia-smi`.
- One-shot (`soar97/triton-cosyvoice:25.06` sha256:b8b6df1f4f4e): `nvidia-smi` sees 4070; `torch.cuda.is_available() True`.
- Ports: host `:8000` is cognee. Triton published `127.0.0.1:18000/18001/18002` only. **No `trtllm-serve`** (that is CV3/DiT).

## LLM (`yuekai/cosyvoice2_llm`)

Campaign file was 1283621816 B with a valid BF16 safetensors header **but** leftover `.aria2` from a 403 abort at ~94%. Preallocated file hashed `84745bcb…`, not HF LFS `91d53bdc8f6bf5752f40e1400305a64f6c8a8e335336ea0f5d5eaac8da974050`.

Redownloaded with image `huggingface-cli` (2026-08-17T14:52Z). Now:

- size 1283621816
- sha256 `91d53bdc8f6bf5752f40e1400305a64f6c8a8e335336ea0f5d5eaac8da974050` (matches hub)
- no `.aria2`
- `added_tokens.json` present after redownload

## Stock CosyVoice2-0.5B

`/home/sf/.cache/speech-out/cosyvoice2-lane2-20260812/models/CosyVoice2-0.5B`

BLS `initialize` hard-requires `spk2info.pt` even when `use_spk2info_cache=False`. Official raw.githubusercontent.com returned 429; fetched 180930 B via jsdelivr retry (`qi-hua/async_cosyvoice` CosyVoice2-0.5B/spk2info.pt).

## Wrapper (campaign, not product crates)

`/home/sf/.cache/speech-out/triton-campaign-20260812/cv2_work/`

- `docker_gpu_args.sh` — verified manual GPU mounts
- `run_cv2_4070.sh` — official stages 0–3, Unet BLS + in-process `tensorrt_llm`
- `run_cv2_host.sh` — host docker launcher, ports 18000–18002, flock required for GPU stages
- `first_chunk_client.py` — clocks from `T_text_done`

4070 knobs: `max_batch_size=1`, `max_num_tokens=2048`, `BLS_INSTANCE_NUM=1`, `TRITON_MAX_BATCH_SIZE=1`, `kv_cache_free_gpu_mem_fraction=0.20`, `Decoupled=True`. Dtype: try `float16` for both `--dtype` and `--gemm_plugin`; fall back to official `bfloat16` if convert/build rejects.

## Engines

`float16` convert + `trtllm-build --gemm_plugin float16` succeeded (TRT-LLM 0.20.0). First attempt died after engine generation writing `model.cache` into the read-only CosyVoice mount; rebuild used writable `/campaign/engines/cv2_build_float16`. `test_llm` produced speech-token ids. Engine: `/home/sf/.cache/speech-out/triton-campaign-20260812/engines/cv2_trt_engines_float16/rank0.engine` (1.3 GiB).

## Warm serve

Named container `triton-cv2-sc` (`soar97/triton-cosyvoice:25.06`). Triton 2.59.0 HTTP 18000 / gRPC 18001 / metrics 18002. Models READY: `audio_tokenizer`, `cosyvoice2`, `speaker_embedding`, `tensorrt_llm`, `token2wav`. GPU ~4693 MiB used / 7161 MiB free while warm. Live `speech-out-daemon` still inactive. **No trtllm-serve.**

## First-chunk vs L20 (official `client_grpc.py`, streaming, cache=False, conc=1)

Clock zero = `T_text_done` (full target_text into `async_stream_infer`; first non-error callback). Hop 15 + lookahead 3.

| run | first_chunk_ms | second_chunk_ms | total_ms | RTF |
|---|---:|---:|---:|---:|
| warmup | 4489.94 | 216.60 | 5157.45 | 0.4376 |
| timed-1 | 220.90 | 167.14 | 1373.21 | 0.1492 |
| timed-2 | 290.13 | 168.54 | 1355.63 | 0.1546 |
| L20 README | 220.43 avg / 218.07 p50 | — | — | 0.1237 |

Timed-1 matches the L20 first-chunk average. 150 ms is AutoModel path A, not this stack.

Evidence: `artifacts/cv2-first-chunk.json`, `bench/cv2_first_chunk/`.

## Status

- CUDA in container: PASS
- LLM complete (sha256=HF LFS): PASS
- float16 engines: PASS
- warm Unet serve 18000/18001: PASS (container `triton-cv2-sc` left running)
- first-chunk clocks: PASS (official client)
- live unit parked: PASS
- bead not closed (builder)
