# CV2 vs CV3 listen — official refs

**historical clone hunt.** live mouth is qwentts named-speaker CustomVoice, not these refs.

**ui:** https://sfub.taila159c4.ts.net:18080/

game rips (deus ex / jfk / golden-mic) are off the grid. they were a bad clone substrate.

## what "stock voices" actually are

cv2/cv3 **do not ship a named sft roster**. official github `asset/` is two wavs: `zero_shot_prompt.wav` (zh female, 3.5s) and `cross_lingual_prompt.wav` (en male, 13.8s). hf/modelscope model trees have `dingding.png` only. cv1 sft has 7 embedding ids (`中文女` etc), not wavs. community `spk2info` (`001`/`longxiaoxia`/`xiaohe`) is embeddings + prompt_text, no matching wavs.

the real official prompt pack is **[FunAudioLLM/CV3-Eval](https://github.com/FunAudioLLM/CV3-Eval)** `data/subjective_zeroshot/waveform/` — ~190 clean 16 kHz mono refs (domain / accent / emotion / speed). apache-2.0. not "in-model speakers"; they are the official eval clone set.

not used here: celebrity/character clones in that pack (trump, 马云, peppa, c3po).

## this grid

| id | source | prompt |
|---|---|---|
| `zh_default` | CosyVoice `asset/zero_shot_prompt.wav` | 希望你以后能够做的比我还好呦。 |
| `en_book_f` | CV3-Eval audiobook EN female | I am the ghost of Christmas present. … |
| `en_pod_m` | CV3-Eval podcast EN male | Building and maintaining optimal gut health … |
| `en_conf_f` | CV3-Eval conference EN female | OKay, so it's a pleasure to introduce my colleague Simon PJ. |
| `zh_book_f` | CV3-Eval audiobook ZH female | 我说你这只大鸟，真是不讲理… |
| `zh_news_m` | CV3-Eval news ZH male | 加大力度整治政绩工程、形象工程、面子工程。 |

2 zh + 6 technical en. 6×8 = 48 cells, both models. live `aa86b67` still parked.

| voice | cv2 unet p50 | cv3 trt p50 |
|---|---:|---:|
| zh_default | **232** | 294 |
| en_book_f | 253 | **268** |
| en_pod_m | **243** | 271 |
| en_conf_f | **242** | 265 |
| zh_book_f | **248** | 288 |
| zh_news_m | **244** | 267 |
| all | **244** | **270** |

cv3 `zh_default` has three slow outliers (448 / 485 / 516). the other five voices sit 256–274.

## leftover

~180 more official refs on disk at `~/.cache/speech-out/triton-campaign-20260812/cv3-eval-10speak/repo/data/subjective_zeroshot/waveform/`. drop a name if you want a dialect / emotion / speed row next.
