# Lab reference preparation (resemble + vibevoice)

**Status:** settled
**Source:** operator request 2026-09-12 (this chat)
**Repo:** speech-core

## Intent

Extend the current TTS voice lab reference-preparation surface so an operator can turn arbitrary source audio into a high-quality cloning reference.

Purpose is specifically: obtain the cleanest, most speaker-faithful reference possible for downstream voice cloning. Not a generic audio-processing suite.

Preserve substantial uncommitted lab work. Do not reset or reconstruct from an older design.

stream.fm is **not** implemented end-to-end. Frontend/API plumbing exists; there is no working backend processor. Do not implement stream.fm. Replace that unfinished concept.

## Capabilities

1. **Resemble Enhance, denoise-only** as the cleanup processor. Not the generative enhance/restore stage. Original remains permanent. Cleanup operates on the current effective/curated reference. Reuse `reference_variants`. Original and denoised can both be auditioned; either can be the active cloning reference. Changing effective regions invalidates stale cleanup.

2. **Offline Microsoft VibeVoice ASR** via `Dubedo/VibeVoice-ASR-HF-NF4` (selective NF4). Not the streaming checkpoint. Structured speaker-attributed timing: speaker, start/end, transcript, provenance.

3. **Reference editor evolution**, not a separate diarization app. Analyze speakers is optional. After analysis, compact speaker list, highlight regions, listen, **use speaker N** sets keep-regions to that speaker's non-overlapping intervals. Manual keep/exclude remains. Do not flatten into an irreversible concatenated file.

4. **Transcript:** source / diarization result vs effective selected-speaker vs operator-locked. Do not clobber locked text. When a speaker is selected, effective transcript follows those regions.

5. **Overlap:** diarization is not source separation. Mark overlapping/unsafe regions; exclude them from automatic speaker extraction. Do not implement overlap recovery.

6. **Surface:** keep three-pane lab. Speaker analysis near waveform. Resemble under cleanup/variants. No implementation jargon (nf4, repo names) on the operator surface. Multi-speaker controls absent until analysis exists.

7. **Runtime:** vibevoice must not stay resident beside TTS on the 12 GB 4070. Explicit load/unload. Do not compromise the active TTS runtime merely to keep vibevoice resident. Processors are real runtime resources.

8. **Provenance/cache:** source artifact, effective intervals, processor/model identity + revision, config, result artifact, analysis version. Stale derived work must not masquerade as current.

## Parked (do not implement)

- Cleanup bakeoff: resemble vs `mossformer2_se_48k` if resemble damages identity or fails on hard ambience.
- Overlap recovery: prefer `penta2himajin/tse-conv-tasnet-48k`; fallback `mossformer2_ss_16k`.
- Live diarization: Microsoft vibevoice streaming checkpoint later.

## Validation

Resemble: noisy single-speaker import; original remains; denoise is a separate variant; both auditionable; either selectable; crop does not destroy source; changing effective source invalidates derived cleanup.

Vibevoice: known two-speaker source; distinct labelled intervals; operator chooses one speaker; effective reference is that speaker's non-overlapping regions; effective transcript follows; manual exclude still works; locked transcript not clobbered.

Resource: selective NF4 actually loads and completes on the local 12 GB 4070; record observed peak VRAM.
