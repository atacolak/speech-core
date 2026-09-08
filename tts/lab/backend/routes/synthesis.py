"""Synthesis endpoint. not on the voicecat path."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from tts.generation import GenerationSettings
from tts.lab.backend.models import DualGuidance, parse_guidance
from tts.lab.backend.routes.voices import get_voice_or_404
from tts.lab.backend.services.breeze import SynthesisRequest, synthesize_e2
from tts.packets import new_id

router = APIRouter()


class SynthesisBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    steer: str
    voice_profile_id: str
    reference_variant_id: str | None = None
    generation: dict[str, Any] | None = None
    synthesis_text: str | None = None


def _settings_from_generation(raw: dict[str, Any] | None) -> GenerationSettings:
    if not raw:
        return GenerationSettings()
    if "guidance" in raw:
        guidance = parse_guidance(raw["guidance"])
        settings = GenerationSettings(seed=int(raw.get("seed", 42)))
        if isinstance(guidance, DualGuidance):
            settings.cfg_scale_ref = float(guidance.reference)
            settings.cfg_scale_ins = float(guidance.instruction)
        else:
            settings.cfg_scale = float(guidance.cfg)
        if "temperature" in raw:
            settings.temperature = float(raw["temperature"])
        if "depth_temperature" in raw:
            settings.depth_temperature = float(raw["depth_temperature"])
        if "do_sample" in raw:
            settings.do_sample = bool(raw["do_sample"])
        if "top_k" in raw:
            settings.top_k = int(raw["top_k"])
        if "top_p" in raw:
            settings.top_p = float(raw["top_p"])
        if "max_new_tokens" in raw:
            settings.max_new_tokens = int(raw["max_new_tokens"])
        return settings
    return GenerationSettings.from_dict(raw)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@router.post("/api/synthesize")
def synthesize(request: Request, body: SynthesisBody) -> dict[str, Any]:
    state = request.app.state.lab
    if state.engine is None:
        raise HTTPException(status_code=503, detail="E2 runtime is not loaded")
    voice = get_voice_or_404(state.store, body.voice_profile_id)
    artifact = state.store.get(voice["source_audio_artifact_id"])
    settings = _settings_from_generation(body.generation)
    result = synthesize_e2(
        SynthesisRequest(
            text=body.text,
            steer=body.steer,
            synthesis_text=body.synthesis_text,
            voice_profile_id=body.voice_profile_id,
            reference_audio=artifact.path,
            reference_text=voice["effective_transcript"] or voice["source_transcript"],
            generation=settings,
        ),
        engine=state.engine,
    )
    output = state.store.import_audio(result.wav_path)
    run_id = new_id("run")
    state.store.pin(output.id, reason=f"run:{run_id}")
    snapshot = {
        "text": body.text,
        "steer": body.steer,
        "voice_profile_id": body.voice_profile_id,
        "reference_variant_id": body.reference_variant_id,
        "generation": body.generation
        or {"guidance": {"mode": "single", "cfg": settings.cfg_scale}, "seed": settings.seed},
        "synthesis_text": body.synthesis_text,
    }
    created = _now()
    state.store.execute(
        """
        INSERT INTO runs (
            id, voice_id, request_json, output_artifact_id, effective_reference_json,
            latency_ms, first_audio_ms, duration_s, rating, tags_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            body.voice_profile_id,
            json.dumps(snapshot),
            output.id,
            json.dumps(
                {
                    "artifact_id": artifact.id,
                    "keep_intervals": voice["keep_intervals"],
                    "transcript": voice["effective_transcript"],
                }
            ),
            float(result.wall_s) * 1000.0,
            None if result.first_audio_s is None else float(result.first_audio_s) * 1000.0,
            float(result.duration_s),
            None,
            json.dumps([]),
            created,
        ),
    )
    state.store.commit()
    return {
        "id": run_id,
        "output_artifact_id": output.id,
        "duration_s": result.duration_s,
        "latency_ms": float(result.wall_s) * 1000.0,
        "first_audio_ms": None if result.first_audio_s is None else float(result.first_audio_s) * 1000.0,
        "request_snapshot": snapshot,
        "wav_path": str(output.path),
    }
