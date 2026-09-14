# Source analysis + voice curation Technical Spec

**Brief:** `docs/superpowers/briefs/2026-09-14-source-lab.md`
**Status:** sound
**Repo:** speech-core

## Intent (do not rewrite)

Source ingest → selective VibeVoice → extract non-overlapping
turns → optional Resemble → voice/reference → Breeze. Park AuK.
Speaker ids are source-local until mapped. No overlap-separation
this sprint.

## Mapping

Keep: artifact store, ProcessorLease (`restore_leftover=False`),
Breeze runtime, leftover hop, `speakers.py` parser (capitalized
NF4 keys), `resemble.py` denoise, `voice_sources` /
`voice_artifacts` / `breeze_auditions` tables.

Change: sources are first-class (not a child of one voice).
Voices collect clips from many sources. Analysis is range-scoped
and coverage-aware. Workbench splits SOURCE BENCH / VOICE BENCH.
AuK UI/runtime chrome leaves the lab.

Do not revert leftover hop / desk contract. Do not steal the
live Breeze occupant (~8.6 GiB as of inspect).

## Data

```text
media_sources          id, kind file|youtube, url/path, title,
                       duration_s, waveform_artifact_id, meta_json
source_analyses        id, source_id, range start/end, processor,
                       result_json (segments+overlaps), coverage
source_speakers        source-local id, mapped_voice_id nullable
clips                  selected non-overlap ranges, source_id,
                       speaker_local_id, audio, clean_transcript
voices                 speaker identity (Ford). no required source.
voice_references       approved clips/derivatives on a voice
derivatives            parent clip/ref, processor resemble
                       denoise|enhance
runs                   already exist; must pin voice + reference
```

Existing `voice_sources` can backfill as media_sources owned by
one voice; new ingest is unowned until extract.

Clean reference transcript: concat selected segment texts only.
Never `SPEAKER_00:` markup into Breeze.

## HTTP (lab-only)

- `POST /api/sources` `{file}` or `{url}` — yt-dlp, no auto-ASR
- `GET /api/sources`, `GET /api/sources/{id}` waveform/meta
- `POST /api/sources/{id}/analyze` `{start_s,end_s}|visible|all`
  VibeVoice lease; 409 live_call; skip already-covered ranges
- `POST /api/sources/{id}/speakers/{local_id}/map` `{voice_id}`
- `POST /api/sources/{id}/extract` `{speaker_local_id, ranges[]}`
  → clip + clean transcript; overlaps rejected
- `POST /api/clips/{id}/resemble` `{mode: denoise|enhance}`
- existing Breeze synthesize records `reference_id`

Fail closed. Missing VibeVoice venv → 501, no fake diarization.

## Runtime

VibeVoice in a **separate venv** (`transformers>=5.3`). Do not
install 5.3 into the Breeze pin. Resemble may stay in the Breeze
venv (already there) behind ProcessorLease. Sequential occupancy:
vibevoice XOR resemble XOR breeze. UI abstracts load ceremony.

## UI

Top nav: **Voices** | **Sources**. Not AuK chips.

SOURCE BENCH: waveform, analyzed coverage, speaker lanes +
aligned transcript, range select, ANALYZE (selection / visible /
whole), map speaker → voice, multi-select non-overlap turns,
extract.

VOICE BENCH: references ★, source material, derivatives,
generated takes, provenance inspector (quiet).

Compare original vs denoise vs enhance appears only when those
artifacts exist. No permanent empty inspector.

## AuK retirement

Remove Load AuK / generate-candidate / precision / intent modes
CLEAN… from the lab web. `/api/auk/*` may 410. Pin dir and
`tts/auk/` stay on disk as parked code. README historical note.

## Non-goals

Overlap TSE, music separation, AuK resurrection, DAW, global
speaker id across sources, auto full-hour ASR, leftover env
rename, pin swap.

## Approach

**Chosen:** first-class media_sources + range analyses; extract
builds clips then attach to voices; specialists sequential.

**Rejected:** keep AuK workbench and bolt VibeVoice onto it.
One-voice-owns-the-youtube-row.
