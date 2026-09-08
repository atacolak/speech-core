"""Voice CRUD. not on the voicecat path."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, ConfigDict, Field

from tts.lab.backend.models import Interval
from tts.lab.backend.services.references import normalize_keep_intervals
from tts.packets import new_id
from tts.wav import duration_s, is_riff_wav, read_wav

router = APIRouter()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _voice_row(row: Any) -> dict[str, Any]:
    keep = json.loads(row["keep_intervals_json"])
    return {
        "id": row["id"],
        "name": row["name"],
        "tags": json.loads(row["tags_json"]),
        "source_audio_artifact_id": row["source_artifact_id"],
        "source_transcript": row["source_transcript"],
        "keep_intervals": keep,
        "effective_transcript": row["effective_transcript"],
        "active_reference_variant_id": row["active_reference_variant_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def get_voice_or_404(store, voice_id: str) -> dict[str, Any]:
    row = store.execute("SELECT * FROM voices WHERE id = ?", (voice_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"voice not found: {voice_id}")
    return _voice_row(row)


class VoicePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = None
    tags: list[str] | None = None
    source_transcript: str | None = None
    effective_transcript: str | None = None
    keep_intervals: list[Interval] | None = None


@router.get("/api/voices")
def list_voices(request: Request, q: str | None = None) -> dict[str, Any]:
    store = request.app.state.lab.store
    rows = store.execute("SELECT * FROM voices ORDER BY updated_at DESC").fetchall()
    items = [_voice_row(row) for row in rows]
    if q:
        needle = q.lower()
        items = [item for item in items if needle in item["name"].lower()]
    return {"items": items}


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
    dest = tmp / f"upload-{new_id('up')}.wav"
    dest.write_bytes(payload)
    if not is_riff_wav(dest):
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="audio must be RIFF/WAVE")
    artifact = store.import_audio(dest)
    dest.unlink(missing_ok=True)
    sr, samples = read_wav(artifact.path)
    duration = duration_s(sr, samples)
    keep = normalize_keep_intervals(
        [Interval(start_s=0.0, end_s=max(duration, 1e-3))], max(duration, 1e-3)
    )
    voice_id = new_id("vp")
    store.pin(artifact.id, reason=f"voice:{voice_id}")
    created = _now()
    try:
        parsed_tags = json.loads(tags) if tags else []
        if not isinstance(parsed_tags, list):
            raise ValueError("tags")
    except (json.JSONDecodeError, ValueError):
        parsed_tags = [part.strip() for part in tags.split(",") if part.strip()]
    store.execute(
        """
        INSERT INTO voices (
            id, name, tags_json, source_artifact_id, source_transcript,
            keep_intervals_json, effective_transcript, active_reference_variant_id,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            voice_id,
            name,
            json.dumps(list(parsed_tags)),
            artifact.id,
            transcript,
            json.dumps([iv.model_dump() for iv in keep]),
            transcript,
            None,
            created,
            created,
        ),
    )
    store.commit()
    return get_voice_or_404(store, voice_id)


@router.get("/api/voices/{voice_id}")
def get_voice(request: Request, voice_id: str) -> dict[str, Any]:
    return get_voice_or_404(request.app.state.lab.store, voice_id)


@router.patch("/api/voices/{voice_id}")
def patch_voice(request: Request, voice_id: str, body: VoicePatch) -> dict[str, Any]:
    store = request.app.state.lab.store
    current = get_voice_or_404(store, voice_id)
    name = body.name if body.name is not None else current["name"]
    tags = body.tags if body.tags is not None else current["tags"]
    source_transcript = (
        body.source_transcript if body.source_transcript is not None else current["source_transcript"]
    )
    effective = (
        body.effective_transcript
        if body.effective_transcript is not None
        else current["effective_transcript"]
    )
    keep = current["keep_intervals"]
    if body.keep_intervals is not None:
        artifact = store.get(current["source_audio_artifact_id"])
        duration = float(artifact.duration_s or 0.0)
        keep_models = normalize_keep_intervals(list(body.keep_intervals), duration)
        keep = [iv.model_dump() for iv in keep_models]
    store.execute(
        """
        UPDATE voices SET name=?, tags_json=?, source_transcript=?,
            keep_intervals_json=?, effective_transcript=?, updated_at=?
        WHERE id=?
        """,
        (name, json.dumps(tags), source_transcript, json.dumps(keep), effective, _now(), voice_id),
    )
    store.commit()
    return get_voice_or_404(store, voice_id)


@router.delete("/api/voices/{voice_id}")
def delete_voice(request: Request, voice_id: str) -> dict[str, str]:
    store = request.app.state.lab.store
    current = get_voice_or_404(store, voice_id)
    store.unpin(current["source_audio_artifact_id"], reason=f"voice:{voice_id}")
    store.execute("DELETE FROM voices WHERE id = ?", (voice_id,))
    store.commit()
    return {"id": voice_id, "status": "deleted"}
