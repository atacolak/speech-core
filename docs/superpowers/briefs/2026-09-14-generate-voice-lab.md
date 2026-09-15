# Generate + Voice Lab

**Status:** settled
**Source:** operator packet 2026-09-14
**Repo:** speech-core

## Intent

Restructure the TTS lab frontend around two top-level
modes: GENERATE and VOICE LAB. Do not polish the current
workbench skeleton.

GENERATE is the realtime Breeze playground.

VOICE LAB is where material is analyzed, speakers extracted,
voices curated, references assembled, processing compared,
and repeatable auditions created.

A source is MATERIAL inside Voice Lab, not a peer app.

## Keep

Backend that earned its place: media_sources, source_analyses,
source_speakers, clips, leftover hop, ProcessorLease,
yt-dlp ingest, cheap peaks, Resemble in Breeze venv,
VibeVoice NF4 already in HF hub. Overlay leftover hop.
Not a pin swap. AuK stays parked.

## Replace

Top nav Voices | Sources. Permanent inspector. Permanent
compare. Source editing mixed into voice editing. Metadata
boxes posing as a corpus. Runtime ceremony on the creative
surface. Workbench `assets | waveform | inspector`.
