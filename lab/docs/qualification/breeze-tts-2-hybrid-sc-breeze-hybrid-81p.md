# Breeze TTS 2 hybrid report (sc-breeze-hybrid-81p)

Recommendation: SHELF

| configuration | peak VRAM | p50 TTFA | p95 TTFA | RTF | stream jitter | quality notes |
| --- | --- | --- | --- | --- | --- | --- |
| A | 7.777720928192139 | 0.3961049644541927 | 0.4064582853668835 | 2.0568514288243023 | 0.17882308857282608 | baseline |
| B_depth | 8.020729541778564 | 0.1931203030264005 | 0.19885306332539768 | 0.7822083514739377 | -0.03312325144303045 |  |
| B_codec | 8.75372314453125 | 0.23293053751438855 | 0.24369248476903882 | 2.019072965065954 | 0.08724307950353241 |  |
| B_backbone_decode | 8.045101642608643 | 0.38351661499217155 | 0.406012363828253 | 1.8678112569680458 | 0.1490928285440895 |  |
| B_backbone_prefill | 8.042840480804443 |  |  |  |  | unsafe_vram |
| B_winner | 8.75372314453125 | 0.3961049644541927 |  |  | 0.17882308857282608 |  |
| C0 | 7.408604621887207 | 0.8787404364822432 | 0.9115021746288985 | 4.427774772135308 | 0.5618792932399082 |  |
| C1 | 7.408734321594238 | 0.6586942109726369 | 0.6765891845901496 | 3.0493518333737195 | 0.32808205627137776 |  |
| C2 | 7.424603462219238 | 0.4217942430269904 | 0.43280108802532774 | 2.9992615402969682 | 0.161239622986177 | comparable vs A |
| C3 |  |  |  |  |  | correctness_changed |
| C4 |  |  |  |  |  | correctness_changed |
| E1 | 8.020768642425537 | 0.1881939439782873 | 0.20284344189381226 | 0.7598434879566245 | -0.031491857751971206 |  |
| E2 | 8.769330024719238 | 0.1388841759627685 | 0.14892522151744925 | 0.7896480518155096 | -0.013632349537219923 | comparable vs A |
| E3 |  |  |  |  |  | jitter_worse |
| E4 |  |  |  |  |  | jitter_worse |
| E5 |  |  |  |  |  | jitter_worse |
| D | 7.176548957824707 | 2.536713011025451 | 2.5808502262206745 | 14.638987727839947 | 2.2317230500781458 | d_killed |

qual_root: ~/.cache/speech-out/breeze-tts-qual-sc-breeze-hybrid-81p

SHELF means the best config is characterized and the bead is parked for a later resource-allocation decision. It is not permission to integrate, propose a pin swap, or spawn a follow-on wiring bead. Recommendation is against this experiment's own A, not leftover.

## Best ≤9 GB E vs best ≤9 GB hybrid

best_e_le_9gib: E2
best_hybrid_le_9gib: C2
e_rejected_for_our_purposes: false

E2 p50 TTFA 0.138884s / RTF 0.789648 / peak 8.76933 GiB vs C2 p50 TTFA 0.421794s / RTF 2.99926 / peak 7.4246 GiB.

## Leftover (commentary only)

Leftover qwentts first-usable is ~70 ms / ~2.4 GB (commentary only; not a pin-swap gate). A win vs leftover still parks the bead.

leftover_restored: true
unit: ~/.config/systemd/user/ata-speech-tts.service
listen: 127.0.0.1:18091
model: ~/.local/share/speech-out/qwen3-tts-0.6b-20260820/models/gguf/qwen-talker-0.6b-customvoice-Q8_0.gguf
codec: ~/.local/share/speech-out/qwen3-tts-0.6b-20260820/models/gguf/qwen-tokenizer-12hz-Q8_0.gguf

## First-audio stage trace

trace_unavailable: full-30 metrics.json does not persist collect_timing stage breakdown (text encoder, backbone prefill, first backbone decode, depth decode, codec, pcm emission).
A: first_model_output_s=0.0294636 first_codec_frame_s=0.39611 first_pcm_s=0.39611 first_nonsilent_s=0.401985
C2: first_model_output_s=0.209061 first_codec_frame_s=0.411847 first_pcm_s=0.411847 first_nonsilent_s=0.420097
E2: first_model_output_s=0.0290365 first_codec_frame_s=0.125902 first_pcm_s=0.125902 first_nonsilent_s=0.134152
E1: first_model_output_s=0.0264765 first_codec_frame_s=0.174369 first_pcm_s=0.174369 first_nonsilent_s=0.182619
B_depth: first_model_output_s=0.0280413 first_codec_frame_s=0.182686 first_pcm_s=0.182686 first_nonsilent_s=0.190936
