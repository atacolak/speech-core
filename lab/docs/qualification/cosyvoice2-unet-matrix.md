# CosyVoice2 Unet matrix — 5 personas × 10 sentences

**when:** 2026-08-17  
**stack:** isolated `triton-cv2-sc` (Unet BLS + in-process TRT-LLM fp16). live `aa86b67` stayed parked. **2026-08-18 live mouth is qwentts.** this matrix is historical.
**clock:** `T_text_done` → first non-empty waveform callback.  
**ui:** https://sfub.taila159c4.ts.net:18080/ (tailnet only)  
**raw:** `/home/sf/.cache/speech-out/triton-campaign-20260812/matrix-host/report.json` + `wavs/`

## is Unet “always ~225 ms”?

**no.** official zero-shot voice is tight. other refs are not.

| slice | n honest | first-chunk p50 | p95 |
|---|---:|---:|---:|
| all cells | 50 | 256 ms | 480 ms |
| honest only (drop 3 junk hops) | 47 | **256 ms** | **480 ms** |
| `zh_default` official | 10 | **225 ms** | 229 ms |
| `human_short` | 9 | 217 ms | 224 ms |
| `human_clean` | 10 | 256 ms | 261 ms |
| `en_cross` official long EN ref | 8 | **401 ms** | 403 ms |
| `human_slow` 16 s ref | 10 | **480 ms** | 482 ms |
| parked CV3 TRT (historical / now-stamp) | 25 / 5 | **282 / 284 ms** | 603 / 398 ms |

min honest 204 ms (`human_short` × `en_hear`). max honest 483 ms (`human_slow`).

## suspects (do not use as latency)

| cell | first-chunk | duration | note |
|---|---:|---:|---|
| `en_cross__en_continue` | 264 ms | 0.40 s | near-silent |
| `en_cross__en_long` | 215 ms | 0.08 s | truncated |
| `human_short__en_hello` | 139 ms | 0.04 s | empty-ish hop; “Hello.” + 2.5 s “Okay.” ref |

these are why a single cherry-picked 139–215 ms is a lie.

## read

- **same voice as the earlier bake-off (`zh_default`)** stays 218–236 ms across 10 texts (zh + en, short + long). that 225 ms number is real for *that* pin, not a one-prompt miracle.
- **long / mismatched refs cost ~150–250 ms.** `en_cross` is a 13.7 s EN clip used as a clone source; first-chunk clusters at **401 ms**. `human_slow` is 16 s of human speech; **480 ms**. Unet still streams; it is not “always faster than parked CV3.”
- **duration ≠ quality.** `human_short` × zh texts ran 11–30 s (runaway). first-chunk can look great while the utterance is garbage. listen.
- parked CV3 ~284 ms still wins vs long-ref Unet. Unet only beats it on short/official refs.

## personas

| id | what |
|---|---|
| `zh_default` | official CosyVoice zero-shot wav + 希望你以后… |
| `en_cross` | official cross-lingual wav + long EN company text |
| `human_clean` | local golden 01, whisper: “The weather looks great today…” |
| `human_short` | local golden 05, whisper: “Okay” |
| `human_slow` | local golden 08, whisper: “The issue is that the system doesn't know when to stop.” |

seed_tts voices were gated on HF; not used.

## sentences

5 zh (birthday, tongue-twister, weather, how-are-you, order number) + 5 en (hear, ready, hello, continue, long ack).
