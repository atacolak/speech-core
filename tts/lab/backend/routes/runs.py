"""Run history. not on the voicecat path."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict

from tts.lab.backend.services.candidates import REFERENCE, record_voice_artifact

router = APIRouter()

SAVED_RATING = "keep"
DEFAULT_TAKE_LIMIT = 5
MAX_TAKE_LIMIT = 50


def _alignment(row: Any) -> dict[str, Any] | None:
    raw = row["alignment_json"]
    return json.loads(raw) if raw else None


def _resume_pending(state: Any, rows: list[Any]) -> None:
    """A pending row read after a restart gets its alignment thread back."""
    for row in rows:
        alignment = _alignment(row)
        if alignment is not None and alignment.get("status") == "pending":
            state.run_alignments.schedule(
                state.store, str(row["id"]), str(row["output_artifact_id"])
            )


def _run_row(row: Any) -> dict[str, Any]:
    snapshot = json.loads(row["request_json"])
    raw_name = snapshot.get("name")
    name = raw_name if isinstance(raw_name, str) and raw_name else None
    return {
        "id": row["id"],
        "voice_id": row["voice_id"],
        "name": name,
        "request_snapshot": snapshot,
        "output_artifact_id": row["output_artifact_id"],
        "effective_reference_snapshot": json.loads(row["effective_reference_json"]),
        "latency_ms": row["latency_ms"],
        "first_audio_ms": row["first_audio_ms"],
        "duration_s": row["duration_s"],
        "rating": row["rating"],
        "tags": json.loads(row["tags_json"]),
        "created_at": row["created_at"],
        "alignment": _alignment(row),
    }


def voice_take_limit(store: Any, voice_id: str | None) -> int:
    if not voice_id:
        return DEFAULT_TAKE_LIMIT
    row = store.execute("SELECT take_limit FROM voices WHERE id = ?", (voice_id,)).fetchone()
    if row is None:
        return DEFAULT_TAKE_LIMIT
    keys = row.keys()
    raw = row["take_limit"] if "take_limit" in keys else None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_TAKE_LIMIT
    return max(1, min(MAX_TAKE_LIMIT, value))


def prune_unsaved_runs(store: Any, voice_id: str | None, take_limit: int | None = None) -> int:
    """Drop oldest unsaved takes so unsaved count stays at take_limit. Saved takes stay."""
    if not voice_id:
        return 0
    limit = DEFAULT_TAKE_LIMIT if take_limit is None else int(take_limit)
    limit = max(1, min(MAX_TAKE_LIMIT, limit))
    rows = store.execute(
        """
        SELECT id, output_artifact_id FROM runs
        WHERE voice_id = ? AND (rating IS NULL OR rating != ?)
        ORDER BY created_at DESC, rowid DESC
        """,
        (voice_id, SAVED_RATING),
    ).fetchall()
    extras = list(rows)[limit:]
    for row in extras:
        store.unpin(str(row["output_artifact_id"]), reason=f"run:{row['id']}")
        store.execute("DELETE FROM runs WHERE id = ?", (row["id"],))
    if extras:
        store.commit()
    return len(extras)


@router.get("/api/runs")
def list_runs(request: Request, voice_id: str | None = None) -> dict[str, Any]:
    state = request.app.state.lab
    store = state.store
    if voice_id:
        rows = store.execute(
            "SELECT * FROM runs WHERE voice_id = ? ORDER BY created_at DESC, rowid DESC",
            (voice_id,),
        ).fetchall()
    else:
        rows = store.execute("SELECT * FROM runs ORDER BY created_at DESC, rowid DESC").fetchall()
    _resume_pending(state, rows)
    return {"items": [_run_row(row) for row in rows]}


@router.get("/api/runs/{run_id}")
def get_run(request: Request, run_id: str) -> dict[str, Any]:
    state = request.app.state.lab
    store = state.store
    row = store.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
    _resume_pending(state, [row])
    return _run_row(row)


class RunPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rating: str | None = None
    tags: list[str] | None = None
    name: str | None = None


@router.patch("/api/runs/{run_id}")
def patch_run(request: Request, run_id: str, body: RunPatch) -> dict[str, Any]:
    store = request.app.state.lab.store
    row = store.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
    current = _run_row(row)
    if body.rating is None:
        rating = current["rating"]
    else:
        rating = body.rating.strip() or None
    tags = body.tags if body.tags is not None else current["tags"]
    snapshot = current["request_snapshot"]
    if body.name is not None:
        title = body.name.strip()
        if not title:
            raise HTTPException(status_code=422, detail="run name is required")
        snapshot = dict(snapshot)
        snapshot["name"] = title
        rating = SAVED_RATING
        voice_id = current.get("voice_id")
        audio_id = current.get("output_artifact_id")
        if voice_id and audio_id:
            instruction = str(snapshot.get("produced_text") or snapshot.get("text") or "")
            record_voice_artifact(
                store,
                str(voice_id),
                artifact_id=f"vt_{run_id}",
                role=REFERENCE,
                kind="take",
                name=title,
                audio_artifact_id=str(audio_id),
                provenance={
                    "instruction": instruction,
                    "settings": {"run_id": run_id},
                },
            )
    store.execute(
        "UPDATE runs SET rating=?, tags_json=?, request_json=? WHERE id=?",
        (rating, json.dumps(tags), json.dumps(snapshot), run_id),
    )
    store.commit()
    voice_id = current.get("voice_id")
    if voice_id:
        prune_unsaved_runs(store, str(voice_id), voice_take_limit(store, str(voice_id)))
    return get_run(request, run_id)


@router.delete("/api/runs/{run_id}")
def delete_run(request: Request, run_id: str) -> dict[str, str]:
    store = request.app.state.lab.store
    row = store.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
    store.unpin(str(row["output_artifact_id"]), reason=f"run:{run_id}")
    store.execute("DELETE FROM runs WHERE id = ?", (run_id,))
    store.commit()
    return {"id": run_id, "status": "deleted"}


@router.get("/api/artifacts/{artifact_id}/audio")
def get_audio(request: Request, artifact_id: str) -> FileResponse:
    store = request.app.state.lab.store
    try:
        artifact = store.get(artifact_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"artifact not found: {artifact_id}") from exc
    suffix = artifact.suffix.lower()
    media = "application/json" if suffix == ".json" else "audio/wav"
    filename = f"{artifact_id}{suffix}" if suffix else f"{artifact_id}.wav"
    return FileResponse(artifact.path, media_type=media, filename=filename)

