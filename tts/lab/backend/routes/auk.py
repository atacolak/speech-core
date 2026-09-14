"""AuK runtime + candidate generation. lab-only; t4's editor is the only client.

Nothing here downloads, converts or silently degrades a precision: an absent
pin is a 501, a live desk call is a 409, and an unloaded runtime is a 409. The
route layer never retries with bf16 when the operator asked for int8.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from tts.auk.pin import AUK_ENCODER_PRECISION, AUK_MODEL_VARIANT, UnknownPrecision
from tts.lab.backend.models import Interval
from tts.lab.backend.routes.voices import get_voice_or_404
from tts.lab.backend.runtime.auk import (
    AukRuntimeManager,
    EngineUnavailable,
    PrecisionMismatch,
    WeightsMissing,
)
from tts.lab.backend.runtime.types import (
    InsufficientVram,
    LiveCallActive,
    RuntimeBusy,
    RuntimeUnloaded,
)
from tts.lab.backend.runtime.worker import WorkerChannelDirty
from tts.lab.backend.services.candidates import record_auk_candidate
from tts.lab.backend.services.references import materialize_keep_wav

router = APIRouter()


class AukLoadBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Explicit precision only. Omitted means the quality pin, never int8.
    precision: str | None = None


class AukComponentBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    component: str = Field(min_length=1)


class AukTaskBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    auk_task: str = Field(min_length=1)
    instruction: str = Field(min_length=1)
    parent_variant_id: str | None = None
    seed: int | None = None
    auk_precision: str | None = None
    settings: dict[str, Any] | None = None


def get_auk(request: Request) -> AukRuntimeManager:
    return request.app.state.lab.auk


@router.get("/api/auk/runtime")
def auk_runtime(request: Request) -> dict[str, Any]:
    return get_auk(request).status()


@router.post("/api/auk/runtime/load")
def auk_load(request: Request, body: AukLoadBody | None = None) -> dict[str, Any]:
    payload = body or AukLoadBody()
    try:
        return get_auk(request).begin_load(payload.precision)
    except UnknownPrecision as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": exc.code, "precision": exc.precision, "message": str(exc)},
        ) from exc
    except WeightsMissing as exc:
        raise HTTPException(
            status_code=501,
            detail={
                "code": exc.code,
                "precision": exc.precision,
                "missing": exc.missing,
                "message": str(exc),
            },
        ) from exc
    except LiveCallActive as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": exc.code,
                "remaining_s": exc.remaining_s,
                "message": "a desk live call owns the engine",
            },
        ) from exc
    except RuntimeBusy as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "state": exc.state, "message": str(exc)},
        ) from exc


@router.post("/api/auk/runtime/unload")
def auk_unload(request: Request) -> dict[str, Any]:
    """Kill the worker and hand the lease back. Parked leftover stays parked."""
    return get_auk(request).unload()


@router.post("/api/auk/runtime/components/offload")
def auk_offload_component(request: Request, body: AukComponentBody) -> dict[str, Any]:
    """Drop one component's residency without touching the others."""
    auk = get_auk(request)
    try:
        return auk.offload_component(body.component)
    except RuntimeUnloaded as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "state": exc.state, "message": f"AuK is {exc.state}"},
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": getattr(exc, "code", "unknown_component"), "message": str(exc)},
        ) from exc


@router.post("/api/voices/{voice_id}/auk")
def run_auk_task(request: Request, voice_id: str, body: AukTaskBody) -> dict[str, Any]:
    """Render the current keep through AuK into a new `kind=auk` candidate."""
    state = request.app.state.lab
    store = state.store
    voice = get_voice_or_404(store, voice_id)
    artifact = store.get(voice["source_audio_artifact_id"])
    keep = [Interval.model_validate(item) for item in voice["keep_intervals"]]
    tmp = store.root / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    source = tmp / f"{voice_id}-auk-src.wav"
    dest = tmp / f"{voice_id}-auk-{body.auk_task}.wav"
    materialize_keep_wav(artifact.path, keep, source)
    if dest.exists():
        dest.unlink()
    try:
        result = state.auk.generate(
            task=body.auk_task,
            instruction=body.instruction,
            source_wav=source,
            output_path=dest,
            seed=body.seed,
            settings=body.settings,
            precision=body.auk_precision,
        )
    except PrecisionMismatch as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": exc.code,
                "requested": exc.requested,
                "loaded": exc.loaded,
                "message": str(exc),
            },
        ) from exc
    except LiveCallActive as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": exc.code,
                "remaining_s": exc.remaining_s,
                "message": "a desk live call owns the engine",
            },
        ) from exc
    except RuntimeUnloaded as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": exc.code,
                "state": exc.state,
                "message": "AuK is not loaded; load a precision first",
            },
        ) from exc
    except InsufficientVram as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": exc.code,
                "free_bytes": exc.free_bytes,
                "required_bytes": exc.required_bytes,
                "message": str(exc),
            },
        ) from exc
    except WeightsMissing as exc:
        raise HTTPException(
            status_code=501,
            detail={"code": exc.code, "missing": exc.missing, "message": str(exc)},
        ) from exc
    except EngineUnavailable as exc:
        raise HTTPException(
            status_code=501,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    except WorkerChannelDirty as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "worker_dirty", "message": str(exc)},
        ) from exc
    except Exception as exc:  # fail closed: never hand back the source as a candidate
        raise HTTPException(
            status_code=500,
            detail={"code": "auk_failed", "message": str(exc)},
        ) from exc
    rendered = Path(str(result.get("output_path") or dest))
    if not rendered.is_file():
        raise HTTPException(
            status_code=500,
            detail={"code": "auk_failed", "message": "AuK produced no audio"},
        )
    processed = store.import_audio(rendered)
    record_auk_candidate(
        store,
        voice_id,
        audio_artifact_id=processed.id,
        auk_task=body.auk_task,
        instruction=body.instruction,
        model_variant=AUK_MODEL_VARIANT,
        auk_precision=state.auk.status()["precision"],
        encoder_precision=AUK_ENCODER_PRECISION,
        seed=body.seed,
        settings=body.settings,
        parent_variant_id=body.parent_variant_id,
    )
    return get_voice_or_404(store, voice_id)
