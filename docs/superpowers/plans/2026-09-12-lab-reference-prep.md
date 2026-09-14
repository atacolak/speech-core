# Lab reference preparation Implementation Plan

**Technical Spec:** `docs/superpowers/specs/2026-09-12-lab-reference-prep.md`
**Design Brief:** `docs/superpowers/briefs/2026-09-12-lab-reference-prep.md`

> **For the project lead:** first `br where` in this repo. campaign parent already minted as the lab-ref-prep epic. mint only the current frontier (`t1`, `t2` first). isolated `builder` workers. Do not implement inline. Title `tN: <ask>`. Body `Asked:` then `## Landed`. Preserve uncommitted lab work; do not revert leftover/desk/E2 files unless a task names them.

**Goal:** Replace unfinished stream.fm with resemble denoise-only and add optional offline vibevoice speaker analysis into the existing reference editor, without a second object model or a second GPU occupant beside TTS.

**Architecture:** Keep `keep_intervals` + `reference_variants`. Add `speaker_analyses`. Denoise writes `kind=resemble`. Analyze writes structured segments; **use speaker** patches keep to non-overlap intervals. Processors are subprocesses behind a GPU lease that can unload E2 without restoring leftover.

**Tech stack:** FastAPI lab, sqlite artifact store, React reference-editor, FakeWorkerHandle tests, subprocess processors. Live E2 (~9.5 GiB resident) must be unloaded before vibevoice.

---

## File map

Create:
- `tts/lab/backend/runtime/processors.py` — GPU lease + subprocess runners
- `tts/lab/backend/services/resemble.py` — denoise-only on keep wav
- `tts/lab/backend/services/speakers.py` — vibevoice parse, overlap, use-speaker keep
- `tts/lab/backend/tests/test_processors_lease.py`
- `tts/lab/backend/tests/test_resemble_variant.py`
- `tts/lab/backend/tests/test_speaker_analysis.py`
- `tts/lab/fixtures/voices/two-speaker-stub.json` — canned analysis for tests (no GPU)

Modify:
- `tts/lab/backend/store/schema.sql` — `speaker_analyses`, `voices.speaker_analysis_id`
- `tts/lab/backend/store/db.py` — migrate those columns; `cache/resemble` dir
- `tts/lab/backend/store/cache.py` — drop streamfm-hardcode
- `tts/lab/backend/models.py` — `kind` includes `resemble`; speaker types
- `tts/lab/backend/runtime/manager.py` — `unload(..., restore_leftover=True)`
- `tts/lab/backend/runtime/types.py` — `processor` occupant on status
- `tts/lab/backend/routes/plan.py` — delete streamfm routes; transcribe 409 if locked
- `tts/lab/backend/routes/voices.py` — denoise / analyze / use speaker; expose analysis
- `tts/lab/backend/services/talker.py` — clone from resemble artifact
- `tts/lab/backend/routes/synthesis.py` — same
- `tts/lab/backend/app.py` — only if a new router file is added; prefer voices.py
- `tts/lab/web/src/lib/api.ts` — replace streamfm helpers
- `tts/lab/web/src/features/voices/reference-editor.tsx`
- `tts/lab/web/src/features/voices/voices-pane.tsx`
- `tts/lab/web/src/features/inspector/inspector-pane.tsx`
- `tts/lab/backend/tests/test_streamfm_cache.py` → rewrite as `test_processor_cache.py`
- `tts/lab/backend/tests/test_artifacts.py` — processor name
- `tts/lab/backend/tests/test_voices_api.py` — no streamfm; denoise/analyze fakes

Do not: leftover hop, voicecat, mossformer, TSE, streaming vibevoice, `enhance()`.

Python for tests: `$HOME/.local/share/speech-out/breeze-tts-2-e2/venv/bin/python -m unittest …`

---

### Task 1: Schema, cache, kill stream.fm names

**Owner:** builder
**Files:**
- Modify: `tts/lab/backend/store/schema.sql`
- Modify: `tts/lab/backend/store/db.py`
- Modify: `tts/lab/backend/store/cache.py`
- Modify: `tts/lab/backend/models.py`
- Modify: `tts/lab/backend/tests/test_streamfm_cache.py` (rename to `test_processor_cache.py`)
- Modify: `tts/lab/backend/tests/test_artifacts.py`
- Test: `tts/lab/backend/tests/test_processor_cache.py`

**Verification:** unittest cache key + schema migrate on a temp dir. No `streamfm` as the canonical processor name in `cache.py`.

- [ ] **Step 1: Failing tests**

```python
# test_processor_cache.py
from tts.lab.backend.models import Interval
from tts.lab.backend.store.cache import processor_cache_key, canonical_processor_config

def test_resemble_key_includes_keep_and_processor():
    keep_a = [Interval(start_s=0.0, end_s=4.0)]
    keep_b = [Interval(start_s=0.0, end_s=3.0)]
    cfg = {"checkpoint": "resemble-denoise", "preprocess_version": "v1"}
    a = processor_cache_key("resemble", "deadbeef", keep_a, cfg)
    b = processor_cache_key("resemble", "deadbeef", keep_b, cfg)
    c = processor_cache_key("resemble", "deadbeef", list(reversed(keep_a)), cfg)
    assert a != b
    assert a == c
    canon = canonical_processor_config("resemble", cfg)
    assert canon["processor"] == "resemble"

def test_analysis_key_ignores_keep():
    from tts.lab.backend.store.cache import analysis_cache_key
    a = analysis_cache_key("deadbeef", "vibevoice-asr", {"model_id": "Dubedo/VibeVoice-ASR-HF-NF4"})
    b = analysis_cache_key("deadbeef", "vibevoice-asr", {"model_id": "Dubedo/VibeVoice-ASR-HF-NF4"})
    assert a == b
```

Also assert `connect(tmp)` creates `speaker_analyses` and `voices.speaker_analysis_id`.

- [ ] **Step 2: Run; expect FAIL** (`processor_cache_key` missing)

Run: `$HOME/.local/share/speech-out/breeze-tts-2-e2/venv/bin/python -m unittest tts.lab.backend.tests.test_processor_cache`

- [ ] **Step 3: Implement**

`schema.sql`: add

```sql
ALTER is not in schema.sql — put CREATE TABLE speaker_analyses (…);
-- voices.speaker_analysis_id TEXT  in CREATE TABLE voices
```

For existing DBs, `_migrate` in `db.py` adds the column + `CREATE TABLE IF NOT EXISTS`.

`cache.py`:

```python
def canonical_processor_config(processor: str, config: dict | None = None) -> dict:
    payload = dict(config or {})
    return {
        "processor": processor,
        "checkpoint": str(payload.get("checkpoint") or ""),
        "config": payload.get("config") or {},
        "preprocess_version": str(payload.get("preprocess_version") or ""),
        "model_id": str(payload.get("model_id") or ""),
        "model_revision": str(payload.get("model_revision") or ""),
    }

def processor_cache_key(processor, source_sha256, keep_intervals, processor_config=None) -> str:
    return sha256_json({
        "source_sha256": source_sha256,
        "keep_intervals": canonical_keep_intervals(keep_intervals),
        **canonical_processor_config(processor, processor_config),
    })

def analysis_cache_key(source_sha256, processor, processor_config=None) -> str:
    return sha256_json({
        "source_sha256": source_sha256,
        **canonical_processor_config(processor, processor_config),
    })
```

Delete `streamfm_cache_key` / `PROCESSOR = "streamfm"`. Update callers. `ReferenceVariant.kind: Literal["original", "resemble", "other"]`.

`db.py`: mkdir `cache/resemble` instead of (or in addition to) `cache/streamfm`.

- [ ] **Step 4: Run tests; expect PASS**
- [ ] **Step 5: Commit** `feat(lab): generalize processor cache; add speaker_analyses`

---

### Task 2: GPU lease, unload without leftover restore

**Owner:** builder
**Files:**
- Modify: `tts/lab/backend/runtime/manager.py`
- Modify: `tts/lab/backend/runtime/types.py`
- Create: `tts/lab/backend/runtime/processors.py`
- Modify: `tts/lab/backend/runtime/routes` if status JSON is built in `routes/runtime.py`
- Test: `tts/lab/backend/tests/test_processors_lease.py`

**Verification:** Fake E2 worker + NoopLeftover. live_call → 409. ready → unload, leftover.restore_calls unchanged, occupant set then cleared.

- [ ] **Step 1: Failing test**

```python
def test_processor_lease_unloads_e2_without_restoring_leftover():
    leftover = NoopLeftover()
    manager, factory, leftover = _manager(leftover=leftover)
    manager.load()
    leftover.park()  # already parked by load
    restores = leftover.restore_calls
    from tts.lab.backend.runtime.processors import ProcessorLease
    lease = ProcessorLease(manager)
    with lease.acquire("resemble"):
        assert manager.status().state == "unloaded"
        assert leftover.restore_calls == restores
        assert manager.status().processor == "resemble"
    assert manager.status().processor is None
    assert leftover.restore_calls == restores

def test_processor_lease_blocked_by_live_call():
    manager, _, _ = _manager()
    manager.load()
    manager.refresh_live_call_lease()
    lease = ProcessorLease(manager)
    with self.assertRaises(LiveCallActive):
        with lease.acquire("vibevoice"):
            pass
    assert manager.status().state == "ready"
```

- [ ] **Step 2: Run; expect FAIL**
- [ ] **Step 3: Implement**

`E2RuntimeManager.unload(self, *, timeout=None, restore_leftover: bool = True)`
`begin_unload(..., restore_leftover=True)` thread honors the flag.

`RuntimeStatus.processor: str | None = None` — set by lease, not by E2.

`ProcessorLease.acquire(name)`:
1. if live_call_active: raise LiveCallActive
2. if state in loading/unloading: raise RuntimeBusy
3. if ready: `unload(restore_leftover=False)`
4. set occupant
5. yield
6. clear occupant (even on error)

HTTP mapping later in t3/t4: LiveCallActive → 409 `processor_blocked_live_call`.

- [ ] **Step 4: PASS**
- [ ] **Step 5: Commit** `feat(lab): processor GPU lease unloads E2 without leftover restore`

---

### Task 3: Resemble denoise variant

**Depends:** t1, t2
**Owner:** builder
**Files:**
- Create: `tts/lab/backend/services/resemble.py`
- Modify: `tts/lab/backend/routes/voices.py` (or `plan.py` replacement)
- Modify: `tts/lab/backend/routes/plan.py` — **delete** streamfm routes
- Modify: `tts/lab/backend/services/talker.py`
- Modify: `tts/lab/backend/routes/synthesis.py`
- Test: `tts/lab/backend/tests/test_resemble_variant.py`

**Verification:** Fake denoise writes a different wav. Original artifact id unchanged. New `kind=resemble`. Activate original vs resemble. Keep-interval change makes cache key miss / stale.

- [ ] **Step 1: Failing HTTP tests with Fake denoise + Fake lease**

```python
def test_denoise_creates_resemble_variant_and_keeps_original(self):
    voice = self._import_voice()
    original_id = voice["original_artifact_id"]
    resp = self.client.post(f"/api/voices/{voice['id']}/reference/denoise")
    self.assertEqual(resp.status_code, 200)
    body = resp.json()
    kinds = {v["kind"] for v in body["variants"]}
    self.assertIn("original", kinds)
    self.assertIn("resemble", kinds)
    self.assertEqual(body["original_artifact_id"], original_id)
    self.assertNotEqual(
        next(v for v in body["variants"] if v["kind"] == "resemble")["audio_artifact_id"],
        original_id,
    )

def test_keep_change_stales_resemble(self):
    # after denoise, exclude an interval; GET voice marks resemble stale
    # clone path (talker/synthesis) uses original until denoise rerun
```

Inject `state.resemble = lambda wav, sr: wav` in tests via create_app dependency if needed. Prefer a module-level hook `resemble_fn` defaulting to real `denoise`.

- [ ] **Step 2: FAIL**
- [ ] **Step 3: Implement**

`resemble.py`:

```python
def denoise_wav(src: Path, dest: Path, *, denoise_fn=None) -> Path:
    # load wav, call resemble_enhance.enhancer.inference.denoise
    # NEVER enhance()
```

Route: materialize keep wav → denoise → import_audio → pin `voice:{id}:resemble` → upsert variant kind=resemble with cache key from t1 → return voice.

Talker/synthesis: `if variant and variant["kind"] != "original": use variant artifact else materialize keep`. Treat unknown/stale resemble as original (compare cache key to current keep).

Delete `/api/voices/{id}/streamfm` and `/api/streamfm/status`.

- [ ] **Step 4: PASS** (`test_resemble_variant`, existing voices tests)
- [ ] **Step 5: Commit** `feat(lab): resemble denoise-only reference variant`

---

### Task 4: Vibevoice analysis + use speaker

**Depends:** t1, t2
**Owner:** builder
**Files:**
- Create: `tts/lab/backend/services/speakers.py`
- Modify: `tts/lab/backend/routes/voices.py`
- Modify: `tts/lab/backend/routes/plan.py` — transcribe 409 if locked
- Test: `tts/lab/backend/tests/test_speaker_analysis.py`

**Verification:** Fake analyzer returns two speakers + overlap. Analyze stores JSON. Use S2 sets keep to S2 non-overlap only. Locked transcript unchanged. Unlocked effective transcript is S2 text.

- [ ] **Step 1: Failing tests**

```python
FAKE = {
  "speakers": [
    {"id": "S1", "label": "Speaker 1", "duration_s": 4.0},
    {"id": "S2", "label": "Speaker 2", "duration_s": 3.0},
  ],
  "segments": [
    {"speaker_id": "S1", "start_s": 0.0, "end_s": 2.0, "text": "hello from one", "overlap": False},
    {"speaker_id": "S1", "start_s": 2.0, "end_s": 3.0, "text": "together", "overlap": True},
    {"speaker_id": "S2", "start_s": 2.0, "end_s": 3.0, "text": "together", "overlap": True},
    {"speaker_id": "S2", "start_s": 3.0, "end_s": 6.0, "text": "hello from two", "overlap": False},
  ],
  "overlaps": [{"start_s": 2.0, "end_s": 3.0, "speakers": ["S1", "S2"]}],
}

def test_use_speaker_drops_overlap():
    ...
    keep = body["keep_intervals"]
    assert keep == [{"start_s": 3.0, "end_s": 6.0}]
    assert "hello from two" in body["effective_transcript"]
    assert "hello from one" not in body["effective_transcript"]

def test_use_speaker_does_not_clobber_locked_transcript():
    self.client.patch(..., json={"effective_transcript": "LOCKED"})
    self.client.post(.../speakers/use, json={"speaker_id": "S2"})
    assert body["effective_transcript"] == "LOCKED"
    assert body["keep_intervals"] == [{"start_s": 3.0, "end_s": 6.0}]
```

Overlap helper:

```python
def mark_overlaps(segments: list[dict]) -> tuple[list[dict], list[dict]]:
    # any pair with intersecting (start,end) and different speaker_id
```

Parse vibevoice `processor.decode(..., return_format="parsed")` into segments. If the parsed schema differs, map conservatively; never invent isolated audio.

- [ ] **Step 2: FAIL**
- [ ] **Step 3: Implement routes + speakers.py**

`POST /speakers/analyze` uses ProcessorLease("vibevoice") + fake/real runner.
`POST /speakers/use` does **not** need GPU. Calls `_patch_keep` after filtering.

Real runner (same task): subprocess loads `Dubedo/VibeVoice-ASR-HF-NF4`, generate, parse, exit. Record `peak_vram_bytes` from nvidia-smi in the worker reply (provenance only).

Transcribe: if `transcript_locked`, 409 `{code: transcript_locked}`.

- [ ] **Step 4: PASS**
- [ ] **Step 5: Commit** `feat(lab): offline vibevoice speaker analysis and use-speaker keep`

---

### Task 5: Reference editor UI

**Depends:** t3, t4
**Owner:** builder
**Files:**
- Modify: `tts/lab/web/src/lib/api.ts`
- Modify: `tts/lab/web/src/features/voices/reference-editor.tsx`
- Modify: `tts/lab/web/src/features/voices/voices-pane.tsx`
- Modify: `tts/lab/web/src/features/inspector/inspector-pane.tsx`
- Modify: `tts/lab/web/src/app.test.tsx` if it asserts stream.fm

**Verification:** `rg streamfm tts/lab/web` empty (except maybe a comment in git history). Vitest/app test: editor shows Denoise not stream.fm. Speaker chips absent without analysis.

Operator copy:
- Analyze speakers
- Speaker 1 · 18.4s
- Use speaker
- Denoise / Original / Denoised
- no nf4, no HuggingFace ids

Highlight selected speaker regions on the existing wavesurfer (second region color, not keep/exclude). Play speaker uses region playback. Do not write a concatenated file.

- [ ] **Step 1:** replace `streamfmVoice` with `denoiseVoice`, add `analyzeSpeakers`, `useSpeaker`
- [ ] **Step 2:** UI wiring as spec
- [ ] **Step 3:** `pnpm --dir tts/lab/web exec vitest run` if the existing suite is runnable; if ESM/jsdom is still broken, add a focused test that at least typechecks via `pnpm --dir tts/lab/web exec tsc --noEmit` and a Python API contract test covering JSON shape. Do not treat the pre-existing vitest ESM failure as this task's block.
- [ ] **Step 4: Commit** `feat(lab): reference editor speakers + resemble denoise`

---

### Task 6: 4070 occupancy evidence

**Depends:** t3, t4
**Owner:** builder
**Files:**
- Create: `docs/qualification/lab-reference-prep-vram.md`
- Optional fixture under `tts/lab/fixtures/voices/` if a two-speaker wav is added (do not commit huge blobs)

**Verification:** On this machine, with E2 **unloaded**, run analyze on a short two-speaker wav (or a recorded meeting clip). Capture `nvidia-smi memory.used` peak. Write the number. Confirm leftover hop was not the occupant (E2 unloaded, `ata-speech-tts` still inactive). Resemble denoise on a noisy clip produces a variant. Do not leave vibevoice resident.

If NF4 cannot load: stop, comment the bead with the OOM/import error. Do not silently 8-bit the whole model.

---

## Parked (beads only, no code)

1. resemble vs `mossformer2_se_48k` if denoise damages identity or misses hard ambience
2. overlap recovery: `penta2himajin/tse-conv-tasnet-48k` then `mossformer2_ss_16k`
3. live/streaming vibevoice checkpoint — not lab preprocessing
