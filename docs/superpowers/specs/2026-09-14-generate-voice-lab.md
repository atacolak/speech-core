# Generate + Voice Lab Technical Spec

**Brief:** `docs/superpowers/briefs/2026-09-14-generate-voice-lab.md`
**Status:** sound
**Repo:** speech-core

## Intent (do not rewrite)

Two modes: GENERATE and VOICE LAB. Sources are material
inside Voice Lab. Timeline owns the source bench. Voice
bench curates a speaker. Auditions are first-class.
AuK stays parked. Overlay leftover hop. Not a pin swap.

## Mapping

Keep: leftover hop, ProcessorLease (`restore_leftover=False`),
Breeze runtime, `media_sources` / `source_analyses` /
`source_speakers` / `clips`, yt-dlp ingest, peaks artifacts,
`speakers.py` parser, `resemble.py` denoise, `voice_artifacts`,
`breeze_auditions` (extend, do not invent a second take store
if runs already hold generated audio).

Change: top-level IA. Voice Lab library = Voices + Material.
Selecting a voice opens VOICE BENCH. Selecting material
opens SOURCE BENCH. GENERATE is the current synthesis
surface without the three-pane inspector as a permanent
sibling of Voice Lab.

Do not: steal live Breeze, recycle lab-web unless asked,
resurrect AuK chrome, implement overlap TSE, download
VibeVoice/Resemble (already local).

## Ontology (shared; workers do not invent synonyms)

```text
SOURCE          immutable imported material (file|youtube)
ANALYSIS        temporal metadata on a source (VibeVoice)
DERIVED AUDIO   non-destructive child (Resemble denoise|enhance)
REGION / TURN   time interval on a source
CLIP            extracted non-overlapping turns + clean transcript
VOICE           persistent speaker identity (Ford). not one wav
MATERIAL CORPUS clips assigned to a voice, many sources
REFERENCE       assembled/approved Breeze reference (reel of clips)
AUDITION        durable test: voice + reference + text + steer + settings + takes[]
TAKE            one generated Breeze output under an audition
```

Speaker ids stay source-local (`westworld_ep3:speaker_00`)
until mapped. Display the voice name after map; provenance
keeps the local id.

Clean transcript = concat selected segment texts. Never
`SPEAKER_00:` markup into Breeze.

Plain transcript strings are DERIVED views of structured
segments `{start_s, end_s, speaker_local_id, text}`.

## Modes

Top nav: **GENERATE** | **VOICE LAB**. No Sources tab.

GENERATE: voice picker, optional reference, text, steer,
generate, takes. Fast everyday Breeze. Runtime chip stays
quiet (existing Load Breeze control).

VOICE LAB: left library

```text
voices
  morpheus
  ford
  + new voice

material
  westworld — ford / dolores
  morpheus reference.wav
  + add source
```

Voice selected → VOICE BENCH. Material selected → SOURCE
BENCH. These are workspaces in the same lab.

## Source bench

Dominant object: THE TIMELINE. Waveform owns the canvas.
Drag to select. Zoom/scrub/play. Speaker lanes with actual
text. Analyzed vs unanalyzed coverage. Aligned transcript.
Analyze ▾ current selection (default) / visible / whole.
Map speaker_00 → Ford. Multi-select clean turns. Save to
existing/new voice.

Timestamp fields are a precision edit after a visual
selection exists, not the primary mechanism.

VibeVoice is an analysis instrument, not a settings panel.
Missing venv → 501 without taking ProcessorLease first
(ANALYZE currently leases before import — t5/lease-guard
must not dump Breeze). Fake analyzer in UI tests.

## Voice bench

Not a giant editable source waveform.

Primary reference ★ with play. Material corpus as clip
cards (source, range, duration, original/denoised, player).
Reference reel: pick clips, assemble contiguous wav behind
the scenes, keep clip lineage, mark one primary.
Auditions: named durable tests, takes underneath.

Inspector/provenance is contextual (drawer). Compare appears
only when ≥2 things are compared.

## Visual levels

1. CANVAS — borderless central working space
2. OBJECTS — turns, clips, reels, auditions
3. CHROME — metadata, diagnostics, runtime — quiet

Do not permanently allocate empty inspector, empty compare,
or runtime panels with no active object.

## Non-goals

Overlap TSE, DAW, global speaker id, AuK resurrection,
music separation, node graphs, always-visible model config,
pin swap, leftover env rename.

## Approach

**Chosen:** replace the room. Keep the backend. Fake data
is allowed to land IA and interaction grammar before GPU
tasks.

**Rejected:** incremental beautification of workbench.tsx.
Keep Voices | Sources as peer apps.
