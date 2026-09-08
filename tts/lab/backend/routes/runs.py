"""Run history. not on the voicecat path."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict

router = APIRouter()


def _run_row(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "voice_id": row["voice_id"],
        "request_snapshot": json.loads(row["request_json"]),
        "output_artifact_id": row["output_artifact_id"],
        "effective_reference_snapshot": json.loads(row["effective_reference_json"]),
        "latency_ms": row["latency_ms"],
        "first_audio_ms": row["first_audio_ms"],
        "duration_s": row["duration_s"],
        "rating": row["rating"],
        "tags": json.loads(row["tags_json"]),
        "created_at": row["created_at"],
    }


@router.get("/api/runs")
def list_runs(request: Request) -> dict[str, Any]:
    store = request.app.state.lab.store
    rows = store.execute("SELECT * FROM runs ORDER BY created_at DESC").fetchall()
    return {"items": [_run_row(row) for row in rows]}


@router.get("/api/runs/{run_id}")
def get_run(request: Request, run_id: str) -> dict[str, Any]:
    store = request.app.state.lab.store
    row = store.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
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
    rating = body.rating if body.rating is not None else current["rating"]
    tags = body.tags if body.tags is not None else current["tags"]
    snapshot = current["request_snapshot"]
    if body.name is not None:
        snapshot = dict(snapshot)
        snapshot["name"] = body.name
    store.execute(
        "UPDATE runs SET rating=?, tags_json=?, request_json=? WHERE id=?",
        (rating, json.dumps(tags), json.dumps(snapshot), run_id),
    )
    store.commit()
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
    return FileResponse(artifact.path, media_type="audio/wav", filename=f"{artifact_id}.wav")
