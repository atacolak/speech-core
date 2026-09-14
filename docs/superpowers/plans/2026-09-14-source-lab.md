# Source analysis lab Implementation Plan

**Goal:** Source bench + voice curation. Park AuK. VibeVoice
range analysis. Optional Resemble after extract. Breeze on
approved refs.

**Spec:** `docs/superpowers/specs/2026-09-14-source-lab.md`
**Brief:** `docs/superpowers/briefs/2026-09-14-source-lab.md`

Local, do not redownload: VibeVoice NF4 hub snapshot, Resemble
in Breeze venv, yt-dlp, ffmpeg. VibeVoice needs its own
transformers>=5.3 venv.

---

### Task 1: Park AuK in the lab UI + README note

**Files:** workbench.tsx, api.ts auk helpers unused, auk-tasks
unmounted, tts/README.md historical note. Keep tts/auk/ and
sqlite provenance. `/api/auk/*` may stay for parked code but
must not appear in chrome.
**Verification:** vitest: no Load AuK, no CLEAN/ISOLATE/enhance
chips, no bf16 radio on the creative surface. README names the
evaluation. No GPU.


**Landed 2026-09-14:** AuK chrome stripped from workbench. README
Parked section. MAIN 47 vitest. tts/auk/ kept.


### Task 2: Data model — sources / analyses / clips / maps

**Files:** schema.sql, db.py, models, voice JSON additive.
**Verification:** a media_source exists without a voice; two
voices can own clips from one source; analysis coverage ranges
persist; extract writes clip + clean transcript; overlap ranges
rejected. Legacy voice_sources still readable. No GPU. No UI.


**Landed 2026-09-14:** media_sources / source_analyses /
source_speakers / clips. MAIN 48 python. Live sqlite not
rewritten (tables empty, additive).

### Task 3: Source ingest (file + YouTube)

**Files:** routes/sources.py, yt-dlp wrapper, waveform/peaks.
**Verification:** POST url does not transcribe; duration+title+
audio artifact; local wav still works. Fake yt-dlp in tests.
No GPU. No VibeVoice.

**Landed 2026-09-14:** ingest.py yt-dlp + cheap peaks. MAIN 45
python. No ProcessorLease. Live hub not recycled.


### Task 4: SOURCE BENCH + VOICE BENCH shell

**Files:** lab web nav Voices|Sources; source bench waveform;
voice bench lists refs/takes. May stub analyze/extract until
t5/t6. No AuK chrome.
**Verification:** vitest nav + empty source bench + voice bench
without a giant empty compare panel.

**Landed 2026-09-14:** Voices|Sources nav, SOURCE BENCH
(waveform stub, coverage, ANALYZE chrome, extract gated),
VOICE BENCH (REFERENCES / SOURCE MATERIAL / DERIVATIVES /
TAKES). MAIN 65 vitest. Dist rebuilt. Live hub not recycled
(Breeze still resident; /api/sources 404 until recycle).


### Task 5: VibeVoice range analysis + speaker lanes

**Depends:** t2, t3, separate vibevoice venv (this task may
create the venv from the existing HF snapshot — no Hub
redownload).
**Verification:** analyze a fixture range returns source-local
speakers + segments; overlap flagged; coverage recorded;
GPU lease; 409 live_call. Fake analyzer in unit tests.

### Task 6: Extract + map + clean transcript + Resemble derivatives

**Depends:** t5 (or fake analyze). Resemble already in Breeze
venv — denoise + optional enhance() as named derivative.
**Verification:** multi-range extract concatenates or keeps
collection with original ranges; clean transcript has no
SPEAKER_ tags; denoise/enhance are sibling artifacts; compare
is possible; sequential lease.

### Parked

- overlap TSE
- AuK resurrection
- leftover env rename
- live-call-lease (separate bead)
- automatic cross-source speaker identity

### Frontier now

t1 (retire AuK UI) + t2 (schema) + t3 (ingest, no GPU) in
parallel. t4 shell after t1 so it does not fight AuK chrome.
t5/t6 wait on schema + ingest. Do not steal live Breeze GPU.
