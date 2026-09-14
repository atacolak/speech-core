# Lab reference processors — measured peak VRAM

**Date:** 2026-09-12
**Host:** `$HOME/workspace/speech-core` (live worktree)
**GPU:** NVIDIA GeForce RTX 4070, **12282 MiB** total (11852 MiB reported usable by torch), driver 595.84, CUDA 13.2

Measured, not model-card theory. Every figure below is an observed `nvidia-smi`
`memory.used` reading captured during a real load + generate on this machine.

Stack used for both runs: `torch 2.9.1+cu128`, `transformers 5.3.0`,
`bitsandbytes 0.50.2`, Python from
`$HOME/.local/share/speech-out/breeze-tts-2-e2/venv`.

## Precondition

Both processors were driven through the shipped GPU lease with the same starting
state, verified immediately before each run:

- `GET http://10.77.67.147:7861/api/runtime` → `state=unloaded`, `processor=null`,
  `live_call_holder=null`, `leftover_parked=true`
- `systemctl --user is-active ata-speech-tts.service` → `inactive`
- `nvidia-smi --query-compute-apps=pid,process_name,used_memory` → empty
- idle GPU baseline: **368–371 MiB** used

So the leftover hop was not the occupant, and no E2 worker was resident, during
either measurement.

## Result

| Processor | Input | Peak `memory.used` | Δ over baseline | torch max reserved | Speakers returned |
|---|---|---|---|---|---|
| VibeVoice ASR NF4 (`Dubedo/VibeVoice-ASR-HF-NF4`) | 40.0 s mono 24 kHz two-speaker | **8150 MiB** | 7779 MiB | 7576.0 MiB | 2 (post-fix; 0 at measurement — see below) |
| Resemble denoise-only | 20.0 s mono 24 kHz keep wav | **3348 MiB** | 2977 MiB | 2782.0 MiB | n/a |

Wall time: 21.2 s for the VibeVoice 40 s load+generate, 9.4 s for the resemble
denoise.

Neither run OOMed. Headroom at the VibeVoice peak: `12282 − 8150 = 4132 MiB`.

The decisive operational number is that peak **8150 MiB** against an alive E2
mouth: E2 residency alone is ~8.7–9.7 GiB, so VibeVoice and live TTS cannot
co-reside on this card. That is why both processors run behind the lease, and
why the lease unloads E2 with `restore_leftover=False` instead of cycling the
leftover hop back onto the GPU.

## VibeVoice ASR NF4

Model: `Dubedo/VibeVoice-ASR-HF-NF4`, revision
`289d51eeddb2f089f6478da4c0ace7d078a69596`.
Weights blob `model.safetensors`: 6,937,627,281 B, sha256
`c92baffa9129f22508a43dcfc746e44879aecd1743ce3918d5cfc91cdb4619c8`.

Shipped path exercised — no explicit `BitsAndBytesConfig` and no 8-bit fallback.
The repository's own `config.json` `quantization_config` drives the selective NF4
load (`_load_in_4bit: true`, `bnb_4bit_quant_type: "nf4"`,
`bnb_4bit_use_double_quant: true`, `bnb_4bit_compute_dtype: bfloat16`,
`llm_int8_skip_modules: [acoustic_tokenizer_encoder, semantic_tokenizer_encoder,
acoustic_projection, semantic_projection, lm_head]`). Logged load: 901 weight
tensors.

### Input A — `tts/lab/fixtures/voices/two-speaker-short.wav`

| | |
|---|---|
| Shape | 24000 Hz, mono, 16-bit PCM, 40.0 s, 1,920,078 B |
| sha256 | `baca70f841c6613b3495b721b6516dc2ba262eb811ae9464043e31e824524f43` |
| rms / peak | 0.08816 / 0.6818 |

Content: a natural two-speaker dialogue span, turns at 0–11.52 s, 11.52–29.57 s,
29.57–40.0 s.

| Metric | Observed |
|---|---|
| `nvidia-smi memory.used` peak | **8150 MiB** |
| peak − baseline | 7779 MiB |
| peak timestamp | 19.75 s into the run |
| nvidia-smi samples | 89 @ 0.2 s |
| `torch.cuda.max_memory_allocated` | 7338.4 MiB |
| `torch.cuda.max_memory_reserved` | 7576.0 MiB |
| `analysis_peak_vram_bytes` (service provenance) | 8,545,894,400 B = **8150 MiB** |
| load + generate wall time | 21.2 s |
| lease occupant during run | `vibevoice` |
| lease occupant after run | `null`, `e2_state_after=unloaded` |

The service's own post-run `_vram_used_bytes()` provenance read (8150 MiB) equals
the sampled peak exactly. The peak is therefore **steady-state residency**, not a
transient spike — the card holds ~7.5 GiB reserved for as long as the model is
resident.

### Input B — earlier 19.8 s concatenated two-speaker file

Same shipped path, a shorter (19.8 s) file built by concatenating turns from two
speakers.

| Metric | Observed |
|---|---|
| `nvidia-smi memory.used` peak | **7922 MiB** |
| peak − baseline | 7551 MiB |
| `torch.cuda.max_memory_reserved` | 7348.0 MiB |
| `analysis_peak_vram_bytes` | 8,306,819,072 B = 7922 MiB |
| wall time | 49.2 s (cold — first load of the session) |

Peak differs by only 228 MiB for roughly half the audio, confirming the peak is
dominated by model residency and not by audio length. The 49.2 s vs 21.2 s wall
time is cold-start variance (compilation/allocator warm-up), not a second
measurement of load cost.

### Model card cross-check

The card claims `~7–8 GB` VRAM. Observed 8150 MiB = 7.96 GiB — consistent.

## Resemble denoise-only

Shipped path exercised: `tts/lab/backend/services/resemble.py::denoise_wav` under
`ProcessorLease("resemble")`, calling
`resemble_enhance.enhancer.inference.denoise`. **`enhance()` was never called** —
the enhancer regenerates the performance, which is not the product intent.

Source: a 20.0 s mono 24 kHz clip cropped from the `george-hotz` reference fixture
(24000 Hz, 16-bit PCM, 960,078 B, sha256
`d66df0c24fffe4d242e52ebdad80031fdc2b69f1fe11dde73ac4ad20f4a52051`, rms 0.14191).

| Metric | Observed |
|---|---|
| `nvidia-smi memory.used` peak | **3348 MiB** |
| peak − baseline | 2977 MiB |
| peak timestamp | 10.52 s into the run |
| nvidia-smi samples | 41 @ 0.2 s |
| `torch.cuda.max_memory_allocated` | 2529.5 MiB |
| `torch.cuda.max_memory_reserved` | 2782.0 MiB |
| denoise wall time | 9.4 s |
| lease occupant during run | `resemble` |
| lease occupant after run | `null`, `e2_state_after=unloaded` |

Original untouched, new artifact written:

| | src | dest |
|---|---|---|
| Path | `/tmp/vram-probe/src-noisy.wav` | `/tmp/vram-probe/denoised2.wav` |
| Bytes before / after | 960,078 / **960,078** | — |
| sha256 before / after | `d66df0c2…` / **`d66df0c2…`** | `3137838a376f8af0356049bd8c76e3f5ee0c29f87e7d2b57b2e6e08f91a90faa` |
| Sample rate | 24000 Hz | 44100 Hz |
| Duration | 20.0 s | 20.0 s |
| Bytes | 960,078 | 1,764,044 |
| rms | 0.14191 | 0.13782 |

`src_unchanged: true`, `dest_is_distinct_file: true`. Cleanup is a light
noise-floor reduction (rms −2.9 %), as expected for denoise-only.

## Measured defect: the shipped parser dropped every speaker turn

**Resolved after measurement — see "Post-fix verification" below.** This section
records the parser state *at the time the peaks were taken*, which is what makes
the 0-speaker result honest rather than a live bug.

At measurement time, `default_analyze` returned
**`speakers: []`, `segments: []`, `overlaps: []`** on both inputs, at both peak
readings above. The model itself diarized correctly. Raw decode for the 40 s input:

```
<|im_start|>assistant
[{"Start":0,"End":11.52,"Speaker":0,"Content":"Zaman üzerine Norbert Elias, …"},
 {"Start":11.52,"End":29.57,"Speaker":1,"Content":"Varlığını alabildiğini somut bir şey gibi …"},
 {"Start":29.57,"End":40.0,"Speaker":0,"Content":"Peki zaman, bizim zihnimizin bir imgesi ise, …"}]
<|im_end|>
```

`processor.decode(..., return_format="parsed")` yields
`[[{Start, End, Speaker, Content}, …]]` — capitalized keys, float seconds, 0-based
speaker ids. Two independent key mismatches in the parser:

1. `_segment` looks up `start_s | start | start_time`, `end_s | end | end_time | stop`,
   `speaker_id | speaker | speaker_label | label`, `text | content | transcript`.
   None match `Start` / `End` / `Speaker` / `Content`, so `_first_number` returns
   `None` and **every** turn is dropped by the `start is None or end is None` guard.
2. `_speakers_from` derives ids as `S{index + 1}` from turn ordering only. The model
   reports 0-based `Speaker` ints, and the parser has no place to apply the `+1`,
   so a naive lowercase remap alone would mislabel speaker 0 as `S1` while
   `_segments_from` stringified the same id.

Feeding the same payload through `parse_analysis` with the keys remapped to the
lowercase names the parser actually reads does produce correct output — proving
the failure is the key lookup and not the model or the lease:

```json
{"speakers": [{"id": "S1", "label": "Speaker 1", "duration_s": 21.95},
              {"id": "S2", "label": "Speaker 2", "duration_s": 18.05}],
 "segments": [{"speaker_id": "S1", "start_s": 0.0,    "end_s": 11.52, "overlap": false},
              {"speaker_id": "S2", "start_s": 11.52,  "end_s": 29.57, "overlap": false},
              {"speaker_id": "S1", "start_s": 29.57,  "end_s": 40.0,  "overlap": false}],
 "overlaps": []}
```

So `≥2 speakers` is achievable on this hardware and this input. At measurement
time it was **not** what the shipped code returned — the 7922/8150 MiB peaks and
the 0-speaker results above all come from unmodified shipped code.

### Post-fix verification

The parser was subsequently patched in `tts/lab/backend/services/speakers.py`:
`_folded()` gives case-insensitive key lookup (so `Start`/`End`/`Speaker`/`Content`
resolve), `_segment` reads `content` from the folded map, and `_speaker_id()` maps
integer speakers through `S{n + 1}` while passing through non-numeric labels.

Re-verified against the **real captured model payload** recorded during the
measurement run (`parsed_turns` from `/tmp/vram-probe/vibe_natural_out.txt` — the
three turns printed above) through the patched `parse_analysis`, CPU-only:

```
input turns: 3 keys: ['Content', 'End', 'Speaker', 'Start']
speakers: [{"id": "S1", "duration_s": 21.95}, {"id": "S2", "duration_s": 18.05}]
segments: 3   overlaps: 0
speaker_count: 2
```

So on the exact bytes the NF4 model produced for `two-speaker-short.wav`,
**`≥2 speakers` is now returned**, with `S1 = 0.0–11.52 s`, `S2 = 11.52–29.57 s`,
`S1 = 29.57–40.0 s`, matching `_speaker_id`'s `Speaker 0 → S1` mapping and the
model's own `0/1/0` labelling. This is a parser-level replay of the recorded
payload, not a new GPU run: no model was re-invoked to produce it.

## Fixture provenance

`tts/lab/fixtures/voices/two-speaker-short.wav` previously held a 16.4 s clip whose
first 3 s matched `george-hotz-how-they-keep-you-trapped.mp3` sample for sample
(correlation 1.0 at 16 kHz after resampling) — a single speaker, despite the name.
It now holds the 40 s genuine two-speaker span
described in "Input A" above, cropped from `$HOME/Downloads/zaman_sample.wav`
with no re-encode (`-ac 1 -ar 24000 -c:a pcm_s16le`, 0–40 s). The file is 1.9 MB.

## End state

Left as the packet requires, and re-verified after the last measurement:

- `GET /api/runtime` → `state=unloaded`, `processor=null`, `leftover_parked=true`
- `nvidia-smi` used 368 MiB, no compute apps
- `ata-speech-tts.service` `inactive`
- E2 was **not** auto-reloaded; leftover was **not** restored

No process from these measurements remained resident: both probes ran in their own
short-lived process and exited, so no VibeVoice or resemble model leaked into the
long-lived lab server.

The operator subsequently loaded E2 for talker work, by their own decision, after
all measurements above were captured. That first load failed on a `transformers`
version mismatch (see below); the leftover was re-parked, the E2 pin was restored,
and the lead then reloaded E2 successfully. The GPU was left to the lead from that
point on. Re-checked while writing this: `state=ready`, `leftover_parked=true`,
`ata-speech-tts.service` inactive.

## Environment closure: the shared E2 venv cannot serve both

The two processors do not merely want different libraries — they need
**incompatible `transformers` versions**, and this is now measured rather than
inferred.

The E2 venv carries one `transformers`. At the time of measurement it was
`5.3.0`, because the NF4 model requires it: `VibeVoiceAsrForConditionalGeneration`
does not exist before 5.3.0, and the model card states `transformers >= 5.3.0`.

That same `5.3.0` breaks the E2 side. With it installed, loading E2 failed:

```
state = error
transformers.tokenization_utils_tokenizers.TokenizersBackend._patch_mistral_regex()
  got multiple values for keyword argument 'fix_mistral_regex'
```

and `qwen_tts 0.1.1` (the codec path) failed to import with
`cannot import name 'check_model_inputs' from 'transformers.utils.generic'`.

After measurement, the pin was restored to `transformers 4.57.3` to get talker
back. Re-verified directly:

| transformers | `import qwen_tts` | `from transformers import VibeVoiceAsrForConditionalGeneration` |
|---|---|---|
| 5.3.0 | fails (`check_model_inputs`) | works |
| 4.57.3 (current) | works | **fails** (`cannot import name`) |

Consequences at `4.57.3`, the current state:

- E2 / talker load path is healthy again.
- `default_analyze` **fail-closes** with `ProcessorUnavailable` — its
  `except ImportError` branch wraps exactly this missing symbol. It does not
  silently degrade; it raises. That is the correct failure mode, but it means
  VibeVoice analysis is unavailable on the shared venv as pinned.
- Resemble denoise is unaffected by the pin either way.

So the numbers above were measured under `5.3.0`, and reproducing them on the
current tree requires a `transformers >= 5.3.0` environment. The durable
resolution is a **separate venv for the vibevoice/resemble processors** rather
than pinning the shared E2 venv against `qwen_tts`. Flagged, not acted on here.

## Reproducing

Both probes ran in a throwaway process against the shipped modules (the live lab
server on `:7861` was running pre-change code at the time, so the HTTP routes did
not yet exist). The lease wiring, which is the part that matters:

```python
import sys, torch, subprocess, threading, time
sys.path.insert(0, "$HOME/workspace/speech-core")
sys.path.insert(0, "$HOME/workspace/speech-core/lab/scripts")

from tts.lab.backend.runtime.leftover import SystemdLeftover
from tts.lab.backend.runtime.manager import E2RuntimeManager
from tts.lab.backend.runtime.processors import ProcessorLease
from tts.lab.backend.runtime.vram import NvidiaSmiVramProbe
from tts.lab.backend.services.speakers import LEASE_NAME, default_analyze
from tts.lab.backend.services.resemble import PROCESSOR as RESEMBLE, denoise_wav

manager = E2RuntimeManager(leftover=SystemdLeftover(), vram=NvidiaSmiVramProbe(), autoload=False)

# sample nvidia-smi memory.used every 0.2 s in a daemon thread; take the max
with ProcessorLease(manager).acquire(LEASE_NAME):          # or RESEMBLE
    analysis = default_analyze(WAV)                        # or denoise_wav(src, dest)
```

Env: `PYTHONPATH=$HOME/workspace/speech-core/lab/scripts`, interpreter
`$HOME/.local/share/speech-out/breeze-tts-2-e2/venv/bin/python`.

## Not verified

- No measurement through the lab HTTP routes (`/reference/denoise`,
  `/speakers/analyze`) — the live server predates those routes, so the equivalent
  shipped functions were called directly under a real `ProcessorLease`.
- No repeat trials for variance. VibeVoice was run twice (7922 / 8150 MiB peak) and
  resemble once.
- Resemble was measured on a single 20 s mono source; longer inputs would raise the
  activation working set but the weights are the dominant term (~2.5 GiB reserved).
- No concurrent-residency test of VibeVoice beside a live E2 worker — the peaks make
  the arithmetic conclusive (8150 + E2 residency ≫ 12282 MiB), but it was not run.
- Reproducing the VibeVoice number requires `transformers >= 5.3.0`. The venv is now
  pinned at `4.57.3`, where the model class does not exist at all — so the shipped
  `default_analyze` fail-closes there by design. See "Environment closure" above.
- No pip installs were made after the pin restore, and no model was loaded after the
  checks above.
