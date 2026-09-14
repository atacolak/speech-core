# E2 is the selected Breeze runtime

not on the voicecat path. not a pin swap. live mouth stays leftover qwentts.

**Decision:** E2 — official bf16, `fast=[depth,codec]`.

Recorded on epic `sc-breeze-hybrid-81p` after the 4070 qualification
(30-run short-class protocol, pin `43e2ea15`).

## Why E2 won

Numbers recovered from
`lab/docs/qualification/breeze-tts-2-hybrid-sc-breeze-hybrid-81p.md`
and the copy under `tts/experiments/reports/`. Do not invent extras.

| config | what it was | p50 TTFA | RTF | peak VRAM | outcome |
|---|---|---|---|---|---|
| A | official bf16 eager baseline | 0.396 s | 2.06 | 7.78 GiB | baseline |
| B_depth | official + depth graphs | 0.193 s | 0.78 | 8.02 GiB | useful stage |
| B_codec | official + codec graphs | 0.233 s | 2.02 | 8.75 GiB | codec graphs alone not enough |
| B_backbone_decode | official + backbone-decode graphs | 0.384 s | 1.87 | 8.05 GiB | little help |
| B_backbone_prefill | official + backbone-prefill graphs | — | — | 8.04 GiB | unsafe_vram / killed |
| C0 | hybrid int8 eager | 0.879 s | 4.43 | 7.41 GiB | slower than A |
| C1 | hybrid + depth graphs | 0.659 s | 3.05 | 7.41 GiB | still slower |
| C2 | hybrid + depth/codec graphs (best hybrid) | 0.422 s | 3.00 | 7.42 GiB | lost to A |
| C3 / C4 | hybrid + backbone graphs | — | — | — | correctness_changed |
| D | full int8 control | 2.537 s | 14.64 | 7.18 GiB | killed |
| E1 | official bf16 + depth graphs, VoiceCat warmup | 0.188 s | 0.76 | 8.02 GiB | close |
| **E2** | **official bf16 + depth+codec graphs, VoiceCat warmup** | **0.139 s** | **0.79** | **8.77 GiB** | **selected** |
| E3 / E4 / E5 | extra graph stages | — | — | — | jitter_worse |

E2 first_pcm 0.126 s, first_nonsilent 0.134 s, startup 11.1 s.
Direction quality was recorded as comparable vs baseline A.

Hybrid int8 did not buy speed: C2 (best hybrid) was slower than eager A.
The speedup was CUDA graphs on official bf16, not quantization.

VRAM ceiling was 9 GiB. E2 peak 8.77 GiB fits. That is why it is the
runtime, not a pile of competing presets.

## What this means for the lab

The playground's ordinary surface is E2 only. A / B / C / D / E1 / E3+
are historical experiment arms. Reproduce them from the report and the
qual configs if needed; they are not first-class UI choices.

Dual-cfg is a separate experimental path. Fast E2 graphs reject it.
See `tts/experiments/reports/dual-cfg.md`.
