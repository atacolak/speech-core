"""Lab desk helpers. not on the voicecat path."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from tts.lab.backend.routes.voices import get_voice_or_404
from tts.planner import flag_pronunciation_risks, load_steer_fixtures, plan_utterance

router = APIRouter()


class PlanBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str


@router.get("/api/fixtures/steers")
def steer_fixtures() -> dict[str, Any]:
    return {"items": load_steer_fixtures()}


@router.post("/api/plan")
def plan_steer(body: PlanBody) -> dict[str, Any]:
    packet = plan_utterance(text=body.text, delivery=None)
    return {
        "text": packet.text,
        "steer": packet.steer,
        "synthesis_text": packet.synthesis_text,
        "pronunciation_risks": flag_pronunciation_risks(body.text),
        "provenance": packet.provenance,
    }


@router.post("/api/voices/{voice_id}/transcribe")
def transcribe_voice(request: Request, voice_id: str) -> dict[str, Any]:
    store = request.app.state.lab.store
    voice = get_voice_or_404(store, voice_id)
    if voice.get("transcript_locked"):
        raise HTTPException(
            status_code=409,
            detail={"code": "transcript_locked", "message": "unlock the transcript first"},
        )
    artifact = store.get(voice["source_audio_artifact_id"])
    try:
        from breeze_tts_qual.transcribe import transcribe_audio
    except ImportError as exc:
        raise HTTPException(status_code=501, detail="transcribe helper missing") from exc
    try:
        result = transcribe_audio(artifact.path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    text = str(result.get("text") or "").strip()
    words = result.get("words") if isinstance(result.get("words"), list) else []
    keep = voice.get("keep_intervals") or []
    if words:
        from tts.lab.backend.services.references import slice_transcript

        effective = slice_transcript(words, keep) or text
    else:
        effective = text
    source = text
    store.execute(
        """
        UPDATE voices SET source_transcript=?, effective_transcript=?,
            source_words_json=?, transcript_locked=0, updated_at=datetime('now')
        WHERE id=?
        """,
        (source, effective, json.dumps(words) if words else None, voice_id),
    )
    store.commit()
    voice = get_voice_or_404(store, voice_id)
    voice["transcribe"] = result
    return voice


