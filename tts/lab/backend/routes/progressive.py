"""Streamed GENERATE HTTP surface. not on the voicecat path.

Reads the live-call lease; never sets it, never takes a second GPU occupant.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from tts.lab.backend.routes.synthesis import SynthesisBody, resolve_synthesis_request
from tts.lab.backend.services.progressive import (
    SAMPLE_RATE,
    GenerationActive,
    iter_generate_pcm,
)
from tts.lab.backend.services.segmenter import segment_text

router = APIRouter()


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


@router.post("/api/generate/stream")
def stream_generate(request: Request, body: SynthesisBody) -> StreamingResponse:
    state = request.app.state.lab
    _preflight(state)
    resolved = resolve_synthesis_request(state, body)
    segments = segment_text(body.synthesis_text or body.text)
    if not segments:
        raise HTTPException(status_code=422, detail={"code": "empty_text"})
    try:
        stream = state.generate_streams.begin(voice_profile_id=body.voice_profile_id)
    except GenerationActive as exc:
        raise HTTPException(
            status_code=409, detail={"code": "generation_active"}
        ) from exc
    return StreamingResponse(
        iter_generate_pcm(state, stream, body, resolved, segments),
        media_type="application/octet-stream",
        headers={"X-Generate-Id": stream.id, "X-Sample-Rate": str(SAMPLE_RATE)},
    )


@router.post("/api/generate/{stream_id}/stop")
def stop_generate(request: Request, stream_id: str) -> dict[str, Any]:
    try:
        stream = request.app.state.lab.generate_streams.stop(stream_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "stream_not_found"}) from exc
    return {"id": stream.id, "stopped": True}

