# Breeze TTS 2 Hybrid Runtime Technical Spec

**Design Brief:** `$HOME/worlds/personal/designs/speech-core/breeze-tts-2-hybrid-runtime.md`
**Bead:** `sc-breeze-hybrid-81p`
**Status:** sound
**Repo:** speech-core

## Intent (from the Brief — do not rewrite)

Determine whether a **hybrid-int8 + selective cuda-graph** Breeze TTS 2 runtime is a **known-good option on the shelf** for later GPU-budget decisions — not the next production TTS.

Primary question (quoted):

> can we obtain materially lower warm first-audio latency than ordinary bf16 eager inference while maintaining realtime streaming, voice quality, and enough vram headroom for the rest of the speech stack?

Do **not** integrate Breeze into speech-core or voicecat. Do **not** replace leftover qwentts. Do **not** rewrite the TTS abstraction. This is a contained runtime benchmark. Live mouth remains leftover qwentts (restore the parked unit after the GPU is free). CosyVoice pin is not swapped. Opener stays dead.

**Terminal state** (quoted): Breeze runtime characterized on RTX 4070 → best known config + measurements captured on bead `sc-breeze-hybrid-81p` → **park the bead**. Successful numbers are not a trigger to integrate.

Reason for parking (quoted):

> another ASR candidate with multi-speaker labelling may become a much larger resident compute/VRAM consumer. Choosing a heavier TTS backend before that GPU budget is known is premature.

Working hypothesis to verify on this 4070 (quoted layout):

```text
text encoder       → int8
backbone           → int8
depth decoder      → bf16
codec              → bf16

depth decoder      → cuda graph
codec              → cuda graph if beneficial / fits
backbone decode    → cuda graph if beneficial / fits
```

Quoted mechanism we specifically do **not** invert:

> We specifically do **not** assume int8 itself provides the speedup.

The experiment answers (quoted):

> among Breeze configs that fit in **≤9 GB** on this 4070, does hybrid-int8 + strategic cuda graphs beat a pruned/narrow official BF16 fast path on warm first-non-silent PCM, and is either a genuinely low-latency streaming backend vs this experiment's own A?

### Invariants (quoted from the Brief)

- Live in `lab/`. Tombstone: `not on the voicecat path.` Do not edit daemons, leftover units, voicecat, or production speech-out.
- Do not bring in ComfyUI as an architectural dependency. If hybrid code currently lives in a ComfyUI integration, extract only: quantized linear loading; which modules are quantized/excluded; cuda graph capture; cache/preallocation requirements.
- Expose a small standalone python runtime speech-core can call directly, roughly:

```python
engine = BreezeEngine(config)

stream = engine.synthesize(
    text=text,
    reference_audio=ref_audio,
    reference_text=ref_text,
)

for pcm in stream:
    ...
```

- A backend does **not** pass merely because its total generation is fast. We require genuine incremental audio production.
- The most important metric is: `request → first non-silent playable PCM`, not merely “model forward started” or “first internal token produced.”
- Hardware: GPU RTX 4070 12 GB; CPU Ryzen 5 5600; RAM 32 GB; OS Linux; batch size 1.
- Run everything with a persistent warm process. Care primarily about warm interactive latency. Also record cold initialization separately.
- Live leftover (`ata-speech-tts.service` / qwentts on CUDA0) currently occupies the 4070. Park it only for exclusive VRAM during this experiment; restore it afterward. Do not retarget units or swap the CosyVoice pin.
- Restore leftover after the GPU is free, even on REJECT. Do not restore leftover as a side-effect of a failed bench.
- One fixed cloned voice for all measurements. Prefer a clean reference of approximately 5–15 seconds with an exact transcript.
- Configurations under an identical interface: A, B (independent bf16 fast stages), C0–C4 (hybrid primary), D full int8 only if cheap to expose, E1–E5 (pruned/narrow official BF16 fast runtime).
- A/B/C must not enable `fast_text_encoder`. E may (E5).
- Treat **9 GB as a hard ceiling**. A configuration is in-envelope only if peak VRAM **≤9 GB**. Scratch any config that cannot fit under 9 GB peak. Do not pursue, benchmark, or optimize any Breeze configuration that requires more than roughly 8–9 GB. **<11.5 GB is no longer acceptable.**
- For every configuration: initialize engine once; several unmeasured warm-up generations; at least **30 measured generations** per short-input class; identical text/reference across configurations; fix seed where supported; same monotonic clock; CUDA sync only where necessary for accurate boundary measurement.
- For each configuration, save generated wav files. Compare voice identity, pronunciation, prosody, naturalness, stability over long text, emotion/delivery control, artifacts, unexpected pauses. Particularly compare bf16 baseline vs hybrid int8. No formal MOS study. An obvious regression is enough to reject a configuration.
- After the core benchmark, a tiny direction test on the same cloned voice: calm and matter-of-fact; slightly amused; urgent but controlled; quiet / thoughtful. Verify the optimized runtime does not materially weaken direction following relative to bf16. Do not spend time tuning prompts.
- SHELF means the best config is characterized and the bead is parked for a later resource-allocation decision. It is **not** permission to integrate, propose a pin swap, or spawn a follow-on wiring bead. Recommendation is against this experiment's own bf16 baseline first; leftover comparison is commentary, not a pin-swap gate. A win vs leftover still parks the bead.
- Compare the best ≤9 GB E configuration directly against the best ≤9 GB hybrid (C*) configuration using the existing metrics. Winner is commentary on the parked bead, not a production selection. Config E stays in this same bead. Do not spawn a second bead.
- Do not stage `/home/sf` machine paths; use `$HOME`/`~`.

### Acceptance thresholds (quoted)

A configuration is in-envelope only if peak VRAM **≤9 GB**. Treat 9 GB as a **hard ceiling**, preferably with margin below that. Target ≤9 GB was already the preferred band; **<11.5 GB is no longer acceptable.** Do not pursue, benchmark, or optimize any Breeze configuration that requires more than roughly 8–9 GB.

Among in-envelope configs, the candidate is SHELF-quality vs this experiment's A if approximately:

```text
warm first audible PCM:
    ideal       <150 ms
    good        <200 ms
    acceptable  <250 ms

sustained generation:
    RTF < 1.0 required
    RTF < 0.7 preferred

streaming:
    no perceptible recurring stalls
    chunk cadence sufficiently regular for continuous playback

VRAM:
    hard ceiling  9 GB peak
    preferred     some margin below 9 GB

quality:
    no obvious degradation from bf16 clone quality
```

The TTFA thresholds matter more than raw RTF once RTF is safely <1.

For voicecat, `180 ms TTFA / 0.7 RTF` is preferable to `500 ms TTFA / 0.2 RTF`.

These bands are measurement bands vs this experiment's own A. They do not mean the candidate is worth integrating into speech-core.

Live leftover qwentts first-usable is ~70 ms / ~2.4 GB. This experiment does **not** require beating leftover to ship a report. Recommendation is against this experiment's own bf16 baseline first; leftover comparison is commentary, not a pin-swap gate. A win vs leftover still parks the bead.

Compare best ≤9 GB E vs best ≤9 GB hybrid in the same report. SHELF candidates may be C* or E* (or `B_winner`) if they are in-envelope (`peak_vram_gib <= 9.0`).

### Configurations (quoted)

**A — bf16 eager baseline**

```text
text encoder       bf16
backbone           bf16
depth decoder      bf16
codec              bf16
cuda graphs        none
```

**B — official selective fast path.** Start from bf16 and independently test:

```text
fast depth decoder
fast codec
fast backbone decode
fast backbone prefill
```

Do not immediately enable every graph. Find the fastest configuration that remains under the **9 GB peak VRAM hard ceiling**. If a stage cannot fit, scratch it. Do not optimize anything above 9 GB even if it is faster. B still excludes `fast_text_encoder`.

**C — hybrid runtime [PRIMARY]**

```text
text encoder       int8
backbone           int8
depth decoder      bf16
codec              bf16
```

Then test graph capture incrementally:

```text
C0  hybrid, eager
C1  + graph depth decoder
C2  + graph codec
C3  + graph backbone decode
C4  + graph backbone prefill
```

Stop adding graph stages when:

- peak allocation would exceed **9 GB**;
- latency stops improving;
- jitter gets worse;
- initialization becomes unreasonable;
- output correctness changes.

Scratch any hybrid graph stage that cannot fit under 9 GB. Do not spend time making a >9 GB hybrid work. Do not run or optimize the overflowing stage. C still excludes `fast_text_encoder`.

This is the candidate we expect to win against E, or lose honestly.

**D — full int8 control.** Only if cheap to expose:

```text
text encoder       int8
backbone           int8
depth decoder      int8
```

We expect this to be slower than the hybrid configuration. Kill this branch quickly if confirmed. Do not optimize full int8 extensively. Scratch D immediately if peak VRAM exceeds 9 GB.

**E — pruned / narrow official BF16 fast runtime**

Test whether the official Breeze runtime can outperform the hybrid path after:

- avoiding/removing unused inference modules where safe;
- using a **minimal VoiceCat-specific CUDA graph warmup profile**, rather than the stock broad `fast.json`;
- warming only the CFG mode(s) and prompt-length buckets actually relevant to us;
- enabling fast stages selectively.

Candidate stages, in priority order:

```text
E1  depth decoder graph
E2  + codec graph
E3  + backbone decode graph
E4  + backbone prefill graph
E5  + text encoder graph
```

Progressively enable useful fast stages. Record peak VRAM after each meaningful configuration. Once the next useful configuration would push peak VRAM beyond 9 GB, **stop**. Do not benchmark or optimize that next stage.

E may enable the text-encoder graph; A/B/C must not (B/C still exclude `fast_text_encoder` except as this E ladder).

If the pruned/narrow official BF16 approach cannot become competitive while staying under ~9 GB, mark E **rejected for our purposes** in the same report. Do not spawn a second bead.

Compare the best ≤9 GB E configuration directly against the best ≤9 GB hybrid (C*) configuration using the existing metrics (warm first PCM / first audible PCM, TTFA p50/p95, RTF, cadence/jitter, peak + steady-state VRAM, startup/warmup, basic quality).

### Required recorded metrics (quoted)

```text
startup time

warm request → first model output
warm request → first codec frame
warm request → first PCM bytes
warm request → first non-silent PCM

p50 TTFA
p95 TTFA
p99 TTFA if enough runs

audio chunk duration
inter-chunk gap p50/p95
maximum observed streaming stall

total generation wall time
audio duration
RTF

peak allocated VRAM
peak reserved VRAM

GPU utilization if easy
GPU power if easy

output sample rate
```

TTFA in the summary table is first non-silent playable PCM.

### Output artifact (quoted)

One compact benchmark report under `lab/docs/qualification/` (or `docs/qualification/` with a lab tombstone) containing a summary table:

```text
configuration
peak VRAM
p50 TTFA
p95 TTFA
RTF
stream jitter
quality notes
```

and a recommendation of exactly one of:

```text
SHELF
PROMISING — NEEDS ONE MORE EXPERIMENT
REJECT
```

SHELF means the best config is characterized and the bead is parked for a later resource-allocation decision. It is **not** permission to integrate, propose a pin swap, or spawn a follow-on wiring bead. SHELF is allowed only for in-envelope rows (`peak_vram_gib <= 9.0`). Candidates may be C0–C4, `B_winner`, and E_winner / E1–E5 in-envelope rows.

The report must name the best ≤9 GB E row and the best ≤9 GB hybrid (C*) row and compare them on the metrics above. If no E row is in-envelope or E cannot compete under 9 GB, mark E rejected for our purposes in that same report.

If reasonably easy, capture one representative short generation showing where latency is spent:

```text
text encoder
backbone prefill
first backbone decode
depth decode
codec
pcm emission
```

We specifically want to know what dominates **first audio**.

## Mapping onto the current system

Claims labeled **observed** / **inferred** / **unknown**.

### Where this lives

- **observed:** `lab/README.md` tombstone is `not on the voicecat path. do not treat anything here as the product.` Live mouth stays at repo root (daemons, qwentts pin, `spec/`).
- **observed:** no Breeze / MediaTek references exist in this repo today. This experiment is a new `lab/` tree, patterned on `lab/scripts/cosyvoice_qual/`.
- **observed:** `docs/superpowers/` did not exist before this spec. Packet path wins: `docs/superpowers/specs/` and `docs/superpowers/plans/`.
- **observed:** lab Python tests use stdlib `unittest` (`python3 -m unittest lab/scripts/test_speech_talker_session.py`). No pytest in lab/.

### Leftover / GPU

- **observed:** durable leftover unit is `ata-speech-tts.service` (`~/.config/systemd/user/ata-speech-tts.service`), qwentts.cpp on `GGML_BACKEND=CUDA0`, `:18091`. Historical alias `qwentts-tts-server.service` still exists; do not retarget or rewrite either unit.
- **observed:** at planning time both TTS units were `inactive`/`dead`. Park is still `systemctl --user stop ata-speech-tts.service` if it is up at bench time. Restore is `systemctl --user start ata-speech-tts.service` after the GPU is free. Do not start leftover during planning or mid-fail as a side-effect.
- **observed:** leftover first-usable is `|s| >= 512` on s16le PCM (`lab/docs/qualification/qwen3-tts-qwentts-q8.md`); usable p50 ~70 ms; VRAM ~2.4–3.1 GB. Commentary only.
- **observed:** CosyVoice qual `lab/scripts/cosyvoice_qual/run_qualification.py` measures `first_pcm_s` as first yielded chunk with `n > 0`, **not** first non-silent. This experiment must not copy that weaker clock as TTFA.
- **observed:** CosyVoice VRAM helper uses `torch.cuda.mem_get_info`, `memory_allocated`, `memory_reserved`, `max_memory_allocated` / `max_memory_reserved`. Reuse that oracle. `reproduce.sh` notes nvidia-smi/NVML may fail; torch.cuda is the oracle. GPU util/power are best-effort via `nvidia-smi` and must not fail the bench if NVML is broken.
- **observed:** qual caches live under `$HOME/.cache/speech-out/<campaign>/` with an isolated venv. Follow `$HOME/.cache/speech-out/breeze-tts-qual-sc-breeze-hybrid-81p/`. Never write `/home/sf` into committed artifacts.

### Breeze public sources (HOW)

- **observed:** official inference is Apache-2.0 `https://github.com/breezeblue-ai/breeze-tts` pin `43e2ea1595297c4059477e2e4a300653761c759b` (main as of 2026-09-04). Weights are BreezeBlue research/non-commercial. Code entry: `infer.py` → `breeze_infer.runtime.load_runtime` + `models.fast_streaming.FastBreezeStreamingRuntime`.
- **observed:** official eager is all `fast_*` false. Per-stage CUDA graphs: `fast_depth_decoder`, `fast_codec`, `fast_backbone_decode`, `fast_backbone_prefill`, plus official `fast_text_encoder`. A/B/C must not enable `fast_text_encoder`. E5 does.
- **observed:** `FastBreezeStreamingRuntime.iter_audio_chunks` yields `FastStreamingChunk` (`numpy` audio, `sample_rate`, `codec_frames`, `is_final`, `timing`). Sample rate comes from `model.config.codec_config.sampling_rate` (24 kHz in public docs). Codec graph path uses `codec_chunk_frames = 1`; eager uses `2`.
- **observed:** official README: eager ~7.7 GiB; `--fast-all` ~14.4 GiB (24 GB recommended). On a 12 GB 4070 with a **9 GB hard ceiling**, B and E must not enable every graph up front. That is why B is independent + compose-under-cap and E is an incremental ladder that stops before exceeding 9 GB.
- **observed:** official warmup is `FastBreezeStreamingRuntime.warmup_from_profile(FastStreamingWarmupProfile)`. Stock `configs/fast.json` is the broad serving profile. Brief E forbids loading that stock file. E uses a minimal VoiceCat-specific profile: CFG modes this experiment actually uses (`cfg_scale=1.0` core / `no_cfg`, `cfg_scale=4.0` direction / `single_cfg`) and prompt-length buckets from `utterances.py` tiny / short / medium (token lengths rounded up to the official 32-token granularity). Do not warm long-class or other unused buckets.
- **observed:** official code has **no** int8 / bitsandbytes / ConvRot path. Hybrid int8 lives in ComfyUI-Breeze-TTS-2 (`Saganaki22/ComfyUI-Breeze-TTS-2`) `int8.py` + published checkpoints at `https://huggingface.co/drbaph/Breeze-TTS-2-comfyui`.
- **observed:** published files matching Brief C and D:
  - C: `Breeze-TTS-2-int8-hybrid.safetensors` — backbone + text encoder INT8 ConvRot; depth decoder bf16; codec unchanged bf16.
  - D: `Breeze-TTS-2-int8-convrot.safetensors` — all 462 transformer projection linears INT8, including depth decoder. Cheap to expose (already built). Include D. Kill quickly if slower than hybrid, as the Brief requires. Do not optimize it. Scratch D immediately if peak VRAM exceeds 9 GB.
  - A/B/E: official bf16 (either `BreezeBlue/Breeze-TTS-2` shards or the bit-exact merged `Breeze-TTS-2-bf16.safetensors` plus shared `audio_tokenizer/`).
- **observed:** ComfyUI `int8.py` replaces named `nn.Linear` with `ConvRotInt8Linear` that calls `comfy_kitchen.int8_linear`. ComfyUI README: official CUDA-graph fast path is intentionally **not** ported there. Therefore graphs for this experiment must come from official `FastBreezeStreamingRuntime`, not ComfyUI `native.py`.
- **inferred:** `comfy-kitchen` is a kernel library, not ComfyUI-the-app. Using it inside lab/ for ConvRot forward, without importing ComfyUI nodes, AIMDO, or a ComfyUI install, satisfies “do not bring in ComfyUI as an architectural dependency.” If `comfy-kitchen` cannot install without ComfyUI, vendor only the kernel used by `ConvRotInt8Linear.forward`. Do not add ComfyUI as a product/runtime dep.
- **unknown until bench:** whether official CUDA graphs remain correct/faster when backbone/text-encoder linears are ConvRot int8. The Brief already gives stop rules (would exceed 9 GB, no latency win, worse jitter, unreasonable init, correctness change). Record and stop. Do not invent a different hybrid.

### Existing harness patterns to reuse (not copy as TTFA)

- **observed:** `run_qualification.py` — dataclass utterance results, wav dump via torchaudio/soundfile, JSON report, `time.perf_counter`, peak VRAM in `finally`.
- **observed:** `live_8788_probe.py` `_pct` / `_stats` for p50/p95.
- **observed:** no 30-run protocol in lab today (CosyVoice n=6 / n=20). This experiment introduces n=30 for short-input classes as the Brief requires.
- **observed:** CosyVoice prompt wav + SHA live in the cache, not in git. Same for Breeze ref audio.

No product-shaped contradiction: the current system cannot run this experiment inside production speech-out, and the Brief forbids that. Mapping onto `lab/` satisfies every invariant.

## Architecture

A persistent isolated Python process in `lab/scripts/breeze_tts_qual/` owns:

1. **Pin / venv** under `$HOME/.cache/speech-out/breeze-tts-qual-sc-breeze-hybrid-81p/` (breeze-tts checkout at `43e2ea1595297c4059477e2e4a300653761c759b`, weights, venv, run artifacts).
2. **BreezeEngine** — Brief-shaped facade. Backend-specific load, ConvRot swap, CUDA graph flags, and official `iter_audio_chunks` stay behind it.
3. **Config table** A / B-independent stages / B-winner / C0–C4 / D / E1–E5 / E-winner, all implementing the same `synthesize(...)` iterator.
4. **Benchmark protocol** — park leftover → load one config → VoiceCat warmup for E (never stock `fast.json`) → if post-warmup peak would exceed 9 GB, do not run that stage → otherwise warmup measured generations → 30-run short classes → medium/long RTF → wav dumps → direction test → JSON+markdown report → unload → next config → when GPU is free, restore leftover.
5. **Metrics** — monotonic `time.perf_counter` from `synthesize` call (request received). First non-silent PCM uses leftover energy gate `|s16| >= 512` (float abs `>= 512/32768`) so this experiment’s “usable” clock matches leftover receipts used as commentary. Do not CUDA-synchronize the streaming loop except at documented measurement boundaries (startup, optional internal stage events already provided by `collect_timing=True`).

Single GPU, batch size 1, `CUDA_VISIBLE_DEVICES=0` after leftover is parked.

Hard VRAM ceiling: **9.0 GiB** peak allocated (`bytes/1024³`). In-envelope means `peak_vram_gib <= 9.0`. A stage whose peak **would exceed** 9.0 GiB is overflowing: do not run the measured protocol on it, do not optimize it, mark `not_run` + `stop_reason=unsafe_vram`, scratch it from SHELF / best-E / best-hybrid.

## Components and interfaces

### Layout (new files; none exist today)

```text
lab/scripts/breeze_tts_qual/          # package; every .py carries the tombstone
  __init__.py
  configs.py                          # EngineConfig named A,B*,C0-C4,D,E1-E5
  engine.py                           # BreezeEngine
  int8_convrot.py                     # extracted ConvRot load/swap; no ComfyUI
  metrics.py                          # TTFA / RTF / VRAM / jitter / silence
  protocol.py                         # warmup, n=30, stop-adding-graphs (9 GB)
  report.py                           # summary table + recommendation
  utterances.py                       # fixed texts + direction set
  run_benchmark.py                    # CLI harness
  park_leftover.py                    # stop/start ata-speech-tts.service only
lab/scripts/test_breeze_tts_qual.py   # stdlib unittest, CPU
lab/docs/qualification/breeze-tts-2-hybrid-sc-breeze-hybrid-81p.md
lab/docs/qualification/breeze-tts-2-hybrid-sc-breeze-hybrid-81p-report.json
```

Plus `lab/README.md` row for this tree. Do not touch `crates/`, systemd unit files, leftover env, voicecat, `CHARTER.md`, or `spec/`.

### `EngineConfig`

Fields (exact names):

```python
@dataclass(frozen=True)
class EngineConfig:
    name: str                    # "A" | "B_depth" | "B_codec" | "B_backbone_decode"
                                 # | "B_backbone_prefill" | "B_winner" | "C0" | "C1"
                                 # | "C2" | "C3" | "C4" | "D"
                                 # | "E1" | "E2" | "E3" | "E4" | "E5" | "E_winner"
    precision: str               # "bf16" | "hybrid_int8" | "full_int8"
    fast_depth_decoder: bool
    fast_codec: bool
    fast_backbone_decode: bool
    fast_backbone_prefill: bool
    fast_text_encoder: bool      # True only for E5 (and E_winner if it includes E5)
```

`CONFIGS` factory returns the Brief table including E1–E5. `precision="hybrid_int8"` loads `Breeze-TTS-2-int8-hybrid.safetensors` and swaps only keys present in that file (text encoder + backbone). `precision="full_int8"` loads `Breeze-TTS-2-int8-convrot.safetensors`. `precision="bf16"` loads official bf16 and does not swap linears. E1–E5 are `precision="bf16"` with cumulative fast flags in Brief priority order; only E5 sets `fast_text_encoder=True`.

### `BreezeEngine`

```python
class BreezeEngine:
    def __init__(self, config: EngineConfig, *, ckpt_dir: Path, device: str = "cuda:0"): ...
    def synthesize(
        self,
        *,
        text: str,
        reference_audio: Path | str,
        reference_text: str,
        instruction: str = "Speak clearly and naturally.",
        seed: int = 42,
    ) -> Iterator[PcmChunk]: ...
    def close(self) -> None: ...
```

`PcmChunk`:

```python
@dataclass(frozen=True)
class PcmChunk:
    pcm: bytes                   # s16le mono
    sample_rate: int
    n_samples: int
    is_final: bool
    t_rel_s: float               # perf_counter since synthesize() entry
    timing: dict                 # official chunk.timing plus lab clocks
```

`synthesize` must yield **before** generation completes whenever the official runtime emits a codec chunk. Buffering the whole utterance and yielding once fails the streaming requirement.

Clocks stamped into the first/later chunks / a returned summary (see protocol):

| Brief stage | How |
|---|---|
| request received | `t0 = time.perf_counter()` at `synthesize` entry |
| text processing complete | after `prepare_inputs` returns |
| first codec frame available | official `timing` when first codec decode is launched / first `codec_frames > 0` |
| first PCM bytes emitted | first yield with `n_samples > 0` |
| first non-silent audible sample | first sample in the concatenated s16 stream with `abs(s) >= 512`, converted to time by chunk `t_rel_s` + offset within chunk |
| generation complete | iterator exhaustion |

Seed 42 is fixed wherever the official runtime supports it (`FastBreezeStreamingRuntime.iter_audio_chunks(..., seed=42)` and `set_all_seeds(42)`).

Voice clone uses official template `ref_edit_tata` (reference audio + exact transcript). Direction test uses the same template plus `--instruction` / `instruction=` set to the Brief phrases; `cfg_scale=4` as official voice-direction docs. Core latency bench uses clone without extra direction (`instruction="Speak clearly and naturally."`, official default, `cfg_scale=1.0`). Do not tune those strings.

### INT8 extraction (`int8_convrot.py`)

Port the ideas in ComfyUI `int8.py` only:

- `scan_checkpoint_quantization`
- `ConvRotInt8Linear`
- `replace_quantized_linears` **before** weights are assigned
- refuse unknown quant formats

Do not import `nodes.py`, `loader.py` Comfy folder search, AIMDO, or Whisper. Kernel: `comfy_kitchen.int8_linear` if the isolated venv can install `comfy-kitchen` without ComfyUI; otherwise vendor that one kernel. Codec stays the bundled `audio_tokenizer/` (bf16).

### Leftover control (`park_leftover.py`)

- Park: if `ata-speech-tts.service` is active, `systemctl --user stop ata-speech-tts.service`. Do not edit the unit file. Do not stop/start `ata-speech-out.service` unless it is holding CUDA0 (it should not; inference is the TTS unit).
- Restore: `systemctl --user start ata-speech-tts.service`. Verify `ActiveState=active`. This is the **last** GPU-related step, after engine teardown / `torch.cuda.empty_cache`, even if the recommendation is REJECT or the bench aborted after occupying the GPU.

### Benchmark CLI

`lab/scripts/breeze_tts_qual/run_benchmark.py`:

- `--qual-root` default `$HOME/.cache/speech-out/breeze-tts-qual-sc-breeze-hybrid-81p`
- `--configs` default `A,B,C0,C1,C2,C3,C4,D,E1,E2,E3,E4,E5`
- `--ref-audio` / `--ref-text` required
- writes `$QUAL_ROOT/runs/$RUN_ID/` wavs + `metrics.json`
- writes repo report paths under `lab/docs/qualification/` using `$HOME` not `/home/sf`

### Fixed utterances

Short-input classes (n=30 measured each, after ≥3 unmeasured warmups):

| class | text |
|---|---|
| tiny | `yeah.` |
| tiny | `got it.` |
| tiny | `one second.` |
| short | `I found the issue. The worker is holding the old session open.` |

Protocol: 30 measured generations **per short-input class**. Tiny is three texts; rotate them in a fixed order so each of the three is used 10 times (30 total for class `tiny`). Short is one text × 30.

Medium (n=3 measured, RTF after startup, ~54 words):

```text
The worker kept the old session open after the handshake, so every later request reused stale credentials and looked like a hang. I closed that session, restarted the listener, and confirmed new requests land on a fresh worker. If it happens again, check the idle timeout before blaming the GPU path.
```

Long (n=3 measured, ~180 words; verifies generation remains below realtime after startup):

```text
This is a long-form continuity check for streaming speech. A voice agent still has to keep producing audio after the first chunk, without stalls that a listener would hear as dropouts, and without drifting off the cloned voice. I am going to keep talking through a few related points so the decoder stays in the loop: the leftover mouth is qwentts and must come back when this experiment releases the GPU; CosyVoice is not being pinned; ComfyUI is not a runtime; and the only question on the table is whether hybrid int8 plus selective CUDA graphs beats our own bf16 eager baseline on this 4070 with honest first-audio clocks. If the stream jitters, if VRAM walks into the ceiling, or if the clone quality falls apart on this paragraph, that configuration is not a candidate. Keep the cadence regular, keep the voice the same person, and finish the paragraph without inserting odd pauses or artifacts that the bf16 baseline does not have.
```

Direction set (same clone, n=1 each, after core bench), instructions exactly:

```text
calm and matter-of-fact
slightly amused
urgent but controlled
quiet / thoughtful
```

Shared spoken text for direction: `I found the issue. The worker is holding the old session open.`

Reference: one English 5–15 s wav + exact transcript at `$QUAL_ROOT/fixtures/ref.wav` and `ref.txt`. Record duration, SHA256, sample rate in the report. Same files for every config.

E VoiceCat warmup uses tiny / short / medium only (not long) plus CFG 1.0 and 4.0.

## Data / control flow

```text
park ata-speech-tts.service if active
        │
        ▼
for config in A → B independents → B_winner → C0 → C1 → C2 → C3 → C4 → E1 → E2 → E3 → E4 → E5 → D
        │
        ├─ load checkpoint (bf16 or ConvRot) into BreezeEngine
        ├─ if E*: VoiceCat warmup profile (not stock fast.json)
        ├─ record startup_s and VRAM after load + unmeasured warmup
        ├─ if peak would exceed 9.0 GiB: mark not_run/unsafe_vram; do not measure; skip rest of family
        ├─ ≥3 unmeasured warmups (tiny/short) when in-envelope
        ├─ 30 measured tiny + 30 measured short
        ├─ 3 medium + 3 long
        ├─ save representative wavs (one per class + all direction)
        ├─ if C-series or E-series: apply stop-adding-graphs before enabling the next stage
        └─ unload engine, empty CUDA cache
        │
        ▼
direction test on A, surviving hybrid candidate (C-series winner), and surviving E candidate if in-envelope
        │
        ▼
write compact report (table + best≤9GB E vs best≤9GB hybrid + recommendation + optional stage trace)
        │
        ▼
GPU free → systemctl --user start ata-speech-tts.service
```

**B procedure (independent, then compose):**

1. A is the bf16 eager baseline (also B’s “no graphs” point).
2. Independently measure `B_depth`, `B_codec`, `B_backbone_decode`, `B_backbone_prefill` (exactly one fast flag true). If an arm’s peak would exceed 9.0 GiB, do not run/optimize it; mark `unsafe_vram` and exclude it.
3. Starting from eager, add flags in Brief order (depth, codec, backbone decode, backbone prefill) only if that independent arm improved p50 TTFA, did not worsen inter-chunk p95, stayed **≤9.0 GiB** peak allocated, did not change correctness, and did not make startup unreasonable. Stop adding when a Brief stop condition hits. The composed result is `B_winner` (may equal A). `B_winner` is SHELF-eligible only if `peak_vram_gib <= 9.0`.

**C procedure (cumulative):** C0 then C1…C4 in order. Before measuring the next Ck, if load/warmup peak **would exceed 9.0 GiB**, do not run that stage or later stages; mark `not_run: unsafe_vram`. Stop adding stages when a Brief stop condition hits; still record a skip/stop reason for later Ck rows rather than silently omitting them from the table. Do not enable `fast_text_encoder`.

**E procedure (cumulative, bf16, VoiceCat warmup):** E1 then E2…E5 in Brief priority order. Warm only CFG 1.0 + 4.0 and tiny/short/medium prompt-length buckets from `utterances.py` (token lengths rounded to official 32). **Do not** `load_warmup_profile` on stock `configs/fast.json`. Record peak VRAM after each E stage. Once the next stage would push peak beyond 9 GB, stop; do not benchmark or optimize that next stage. E5 is the only CONFIGS row with `fast_text_encoder=True`. Compose `E_winner` as the fastest in-envelope E row (may be none). If no E row is in-envelope or E cannot compete under ~9 GB, mark E rejected for our purposes in the same report.

**D:** run once on the short class (n=30) plus one tiny/medium listen wav. If p50 TTFA is slower than C0 (or C-winner) as expected, kill further D graph work. No D graph sweep. Scratch D immediately if peak VRAM exceeds 9 GB.

Stop conditions, all recorded:

| condition | measurement |
|---|---|
| peak allocation would exceed 9 GB | `max_memory_allocated` **> 9.0 GiB** (GiB = bytes/1024³). 9.0 exactly is in-envelope. Do **not** run or optimize the overflowing stage. |
| latency stops improving | p50 TTFA (first non-silent) of this stage ≥ previous stage |
| jitter gets worse | inter-chunk gap p95 of this stage > previous stage |
| initialization unreasonable | operator/builder records startup_s and sets `stop_reason=init_unreasonable` (no invented numeric cap in this spec) |
| output correctness changes | listen fail vs A, or first-chunk energy/sample-rate/shape anomaly vs A on the same text |

Constants: protocol `UNSAFE_PEAK_GIB = 9.0` (unsafe iff peak **>** 9.0); report `VRAM_ACCEPTABLE_GIB = 9.0` (SHELF iff `peak_vram_gib <= 9.0`).

## Error handling

- Checkpoint / quant format mismatch: fail the config, do not silently load int8 tensors as float.
- OOM: catch, record peak VRAM, mark config failed/unsafe, skip remaining graph add-ons for that family, continue other families if VRAM can be recovered; always restore leftover when GPU is free.
- Peak would exceed 9.0 GiB after load/warmup: mark `not_run` + `unsafe_vram`; do not start the 30-run protocol; do not try to shrink graphs or otherwise optimize that stage.
- Missing ref wav/transcript: refuse to start measured runs.
- `synthesize` that yields no PCM or only a single post-hoc buffer: fail streaming contract for that config.
- nvidia-smi failure: leave GPU util/power null; do not fail the run.
- Park/restore: if restore fails, the report must say leftover is **not** restored; do not rewrite the unit to “fix” it.

## Testing (behavioral contracts; exact tests live in the plan)

CPU unittest (no GPU, no leftover start):

- Config table matches A / B independents / C0–C4 / D / E1–E5. E1 is depth-only. E5 has `fast_text_encoder=True`. A/B/C (and D) still have `fast_text_encoder=False`.
- First-non-silent detector: leading zeros then `|s|>=512` reports the correct sample/time; sub-threshold PCM is not “audible.”
- Percentiles p50/p95/p99 on a 30-length series; RTF = wall/audio_duration; jitter from inter-chunk gaps.
- Fake `BreezeEngine` yields multiple PCM chunks before `is_final`; harness records first-bytes vs first-non-silent separately.
- Graph-stop helper encodes the five Brief conditions with ceiling **9.0 GiB** (`>` not `>=`).
- Report writer emits the required columns and only the three recommendation tokens: `SHELF`, `PROMISING — NEEDS ONE MORE EXPERIMENT`, `REJECT`. `choose_recommendation` SHELF-eligible names are C0–C4, `B_winner`, and E_winner / E1–E5 in-envelope rows; SHELF requires `peak_vram_gib <= 9.0`.
- Tombstone string present in every created runtime `.py`.
- `int8_convrot.replace_quantized_linears` swaps listed `nn.Linear` and errors on missing/wrong-type modules (kernel mocked).
- VoiceCat warmup profile uses tiny/short/medium + CFG 1.0/4.0 and does not reference `fast.json`.

GPU/harness (anti-gameable, not unittest-fakeable):

- A yields ≥2 PCM chunks on short text before completion.
- 30 measured runs exist per short-input class per run config that was actually executed (overflowing stages are `not_run`, not measured).
- Wav files exist per executed config × class and direction set.
- Peak allocated and reserved VRAM recorded after each E stage (and every other executed config).
- Report contains A, B (independents + winner), C0–C4 (or stop_reason), E1–E5 (or stop_reason), D-if-run, best ≤9 GB E vs best ≤9 GB hybrid, and one recommendation (`SHELF` | `PROMISING — NEEDS ONE MORE EXPERIMENT` | `REJECT`). SHELF is not a trigger to integrate. SHELF only if the chosen row is in-envelope (`<= 9.0 GiB`).
- After the last GPU task: `systemctl --user is-active ata-speech-tts.service` is `active`.
- After leftover restore, the lead comments measurements on `sc-breeze-hybrid-81p` and parks the bead. Builders do not close the bead.

## Non-goals

From the Brief and packet. Do not:

- integrate Breeze into speech-core or voicecat, replace leftover qwentts, or rewrite the TTS abstraction
- treat SHELF / successful numbers as permission to integrate, propose a pin-swap, or spawn a follow-on wiring bead
- select the next production TTS from this bead
- edit daemons, leftover unit files, leftover env, voicecat, CHARTER, `spec/` / `specs/*`, or `crates/`
- retarget leftover units or swap the CosyVoice pin
- restore leftover as a side-effect of a failed bench (restore only after GPU is free)
- treat leftover restore as integration
- close bead `sc-breeze-hybrid-81p` from a builder task
- bring ComfyUI in as a runtime/architectural dependency
- build TensorRT support
- optimize full int8 beyond exposing cheap D
- tune voice prompts extensively
- benchmark every possible quantization format
- investigate audio.cpp
- run a formal MOS study
- require beating leftover ~70 ms / ~2.4 GB to ship a report
- enable official `fast_text_encoder` on A/B/C (E5 may enable it)
- load stock broad `configs/fast.json` for the E ladder
- pursue, benchmark, or optimize any configuration whose peak VRAM exceeds **9 GB** (including treating formerly-acceptable <11.5 GB as in-envelope)
- start leftover during planning

## Implementation approach chosen (and rejected internals)

**Chosen:** wrap official `FastBreezeStreamingRuntime` (graphs + streaming) and load published ConvRot checkpoints via extracted `int8.py` linear swap + `comfy-kitchen` kernel (or a vendored copy of that kernel). Config flags map 1:1 onto official `fast_depth_decoder` / `fast_codec` / `fast_backbone_decode` / `fast_backbone_prefill` / `fast_text_encoder` (the last only on E5). Isolated venv + `$HOME/.cache/speech-out/breeze-tts-qual-sc-breeze-hybrid-81p/`. D is in scope because `Breeze-TTS-2-int8-convrot.safetensors` makes it cheap. E is in scope on this same bead as the pruned/narrow official BF16 comparator, warmed with a VoiceCat-specific profile.

This is YAGNI: official already streams PCM and already has the graph stages the Brief names; the missing pieces are hybrid int8 module loading (already a checkpoint + a small linear class) and a narrow warmup profile instead of stock `fast.json` for E.

**Rejected A — ComfyUI nodepack as the engine.** Violates the ComfyUI dependency invariant. Their graph path is not the official one.

**Rejected B — bitsandbytes/torchao dynamic int8 on official bf16 weights.** Different quant than the hybrid ConvRot checkpoint the hypothesis is about; more invention; D would not be “cheap.”

**Rejected C — official `--fast-all` as config B or E.** Official documents ~14.4 GiB; Brief requires a **9 GB hard ceiling** and not enabling every graph immediately. **<11.5 GB is no longer acceptable.**

## Open questions

None that affect product. Implementation risks (graphs×ConvRot correctness, NVML absence, comfy-kitchen install path, exact token lengths of tiny/short/medium after the official tokenizer) are handled by Brief stop rules, the 9 GB hard ceiling, and the kernel-vendor fallback above.

[You have received this identical output 3 times. Re-reading '$HOME/workspace/speech-core/docs/superpowers/specs/2026-09-06-breeze-tts-2-hybrid-runtime.md:raw' will not change it — use a narrower selector (path:A-B), or proceed with the edit.]

[Showing lines 1-300 of 302. Use :301 to continue]