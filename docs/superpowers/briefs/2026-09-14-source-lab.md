# Source analysis + voice curation lab

**Status:** settled
**Source:** operator request 2026-09-14
**Repo:** speech-core

## Intent

Replace the voice-workbench-as-reference-editor with a lab for:

```text
source ingest
  → selective VibeVoice analysis
  → speaker discovery
  → clip extraction (non-overlapping turns)
  → optional Resemble denoise/enhance
  → voice / reference curation
  → Breeze generation
```

A YouTube URL is a SOURCE, not a voice. A voice (Ford, Dolores)
collects clips from many sources.

## AuK

Parked. Unsatisfactory for reference prep. Strip lab UI/runtime
chrome. Keep generic artifact/provenance tables. README note only.

## Models (confirmed local — do not redownload)

- VibeVoice: `Dubedo/VibeVoice-ASR-HF-NF4` already in HF hub
  (`model.safetensors` 6.94 GB, rev `289d51ee…`). Needs
  `transformers>=5.3` — **separate venv**, not the Breeze 4.57 pin.
- Resemble: `resemble_enhance` already in the Breeze venv
  (`enhancer_stage2` 713 MB). Denoise is shipped; `enhance()` is
  now an optional derivative after extraction, not ingest.
- yt-dlp 2026.08.19 + ffmpeg 6.1.1 already on PATH.
- Breeze E2 remains the realtime mouth. Overlay leftover hop.
  Not a pin swap.

## Ontology (do not conflate)

SOURCE, ANALYSIS, CLIP, VOICE, REFERENCE, DERIVATIVE, GENERATED TAKE.

Speaker ids are **source-local** (`westworld_clip_01:speaker_00`)
until mapped to a voice. Overlaps: mark and exclude. No TSE this
sprint. No full auto-transcribe of 1h media.

## Benches

SOURCE BENCH — ingest, waveform, range analyze, speaker lanes,
extract. VOICE BENCH — references, derivatives, provenance, Breeze.

## Success

Westworld URL → range analyze → map speaker_00 to Ford → extract
clean turns + plain transcript → optional Resemble → approve
reference → Breeze take records that reference. No AuK chrome.
