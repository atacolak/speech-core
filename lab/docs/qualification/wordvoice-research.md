# WordVoice research — word-level control, not a streamer (2026-08-18)

**not live. not loaded.** leftover gpu stays `qwentts-tts-server` @ **2440 MiB**.
this is desk research against the live qwentts pin and the parked CosyVoice3 tree.

sources:

- paper [arXiv:2607.06461](https://arxiv.org/abs/2607.06461) (Nie et al., 2026-07-07)
- code [XXH333/WordVoice-main](https://github.com/XXH333/WordVoice-main)
- weights [XXH333/WordVoice-base-0.5B](https://huggingface.co/XXH333/WordVoice-base-0.5B)
- space [XXH333/wordvoice-tts](https://huggingface.co/spaces/XXH333/wordvoice-tts)
- demo [xxh333.github.io/wordvoice-demo](https://xxh333.github.io/wordvoice-demo/)
- data [XXH333/WordVoice-5A](https://huggingface.co/datasets/XXH333/WordVoice-5A) (~4.7 kh, cc-by-4.0)

---

## what it actually is

a **CosyVoice3 fork** with an extra "plan this word, then emit its speech tokens" loop.

not a streaming-tts paper. not a first-pcm paper. not a qwen paper.

they take `Fun-CosyVoice3-0.5B-2512` (same family as parked `aa86b67`) and add:

1. **WordVoice-LLM** — Qwen2.5-0.5B AR. before each word's codec tokens, emit a bound-token `⟨b⟩` and predict 5 attributes (or accept user overrides). then generate that word's speech-token chunk.
2. **WordVoice-FM** — CosyVoice3 flow matching + a word-style upsample/modulate so duration/energy/pitch actually land on the waveform, not just the discrete tokens.
3. **WordVoice-5A** — 4684 h re-annotation of LEMAS (2546 zh / 2138 en) with those 5 labels. pipeline = MFA + Qwen3FA + loudness snap, keep top ~8%.

tags the space accepts:

| tag | range | meaning |
| --- | --- | --- |
| `[dur:N]` | ms | word length (infer quantizes `/40`, clamp 1–35 tokens @ 40 ms) |
| `[eng:x]` | 0–1 | loudness |
| `[pit:x]` | −1–1 | core f0 |
| `[bnd:b0…b4]` | pause after the word (0 / ≤50 / ≤180 / ≤400 / >400 ms) |
| `[ton:…]` | `flat rise rrise fall ffall peak valley` | contour shape |

example from their own infer: `are[pit:0.2] you crazy[eng:0.9][dur:400]?`

two modes:

- **free** — model plans the 5-D itself (still needs a prompt wav + transcript)
- **control** — you pin some words; untagged words stay free

target use they name: **audiobook / video dub / acting**. strict time-align and "say *this* word louder." conversation barge-in is not the claim.

---

## streaming? no.

official `wordvoice_infer.py` calls `wordvoice_inference(..., stream=False)`.

`CosyVoice3Model.wordvoice_tts` **ignores `stream`**. it:

1. runs the full LLM job (`wordvoice_llm_job`) to completion
2. then one `wordvoice_token2wav(..., finalize=True)`
3. yields **one** `{tts_speech, dur/eng/pit/bnd/ton lists}`

that is a finished-sentence wav. the `stream=` kwarg is leftover CosyVoice2/3 plumbing. do not cite this paper as "they stream word-by-word pcm."

the *architecture* is word-chunked AR (plan word i → tokens for word i → next). that *could* be a first-pcm-after-first-word hatch if someone rewired token2wav to hop. they did not ship that. CosyVoice official bistream already lost here (`sc-01p` closed; first pcm after last text ~2463 ms).

prompt path is also one-shot: **MMS-FA align the reference clip**, extract 5-D from it, then synthesize. new voice = aligner pass, not a named-speaker id.

HF space `startup_duration_timeout: 45m`. that is a fat pytorch load, not a 70 ms mouth.

paper reports **no** first-pcm, RTF, or VRAM. trained 8× A800, LLM 7 epochs, FM 20.

---

## paper numbers (control, not latency)

vs CosyVoice3 baseline on their test set (crowd MOS, 20 listeners):

| | N-MOS zh / en | Spk-MOS zh / en | Ctrl-MOS zh / en |
| --- | --- | --- | --- |
| CosyVoice3 | 3.55 / 3.64 | **3.53 / 3.84** | 3.03 / 3.32 |
| WordVoice-Free | 3.65 / 3.73 | 3.33 / 3.87 | 3.38 / 3.51 |
| WordVoice-Control | **3.69 / 3.77** | 3.32 / 3.85 | **3.45 / 3.65** |

control wins. speaker similarity is the tax (they say so). WER also ticks up (zh 2.31 → 2.86, en 1.06 → 1.57).

objective control (lower better), en Control vs CosyVoice3: Dur-MAE 0.045 vs 0.081, Eng 0.048 vs 0.090, Pit 0.078 vs 0.177, Bnd-ER 23% vs 44%, Ton-RER 27% vs 41%.

so: if you *hand it* the 5-D, it mostly does what you asked. free mode is only a little better than stock CosyVoice3. the demo's "oracle" clips feed **GT attributes extracted from the real take** — that is the upper bound, not what a live agent would type.

---

## weights / what would land on this box

`download_models.py` pulls three trees:

| artifact | size on hf | role |
| --- | ---: | --- |
| `wordvoice_llm_en.pt` | 2026 806 691 (~1.89 GiB) | en LLM head (load **one** language) |
| `wordvoice_llm_zh.pt` | 2026 806 691 (~1.89 GiB) | zh LLM head |
| `wordvoice_fm.pt` | 1329 229 360 (~1.24 GiB) | FM / style modulate |
| `Fun-CosyVoice3-0.5B-2512` | same pin we already have under `cosyvoice-qual-sc-e71.4` | tokenizer, hift, frontend |
| torchaudio `MMS_FA` | small | prompt aligner |

disk is cheap. **vram is not.** this is pytorch CosyVoice3 + a second 0.5B LLM + FM, plus MMS-FA on the prompt. our parked CV3 TRT was ~5–6 GB. isolated AutoModel CV2 beside it was ~4 GB extra and left ~2.7 GB. WordVoice is in that class, **not** the 2.4 GB qwentts class.

12 GB 4070: **cannot coexist with live qwentts.** isolated after parking `:18091`: plausible, unmeasured. do not compile a new TRT plan while the mouth is up.

`load_trt` exists on the `WordVoice` class (same CosyVoice3 DiT estimator hook). official infer does **not** pass it. fp16 DiT TRT is the same "use at caution" warning we already have.

---

## can we do this on the rig?

### live qwentts (the pin)

**not this paper.**

qwentts.cpp 0.6B CustomVoice is talker + 12 Hz codec. no flow matching. no bound-token. no word index. `instructions=` is one utterance string. we already measured it moves **duration** (fast 4.80 s vs whisper 7.36 s on the same line) and is weak on emotion. that is **not** `crazy[eng:0.9][dur:400]`.

closed named roster. no clone. a new human is Base-clone (unmeasured) or Base-SFT → GGUF (unproven). WordVoice-5A does not become a tenth ryan row.

fake-it options that are *not* WordVoice:

- keep using instruct for vibe ("rushed", "whisper"). ears, not MOS.
- call-side first-clause flush (the real leftover). orthogonal.
- SSML-ish wrappers in the speak text. qwentts will read them as words or ignore them.

do not train a WordVoice-shaped head on this Q8 and call it a weekend.

### parked CosyVoice3

**this is the honest port**, later, isolated.

same backbone they fine-tuned. we already have the Fun-CosyVoice3-0.5B tree and the aa86b67 TRT worker. WordVoice replaces `llm.pt` / `flow.pt` with `wordvoice_llm_{en,zh}.pt` + `wordvoice_fm.pt` and needs MMS-FA on every new prompt wav.

what we would get: word tags on a **clone** path (prompt wav + transcript), not ryan. first-pcm unknown; default infer is full-utterance. even if `stream=True` were wired, expect CosyVoice hop tax (historical live CV3 ~282 ms, isolated AutoModel ~1149 ms), not 70 ms.

product fit is **acting / dub / "hit this word"**, not "start speaking when omp emits a period."

### qwen Base / 1.7B instruct, someday

1.7B CustomVoice is the checkpoint that is *supposed* to obey instruct. VoiceDesign invents a mouth from a paragraph. neither is loaded. neither has word-level 5-D. you could *imagine* SFT on WordVoice-5A → a controller that emits tags or a new talker. that is a training campaign, not a pin swap.

---

## what to steal without loading it

the **idea** that is cheap:

- a speakable clause is not a word-tag graph. first-clause flush stays the hundreds-of-ms win.
- if we ever want "say *crazy* louder", the wire is an optional side-channel on `speak` (`instructions` already exists; a structured `word_controls` would be a new `sdc-*` + worker parse). do not invent that to chase this paper.
- CosyVoice stays rollback. do not reopen `sc-01p` as "WordVoice bistream."

the **idea** that is expensive and maybe worth a future isolated listen:

- park qwentts, load WordVoice-en + Fun-CosyVoice3 + MMS-FA, run their `wordvoice_infer.py` en sample, measure first-pcm / vram / cancel. ears vs ryan instruct on the same line. only if you want acting, not if you want stream.

---

## leftover / honesty

- not heard on this box. space/demo wavs only.
- no published first-pcm. 45 min space boot is the only runtime clue.
- `stream` in their API is a lie.
- en and zh are **two** 1.89 GiB LLMs. live would pick one.
- apache-2.0 code+weights; dataset cc-by-4.0 (LEMAS-derived).
- did not message niru. did not touch voicecat. did not spawn a bead.
