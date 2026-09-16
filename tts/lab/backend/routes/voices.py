"""Voice CRUD. not on the voicecat path."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from tts.lab.backend.audio import suffix_of
from tts.lab.backend.models import Interval
from tts.lab.backend.runtime.processors import ProcessorLease
from tts.lab.backend.runtime.types import LiveCallActive, RuntimeBusy
from tts.lab.backend.services.candidates import (
    EXPERIMENT,
    REFERENCE,
    auk_processor_config,
    auk_provenance_from_row,
    record_voice_artifact,
    set_default_reference,
)
from tts.lab.backend.services.references import (
    exclude_intervals,
    keep_duration_s,
    keep_only_interval,
    materialize_keep_wav,
    normalize_keep_intervals,
    processed_variant_is_current,
    slice_transcript,
)
from tts.lab.backend.services.resemble import (
    PROCESSOR as RESEMBLE_PROCESSOR,
    PROCESSOR_CONFIG as RESEMBLE_CONFIG,
    ProcessorUnavailable,
    denoise_wav,
)
from tts.lab.backend.services.speakers import (
    LEASE_NAME as VIBEVOICE_LEASE,
    PROCESSOR as VIBEVOICE_PROCESSOR,
    PROCESSOR_CONFIG as VIBEVOICE_CONFIG,
    analyze_audio,
    keep_for_speaker,
    words_for_speaker,
)
from tts.lab.backend.services.sources import clips_for_voice
from tts.lab.backend.services.voices import (
    VoiceImportError,
    import_voice_from_path,
    primary_source_row,
    source_rows,
    sync_primary_source,
)
from tts.lab.backend.store.cache import AUK_PROCESSOR, analysis_cache_key, processor_cache_key
from tts.packets import new_id

router = APIRouter()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _variant_rows(store, voice_id: str) -> list[dict[str, Any]]:
    rows = store.execute(
        "SELECT * FROM reference_variants WHERE voice_id = ? ORDER BY created_at ASC",
        (voice_id,),
    ).fetchall()
    items = []
    for row in rows:
        item = {
            "id": row["id"],
            "voice_profile_id": row["voice_id"],
            "kind": row["kind"],
            "audio_artifact_id": row["audio_artifact_id"],
            "processor_config": json.loads(row["processor_config_json"] or "null"),
            "processor_cache_key": row["processor_cache_key"],
            "duration_s": row["duration_s"],
            "pinned": bool(row["pinned"]),
            **auk_provenance_from_row(row),
            "approved": bool(row["approved"]),
        }
        if item["kind"] == AUK_PROCESSOR:
            # An AuK candidate's config *is* its provenance: the columns are the
            # single source of truth, so a keep change can re-derive the key.
            item["processor_config"] = auk_processor_config(item)
        items.append(item)
    return items


def _speaker_analysis(store, row: Any) -> dict[str, Any] | None:
    if "speaker_analysis_id" not in row.keys() or not row["speaker_analysis_id"]:
        return None
    analysis_row = store.execute(
        "SELECT * FROM speaker_analyses WHERE id = ?", (row["speaker_analysis_id"],)
    ).fetchone()
    if analysis_row is None:
        return None
    try:
        result = json.loads(analysis_row["result_json"])
    except json.JSONDecodeError:
        return None
    if not isinstance(result, dict):
        return None
    return {
        "id": analysis_row["id"],
        "speakers": result.get("speakers") or [],
        "segments": result.get("segments") or [],
        "overlaps": result.get("overlaps") or [],
        # Stale, never deleted: the operator re-analyzes the new source.
        "stale": analysis_row["source_artifact_id"] != row["source_artifact_id"],
        "model_id": analysis_row["model_id"],
        "model_revision": analysis_row["model_revision"],
        "cache_key": analysis_row["cache_key"],
        "peak_vram_bytes": result.get("peak_vram_bytes"),
    }


def _sources_json(store, row: Any) -> list[dict[str, Any]]:
    """The profile's sources, primary first.

    Migrate window: the primary source's transcript/words/keep still live on the
    voice row (the transcribe and keep paths write them), so it reads those and
    its own mirrored columns for the rest. Sources past the primary are their own
    truth.
    """
    sources = source_rows(store, row["id"])
    primary = primary_source_row(store, row["id"], legacy_artifact_id=row["source_artifact_id"])
    primary_id = None if primary is None else primary["id"]
    keep = json.loads(row["keep_intervals_json"])
    items = []
    for source in sources:
        artifact = store.get(source["artifact_id"])
        item = {
            "id": source["id"],
            "label": source["label"],
            "artifact_id": source["artifact_id"],
            "transcript": source["transcript"],
            "duration_s": float(artifact.duration_s or 0.0),
            "keep_intervals": json.loads(source["keep_intervals_json"] or "[]"),
        }
        if source["id"] == primary_id:
            item["transcript"] = row["source_transcript"]
            item["keep_intervals"] = keep
        items.append(item)
    return items


def _artifacts_json(store, row: Any, sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The profile's experiments and references, oldest first."""
    rows = store.execute(
        "SELECT * FROM voice_artifacts WHERE voice_id = ? ORDER BY created_at ASC, rowid ASC",
        (row["id"],),
    ).fetchall()
    default_id = row["default_reference_id"] if "default_reference_id" in row.keys() else None
    by_source = {item["id"]: item for item in sources}
    primary = sources[0] if sources else None
    shas: dict[str, str] = {}
    items = []
    for art in rows:
        item = {
            "id": art["id"],
            "role": art["role"],
            "kind": art["kind"],
            "name": art["name"],
            "audio_artifact_id": art["audio_artifact_id"],
            "parent_id": art["parent_id"],
            "source_id": art["source_id"],
            "keep_intervals": json.loads(art["keep_intervals_json"] or "[]"),
            "processor_cache_key": art["processor_cache_key"],
            "processor_config": json.loads(art["processor_config_json"] or "null"),
            "approved": art["role"] == REFERENCE,
            "approved_at": art["approved_at"],
            "tags": json.loads(art["tags_json"] or "[]"),
            "default": art["id"] == default_id,
            "stale": False,
            "created_at": art["created_at"],
        }
        item.update(auk_provenance_from_row(art))
        if item["kind"] == AUK_PROCESSOR:
            # An AuK artifact's config *is* its provenance, parent edge included.
            item["processor_config"] = auk_processor_config(item)
        item.pop("parent_variant_id", None)
        source = by_source.get(art["source_id"]) or primary
        if source is not None:
            artifact_id = str(source["artifact_id"])
            if artifact_id not in shas:
                shas[artifact_id] = store.get(artifact_id).sha256
            item["stale"] = not processed_variant_is_current(
                item, shas[artifact_id], source["keep_intervals"]
            )
        items.append(item)
    return items


def _voice_row(store, row: Any) -> dict[str, Any]:
    keep = json.loads(row["keep_intervals_json"])
    artifact = store.get(row["source_artifact_id"])
    variants = _variant_rows(store, row["id"])
    for item in variants:
        item["stale"] = item["kind"] != "original" and not processed_variant_is_current(
            item, artifact.sha256, keep
        )
    active_id = row["active_reference_variant_id"]
    active = next((item for item in variants if item["id"] == active_id), None)
    if active is None and variants:
        active = next((item for item in variants if item["kind"] == "original"), variants[0])
    original_format = (row["original_format"] if "original_format" in row.keys() else None) or (
        artifact.suffix.lstrip(".") if artifact.suffix else "wav"
    )
    original_id = (
        row["original_artifact_id"] if "original_artifact_id" in row.keys() else None
    ) or row["source_artifact_id"]
    words: list[dict[str, Any]] = []
    if "source_words_json" in row.keys() and row["source_words_json"]:
        try:
            parsed = json.loads(row["source_words_json"])
            if isinstance(parsed, list):
                words = [item for item in parsed if isinstance(item, dict)]
        except json.JSONDecodeError:
            words = []
    locked = 0
    if "transcript_locked" in row.keys() and row["transcript_locked"] is not None:
        locked = int(row["transcript_locked"])
    source_duration = float(artifact.duration_s or 0.0)
    sources = _sources_json(store, row)
    artifacts = _artifacts_json(store, row, sources)
    default_reference_id = next((item["id"] for item in artifacts if item["default"]), None)
    return {
        "id": row["id"],
        "name": row["name"],
        "tags": json.loads(row["tags_json"]),
        "source_audio_artifact_id": row["source_artifact_id"],
        "original_artifact_id": original_id,
        "original_format": original_format,
        "source_transcript": row["source_transcript"],
        "keep_intervals": keep,
        "effective_transcript": row["effective_transcript"],
        "source_words": words,
        "transcript_locked": bool(locked),
        "active_reference_variant_id": None if active is None else active["id"],
        "default_reference_id": default_reference_id,
        "sources": sources,
        "artifacts": artifacts,
        "clips": clips_for_voice(store, row["id"]),
        "active_variant": active,
        "variants": variants,
        "speaker_analysis": _speaker_analysis(store, row),
        "duration_s": source_duration,
        "source_duration_s": source_duration,
        "effective_duration_s": keep_duration_s(keep),
        "notes": row["notes"] if "notes" in row.keys() else None,
        "generation": _voice_generation(row),
        "take_limit": _voice_take_limit(row),
        "latest_take_id": _latest_take_id(store, row),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }



def _voice_generation(row: Any) -> dict[str, Any] | None:
    if "generation_json" not in row.keys():
        return None
    raw = row["generation_json"]
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _voice_take_limit(row: Any) -> int:
    if "take_limit" not in row.keys() or row["take_limit"] is None:
        return 5
    try:
        value = int(row["take_limit"])
    except (TypeError, ValueError):
        return 5
    return max(1, min(50, value))


def _latest_take_id(store, row: Any) -> str | None:
    if "latest_take_id" not in row.keys() or not row["latest_take_id"]:
        return None
    found = store.execute(
        "SELECT id FROM runs WHERE id = ? AND voice_id = ?",
        (row["latest_take_id"], row["id"]),
    ).fetchone()
    return None if found is None else str(found["id"])


def _source_words(voice: dict[str, Any]) -> list[dict[str, Any]]:
    words = voice.get("source_words") or []
    return [item for item in words if isinstance(item, dict)]


def _effective_for_keep(voice: dict[str, Any], keep: list[dict[str, float]], *, reset: bool = False) -> str:
    if voice.get("transcript_locked"):
        return str(voice.get("effective_transcript") or "")
    words = _source_words(voice)
    if words:
        return slice_transcript(words, keep)
    if reset:
        return str(voice.get("source_transcript") or "")
    return str(voice.get("effective_transcript") or voice.get("source_transcript") or "")


def get_voice_or_404(store, voice_id: str) -> dict[str, Any]:
    row = store.execute("SELECT * FROM voices WHERE id = ?", (voice_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"voice not found: {voice_id}")
    return _voice_row(store, row)


def _ensure_original_variant(store, voice: dict[str, Any]) -> None:
    if any(item["kind"] == "original" for item in voice["variants"]):
        return
    artifact = store.get(voice["source_audio_artifact_id"])
    variant_id = new_id("rv")
    store.execute(
        """
        INSERT INTO reference_variants (
            id, voice_id, kind, audio_artifact_id, processor_config_json,
            processor_cache_key, duration_s, pinned, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            variant_id,
            voice["id"],
            "original",
            artifact.id,
            None,
            None,
            float(artifact.duration_s or 0.0),
            1,
            _now(),
        ),
    )
    store.execute(
        "UPDATE voices SET active_reference_variant_id=? WHERE id=?",
        (variant_id, voice["id"]),
    )
    store.commit()


class VoicePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = None
    tags: list[str] | None = None
    source_transcript: str | None = None
    effective_transcript: str | None = None
    keep_intervals: list[Interval] | None = None
    notes: str | None = None
    generation: dict[str, Any] | None = None
    take_limit: int | None = Field(default=None, ge=1, le=50)


class IntervalBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_s: float | None = None
    end_s: float | None = None
    intervals: list[Interval] | None = None

    def as_list(self) -> list[Interval]:
        if self.intervals:
            return list(self.intervals)
        if self.start_s is None or self.end_s is None:
            raise HTTPException(status_code=422, detail="interval start_s and end_s are required")
        return [Interval(start_s=self.start_s, end_s=self.end_s)]


class ActivateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    variant_id: str | None = None
    kind: str | None = None


class UseSpeakerBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    speaker_id: str


def _desk_voice_item(item: dict[str, Any]) -> dict[str, str]:
    transcript = str(item.get("effective_transcript") or item.get("source_transcript") or "").strip()
    return {
        "id": str(item["id"]),
        "name": str(item["name"]),
        "transcript": transcript,
    }


@router.get("/api/voices")
def list_voices(
    request: Request,
    q: str | None = None,
    for_client: str | None = Query(default=None, alias="for"),
) -> dict[str, Any]:
    store = request.app.state.lab.store
    rows = store.execute("SELECT * FROM voices ORDER BY updated_at DESC").fetchall()
    items = [_voice_row(store, row) for row in rows]
    if q:
        needle = q.lower()
        items = [
            item
            for item in items
            if needle in item["name"].lower() or needle in (item["source_transcript"] or "").lower()
        ]
    samples = [item for item in items if "sample" in (item.get("tags") or [])]
    rest = [item for item in items if "sample" not in (item.get("tags") or [])]
    ordered = samples + rest
    if for_client == "desk":
        return {"items": [_desk_voice_item(item) for item in ordered]}
    return {"items": ordered}


@router.post("/api/voices")
async def create_voice(
    request: Request,
    name: str = Form(),
    transcript: str = Form(""),
    tags: str = Form("[]"),
    audio: UploadFile = File(),
) -> dict[str, Any]:
    store = request.app.state.lab.store
    payload = await audio.read()
    if not payload:
        raise HTTPException(status_code=400, detail="audio is required")
    tmp = store.root / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    suffix = suffix_of(audio.filename or "upload.wav")
    original_path = tmp / f"upload-{new_id('up')}{suffix}"
    original_path.write_bytes(payload)
    try:
        parsed_tags = json.loads(tags) if tags else []
        if not isinstance(parsed_tags, list):
            raise ValueError("tags")
    except (json.JSONDecodeError, ValueError):
        parsed_tags = [part.strip() for part in tags.split(",") if part.strip()]
    try:
        voice_id = import_voice_from_path(
            store,
            original_path,
            name=name,
            transcript=transcript,
            tags=list(parsed_tags),
        )
        return get_voice_or_404(store, voice_id)
    except VoiceImportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        original_path.unlink(missing_ok=True)


@router.get("/api/voices/{voice_id}")
def get_voice(request: Request, voice_id: str) -> dict[str, Any]:
    return get_voice_or_404(request.app.state.lab.store, voice_id)


@router.patch("/api/voices/{voice_id}")
def patch_voice(request: Request, voice_id: str, body: VoicePatch) -> dict[str, Any]:
    store = request.app.state.lab.store
    current = get_voice_or_404(store, voice_id)
    name = body.name if body.name is not None else current["name"]
    if body.name is not None and not str(name).strip():
        raise HTTPException(status_code=422, detail="voice name is required")
    name = str(name).strip()
    tags = body.tags if body.tags is not None else current["tags"]
    source_transcript = (
        body.source_transcript if body.source_transcript is not None else current["source_transcript"]
    )
    effective = (
        body.effective_transcript
        if body.effective_transcript is not None
        else current["effective_transcript"]
    )
    notes = body.notes if body.notes is not None else current.get("notes")
    generation = body.generation if body.generation is not None else current.get("generation")
    take_limit = body.take_limit if body.take_limit is not None else int(current.get("take_limit") or 5)
    keep = current["keep_intervals"]
    locked = bool(current.get("transcript_locked"))
    if body.effective_transcript is not None:
        locked = True
    if body.keep_intervals is not None:
        artifact = store.get(current["source_audio_artifact_id"])
        duration = float(artifact.duration_s or 0.0)
        keep_models = normalize_keep_intervals(list(body.keep_intervals), duration)
        keep = [iv.model_dump() for iv in keep_models]
        if not locked:
            current = {**current, "keep_intervals": keep}
            effective = _effective_for_keep(current, keep)
    store.execute(
        """
        UPDATE voices SET name=?, tags_json=?, source_transcript=?,
            keep_intervals_json=?, effective_transcript=?, notes=?,
            generation_json=?, take_limit=?, transcript_locked=?, updated_at=?
        WHERE id=?
        """,
        (
            name,
            json.dumps(tags),
            source_transcript,
            json.dumps(keep),
            effective,
            notes,
            json.dumps(generation) if generation else None,
            int(take_limit),
            1 if locked else 0,
            _now(),
            voice_id,
        ),
    )
    sync_primary_source(store, voice_id)
    store.commit()
    if body.take_limit is not None:
        from tts.lab.backend.routes.runs import prune_unsaved_runs

        prune_unsaved_runs(store, voice_id, int(take_limit))
    return get_voice_or_404(store, voice_id)


@router.delete("/api/voices/{voice_id}")
def delete_voice(request: Request, voice_id: str) -> dict[str, str]:
    store = request.app.state.lab.store
    current = get_voice_or_404(store, voice_id)
    store.unpin(current["source_audio_artifact_id"], reason=f"voice:{voice_id}")
    store.unpin(current["original_artifact_id"], reason=f"voice:{voice_id}:original")
    for source in source_rows(store, voice_id):
        store.unpin(str(source["artifact_id"]), reason=f"voice:{voice_id}:source")
    run_rows = store.execute(
        "SELECT id, output_artifact_id FROM runs WHERE voice_id = ?",
        (voice_id,),
    ).fetchall()
    for run in run_rows:
        store.unpin(str(run["output_artifact_id"]), reason=f"run:{run['id']}")
    store.execute("DELETE FROM runs WHERE voice_id = ?", (voice_id,))
    store.execute("DELETE FROM reference_variants WHERE voice_id = ?", (voice_id,))
    store.execute("DELETE FROM speaker_analyses WHERE voice_id = ?", (voice_id,))
    store.execute(
        "DELETE FROM breeze_auditions WHERE artifact_id IN (SELECT id FROM voice_artifacts WHERE voice_id = ?)",
        (voice_id,),
    )
    store.execute("DELETE FROM voice_artifacts WHERE voice_id = ?", (voice_id,))
    store.execute("DELETE FROM voice_sources WHERE voice_id = ?", (voice_id,))
    store.execute("DELETE FROM voices WHERE id = ?", (voice_id,))
    store.commit()
    return {"id": voice_id, "status": "deleted"}


def _patch_keep(store, voice_id: str, keep: list[Interval], *, reset: bool = False) -> dict[str, Any]:
    voice = get_voice_or_404(store, voice_id)
    dumped = [iv.model_dump() for iv in keep]
    effective = _effective_for_keep(voice, dumped, reset=reset)
    store.execute(
        "UPDATE voices SET keep_intervals_json=?, effective_transcript=?, updated_at=? WHERE id=?",
        (json.dumps(dumped), effective, _now(), voice_id),
    )
    sync_primary_source(store, voice_id)
    store.commit()
    return get_voice_or_404(store, voice_id)


@router.post("/api/voices/{voice_id}/reference/keep-only")
def keep_only(request: Request, voice_id: str, body: IntervalBody) -> dict[str, Any]:
    store = request.app.state.lab.store
    voice = get_voice_or_404(store, voice_id)
    duration = float(voice["duration_s"] or 0.0)
    selected = body.as_list()
    if len(selected) != 1:
        raise HTTPException(status_code=422, detail="keep-only takes one interval")
    keep = keep_only_interval(selected[0], duration)
    return _patch_keep(store, voice_id, keep)


@router.post("/api/voices/{voice_id}/reference/exclude")
def exclude(request: Request, voice_id: str, body: IntervalBody) -> dict[str, Any]:
    store = request.app.state.lab.store
    voice = get_voice_or_404(store, voice_id)
    keep = exclude_intervals(
        [Interval.model_validate(item) for item in voice["keep_intervals"]],
        body.as_list(),
    )
    return _patch_keep(store, voice_id, keep)


@router.post("/api/voices/{voice_id}/reference/reset")
def reset_reference(request: Request, voice_id: str) -> dict[str, Any]:
    store = request.app.state.lab.store
    voice = get_voice_or_404(store, voice_id)
    duration = float(voice["duration_s"] or 0.0)
    keep = normalize_keep_intervals(
        [Interval(start_s=0.0, end_s=max(duration, 1e-3))],
        max(duration, 1e-3),
    )
    return _patch_keep(store, voice_id, keep, reset=True)


@router.post("/api/voices/{voice_id}/reference/activate")
def activate_variant(request: Request, voice_id: str, body: ActivateBody) -> dict[str, Any]:
    store = request.app.state.lab.store
    voice = get_voice_or_404(store, voice_id)
    target = None
    if body.variant_id:
        target = next((item for item in voice["variants"] if item["id"] == body.variant_id), None)
    elif body.kind:
        target = next((item for item in voice["variants"] if item["kind"] == body.kind), None)
    if target is not None:
        store.execute(
            "UPDATE voices SET active_reference_variant_id=?, updated_at=? WHERE id=?",
            (target["id"], _now(), voice_id),
        )
        set_default_reference(store, voice_id, target["id"])
        store.commit()
        return get_voice_or_404(store, voice_id)
    # An enrolled reference artifact, not a legacy variant. A GENERATION, an
    # experiment or a foreign id is never eligible, and stored ids stay put.
    artifact = None
    if body.variant_id:
        artifact = next(
            (item for item in voice["artifacts"] if item["id"] == body.variant_id), None
        )
    if artifact is None or artifact.get("role") != REFERENCE:
        raise HTTPException(status_code=404, detail="reference not found")
    set_default_reference(store, voice_id, str(artifact["id"]))
    store.commit()
    return get_voice_or_404(store, voice_id)


@router.post("/api/voices/{voice_id}/speakers/analyze")
def analyze_speakers(request: Request, voice_id: str) -> dict[str, Any]:
    """Offline vibevoice diarization of the SOURCE artifact. Never the keep crop."""
    state = request.app.state.lab
    store = state.store
    voice = get_voice_or_404(store, voice_id)
    artifact = store.get(voice["source_audio_artifact_id"])
    cache_key = analysis_cache_key(artifact.sha256, VIBEVOICE_PROCESSOR, VIBEVOICE_CONFIG)
    cached = store.execute(
        """
        SELECT id FROM speaker_analyses
        WHERE voice_id = ? AND source_artifact_id = ? AND cache_key = ?
        """,
        (voice_id, artifact.id, cache_key),
    ).fetchone()
    if cached is None:
        try:
            with ProcessorLease(state.runtime).acquire(VIBEVOICE_LEASE):
                analysis = analyze_audio(artifact.path)
        except LiveCallActive as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "processor_blocked_live_call",
                    "message": "a desk live call owns the engine",
                    "remaining_s": exc.remaining_s,
                },
            ) from exc
        except RuntimeBusy as exc:
            raise HTTPException(
                status_code=503,
                detail={"code": "runtime_busy", "state": exc.state, "message": str(exc)},
            ) from exc
        except ProcessorUnavailable as exc:
            raise HTTPException(
                status_code=501,
                detail={
                    "code": "processor_unavailable",
                    "processor": VIBEVOICE_PROCESSOR,
                    "message": str(exc),
                },
            ) from exc
        except Exception as exc:  # fail closed: never invent a speaker split
            raise HTTPException(
                status_code=500,
                detail={
                    "code": "processor_failed",
                    "processor": VIBEVOICE_PROCESSOR,
                    "message": str(exc),
                },
            ) from exc
        analysis_id = new_id("sa")
        store.execute(
            """
            INSERT INTO speaker_analyses (
                id, voice_id, source_artifact_id, processor, model_id, model_revision,
                config_json, cache_key, result_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                analysis_id,
                voice_id,
                artifact.id,
                VIBEVOICE_PROCESSOR,
                str(VIBEVOICE_CONFIG.get("model_id") or ""),
                str(VIBEVOICE_CONFIG.get("model_revision") or ""),
                json.dumps(VIBEVOICE_CONFIG),
                cache_key,
                json.dumps(analysis),
                _now(),
            ),
        )
    else:
        analysis_id = cached["id"]
    store.execute(
        "UPDATE voices SET speaker_analysis_id=?, updated_at=? WHERE id=?",
        (analysis_id, _now(), voice_id),
    )
    store.commit()
    return get_voice_or_404(store, voice_id)


@router.post("/api/voices/{voice_id}/speakers/use")
def use_speaker(request: Request, voice_id: str, body: UseSpeakerBody) -> dict[str, Any]:
    """Keep only one speaker's non-overlap slices. No GPU: reads the stored analysis."""
    store = request.app.state.lab.store
    voice = get_voice_or_404(store, voice_id)
    analysis = voice["speaker_analysis"]
    if analysis is None:
        raise HTTPException(status_code=422, detail="no speaker analysis for this voice")
    if analysis["stale"]:
        raise HTTPException(status_code=422, detail="speaker analysis is stale; re-analyze")
    try:
        keep = keep_for_speaker(analysis, body.speaker_id, float(voice["duration_s"] or 0.0))
    except KeyError as exc:
        raise HTTPException(status_code=422, detail=f"unknown speaker: {body.speaker_id}") from exc
    if not keep:
        raise HTTPException(
            status_code=422, detail="that speaker has no non-overlapping audio"
        )
    if not voice["transcript_locked"]:
        words = words_for_speaker(analysis, body.speaker_id)
        store.execute(
            "UPDATE voices SET source_words_json=? WHERE id=?",
            (json.dumps(words) if words else None, voice_id),
        )
        store.commit()
    return _patch_keep(store, voice_id, keep)


@router.post("/api/voices/{voice_id}/reference/denoise")
def denoise_reference(request: Request, voice_id: str) -> dict[str, Any]:
    """Resemble denoise-only cleanup of the current keep. Never `enhance()`."""
    state = request.app.state.lab
    store = state.store
    voice = get_voice_or_404(store, voice_id)
    artifact = store.get(voice["source_audio_artifact_id"])
    keep = [Interval.model_validate(item) for item in voice["keep_intervals"]]
    tmp = store.root / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    src = tmp / f"{voice_id}-denoise-src.wav"
    dest = tmp / f"{voice_id}-resemble.wav"
    materialize_keep_wav(artifact.path, keep, src)
    try:
        with ProcessorLease(state.runtime).acquire(RESEMBLE_PROCESSOR):
            denoise_wav(src, dest)
    except LiveCallActive as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "processor_blocked_live_call",
                "message": "a desk live call owns the engine",
                "remaining_s": exc.remaining_s,
            },
        ) from exc
    except RuntimeBusy as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "runtime_busy", "state": exc.state, "message": str(exc)},
        ) from exc
    except ProcessorUnavailable as exc:
        raise HTTPException(
            status_code=501,
            detail={
                "code": "processor_unavailable",
                "processor": RESEMBLE_PROCESSOR,
                "message": str(exc),
            },
        ) from exc
    except Exception as exc:  # fail closed: never silently hand back the keep wav
        raise HTTPException(
            status_code=500,
            detail={
                "code": "processor_failed",
                "processor": RESEMBLE_PROCESSOR,
                "message": str(exc),
            },
        ) from exc
    processed = store.import_audio(dest)
    store.pin(processed.id, reason=f"voice:{voice_id}:{RESEMBLE_PROCESSOR}")
    cache_key = processor_cache_key(RESEMBLE_PROCESSOR, artifact.sha256, keep, RESEMBLE_CONFIG)
    existing = next(
        (item for item in voice["variants"] if item["kind"] == RESEMBLE_PROCESSOR),
        None,
    )
    duration = float(processed.duration_s or 0.0)
    if existing is None:
        variant_id = new_id("rv")
        store.execute(
            """
            INSERT INTO reference_variants (
                id, voice_id, kind, audio_artifact_id, processor_config_json,
                processor_cache_key, duration_s, pinned, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                variant_id,
                voice_id,
                RESEMBLE_PROCESSOR,
                processed.id,
                json.dumps(RESEMBLE_CONFIG),
                cache_key,
                duration,
                0,
                _now(),
            ),
        )
    else:
        variant_id = str(existing["id"])
        store.execute(
            """
            UPDATE reference_variants
            SET audio_artifact_id=?, processor_config_json=?, processor_cache_key=?, duration_s=?
            WHERE id=?
            """,
            (processed.id, json.dumps(RESEMBLE_CONFIG), cache_key, duration, existing["id"]),
        )
    source = primary_source_row(store, voice_id, legacy_artifact_id=voice["source_audio_artifact_id"])
    record_voice_artifact(
        store,
        voice_id,
        artifact_id=variant_id,
        role=EXPERIMENT,
        kind=RESEMBLE_PROCESSOR,
        name="Resemble",
        audio_artifact_id=processed.id,
        source_id=None if source is None else str(source["id"]),
        keep_intervals=keep,
        processor_config=RESEMBLE_CONFIG,
        processor_cache_key=cache_key,
    )
    store.commit()
    return get_voice_or_404(store, voice_id)


@router.get("/api/voices/{voice_id}/reference/audio")
def effective_reference_audio(request: Request, voice_id: str) -> FileResponse:
    store = request.app.state.lab.store
    voice = get_voice_or_404(store, voice_id)
    artifact = store.get(voice["source_audio_artifact_id"])
    dest = store.root / "tmp" / f"{voice_id}-effective.wav"
    dest.parent.mkdir(parents=True, exist_ok=True)
    keep = [Interval.model_validate(item) for item in voice["keep_intervals"]]
    materialize_keep_wav(artifact.path, keep, dest)
    return FileResponse(dest, media_type="audio/wav", filename=f"{voice_id}-reference.wav")
