# CosyVoice2 bake-off — first audio vs parked CV3

**beads:** `sc-cv2-triton-trtllm-vw8` (closed), `sc-cv2-bakeoff-2pe`  
**host:** RTX 4070 12 GB, 2026-08-17  
**live pin then:** `speech-out-daemon` **parked** (aa86b67 CosyVoice3 TRT). **live pin now (2026-08-18):** qwentts.cpp. this bake-off is historical. CosyVoice is not the production mouth.

same prompt / voice for A and B:

- target: `收到好友从远方寄来的生日礼物，那份意外的惊喜与深深的祝福让我心中充满了甜蜜的快乐，笑容如花儿般绽放。`
- ref: `希望你以后能够做的比我还好呦。` + stock `zero_shot_prompt.wav`

## clocks (do not mix)

| id | zero |
|---|---|
| **T_text0** | first clause / first usable text exists |
| **T_text_done** | full sentence known and handed to the engine |
| **T_pcm** | first real non-silent PCM / first gRPC waveform chunk |

if the sentence is **already finished**, the product clock is **T_text_done**.  
if an LLM is **still emitting tokens**, the product clock is **T_text0**.  
never compare T_text0-bistream to T_text_done-triton.

## results

| arm | what | n | first-audio p50 | p95 | clock |
|---|---|---:|---:|---:|---|
| **B Triton Unet** fp16, Decoupled, hop 15+3 | full text in, streaming PCM out | 7 (after 1 warmup) | **224.9 ms** | 290 ms | T_text_done |
| **C CV3 TRT aa86b67** (parked) | live pin historical | 25 | **282.0 ms** | 603 ms | T_text_done |
| **C CV3** live :8788 now-stamp | same pin, n=5 | 5 | **283.8 ms** | 398 ms | T_text_done (daemon) |
| **A1 AutoModel** full-text `stream=True` | pytorch CV2, no worker hop/poll patches | 8 | **2354 ms** | 2379 ms | T_text_done |
| **A2 AutoModel bistream** 0 ms inter-clause | official 4-clause generator, `stream=True` | 8 | **2463 ms** | 2472 ms | **T_text0** |
| **A3 AutoModel bistream** 80 ms / clause | simulated still-arriving LLM | 8 | **2658 ms** | 2676 ms | **T_text0** |

raw: `triton-campaign-20260812/artifacts/cv2-first-chunk.json` (first 2 timed), docker `n3–n7` logs, `artifacts/cv2-bakeoff-automodel.json`.

L20 README Unet streaming cache=False conc=1: avg 220.43 / p50 218.07. 4070 timed p50 **224.9** matches.

## does bistream skim time?

**not on this path, not with a finished sentence.**

A2 vs A1 on **T_text0 / T_text_done-as-known**: bistream is **~110 ms slower**, not faster. first `yield speech` in the logs is **after** `no more text token, decode until met eos`. incremental text was consumed; PCM still waited for the big first hop (~4.36 s of audio). mix is 5:15; CV2 `token_hop_len` is 25. we did **not** get mouth-moving-while-text-arrives as audible PCM.

A3 adds the 3×80 ms clause delays onto T_text0 (~+300 ms). that is the LLM-overlap case. it still loses to A1 on T_text0 because PCM does not start during the gaps. the 80 ms sleeps just postpone the same late first hop.

the **0.70 s T_text_done** number on A2 is a trap: that clock starts when the LM *finishes pulling* the last clause (~1.76 s in), not when we *had* the sentence. ignore it for product choice.

**150 ms** remains unreproduced. official bistream example even uses `stream=False` (one wav after all text).

## pick

| product shape | fastest honest arm |
|---|---|
| sentence / clause already known | **B Triton Unet ~225 ms** (beats parked CV3 ~284 ms by ~60 ms) |
| tokens still arriving from an LLM | A3 as measured **does not win**. path A needs a smaller hop / earlier token2wav, or a new engine. not this bake-off. |

do not swap the systemd unit. triton is an isolated container (`triton-cv2-sc`), restore live with the park snapshot `unpark.sh`.

## not measured here

- speaker-cache Unet (`use_spk2info_cache=True`) — L20 says ~185 ms
- AutoModel + progressive-worker hop5/poll (sc-zxf 1149 ms was short EN prompts, different text)
- cancel on Unet
- quality MOS vs CV3 on the birthday sentence
