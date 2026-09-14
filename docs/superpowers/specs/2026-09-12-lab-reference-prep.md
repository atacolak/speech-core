# Lab reference preparation Technical Spec

**Design Brief:** `$HOME/workspace/speech-core/docs/superpowers/briefs/2026-09-12-lab-reference-prep.md`
**Status:** sound
**Repo:** speech-core

## Intent (from the Brief — do not rewrite)

Replace unfinished stream.fm with resemble-enhance **denoise-only**. Add optional offline vibevoice (`Dubedo/VibeVoice-ASR-HF-NF4`) speaker analysis into the existing reference editor. Selecting a speaker sets keep-intervals to that speaker's non-overlapping regions on the original source. Cleanup runs on the current effective reference and is a `reference_variants` row. Original source is never overwritten. Locked transcripts are never clobbered. Processors are explicit GPU occupants and must not sit beside live TTS. Overlap recovery, mossformer bakeoff, and streaming vibevoice are parked.

## Mapping onto the current system

Observed in the **current uncommitted worktree** (do not revert this):

- Voices already have `source_artifact_id`, `original_artifact_id`, `keep_intervals_json`, `source_transcript`, `effective_transcript`, `source_words_json`, `transcript_locked`, `active_reference_variant_id`.
- `reference_variants` already exists (`kind`, `audio_artifact_id`, `processor_config_json`, `processor_cache_key`).
- Keep math already lives in `tts/lab/backend/services/references.py` (`normalize`, `exclude`, `keep_only`, `slice_transcript`, `materialize_keep_wav`).
- Activate already exists: `POST /api/voices/{id}/reference/activate` by `variant_id` or `kind`.
- stream.fm is a stub: `POST /api/voices/{id}/streamfm` calls `tts.preprocess.process_reference`, which reports `unavailable` unless a `streamfm` CLI exists. UI radios and `streamfmVoice()` exist. **Replace, do not finish.**
- GPU occupancy already exists for E2: `E2RuntimeManager` parks leftover, loads a subprocess worker, unloads and restores leftover. Live-call lease blocks competing lab generate. Current GPU: ~9506 MiB used / 2348 MiB free of 12282 MiB with E2 resident.
- Transcript lock: patching `effective_transcript` sets `transcript_locked=1`; keep edits then leave it alone (`_effective_for_keep`).

Reuse that. Do not invent a parallel voice/reference store.

## Architecture

One extra sqlite table for speaker analysis. One extra variant kind `resemble`. One GPU lease over E2 + lab processors.

```
source artifact (immutable)
  -> keep_intervals (operator + optional "use speaker")
  -> materialize_keep_wav (effective original)
  -> resemble denoise -> reference_variants.kind=resemble
  -> active_reference_variant_id chooses clone audio

source artifact
  -> vibevoice analysis (optional) -> speaker_analyses.result_json
  -> UI highlights / listen
  -> use speaker -> keep_intervals = non-overlapping segments
```

Processors run in **throwaway subprocesses**, same idea as `SubprocessWorker`, not in the FastAPI process and not immortal.

## Components and interfaces

### Data

`voices` gains optional `speaker_analysis_id TEXT`.

New table `speaker_analyses`:

| column | meaning |
|---|---|
| id | `sa_…` |
| voice_id | owner |
| source_artifact_id | analyzed audio |
| processor | `vibevoice-asr` |
| model_id | `Dubedo/VibeVoice-ASR-HF-NF4` |
| model_revision | HF revision |
| config_json | processor config |
| cache_key | sha of source sha + processor identity |
| result_json | speakers, segments, overlaps |
| created_at | utc |

`result_json` shape:

```json
{
  "version": 1,
  "speakers": [{"id": "S1", "label": "Speaker 1", "duration_s": 18.4}],
  "segments": [
    {"speaker_id": "S1", "start_s": 0.20, "end_s": 3.10, "text": "…", "overlap": false}
  ],
  "overlaps": [{"start_s": 5.0, "end_s": 5.8, "speakers": ["S1", "S2"]}]
}
```

Overlap: if vibevoice emits overlapping timestamps (or two segments share time), those ranges go in `overlaps` and matching segment slices are `overlap: true`. Automatic **use speaker** drops overlap slices.

`reference_variants.kind` becomes `original | resemble | other`. Existing `streamfm` rows, if any, are treated as stale `other` and not offered as cleanup.

Cache: generalize `tts/lab/backend/store/cache.py` from streamfm-hardcoded to `processor_cache_key(processor, source_sha256, keep_intervals, config)`. Resemble key **includes keep intervals**. Analysis key is source-sha only (full file).

### HTTP (lab-only)

Replace stream.fm routes.

- `POST /api/voices/{id}/reference/denoise` — denoise current keep wav; upsert `kind=resemble` variant; return voice.
- `POST /api/voices/{id}/speakers/analyze` — run vibevoice on **source** artifact; store analysis; return voice + analysis.
- `POST /api/voices/{id}/speakers/use` body `{speaker_id}` — set keep to that speaker's non-overlap intervals; refresh effective transcript unless locked; return voice.
- `GET /api/voices/{id}` already returns voice; include `speaker_analysis` when present.
- Delete `POST /api/voices/{id}/streamfm` and `GET /api/streamfm/status`.

Errors:

| code | when |
|---|---|
| 409 `processor_blocked_live_call` | leftover hop / desk session holds the live-call lease |
| 409 `talker_voice_missing` | unchanged, leftover only |
| 503 `engine_loading` / busy | E2 mid load/unload and lease cannot be taken yet |
| 501 | processor dependency missing (tests may fake) |
| 422 | no analysis / unknown speaker / empty non-overlap keep |

Voice JSON additions (operator-facing, no repo names):

```json
"speaker_analysis": {
  "id": "sa_…",
  "speakers": [{"id": "S1", "label": "Speaker 1", "duration_s": 18.4}],
  "segments": [...],
  "overlaps": [...],
  "stale": false
}
```

`stale` is true if `source_artifact_id` no longer matches the voice source. Do not auto-delete; UI hides speaker chips and offers re-analyze.

Provenance (debug only, inspector/runtime): model_id, revision, cache_key, peak_vram_bytes. Not on the speaker chips.

### Resemble

Call `resemble_enhance.enhancer.inference.denoise(dwav, sr, device)` — **not** `enhance()`. Input is `materialize_keep_wav` of current keep intervals. Output is a new artifact, pinned `voice:{id}:resemble`. Original artifact stays pinned as original.

If keep changes, cache key changes. Serving/activating `resemble` when `processor_cache_key` does not match current keep marks the variant stale; clone path falls back to original until regenerate. Do not silently play old denoise as current.

### Vibevoice

`Dubedo/VibeVoice-ASR-HF-NF4` via `VibeVoiceAsrForConditionalGeneration` + `AutoProcessor`, `return_format="parsed"`. Map parsed speaker turns into segments. Compute overlaps. Do not concatenate a new source wav.

**Use speaker** = `keep_intervals = non-overlap segments for that id`, then existing `_patch_keep`. Transcript: if not locked, set `source_words` from analysis words/segments and `effective_transcript` via `slice_transcript` (or segment texts in keep). If locked, keep text, still change intervals.

Existing `POST /api/voices/{id}/transcribe` stays for single-speaker unanalyzed audio. If analysis exists and transcript is not locked, transcribe must not wipe speaker-attributed words unless the operator asked for a fresh transcribe (then it replaces source_words and clears analysis? **No** — transcribe updates source_words/transcripts only; it does not delete analysis. If locked, transcribe is a 409 `transcript_locked` or is ignored for text — prefer 409 so the UI can say unlock first).

Decision: `transcribe` on a locked voice returns 409 `transcript_locked`. Unlocked: may replace source_words + transcripts, leaves analysis in place.

### GPU lease

New `tts/lab/backend/runtime/processors.py` (or similar) owned by lab state:

```
acquire(name: "resemble" | "vibevoice")
  if live_call_active: 409 processor_blocked_live_call
  if E2 loading/unloading: wait or 503 runtime_busy
  if E2 ready: unload E2 with restore_leftover=False (leftover stays parked)
  occupant = name
run subprocess
always: kill subprocess, occupant=none, leftover stays parked, E2 stays unloaded
```

Do **not** auto-reload E2 after a processor. Operator reloads Breeze if they want it. Status must show occupant (`processor: resemble|vibevoice|null`) so this is visible.

Resemble is small but still goes through the lease. No immortal `load_enhancer` cache across requests in the API process (`@cache` inside a killed worker is fine).

Extend `E2RuntimeManager.unload` with `restore_leftover: bool = True` so processor swap does not bounce parked qwen onto the GPU.

### UI

`reference-editor.tsx` only. No new pane.

Near waveform: **Analyze speakers** (hidden jargon). After analysis: chips `Speaker 1 · 18.4s`. Click chip: highlight those regions (distinct from exclusion paint), play concatenated? **Play speaker** plays regions in timeline order without writing a file (wavesurfer regions). **Use speaker** calls `/speakers/use`.

Cleanup block: replace stream.fm copy with **Denoise** / Original / Denoised. Same radio+AudioBar pattern.

`voices-pane.tsx` and inspector: `resemble` label instead of `stream.fm`.

Multi-speaker chrome is unmounted when `speaker_analysis` is missing or stale.

### Talker / synthesis

`talker.py` and `synthesis.py` currently special-case `kind == "streamfm"`. Change to: if active variant is `resemble` (or any non-original with an artifact), clone from that artifact; else materialize keep from source. Original keep edits still apply only to the original path. Resemble artifact is already the keep-wav denoise, so do not re-crop it.

## Data / control flow

Import → optional analyze → optional use speaker → optional denoise → activate variant → synthesize.

Diarization always on full source. Denoise always on current keep materialization.

## Error handling

Fail closed. Missing weights → 501 with message, not a fake clean wav. CUDA OOM → processor error, E2 left unloaded, leftover still parked. Live call → 409, no unload.

## Testing (behavioral contracts; exact tests live in the plan)

- Cache key changes when keep changes (resemble); analysis key ignores keep.
- Denoise fake worker: original artifact unchanged; new resemble variant; activate either; keep edit marks resemble stale.
- Use speaker: keep becomes non-overlap intervals; overlap excluded; locked transcript unchanged; unlocked transcript follows segments.
- Lease: live_call_active → 409 and E2 worker untouched. E2 ready → unload without leftover restore, then processor runs.
- UI tests: no stream.fm strings; denoise/analyze/use speaker controls.

GPU peak is a recorded qualification note, not a unit test.

## Non-goals

stream.fm implementation. `enhance()` generative restore. mossformer. TSE / overlap recovery. Streaming vibevoice. New top-level pane. Permanent vibevoice residency. Concatenated flattened speaker wav as the new source. Voicecat / leftover hop protocol changes. Pin swap.

## Implementation approach chosen (and rejected internals)

**Chosen:** sqlite `speaker_analyses` + existing keep_intervals + `kind=resemble` variant + subprocess processors behind a GPU lease that can unload E2 without restoring leftover.

**Rejected:** in-process model globals (leaks VRAM next to E2). New source artifact per speaker (destroys inspectability). Running denoise on the full source then cropping (would denoise excluded speakers/noise the operator already cut). Auto-reload E2 after analysis (long warmup, surprising).

## Open questions

None. Parked items are explicit non-goals.
