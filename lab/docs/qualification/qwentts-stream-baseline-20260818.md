# llm→qwen stream baseline — 2026-08-18T21:24+10

**before** the hop. do not mix clock domains.

live leftover: qwentts-tts-server pid **2693825** @ **2440 MiB**.
speech-out-daemon pid **2667128** (aa86b67 binary, ActiveEnter 17:51:39 AEST).
first-clause opener flush **reverted**. SENTENCE only.

## product sit (voicecat wall_ms, niru)

from `docs/SEAM-LATENCY.md` 08:41 sit + sdc-z7b:

| hop | SmallWebRTC | domain |
| --- | ---: | --- |
| ice completed → first `pcm_in` | **172 ms** (was 365) | server wall |
| `transcript_final` → first `llm_text` | **2.859 s** | observer wall_ms |
| first `llm_text` → `tts_start` | **0.374 s** | SENTENCE wait |
| `tts_start` → first PCM (TTFB metric) | **0.549 s** | leftover Cosy label; engine is qwen |
| `tts_start` → hear | **586 ms** | browser_sink |
| `transcript_final` → hear | **3.819 s** | same process |

Discord columns in that file are leftover. do not subtract them from these.

## speech-out now-stamp (`:8788`, daemon_mono)

client: `speech-out-aa86b67 play --play-command true`. 2026-08-18T21:24+10.

| text | D first-pcm | W first-pcm | samples | wall |
| --- | ---: | ---: | ---: | ---: |
| `hey, good to see you.` | **34.9 ms** | 34.8 ms | 48000 | 233 ms |
| same + second sentence | **33.3 ms** | 33.2 ms | 90240 | 413 ms |

first codec frame, often hush. **usable** energy is the isolated http number.

## isolated qwen http (usable)

`bench/ttfp-oneshot-vs-clause.json` + locked styles:

| arm | first-use p50 | e2e p50 |
| --- | ---: | ---: |
| informal / crisp / clip | **~70 ms** | ~800–844 ms |
| first clause only | **71.0 ms** | 234 ms |
| full paragraph | **71.3 ms** | 782 ms |

extra text does **not** delay first usable audio (~0.2 ms).

## what the hop must beat

`llm_text` → `tts_start` **0.374 s** is SENTENCE holding the reply.
target: first `speak` / first pcm when the **first speakable unit** exists
(~70 ms after that text hits qwen), not after the last token.

not token drip. not `Hello,` opener hack.
