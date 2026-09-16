"""Synthesis endpoint. not on the voicecat path."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from tts.generation import GenerationSettings
from tts.lab.backend.models import DualGuidance, parse_guidance
from tts.lab.backend.routes.voices import get_voice_or_404
from tts.lab.backend.runtime.types import LiveCallActive, RuntimeBusy, RuntimeUnloaded
from tts.lab.backend.services.breeze import SynthesisRequest, synthesize_e2
from tts.lab.backend.services.run_alignment import pending_alignment
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


def _reference_transcript(voice: dict[str, Any]) -> str:
    return str(voice.get("effective_transcript") or voice.get("source_transcript") or "").strip()


def _fill_ref_text(store: Any, voice: dict[str, Any], reference_path: Path) -> str:
    text = _reference_transcript(voice)
    if text:
        return text
    try:
        from breeze_tts_qual.transcribe import transcribe_audio

        result = transcribe_audio(reference_path)
        text = str(result.get("text") or "").strip()
    except (FileNotFoundError, ImportError, PermissionError):
        text = ""
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"code": "transcribe_failed", "message": str(exc)},
        ) from exc
    if not text:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "missing_ref_text",
                "message": (
                    "clone needs a transcript of the reference audio. "
                    "transcribe or paste it in the voice editor."
                ),
            },
        )
    store.execute(
        "UPDATE voices SET source_transcript=?, effective_transcript=?, updated_at=? WHERE id=?",
        (text, text, _now(), voice["id"]),
    )
    store.commit()
    return text


@dataclass(frozen=True)
class ResolvedSynthesis:
    voice: dict[str, Any]
    artifact: Any
    reference_path: Path
    reference_text: str
    settings: GenerationSettings


def resolve_synthesis_request(state: Any, body: SynthesisBody) -> ResolvedSynthesis:
    """Resolve the voice, reference audio/transcript, and settings for one request."""
    voice = get_voice_or_404(state.store, body.voice_profile_id)
    from tts.lab.backend.models import Interval
    from tts.lab.backend.services.references import (
        materialize_keep_wav,
        processed_variant_is_current,
    )

    source = state.store.get(voice["source_audio_artifact_id"])
    variant = voice.get("active_variant")
    if body.reference_variant_id:
        variant = next(
            (item for item in voice.get("variants") or [] if item["id"] == body.reference_variant_id),
            variant,
        )
    if variant is not None and processed_variant_is_current(
        variant, source.sha256, voice["keep_intervals"]
    ):
        artifact = state.store.get(variant["audio_artifact_id"])
        reference_path = artifact.path
    else:
        artifact = source
        dest = state.store.root / "tmp" / f"{body.voice_profile_id}-synth-ref.wav"
        dest.parent.mkdir(parents=True, exist_ok=True)
        keep = [Interval.model_validate(item) for item in voice["keep_intervals"]]
        materialize_keep_wav(source.path, keep, dest)
        reference_path = dest
    settings = _settings_from_generation(body.generation)
    reference_text = _fill_ref_text(state.store, voice, Path(reference_path))
    return ResolvedSynthesis(
        voice=voice,
        artifact=artifact,
        reference_path=reference_path,
        reference_text=reference_text,
        settings=settings,
    )


def record_synthesis_run(
    state: Any,
    body: SynthesisBody,
    resolved: ResolvedSynthesis,
    result: Any,
    *,
    produced: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist the produced audio as a run and return the route response."""
    voice = resolved.voice
    artifact = resolved.artifact
    settings = resolved.settings
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
    if produced is not None:
        snapshot.update(produced)
    created = _now()
    state.store.execute(
        """
        INSERT INTO runs (
            id, voice_id, request_json, output_artifact_id, effective_reference_json,
            latency_ms, first_audio_ms, duration_s, rating, tags_json, created_at,
            alignment_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            json.dumps(pending_alignment()),
        ),
    )
    if body.voice_profile_id:
        state.store.execute(
            "UPDATE voices SET latest_take_id = ?, updated_at = ? WHERE id = ?",
            (run_id, created, body.voice_profile_id),
        )
    state.store.commit()
    if body.generation is not None:
        state.store.execute(
            "UPDATE voices SET generation_json=?, updated_at=? WHERE id=?",
            (json.dumps(body.generation), created, body.voice_profile_id),
        )
        state.store.commit()
    from tts.lab.backend.routes.runs import prune_unsaved_runs, voice_take_limit

    prune_unsaved_runs(
        state.store,
        body.voice_profile_id,
        voice_take_limit(state.store, body.voice_profile_id),
    )
    # The run is durable before the CPU aligner starts; the worker only updates it.
    state.run_alignments.schedule(state.store, run_id, output.id)
    return {
        "id": run_id,
        "output_artifact_id": output.id,
        "duration_s": result.duration_s,
        "latency_ms": float(result.wall_s) * 1000.0,
        "first_audio_ms": None if result.first_audio_s is None else float(result.first_audio_s) * 1000.0,
        "request_snapshot": snapshot,
        "wav_path": str(output.path),
    }


@router.post("/api/synthesize")
def synthesize(request: Request, body: SynthesisBody) -> dict[str, Any]:
    state = request.app.state.lab
    if state.engine is None:
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
        remaining = float(getattr(state.runtime, "live_call_remaining_s", lambda: 0.0)())
        if remaining > 0:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "live_call_active",
                    "message": "live call owns the engine",
                    "remaining_s": remaining,
                },
            )
    resolved = resolve_synthesis_request(state, body)
    try:
        if state.engine is not None:
            result = synthesize_e2(
                SynthesisRequest(
                    text=body.text,
                    steer=body.steer,
                    synthesis_text=body.synthesis_text,
                    voice_profile_id=body.voice_profile_id,
                    reference_audio=resolved.reference_path,
                    reference_text=resolved.reference_text,
                    generation=resolved.settings,
                ),
                engine=state.engine,
            )
        else:
            payload = state.runtime.synthesize(
                {
                    "text": body.text,
                    "steer": body.steer,
                    "synthesis_text": body.synthesis_text,
                    "voice_profile_id": body.voice_profile_id,
                    "reference_audio": str(resolved.reference_path),
                    "reference_text": resolved.reference_text,
                    "generation": resolved.settings.to_dict(),
                }
            )
            result = SimpleNamespace(
                wav_path=Path(payload["wav_path"]),
                duration_s=float(payload["duration_s"]),
                wall_s=float(payload["wall_s"]),
                first_audio_s=payload.get("first_audio_s"),
            )
    except RuntimeUnloaded as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "runtime_unloaded", "runtime": "e2", "state": exc.state},
        ) from exc
    except RuntimeBusy as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "runtime_busy", "runtime": "e2", "state": exc.state},
        ) from exc
    except LiveCallActive as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "live_call_active",
                "message": "live call owns the engine",
                "remaining_s": exc.remaining_s,
            },
        ) from exc
    except RuntimeError as exc:
        message = str(exc)
        missing = "missing template fields" in message or "ref_text" in message
        raise HTTPException(
            status_code=422 if missing else 500,
            detail={
                "code": "missing_ref_text" if missing else "synthesize_failed",
                "message": message,
            },
        ) from exc
    return record_synthesis_run(state, body, resolved, result)
