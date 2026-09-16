"""Media source bench: ingest a source, analyze ranges, extract clips.

not on the voicecat path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from tts.lab.backend.audio import suffix_of
from tts.lab.backend.models import Interval
from tts.lab.backend.runtime.processors import ProcessorLease
from tts.lab.backend.runtime.types import LiveCallActive, RuntimeBusy
from tts.lab.backend.services.ingest import IngestUnavailable, ingest_url, write_peaks
from tts.lab.backend.services.resemble import ProcessorUnavailable
from tts.wav import is_riff_wav
from tts.lab.backend.services.sources import (
    OverlappingRanges,
    SourceLimitReached,
    analyze_range,
    coverage,
    create_media_source,
    extract_clip,
    list_sources,
    map_speaker,
    record_analysis,
    source_detail,
    source_duration_s,
    source_row,
    uncovered_ranges,
)
from tts.lab.backend.services.speakers import LEASE_NAME as VIBEVOICE_LEASE
from tts.lab.backend.services.speakers import PROCESSOR as VIBEVOICE_PROCESSOR
from tts.lab.backend.store.artifacts import ArtifactStore
from tts.packets import new_id

router = APIRouter()

_EPS = 1e-9


class AnalyzeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_s: float | None = None
    end_s: float | None = None
    all: bool = False


class MapBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    voice_id: str | None = None


class ExtractBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    speaker_local_id: str
    ranges: list[Interval] = Field(min_length=1)


class FromArtifactBody(BaseModel):
    """One retained take/artifact: the operator names it explicitly."""

    model_config = ConfigDict(extra="forbid")

    artifact_id: str
    run_id: str | None = None
    title: str = ""


def source_or_404(store: ArtifactStore, source_id: str) -> Any:
    try:
        return source_row(store, source_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=f"source not found: {source_id}") from exc


def _invalid_range(message: str) -> HTTPException:
    return HTTPException(status_code=422, detail={"code": "invalid_range", "message": message})


def _parsed_meta(meta: str) -> dict[str, Any]:
    """The operator's form meta, or a 422. Both ingests carry the same field."""
    try:
        parsed = json.loads(meta) if meta else {}
        if not isinstance(parsed, dict):
            raise ValueError("meta must be an object")
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(
            status_code=422, detail={"code": "invalid_meta", "message": str(exc)}
        ) from exc
    return parsed


def _retained_peaks(store: ArtifactStore, artifact: Any) -> Path | None:
    """The one artifact a retained ingest generates: the waveform peaks.

    Non-wav content (a converted container, say) registers without peaks, exactly
    like an upload that arrives without a waveform.
    """
    if not is_riff_wav(artifact.path):
        return None
    tmp = store.root / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    peaks = tmp / f"peaks-{new_id('up')}.json"
    write_peaks(artifact.path, peaks)
    return peaks


def _analyze_interval(body: AnalyzeBody, duration_s: float) -> Interval:
    """An explicit range, else the whole source. Never a half-specified range."""
    if body.all and (body.start_s is not None or body.end_s is not None):
        raise _invalid_range("all and an explicit range are mutually exclusive")
    if body.start_s is None and body.end_s is None:
        if duration_s <= 0:
            raise _invalid_range("the source has no known duration: pass start_s and end_s")
        return Interval(start_s=0.0, end_s=duration_s)
    if body.start_s is None or body.end_s is None:
        raise _invalid_range("start_s and end_s move together")
    try:
        interval = Interval(start_s=body.start_s, end_s=body.end_s)
    except ValueError as exc:
        raise _invalid_range(str(exc)) from exc
    if interval.end_s > duration_s + _EPS:
        raise _invalid_range(
            f"range {interval.start_s}-{interval.end_s} exceeds the source ({duration_s:.3f}s)"
        )
    return interval


@router.get("/api/sources")
def list_media_sources(request: Request) -> dict[str, Any]:
    """Every ingested source. May be empty: ingest lives behind its own route."""
    store = request.app.state.lab.store
    return {"items": [source_detail(store, str(row["id"])) for row in list_sources(store)]}


@router.post("/api/sources")
async def create_source(
    request: Request,
    file: UploadFile | None = File(None),
    url: str = Form(""),
    title: str = Form(""),
    meta: str = Form(""),
    waveform: UploadFile | None = File(None),
) -> dict[str, Any]:
    """Ingest a local audio file or a URL as a media source. Ingest never transcribes."""
    store = request.app.state.lab.store
    parsed_meta = _parsed_meta(meta)
    if file is None or not file.filename:
        if not url:
            raise HTTPException(status_code=400, detail="file is required")
        try:
            source_id = ingest_url(store, url=url, title=title, meta=parsed_meta)
        except IngestUnavailable as exc:
            raise HTTPException(
                status_code=501,
                detail={"code": "ingest_unavailable", "message": str(exc), "url": url},
            ) from exc
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return source_detail(store, source_id)
    tmp = store.root / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    upload = tmp / f"source-{new_id('up')}{suffix_of(file.filename)}"
    upload.write_bytes(await file.read())
    peaks = None
    if waveform is not None and waveform.filename:
        peaks = tmp / f"waveform-{new_id('up')}{suffix_of(waveform.filename)}"
        peaks.write_bytes(await waveform.read())
    elif is_riff_wav(upload):
        peaks = tmp / f"peaks-{new_id('up')}.json"
        write_peaks(upload, peaks)

    try:
        source_id = create_media_source(
            store,
            kind="file",
            origin=file.filename,
            title=title.strip() or file.filename.rsplit(".", 1)[0],
            audio_path=upload,
            waveform_path=peaks,
            meta=parsed_meta,
        )
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        upload.unlink(missing_ok=True)
        if peaks is not None:
            peaks.unlink(missing_ok=True)
    return source_detail(store, source_id)


@router.post("/api/sources/from-artifact")
def create_source_from_artifact(request: Request, body: FromArtifactBody) -> dict[str, Any]:
    """Register one retained take/artifact as a material.

    The audio is never copied: the source points at the object the run already
    owns, and only its peaks artifact is new. Nothing is enrolled onto a voice
    and no other run is imported.
    """
    store = request.app.state.lab.store
    try:
        artifact = store.get(body.artifact_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail=f"artifact not found: {body.artifact_id}"
        ) from exc
    meta: dict[str, Any] = {"artifact_id": artifact.id}
    if body.run_id is not None:
        run = store.execute(
            "SELECT output_artifact_id FROM runs WHERE id = ?", (body.run_id,)
        ).fetchone()
        if run is None:
            raise HTTPException(status_code=404, detail=f"run not found: {body.run_id}")
        if str(run["output_artifact_id"]) != artifact.id:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "artifact_run_mismatch",
                    "message": f"run {body.run_id} does not own artifact {artifact.id}",
                },
            )
        meta["run_id"] = body.run_id
    title = body.title.strip() or (f"take {body.run_id}" if body.run_id else artifact.id)
    peaks: Path | None = None
    try:
        peaks = _retained_peaks(store, artifact)
        source_id = create_media_source(
            store,
            kind="file",
            origin=f"artifact:{artifact.id}",
            title=title,
            audio_path=artifact.path,
            waveform_path=peaks,
            duration_s=artifact.duration_s,
            meta=meta,
        )
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        if peaks is not None:
            peaks.unlink(missing_ok=True)
    return source_detail(store, source_id)


@router.get("/api/sources/{source_id}")
def get_source(request: Request, source_id: str) -> dict[str, Any]:
    source_or_404(request.app.state.lab.store, source_id)
    return source_detail(request.app.state.lab.store, source_id)


@router.get("/api/sources/{source_id}/audio")
def source_audio(request: Request, source_id: str) -> FileResponse:
    store = request.app.state.lab.store
    row = source_or_404(store, source_id)
    artifact = store.get(str(row["audio_artifact_id"]))
    return FileResponse(artifact.path, media_type="audio/wav", filename=f"{source_id}.wav")


@router.post("/api/sources/{source_id}/analyze")
def analyze_source(request: Request, source_id: str, body: AnalyzeBody) -> dict[str, Any]:
    """Diarize the ranges of `source_id` that no analysis covers yet."""
    state = request.app.state.lab
    store = state.store
    row = source_or_404(store, source_id)
    interval = _analyze_interval(body, source_duration_s(store, source_id))
    artifact = store.get(str(row["audio_artifact_id"]))
    added = 0
    for piece in uncovered_ranges(interval, coverage(store, source_id)):
        try:
            with ProcessorLease(state.runtime).acquire(VIBEVOICE_LEASE):
                analysis = analyze_range(store, artifact.path, piece)
        except LiveCallActive as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "processor_blocked_live_call",
                    "message": "a desk live call owns the engine",
                    "remaining_s": exc.remaining_s,
                },
            ) from exc
        except RuntimeBusy as exc:
            raise HTTPException(
                status_code=503,
                detail={"code": "runtime_busy", "state": exc.state, "message": str(exc)},
            ) from exc
        except ProcessorUnavailable as exc:
            raise HTTPException(
                status_code=501,
                detail={
                    "code": "processor_unavailable",
                    "processor": VIBEVOICE_PROCESSOR,
                    "message": str(exc),
                },
            ) from exc
        except Exception as exc:  # fail closed: never invent a speaker split
            raise HTTPException(
                status_code=500,
                detail={
                    "code": "processor_failed",
                    "processor": VIBEVOICE_PROCESSOR,
                    "message": str(exc),
                },
            ) from exc
        record_analysis(store, source_id, piece, analysis)
        added += 1
    detail = source_detail(store, source_id)
    detail["analyses_added"] = added
    return detail


@router.post("/api/sources/{source_id}/speakers/{local_id}/map")
def map_source_speaker(
    request: Request, source_id: str, local_id: str, body: MapBody
) -> dict[str, Any]:
    """Bind a source-local speaker to a voice, or to none."""
    store = request.app.state.lab.store
    source_or_404(store, source_id)
    if body.voice_id is not None:
        known = store.execute("SELECT 1 FROM voices WHERE id = ?", (body.voice_id,)).fetchone()
        if known is None:
            raise HTTPException(status_code=404, detail=f"voice not found: {body.voice_id}")
    try:
        map_speaker(store, source_id, local_id, body.voice_id)
    except LookupError as exc:
        raise HTTPException(
            status_code=404, detail=f"speaker not found: {local_id} on {source_id}"
        ) from exc
    return source_detail(store, source_id)


@router.post("/api/sources/{source_id}/extract")
def extract_source_clip(request: Request, source_id: str, body: ExtractBody) -> dict[str, Any]:
    """Crop the selected turns for one speaker; the mapped voice owns the clip."""
    store = request.app.state.lab.store
    source_or_404(store, source_id)
    try:
        return extract_clip(store, source_id, body.speaker_local_id, body.ranges)
    except SourceLimitReached as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "source_limit_reached", "message": str(exc)},
        ) from exc
    except OverlappingRanges as exc:
        raise HTTPException(
            status_code=422, detail={"code": "overlapping_ranges", "message": str(exc)}
        ) from exc
    except LookupError as exc:
        raise HTTPException(
            status_code=404, detail=f"speaker not found: {body.speaker_local_id} on {source_id}"
        ) from exc
    except ValueError as exc:
        raise _invalid_range(str(exc)) from exc
