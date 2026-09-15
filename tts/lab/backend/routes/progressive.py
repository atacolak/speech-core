"""Progressive GENERATE HTTP surface. not on the voicecat path.

Reads the live-call lease; never sets it, never takes a second GPU occupant.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from tts.lab.backend.routes.synthesis import SynthesisBody, resolve_synthesis_request
from tts.lab.backend.services.progressive import GenerationActive, ProgressiveJob
from tts.lab.backend.services.segmenter import segment_text

router = APIRouter()


class CursorBody(BaseModel):
    index: int


def _preflight(state: Any) -> None:
    """The same status ladder /api/synthesize applies before it touches the engine."""
    if state.engine is not None:
        return
    runtime_state = state.runtime.status().state
    if runtime_state in {"unloaded", "insufficient_vram", "error"}:
        raise HTTPException(
            status_code=409,
            detail={"code": "runtime_unloaded", "runtime": "e2", "state": runtime_state},
        )
    if runtime_state in {"loading", "unloading"}:
        raise HTTPException(
            status_code=409,
            detail={"code": "runtime_busy", "runtime": "e2", "state": runtime_state},
        )
    remaining = float(state.runtime.live_call_remaining_s())
    if remaining > 0:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "live_call_active",
                "message": "live call owns the engine",
                "remaining_s": remaining,
            },
        )


def _get_job(request: Request, job_id: str) -> ProgressiveJob:
    """Registry lookup; an unknown id is a 404, never a 500."""
    try:
        return request.app.state.lab.progressive.get(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "job_not_found"}) from exc


@router.post("/api/generate")
def start_generate(request: Request, body: SynthesisBody) -> dict[str, Any]:
    state = request.app.state.lab
    _preflight(state)
    resolved = resolve_synthesis_request(state, body)
    segments = segment_text(body.synthesis_text or body.text)
    if not segments:
        raise HTTPException(status_code=422, detail={"code": "empty_text"})
    try:
        job = state.progressive.create(
            state, body=body, resolved=resolved, segments=segments
        )
    except GenerationActive as exc:
        raise HTTPException(
            status_code=409, detail={"code": "generation_active"}
        ) from exc
    return job.to_dict()


@router.get("/api/generate/{job_id}")
def get_generate(request: Request, job_id: str) -> dict[str, Any]:
    return _get_job(request, job_id).to_dict()


@router.post("/api/generate/{job_id}/cursor")
def set_generate_cursor(request: Request, job_id: str, body: CursorBody) -> dict[str, Any]:
    job = _get_job(request, job_id)
    if body.index < -1 or body.index >= len(job.segments):
        raise HTTPException(status_code=422, detail={"code": "invalid_cursor"})
    try:
        job = request.app.state.lab.progressive.set_cursor(job_id, body.index)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"code": "invalid_cursor"}) from exc
    return job.to_dict()


@router.post("/api/generate/{job_id}/cancel")
def cancel_generate(request: Request, job_id: str) -> dict[str, Any]:
    _get_job(request, job_id)
    return request.app.state.lab.progressive.cancel(job_id).to_dict()


@router.get("/api/generate/{job_id}/segments/{index}/audio")
def get_generate_segment(request: Request, job_id: str, index: int) -> FileResponse:
    job = _get_job(request, job_id)
    if index < 0 or index >= len(job.segments):
        raise HTTPException(status_code=404, detail={"code": "segment_not_found"})
    segment = job.segments[index]
    if segment.state != "generated" or segment.wav_path is None:
        raise HTTPException(status_code=409, detail={"code": "segment_not_ready"})
    return FileResponse(segment.wav_path, media_type="audio/wav")
