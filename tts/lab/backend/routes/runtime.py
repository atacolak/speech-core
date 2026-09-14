"""Desk-facing runtime + talker. /e2 stays as an alias."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from tts.lab.backend.routes.voices import get_voice_or_404
from tts.lab.backend.runtime.types import RuntimeBusy
from tts.lab.backend.store.session import read_active_voice_id, write_active_voice_id
from tts.paths import ENGINE_ID

router = APIRouter()


class ActiveVoiceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    voice_id: str | None = None


class TalkerVoiceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    voice_id: str = Field(min_length=1)


def _status_payload(request: Request, status: dict[str, object] | None = None) -> dict[str, object]:
    state = request.app.state.lab
    payload = status if status is not None else state.runtime.status().to_dict()
    payload["engine"] = ENGINE_ID
    payload["voicecat_path"] = True
    payload["leftover_parked"] = bool(payload.get("leftover_parked") or state.leftover_parked)
    payload["active_voice_id"] = read_active_voice_id(state.store.root)
    return payload


def _talker_payload(request: Request) -> dict[str, object]:
    payload = _status_payload(request)
    return {
        "engine": ENGINE_ID,
        "state": payload["state"],
        "voice_id": payload.get("active_voice_id"),
    }


@router.get("/api/runtime")
@router.get("/api/runtime/e2")
@router.get("/api/runtime/breeze")
def runtime(request: Request) -> dict[str, object]:
    return _status_payload(request)


@router.put("/api/runtime/active-voice")
@router.put("/api/runtime/breeze/active-voice")
def set_active_voice(request: Request, body: ActiveVoiceBody) -> dict[str, object]:
    write_active_voice_id(request.app.state.lab.store.root, body.voice_id)
    return _status_payload(request)


@router.get("/api/talker")
def get_talker(request: Request) -> dict[str, object]:
    return _talker_payload(request)


@router.post("/api/talker/voice")
def set_talker_voice(request: Request, body: TalkerVoiceBody) -> dict[str, object]:
    get_voice_or_404(request.app.state.lab.store, body.voice_id)
    write_active_voice_id(request.app.state.lab.store.root, body.voice_id)
    return _talker_payload(request)


@router.post("/api/runtime/load")
@router.post("/api/runtime/e2/load")
@router.post("/api/runtime/breeze/load")
def load_e2(request: Request) -> dict[str, object]:
    try:
        return _status_payload(request, request.app.state.lab.runtime.begin_load().to_dict())
    except RuntimeBusy as exc:
        raise HTTPException(status_code=409, detail={"code": exc.code, "state": exc.state}) from exc


@router.post("/api/runtime/e2/unload")
@router.post("/api/runtime/breeze/unload")
def unload_e2(request: Request) -> dict[str, object]:
    """Park leftover. Lab unload must not bounce qwen onto the 4070."""
    return _status_payload(
        request,
        request.app.state.lab.runtime.begin_unload(restore_leftover=False).to_dict(),
    )


class LiveCallLeaseBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ttl_s: float | None = Field(default=None, gt=0, le=600)
    session_id: str | None = None


@router.post("/api/runtime/live-call")
@router.post("/api/runtime/e2/live-call")
@router.post("/api/runtime/breeze/live-call")
def hold_live_call(request: Request, body: LiveCallLeaseBody | None = None) -> dict[str, object]:
    """VoiceCat desk session hold. Not inferred from hop traffic."""
    runtime = request.app.state.lab.runtime
    payload = body or LiveCallLeaseBody()
    runtime.hold_session_lease(ttl_s=payload.ttl_s)
    return _status_payload(request)


@router.delete("/api/runtime/live-call")
@router.delete("/api/runtime/e2/live-call")
@router.delete("/api/runtime/breeze/live-call")
def clear_live_call(request: Request) -> dict[str, object]:
    """Clear the session lease. In-flight hop TTL may still hold."""
    request.app.state.lab.runtime.clear_session_lease()
    return _status_payload(request)
