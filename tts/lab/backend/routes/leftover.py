"""Leftover v1 mouth hop. :8788 client of the loaded Breeze worker.

OpenAI-compat PCM so the leftover progressive worker can stay
the mouth hop without a second 3B or a second desk TTS client.
Engine not resident is 503 (engine_not_ready / engine_loading), not 409.
Kick begin_load on first hop if unloaded; do not wait for CUDA. Never
proxy the parked legacy :18091. 409 is talker_voice_missing only. Lab Generate
lease remains 409 live_call_active on /api/synthesize.
Streams s16le as Breeze iter_audio_chunks yields. Pull the first
pcm_chunk before returning so CUDA starts before ASGI iterates.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from tts.lab.backend.runtime.types import RuntimeBusy, RuntimeUnloaded
from tts.lab.backend.services.talker import (
    _talker_request,
    resolve_talker_voice_id,
)
from tts.lab.backend.store.session import read_active_voice_id

router = APIRouter()


class LeftoverSpeechBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    input: str = Field(min_length=1)
    voice: str | None = None
    response_format: str | None = None
    stream: bool | None = None
    instructions: str | None = None


def _engine_not_resident(runtime_state: str) -> HTTPException:
    """Mouth-visible not-resident. Distinct from 409 live_call_active."""
    if runtime_state == "loading":
        code = "engine_loading"
    elif runtime_state == "unloading":
        code = "runtime_busy"
    else:
        code = "engine_not_ready"
    return HTTPException(
        status_code=503,
        detail={"code": code, "engine": "breeze-tts2", "state": runtime_state},
        headers={
            "Retry-After": "5",
            "X-Speech-Engine-State": runtime_state,
            "X-Speech-Engine-Code": code,
        },
    )


@router.post("/internal/leftover/v1/audio/speech")
def leftover_speech(request: Request, body: LeftoverSpeechBody) -> StreamingResponse:
    state = request.app.state.lab
    runtime_state = state.runtime.status().state
    if runtime_state != "ready":
        if runtime_state in {"unloaded", "error", "insufficient_vram"}:
            begin = getattr(state.runtime, "begin_load", None)
            if callable(begin):
                try:
                    begin()
                except RuntimeBusy:
                    pass
            runtime_state = state.runtime.status().state
        raise _engine_not_resident(runtime_state)
    voice_id = resolve_talker_voice_id(
        state.store,
        requested=body.voice,
        talker_id=read_active_voice_id(state.store.root),
    )
    if not voice_id:
        raise HTTPException(
            status_code=409,
            detail={"code": "talker_voice_missing", "message": "no talker voice_id"},
        )
    chunks = None
    try:
        state.runtime.refresh_live_call_lease()
        payload = _talker_request(
            state, text=body.input, voice_id=voice_id, steer=body.instructions
        )
        chunks = iter(state.runtime.synthesize_stream(payload))
        first = next(chunks)
    except StopIteration:
        first = b""
        chunks = iter(())
    except Exception as exc:
        closer = getattr(chunks, "close", None)
        if closer is not None:
            closer()
        if isinstance(exc, RuntimeUnloaded):
            raise _engine_not_resident(exc.state) from exc
        if isinstance(exc, RuntimeBusy):
            raise _engine_not_resident(exc.state) from exc
        if isinstance(exc, HTTPException):
            raise
        if isinstance(exc, RuntimeError):
            message = str(exc)
            missing = "transcript" in message.lower() or "ref_text" in message.lower()
            raise HTTPException(
                status_code=422 if missing else 500,
                detail={"code": "missing_ref_text" if missing else "synthesize_failed", "message": message},
            ) from exc
        raise

    def body_iter():
        try:
            if first:
                yield first
            yield from chunks
        except RuntimeError:
            return
        finally:
            closer = getattr(chunks, "close", None)
            if closer is not None:
                try:
                    closer()
                except Exception:
                    pass
            refresh = getattr(state.runtime, "refresh_live_call_lease", None)
            if callable(refresh):
                refresh()

    return StreamingResponse(body_iter(), media_type="application/octet-stream")
