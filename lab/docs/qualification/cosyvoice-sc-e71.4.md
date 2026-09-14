# CosyVoice qualification — sc-e71.4

**bead:** `sc-e71.4`  
**run id:** `20260727T051625Z`  
**executed:** 2026-07-27T05:16:25Z → 2026-07-27T05:18:57Z (UTC)  
**host:** local RTX 4070 (12 GB), Ubuntu, kernel driver 595.71.05  
**status of this document:** measurement evidence only — not cutover approval, not self-certification  

Machine-readable twin: [`cosyvoice-sc-e71.4-report.json`](./cosyvoice-sc-e71.4-report.json)  
Dependency lock: [`cosyvoice-sc-e71.4-requirements-lock.txt`](./cosyvoice-sc-e71.4-requirements-lock.txt)  
Harness: [`../../scripts/cosyvoice_qual/run_qualification.py`](../../scripts/cosyvoice_qual/run_qualification.py)  
Reproduce: [`../../scripts/cosyvoice_qual/reproduce.sh`](../../scripts/cosyvoice_qual/reproduce.sh)

---

## 1. Pin (exact)

| Item | Value |
| --- | --- |
| Code repo | `https://github.com/QwenAudio/CosyVoice` (`FunAudioLLM/CosyVoice` → 301) |
| Code commit | `074ca6dc9e80a2f424f1f74b48bdd7d3fea531cc` |
| Matcha-TTS submodule | `dd9105b34bf2be2230f4aa1e4769fb586a3c824e` |
| Model id | `FunAudioLLM/Fun-CosyVoice3-0.5B-2512` |
| Model revision | `29e01c4e8d000f4bcd70751be16fa94bf3d85a18` |
| Runtime path | in-process `AutoModel` → `inference_zero_shot(..., stream=True)` @ **24 kHz** float PCM |
| Python | 3.10.20 (uv-managed) |
| Torch | `2.3.1+cu121` / torchaudio `2.3.1` |
| Isolation root | `~/.cache/speech-out/cosyvoice-qual-sc-e71.4` (not Supertonic venvs) |

### Fallback pin (not executed this run)

Same code commit + HF `FunAudioLLM/CosyVoice2-0.5B` rev `eec1ae6c79877dbd9379285cf8789c9e0879293d`.

### Content hashes measured on disk (sha256)

Navigator notes recorded Hugging Face LFS OIDs; **content** SHA-256 after `hf download` is:

| File | Bytes | sha256 |
| --- | ---: | --- |
| `llm.pt` | 2024669519 | `69f43bd545131c30e98947fb360ea8b4dc9916d8e83dded7757c7ea4f5a24970` |
| `flow.pt` | 1329116148 | `a6fab32a7825e5b0bc855ddd948f8db9370b0a786fbc249caa4595e95b608e4b` |
| `hift.pt` | 83202622 | `b279d7641eb97ae55b3b540cfba4f953c26492a2df758328a89a4d007ab87a65` |
| `speech_tokenizer_v3.onnx` | 969451503 | `23236a74175dbdda47afc66dbadd5bcb41303c467a57c261cb8539ad9db9208d` |
| `campplus.onnx` | 28303423 | `a6ac6a63997761ae2997373e2ee1c47040854b4b759ea41ec48e4e42df0f4d73` |
| `CosyVoice-BlankEN/model.safetensors` | 988097824 | `130282af0dfa9fe5840737cc49a0d339d06075f83c5a315c3372c9a0740d0b96` |
| `cosyvoice3.yaml` | 6934 | (see report.json) |
| repo `asset/zero_shot_prompt.wav` | 334138 | `c7b31d6dbe7cc6a716dded00550db5b50940bf209e424e4ad207b12e657c8ff6` |

Byte sizes match the navigator tree listing; content digests differ from LFS pointer OIDs (expected).

### Legal / license ambiguity (explicit)

- Code and model card tags: **Apache-2.0**.
- Model card disclaimer: content is **“for academic purposes only”** and “intended to demonstrate technical capabilities.”
- **Product use requires legal review.** This qualification does not clear that gate.

---

## 2. Environment

| Probe | Result |
| --- | --- |
| `torch.cuda.is_available()` | **True** |
| Device | NVIDIA GeForce RTX 4070, capability `(8, 9)` |
| VRAM total (torch) | 11852.9 MiB (~12.4 GB marketing) |
| Idle free before load | ~12.1 GB |
| `nvidia-smi` / NVML | **Broken** — kernel 595.71.05 vs userspace libcuda 595.84 (`Can't initialize NVML`). CUDA contexts still work; do **not** use nvidia-smi as oracle. |
| Dogfood services | left running entire run |

Supertonic paths **not** modified:

- `~/.cache/speech-out/supertonic-venv`
- `~/.cache/speech-out/supertonic-venv-gpu`
- `~/.cache/supertonic3`

Optional packages skipped vs upstream `requirements.txt` (not required for `AutoModel` zero-shot path): `deepspeed`, `tensorrt-*`, `gradio`, `fastapi` stack. Full freeze: lockfile above. `openai-whisper==20231117` required by frontend import (installed with `setuptools==69.5.1`).

---

## 3. Test utterances and voice asset

**Prompt wav:** pinned repo `asset/zero_shot_prompt.wav`  
**Prompt text (CV3):**  
`You are a helpful assistant.<|endofprompt|>希望你以后能够做的比我还好呦。`

| Name | Text (abbrev.) | Mode |
| --- | --- | --- |
| `en_zero_shot_stream_cold` | CosyVoice is undergoing a comprehensive upgrade… | stream |
| `en_zero_shot_stream_warm` | same | stream |
| `en_short_stream_warm` | Hello. This is a short warm-path latency probe. | stream |
| `zh_zero_shot_stream_warm` | 八百标兵奔北坡… | stream |
| `en_zero_shot_buffered_warm` | Buffered path check… | stream=False |
| `en_cancel_break_after_1_chunk` | long EN paragraph | stream, break after 1 chunk |

WAV artifacts (local cache, not committed):  
`~/.cache/speech-out/cosyvoice-qual-sc-e71.4/runs/20260727T051625Z/artifacts/*.wav`

---

## 4. Measured results

### 4.1 Startup / load

| Metric | Value |
| --- | ---: |
| Cold `AutoModel` load | **78.00 s** (includes first-time wetext FST download from modelscope) |
| Subsequent process load (cancel worker) | **12.83 s** (wetext cached) |
| Sample rate | **24000** |
| VRAM after load (used) | **4833 MiB** |
| Torch allocated after load | **3284 MiB** |

### 4.2 Latency / RTF / chunks

| Utterance | first PCM (s) | total (s) | audio (s) | RTF | chunks | peak alloc MiB | used MiB after |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| en stream cold | **3.331** | 6.757 | 8.68 | **0.778** | 4 | 3431 | 4919 |
| en stream warm | **2.380** | 3.688 | 7.20 | **0.512** | 2 | 3418 | 4919 |
| en short warm | **1.864** | 1.868 | 3.64 | **0.513** | 1 | 3397 | 4919 |
| zh stream warm | **2.521** | 5.356 | 9.56 | **0.560** | 3 | 3436 | 4919 |
| en buffered warm | **2.519** | 2.524 | 5.48 | **0.461** | 1 | 3399 | 4919 |
| cancel break@1 | **2.208** | 2.208 | 4.36 | 0.506 | 1 | 3416 | 5091 |

Peak VRAM **used** across utterances: **~5091 MiB (~5.0 GB)**.  
Peak torch **allocated**: **~3436 MiB**.

Cold first chunk emitted ~1.36 s of audio; warm paths often emitted larger first hops (up to ~4.36 s) because CV3 `token_hop_len` starts at 25 tokens @ 25 Hz with `pre_lookahead_len=3` and scales up.

### 4.3 Cancellation

| Method | Result |
| --- | --- |
| In-process `break` after first yielded chunk | Consumer stops immediately (`cancel_ack_s ≈ first_pcm_s`). **No native request cancel.** Upstream `tts()` still `p.join()`s the LLM worker thread. Not proof of synthesis cancellation (contract § cooperative cancellation). |
| Process-per-utterance **SIGKILL** after first `CHUNK` | **Works.** first chunk ~2.83 s after load-complete; kill ~50 ms later; worker rc=137; GPU used dropped to **~285 MiB** in parent probe after child death. |

**Adaptation required for product:** process-per-utterance **or** fork `tts()` with a cancel event that stops the LLM worker and clears session uuid dicts. Stock FastAPI does not pass `stream=True` by default (navigator finding) — progressive path must set it explicitly.

### 4.4 Cleanup

After `del cosyvoice` + `gc.collect()` + `torch.cuda.empty_cache()`:

- used ≈ **3873 MiB** still held (allocator / ORT residue — not full reclaim in-process)
- process kill reclaims essentially all GPU context (see cancel worker)

### 4.5 Coexistence / dogfood impact

| Check | Before | After |
| --- | --- | --- |
| `speech-core-daemon` PIDs | 1731, 1733 | **unchanged** |
| `speech-out daemon` PID | 1744 | **unchanged** |
| listen `:8788` | speech-out | **unchanged** |
| Supertonic venvs | intact | **intact** |
| Services stopped? | no | **no** |

No speech-in/alignment concurrent load was injected this run (daemons idle, no GPU context). Solo TTS VRAM headroom on 12 GB: ~5 GB used → ~7 GB free theoretical; concurrent ASR+TTS not measured — **explicit gap**.

### 4.6 Quality (operator-facing)

- EN and ZH zero-shot WAV files produced without exception; audible clone path exercised with stock prompt.
- No MOS panel or WER loop in this bead — qualitative smoke only.
- Artifacts retained under the run `artifacts/` directory for reviewer listening.

---

## 5. Gate comparison (evidence vs adapt criteria)

Criteria from bead notes / navigator (warm first PCM 300–500 ms, cancel ≤1 hop, ≤8–9 GB VRAM solo, RTF<1, no host impact):

| Gate | Target | Observed | Met? |
| --- | --- | --- | --- |
| Warm first PCM | 300–500 ms | **1.86–2.38 s** (short/warm EN) | **No** |
| RTF | < 1 | 0.46–0.78 | **Yes** |
| VRAM solo | ≤ 8–9 GB | ~5.0 GB used / ~3.4 GB peak alloc | **Yes** |
| Cancel granularity | ≤1 hop clean | process-kill yes; native no | **Partial — needs adaptation** |
| Host / dogfood | no replace/stop | PIDs/ports unchanged | **Yes** |
| Progressive PCM | stream path | yes with `stream=True` | **Yes** |
| License clarity | production-clear | academic disclaimer remains | **No — legal open** |

**Engineering read (not ratification):** pin loads and streams on the available GPU without touching dogfood; RTF and solo VRAM clear; **first-PCM latency misses the 300–500 ms warm target by ~4–8×** on stock CV3 hop settings; cancellation must be process-level or a code adaptation; legal disclaimer blocks silent product adoption.

---

## 6. Rollback

1. Do **not** point `speech-out` at CosyVoice; leave `SPEECH_OUT_BACKEND=supertonic-http` / managed Supertonic as today.  
2. Qualification tree is disposable:  
   `rm -rf ~/.cache/speech-out/cosyvoice-qual-sc-e71.4`  
   (does not affect Supertonic caches).  
3. wetext side cache: `~/.cache/modelscope/hub/pengzhendong/wetext` (optional remove).  
4. No production config, unit, or binary was changed by this run.

---

## 7. Reproduce

```bash
# from speech-core checkout
./scripts/cosyvoice_qual/reproduce.sh
# or with explicit roots:
COSYVOICE_QUAL_ROOT=~/.cache/speech-out/cosyvoice-qual-sc-e71.4 \
  ./scripts/cosyvoice_qual/reproduce.sh --skip-download
```

Expected independent checks:

1. `report.json` pin commit/revision/hashes match this document.  
2. `torch.cuda.is_available() is True` on the qual venv.  
3. At least one stream utterance with `first_pcm_s` recorded and RTF &lt; 1.  
4. speech-out PID on `:8788` unchanged across the run.  
5. Listen to `artifacts/en_zero_shot_stream_warm.wav` and `zh_zero_shot_stream_warm.wav`.

---

## 8. Failures and gaps (explicit)

1. **Warm first-PCM gate failed** (seconds, not hundreds of ms). Stock hop/lookahead dominates; bistream + smaller hops / TensorRT / vLLM not qualified.  
2. **No native cancel API** — generator `break` insufficient; process-per-utterance demonstrated.  
3. **In-process CUDA cleanup incomplete** after `empty_cache`.  
4. **Concurrent speech-in + alignment + TTS VRAM** not measured (services idle).  
5. **nvidia-smi unusable** on this host; metrics from `torch.cuda` only.  
6. **Legal/academic disclaimer** unresolved.  
7. **CosyVoice2 fallback** not GPU-executed this run.  
8. **RL head** `llm.rl.pt` downloaded but not swapped/measured.  
9. Upstream extras (deepspeed/TRT/gradio) not installed — lock is lean inference, not full upstream freeze.

---

## 9. Artifact index

| Path | Role |
| --- | --- |
| `docs/qualification/cosyvoice-sc-e71.4.md` | this report |
| `docs/qualification/cosyvoice-sc-e71.4-report.json` | full machine report |
| `docs/qualification/cosyvoice-sc-e71.4-requirements-lock.txt` | pip freeze |
| `scripts/cosyvoice_qual/run_qualification.py` | harness |
| `scripts/cosyvoice_qual/reproduce.sh` | reproducible commands |
| `~/.cache/speech-out/cosyvoice-qual-sc-e71.4/` | isolated env, models, WAVs, logs |

Navigator pin source: `our-town-v2` transcript `run_cc05d0b5d727`.
