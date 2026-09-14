# CosyVoice2-0.5B qualification — sc-zxf

**bead:** `sc-zxf`  
**executed:** 2026-08-17T10:41Z → 2026-08-17T10:56Z (UTC)  
**host:** local RTX 4070 12 GB (12282 MiB), Ubuntu, driver visible via nvidia-smi  
**status of this document:** isolated-pin evidence from 2026-08-17 — **not** live. live mouth is qwentts (sc-o5i). CosyVoice is rollback only.

Machine twin: [`cosyvoice2-sc-zxf-report.json`](./cosyvoice2-sc-zxf-report.json)  
Reproduce (isolated only): [`../../scripts/cosyvoice_qual/reproduce-sc-zxf.sh`](../../scripts/cosyvoice_qual/reproduce-sc-zxf.sh)  
Shared prompts: [`../../scripts/cosyvoice_qual/sc-zxf-prompts.txt`](../../scripts/cosyvoice_qual/sc-zxf-prompts.txt)

---

## Verdict

| Decision | Value |
| --- | --- |
| **prefer-cv2** | **no** |
| **keep live pin** | **aa86b67 / CosyVoice3 TRT** |
| **swap recommended** | **no** |
| **rollback** | **do nothing** — live unit was never switched |

Rule (binding): prefer CosyVoice2 only if isolated first-real-PCM p50 **meaningfully beats** live CosyVoice3 TRT (~282 ms historical / 284 ms now-stamp) **and** quality is not worse. Isolated CV2 AutoModel (host opts ON) first-real-PCM p50 is **1149 ms** (fp32) / **1250 ms** (fp16). That is ~4× slower than the live pin. Quality proxies are not a reason to swap either.

Triton + TensorRT-LLM fp16 warm (operator-specified isolated target) was **inspected only**. It is **BLOCKED-COEXISTENCE** on this 12 GB card beside the live ~5 GB pin. It was not served.

---

## 1. Pins (exact)

### 1.1 Live CosyVoice3 TRT (do not disturb)

| Item | Value |
| --- | --- |
| Unit | `speech-out-daemon.service` |
| Description | `Speech Out CosyVoice3 TensorRT low-latency candidate (aa86b67)` |
| Candidate | `/home/sf/.local/share/discord-voice-agent/candidates/speech-out-cosy3-trt-aa86b67-20260812` |
| Binary | `.../bin/speech-out-aa86b67` |
| Main PID (entire run) | **4345** |
| Worker PID (entire run) | **4378** |
| ActiveEnterTimestamp | Sun 2026-08-16 14:57:23 AEST (unchanged after every GPU action) |
| Bind | `0.0.0.0:8788` |
| Model | `/home/sf/.cache/speech-out/cosyvoice-qual-sc-e71.4/models/Fun-CosyVoice3-0.5B` |
| Model id / rev | `FunAudioLLM/Fun-CosyVoice3-0.5B-2512` @ `29e01c4e8d000f4bcd70751be16fa94bf3d85a18` |
| Code | `FunAudioLLM/CosyVoice` @ `074ca6dc9e80a2f424f1f74b48bdd7d3fea531cc` |
| Worker | candidate `worker/cosyvoice_progressive_worker.py` |
| Env file | `~/.config/speech-core/speech-out.env` (**not edited**) |
| Live knobs | `LOAD_TRT=1` `FLOW_STEPS=4` `BASE_HOP=5` `POLL_SLEEP=0.001` `FP16=0` |
| Ref wav sha256 | `c7b31d6dbe7cc6a716dded00550db5b50940bf209e424e4ad207b12e657c8ff6` |
| Ref text | `You are a helpful assistant.<\|endofprompt\|>希望你以后能够做的比我还好呦。` |
| Live GPU | worker **4946 MiB**; card 5258 / 6596 free / 12282 total |

This unit was probed on `:8788` only. A second CosyVoice3 was **not** loaded.

### 1.2 Isolated CosyVoice2-0.5B

| Item | Value |
| --- | --- |
| Model id / rev | `FunAudioLLM/CosyVoice2-0.5B` @ `eec1ae6c79877dbd9379285cf8789c9e0879293d` |
| Code | `FunAudioLLM/CosyVoice` @ `074ca6dc9e80a2f424f1f74b48bdd7d3fea531cc` |
| Matcha-TTS | `dd9105b34bf2be2230f4aa1e4769fb586a3c824e` |
| Pin root | `/home/sf/.cache/speech-out/cosyvoice2-lane2-20260812` |
| Model dir | `.../models/CosyVoice2-0.5B` |
| Code root | `.../src/CosyVoice` |
| Download status | `OK` (`lane2/artifacts/model-download.status`) |
| Python | `/home/sf/.cache/speech-out/cosyvoice-qual-sc-e71.4/venv/bin/python` (3.10.20, torch `2.3.1+cu121`) |
| Worker (isolated copy) | `$CAMPAIGN/lane2/artifacts/cosyvoice_progressive_worker.py` |
| Harness | `$CAMPAIGN/lane2/artifacts/ttfp_harness.py` |
| Ref wav | pin `src/CosyVoice/asset/zero_shot_prompt.wav` sha256 `c7b31d6dbe7cc6a716dded00550db5b50940bf209e424e4ad207b12e657c8ff6` |
| Ref text (CV2 official) | `希望你以后能够做的比我还好呦。` |
| Hash file | `$CAMPAIGN/lane2/pins/model-sha256.txt` (re-verified 2026-08-17, all OK) |

Content SHA-256 (re-checked this run):

| File | sha256 |
| --- | --- |
| `llm.pt` | `b144ef55b51ce8cfb79a73c90dbba0bdaba4e451c0ebcfab20f769264f84a608` |
| `flow.pt` | `ff4c2f867674411e0a08cee702996df13fa67c1cd864c06108da88d16d088541` |
| `hift.pt` | `3386cc880324d4e98e05987b99107f49e40ed925b8ecc87c1f4939432d429879` |
| `speech_tokenizer_v2.onnx` | `d43342aa12163a80bf07bffb94c9de2e120a8df2f9917cd2f642e7f4219c6f71` |
| `speech_tokenizer_v2.batch.onnx` | `5b45a98572ed21e3a3ebf50201f3020567f7db40e9a57509b790b2982f5c07b7` |
| `flow.decoder.estimator.fp32.onnx` | `cd54e4281701e6630730da64502d77b7e8b6e5c057cca65128bffb50f85cbf98` |
| `campplus.onnx` | `a6ac6a63997761ae2997373e2ee1c47040854b4b759ea41ec48e4e42df0f4d73` |
| `cosyvoice2.yaml` | `0af2c0d010c477187c39f3e8fd5f1ae2e4e6f90ad03ba37c10ed6c6a87b05959` |
| `CosyVoice-BlankEN/model.safetensors` | `130282af0dfa9fe5840737cc49a0d339d06075f83c5a315c3372c9a0740d0b96` |

Note: `flow.decoder.estimator.fp32.onnx.part` (83886080 B) remains beside the complete onnx. The complete file hashed. Isolated arms used official AutoModel **without** `load_trt` / `load_jit` (no `flow.encoder*.zip`; TRT compile forbidden while live is up).

---

## 2. Clock contract

Clock starts at first usable text **into CosyVoice**, not OMP start, not model load, not harness spawn.

Worker clock **W** = `time.perf_counter` in the isolated worker / live worker child.

| id | meaning | domain |
| --- | --- | --- |
| t0 | speak text handed to inference (`speak` / worker receive) | **W** |
| token | `tokens_ready_s` — first speech tokens ready (LLM) | **W** |
| acoustic | `token2wav_first_s` — first flow+HiFT | **W** |
| pcm | first **real** non-silent PCM (`rms > 1e-4` f32). Also record first-any and worker `first_pcm` | **W** |
| live D | daemon `request_received` → first ws PCM frame | **daemon_mono only** |

**Never subtract D − W.** Live `:8788` numbers are a now-stamp of the running unit, not a replacement for the n=25 historical worker baseline.

---

## 3. Live unit now-stamp (`:8788` only)

Same DEFAULT_PROMPTS as the isolated harness (`scripts/cosyvoice_qual/sc-zxf-prompts.txt`), **n=5**, skip=2 (first measured harness texts after 2 warmups). Client: candidate `speech-out-aa86b67 play` with `--play-command true`. No extra GPU model.

| Metric (daemon_mono) | n=5 |
| --- | ---: |
| request_received → first PCM p50 | **283.79 ms** |
| same, p95 | 397.97 ms |
| min / max | 268.01 / 426.15 ms |
| worker_first_pcm_latency_ms p50 | 283.74 ms |
| completed | 5/5 |
| token / acoustic on WS | **not_on_daemon_ws** |

One earlier one-shot of prompt 0 (`Hello. This is a short warm-path latency probe.`) was 385.20 ms D first-PCM (cold-ish after idle); the n=5 set is the written now-stamp.

Live pids **4345 / 4378** and `ActiveEnterTimestamp` were unchanged after the probe. nvidia-smi stayed at live-only 4946 MiB.

Historical CV3 TRT n=25 (do **not** reload CV3; file `$CAMPAIGN/lane1/results/trt_s4_h5_p1ms_n25.json`):

| Stage (W) | p50 | p95 |
| --- | ---: | ---: |
| first real PCM | **281.96 ms** | 603.39 ms |
| tokens_ready | **177.78 ms** | 324.00 ms |
| token2wav_first | **99.81 ms** | 103.56 ms |
| hop / token_count_at_first | 5 / 11 | 5 / 11 |
| non-silent | 25/25 | |

Historical cancel (`lane1/results/trt-cancel-survival.json`): ack **161.01 ms**, 0 frames after ack, same pid survived.

---

## 4. Isolated CosyVoice2 AutoModel

All GPU loads went through `$CAMPAIGN/gpu-run` (`CAMPAIGN_LANE=cosy2`). Live pids snapshotted before and after every load. Live unit survived every arm.

### 4.1 Arm A — speaker_cache / host opts ON, fp32, n=20 + 2 warmup

Result: `$CAMPAIGN/lane2/results/sc-zxf/speaker_cache_fp32_n20.json`

| Stage (W) | p50 | p95 |
| --- | ---: | ---: |
| t0 → first real PCM | **1149.26 ms** | 1169.91 ms |
| t0 → first any PCM | 1147.52 ms | 1168.95 ms |
| worker first_pcm event | 1146.59 ms | 1167.99 ms |
| tokens_ready | **617.96 ms** | 639.23 ms |
| token2wav_first | **520.82 ms** | 542.03 ms |
| hop / token_count_at_first | 25 / 41 | 25 / 41 |
| non-silent | 20/20 | |
| host RTF (synth/audio) | 0.568 | 0.637 |
| model load / warm_ready | 14.31 s / 20.01 s | |

Old n=5 speaker_cache (not enough): p50 1348 ms. This n=20 is faster than that smoke but still ~4× the live CV3 TRT pin.

### 4.2 Arm B — same + `SPEECH_OUT_COSYVOICE_FP16=1`

Result: `$CAMPAIGN/lane2/results/sc-zxf/speaker_cache_fp16_n20.json`  
Loaded without killing live. **Did not help TTFA.**

| Stage (W) | p50 | p95 |
| --- | ---: | ---: |
| t0 → first real PCM | **1249.96 ms** | 1270.48 ms |
| tokens_ready | 681.45 ms | 704.01 ms |
| token2wav_first | 563.98 ms | 585.52 ms |
| hop / token_count_at_first | 25 / 41 | 25 / 41 |
| non-silent | 20/20 | |
| model load / warm_ready | 16.21 s / 21.28 s | |

### 4.3 VRAM (coexistence)

nvidia-smi is usable on this host today. Isolated process VRAM is from `torch.cuda` snapshots emitted by the isolated worker (`model_loaded` / `worker_ready`). nvidia-smi after process exit returns to live-only.

| Probe | Value |
| --- | ---: |
| Live worker nvidia-smi (steady) | **4946 MiB** |
| Card before isolated load | 5258 used / **6596 free** / 12282 total |
| Arm A after load (`torch.cuda.mem_get_info` used) | **9124.8 MiB** card-used; torch alloc **2442** / reserved **2642** |
| Arm A after prompt cache | **9164.8 MiB** card-used; free seen by torch **2688** |
| Isolated incremental (card used − live card used) | **~3.9 GiB** |
| After isolated process exit | live-only again (4345/4378 alive, 4946 MiB) |

12 GB held live CV3 TRT + isolated CV2 AutoModel. Headroom after both warm: ~2.7 GiB free. That is **not** enough for Triton + TensorRT-LLM beside the live pin.

### 4.4 Cancel (isolated CV2 worker)

Helper: `$CAMPAIGN/scripts/trt_cancel_survival.py`  
Result: `$CAMPAIGN/lane2/results/sc-zxf/cv2-cancel-survival.json`

| Check | Observed |
| --- | --- |
| cancel_ack | **yes** (`type=cancel_ack`) |
| cancel → ack | **7.08 ms** |
| frames after ack | **0** |
| same pid synthesizes after | **yes** (post-cancel `completed`, pid 3677845) |
| live unit | still 4345 / 4378 |

---

## 5. Quality

CV2 is a **latency hatch**, not a quality upgrade. Official CosyVoice README (same code pin):

| Model | test-zh CER↓ | test-zh SS↑ | test-en WER↓ | test-en SS↑ |
| --- | ---: | ---: | ---: | ---: |
| CosyVoice2-0.5B | 1.45 | 75.7 | 2.57 | 65.9 |
| Fun-CosyVoice3-0.5B-2512 | 1.21 | 78.0 | 2.24 | 71.8 |

Same-prompt listen artifacts (isolated CV2 fp32, official CV2 ref text; historical CV3 TRT wavs reused — **no second CV3 load**):

- CV2 wavs: `$CAMPAIGN/lane2/results/sc-zxf/audio/cv2-quality10-fp32/{01..10}.wav`
- CV3 wavs: `$CAMPAIGN/lane1/audio/quality10-trt-s4-h5-p1ms/{01..10}.wav`
- prompts: `$CAMPAIGN/lane1/quality-prompts.txt`

CPU proxies (no extra GPU):

| Proxy | CV3 TRT historical | isolated CV2 fp32 |
| --- | ---: | ---: |
| faster-whisper-base.en int8 CPU micro-WER | 0.333 (4/10 exact) | 0.318 (3/10 exact) |
| campplus prompt cosine p50 | 0.690 | 0.718 |
| campplus CV3↔CV2 pairwise p50 | — | 0.838 |

Files: `$CAMPAIGN/lane2/results/sc-zxf/quality10-whisper.json`, `quality10-campplus.json`.

Read: content WER is **not worse** on this 10-prompt CPU whisper loop; speaker cosine to the shared prompt is similar/slightly higher for CV2; pairwise 0.84 is “same family,” not identity. Official published scores still prefer CV3. **This is not a quality upgrade.** Combined with the TTFA loss, prefer-cv2 stays **no**.

---

## 6. Triton + TensorRT-LLM fp16 (inspect only — not served)

| Item | State |
| --- | --- |
| Image | `soar97/triton-cosyvoice:25.06` id `b8b6df1f4f4e` digest `sha256:b8b6df1f4f4ef248ccb3639b8fc6af446b1e76bc8608f4aaf44e392cdc399a27` (on disk, 23.3 GB / 61.6 GB unpacked) |
| Campaign | `/home/sf/.cache/speech-out/triton-campaign-20260812` |
| CV2 LLM `model.safetensors` | **1283621816 B + `.aria2` present** → treat as **incomplete** |
| CV3 LLM | 1284681320 B + leftover `.part` |
| Engines / `.plan` | **none** (`engines/` empty; `cv3_1_engine_build` previously BLOCKED) |
| Prior readiness | `stage-readiness.json`: full serve **HIGH oom risk** on 12 GB even without the live 5 GB pin |
| Would-be VRAM | live already **~5.0 GiB**; isolated AutoModel added **~3.9 GiB** and left **~2.7 GiB**. Triton multi-model + `trtllm-serve` will not fit beside live. |

**BLOCKED-COEXISTENCE.** Docker Triton / `trtllm-serve` / new `.plan` compile were **not** started.

---

## 7. Live unit after the run

| Check | After last GPU action |
| --- | --- |
| Description | `Speech Out CosyVoice3 TensorRT low-latency candidate (aa86b67)` |
| ActiveState | `active` / `running` |
| MainPID | **4345** |
| Worker | **4378** |
| ActiveEnterTimestamp | Sun 2026-08-16 14:57:23 AEST |
| Candidate path | unchanged aa86b67-20260812 |
| nvidia-smi | 4378 @ 4946 MiB; 5258 / 6596 free |
| speech-out.env / unit / candidate tree | **not edited** |

---

## 8. Why CV2 loses TTFA here

Isolated CV2 warm path still uses stock hop **25** and needs **41** tokens before first token2wav (vs live CV3 TRT hop **5** / **11** tokens, 4 flow steps, official TRT estimator). Token stage alone is ~618 ms; acoustic ~521 ms; sum matches the ~1149 ms first PCM. fp16 did not shrink either stage. JIT is unavailable (`flow.encoder*.zip` missing). TRT compile for CV2 was forbidden while live holds the GPU.

---

## 9. Rollback

Do nothing. Live unit was never pointed at CosyVoice2. Isolated trees are disposable cache:

- `/home/sf/.cache/speech-out/cosyvoice2-lane2-20260812`
- `$CAMPAIGN/lane2/results/sc-zxf/`

---

## 10. Artifact index

| Path | Role |
| --- | --- |
| `docs/qualification/cosyvoice2-sc-zxf.md` | this report |
| `docs/qualification/cosyvoice2-sc-zxf-report.json` | machine twin |
| `docs/current-state.md` | live-pin pointer |
| `scripts/cosyvoice_qual/reproduce-sc-zxf.sh` | isolated commands only |
| `scripts/cosyvoice_qual/sc-zxf-prompts.txt` | shared prompts |
| `scripts/cosyvoice_qual/live_8788_probe.py` | live `:8788` now-stamp |
| `$CAMPAIGN/lane2/results/sc-zxf/` | raw JSON, wavs, pid/smi snapshots |
