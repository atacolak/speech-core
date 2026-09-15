# Generate + Voice Lab Implementation Plan

**Goal:** GENERATE | VOICE LAB. Sources are material.
Timeline owns source bench. Voice bench curates a speaker.
Auditions first-class. Park AuK. Keep leftover hop.

**Spec:** `docs/superpowers/specs/2026-09-14-generate-voice-lab.md`
**Brief:** `docs/superpowers/briefs/2026-09-14-generate-voice-lab.md`

Local, do not redownload: VibeVoice NF4 hub snapshot,
Resemble in Breeze venv, yt-dlp, ffmpeg.

UI first. Fake source/voice data is allowed until t2/t4
have real timeline/extract. Do not steal live Breeze.

---

### Task 1: LAB SHELL / IA

**Files:** top-bar, app-shell, workspace.ts, new Voice Lab
library (voices + material), GENERATE = synthesis pane.
Delete top-level Sources tab. Do not keep workbench.tsx
as the Voice Lab default. Do not rewrite source-bench
timeline here (t2).

**Verification:** vitest: GENERATE | VOICE LAB nav; Voice
Lab shows Voices and Material headings; selecting a voice
opens voice bench (not workbench inspector trio); selecting
material opens source bench; no Sources tab; no Load AuK;
no permanent empty compare on GENERATE. No GPU.

**Landed 2026-09-14:** GENERATE | VOICE LAB. Sources tab gone.
Voice Lab library = Voices + Material. MAIN 68 vitest.
Settings is a GENERATE drawer, not a permanent inspector.


### Task 2: SOURCE TIMELINE

Drag-select waveform, speaker lanes, aligned transcript,
coverage, analyze ▾ selection/visible/whole, speaker map.
Fake VibeVoice in tests. No GPU.

### Task 3: TRANSCRIPT MODEL

Structured segments stay canonical. Derived clean string.
Crop/select updates transcript. No free-floating textarea
as the only store.

### Task 4: EXTRACTION / MATERIAL CORPUS

Save selected turns to existing/new voice. Clip cards with
provenance. Overlaps excluded. No SPEAKER_ markup.

### Task 5: PROCESSING LAYERS

Resemble denoise/enhance as derived audio on the same
timeline. A/B. Immutable source. No load/unload ceremony.
Lease-guard: 501 before ProcessorLease if venv missing.

### Task 6: REFERENCE REELS

Assemble clips into a contiguous reference wav. Keep
lineage. Multiple reels, one primary.

### Task 7: AUDITIONS

Durable text+steer+reference+settings. Takes grouped
under the audition. Star/save. Reuse after reference
change. Extend `breeze_auditions` / runs — do not invent
a second take store if runs already hold output.

### Task 8: VISUAL SYSTEM

Canvas / object / chrome. Contextual drawers. Kill
indiscriminate bordered rectangles and empty panels.

### Task 9: AUK CLEANUP

Unmount remaining auk-tasks from active chrome.
`tts/auk/` parked. README historical note already exists.

### Parked

- overlap TSE
- AuK resurrection
- leftover env rename
- live-call-lease (separate bead)
- automatic cross-source speaker identity

### Frontier now

t1 shell only. t2 after t1 so they do not fight app-shell.
t5 GPU waits on lease-guard. Do not steal live Breeze.
