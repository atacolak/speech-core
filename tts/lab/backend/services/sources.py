"""First-class media sources: ingest, range analysis, clips, speaker maps.

A source is not a voice. It exists unowned until a voice extracts a clip from
it, and it can be analyzed in ranges: a range that is already covered is never
decoded twice. A clip is the set of non-overlapping turns one source-local
speaker owns inside the operator's ranges, with the overlap turns excluded
rather than separated.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from tts.lab.backend.models import Interval, MediaSourceKind
from tts.lab.backend.services.candidates import (
    EXPERIMENT,
    ORIGINAL_KIND,
    REFERENCE,
    record_voice_artifact,
)
from tts.lab.backend.services.references import materialize_keep_wav, normalize_keep_intervals
from tts.lab.backend.services.speakers import (
    MODEL_ID as VIBEVOICE_MODEL_ID,
    PROCESSOR as VIBEVOICE_PROCESSOR,
    PROCESSOR_CONFIG as VIBEVOICE_CONFIG,
    analyze_audio,
)
from tts.lab.backend.services.voices import add_voice_source, source_count, voice_source_limit
from tts.lab.backend.store.artifacts import ArtifactStore
from tts.lab.backend.store.cache import processor_cache_key
from tts.packets import new_id

_EPS = 1e-9
# A crop is a lineage child, never a source: its kind names the operation.
CROP_KIND = "crop"
# Diarization markup the decoder sometimes prefixes a turn with. Breeze gets prose.
_SPEAKER_MARKUP = re.compile(r"\bspeaker[_\s-]*\w+\s*:", re.IGNORECASE)


class OverlappingRanges(ValueError):
    """Selected clip ranges that are not disjoint. Overlap is never separated."""


class SourceLimitReached(ValueError):
    """The voice already holds `source_limit` sources. Nothing was written."""

    def __init__(self, limit: int) -> None:
        super().__init__(f"voice source limit reached ({limit})")
        self.limit = int(limit)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load(json_text: str | None, fallback: Any) -> Any:
    if not json_text:
        return fallback
    try:
        return json.loads(json_text)
    except json.JSONDecodeError:
        return fallback


def source_artifact_id(store: ArtifactStore, voice_id: str, source_id: str) -> str:
    """The immutable audio artifact one enrolled source owns."""
    row = store.execute(
        "SELECT artifact_id FROM voice_sources WHERE id = ? AND voice_id = ?",
        (source_id, voice_id),
    ).fetchone()
    if row is None:
        raise LookupError(source_id)
    return str(row["artifact_id"])


def ensure_source_original_artifact(store: ArtifactStore, voice_id: str, source_id: str) -> str:
    """The lineage root of one voice source: its one `original` reference artifact.

    Idempotent, and never the operator's selection: the row's audio is the
    immutable source artifact, so crop and processor children point their
    `parent_id` at it instead of at a `voice_sources` row.
    """
    existing = store.execute(
        """
        SELECT id FROM voice_artifacts
        WHERE voice_id = ? AND source_id = ? AND kind = ?
        ORDER BY rowid ASC
        """,
        (voice_id, source_id, ORIGINAL_KIND),
    ).fetchone()
    if existing is not None:
        return str(existing["id"])
    artifact_id = new_id("va")
    record_voice_artifact(
        store,
        voice_id,
        artifact_id=artifact_id,
        role=REFERENCE,
        kind=ORIGINAL_KIND,
        name="Original",
        audio_artifact_id=source_artifact_id(store, voice_id, source_id),
        source_id=source_id,
    )
    return artifact_id


def crop_source_child(
    store: ArtifactStore,
    voice_id: str,
    source_id: str,
    intervals: Sequence[Interval],
    *,
    name: str,
) -> str:
    """Cut one crop of a source into a lineage child of that source's ORIGINAL.

    The source's audio, keep and rows are never touched: the child is a new
    object under a new `voice_artifacts` row whose `source_id` is the source and
    whose `parent_id` is the source's lineage root.
    """
    artifact = store.get(source_artifact_id(store, voice_id, source_id))
    duration = float(artifact.duration_s or 0.0)
    if duration <= 0:
        raise ValueError("source audio is empty")
    keep = normalize_keep_intervals(list(intervals), duration)
    tmp = store.root / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    dest = tmp / f"crop-{new_id('crop')}.wav"
    materialize_keep_wav(artifact.path, keep, dest)
    try:
        audio = store.import_audio(dest)
    finally:
        dest.unlink(missing_ok=True)
    parent_id = ensure_source_original_artifact(store, voice_id, source_id)
    store.pin(audio.id, reason=f"voice:{voice_id}:crop")
    artifact_id = new_id("va")
    record_voice_artifact(
        store,
        voice_id,
        artifact_id=artifact_id,
        role=EXPERIMENT,
        kind=CROP_KIND,
        name=name or "Crop",
        audio_artifact_id=audio.id,
        parent_id=parent_id,
        source_id=source_id,
        keep_intervals=keep,
        processor_cache_key=processor_cache_key(CROP_KIND, artifact.sha256, keep, None),
    )
    store.commit()
    return artifact_id


def create_media_source(
    store: ArtifactStore,
    *,
    kind: MediaSourceKind,
    origin: str,
    title: str,
    audio_path: Path | str,
    waveform_path: Path | str | None = None,
    duration_s: float | None = None,
    meta: dict[str, Any] | None = None,
) -> str:
    """Import the source audio as an artifact and register the source row."""
    artifact = store.import_audio(audio_path)
    source_id = new_id("ms")
    store.execute(
        """
        INSERT INTO media_sources (
            id, kind, origin, title, audio_artifact_id, waveform_artifact_id,
            duration_s, meta_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source_id,
            kind,
            origin,
            title,
            artifact.id,
            None if waveform_path is None else store.import_audio(waveform_path).id,
            float(duration_s if duration_s is not None else artifact.duration_s or 0.0),
            json.dumps(meta or {}),
            _now(),
        ),
    )
    store.pin(artifact.id, reason=f"source:{source_id}")
    store.commit()
    return source_id


def source_row(store: ArtifactStore, source_id: str) -> Any:
    row = store.execute("SELECT * FROM media_sources WHERE id = ?", (source_id,)).fetchone()
    if row is None:
        raise LookupError(source_id)
    return row


def list_sources(store: ArtifactStore) -> list[Any]:
    return store.execute(
        "SELECT * FROM media_sources ORDER BY created_at ASC, rowid ASC"
    ).fetchall()


def source_duration_s(store: ArtifactStore, source_id: str) -> float:
    """The length of the audio a processor actually sees: the artifact first.

    The stored `duration_s` is the reported length (a youtube video can outrun
    its downloaded audio), so it only answers when the audio has no length.
    """
    row = source_row(store, source_id)
    measured = float(store.get(str(row["audio_artifact_id"])).duration_s or 0.0)
    return measured or float(row["duration_s"] or 0.0)


def coverage(store: ArtifactStore, source_id: str) -> list[Interval]:
    """Merged union of the analyzed ranges. Touching ranges are one span."""
    rows = store.execute(
        "SELECT start_s, end_s FROM source_analyses WHERE source_id = ? ORDER BY start_s ASC",
        (source_id,),
    ).fetchall()
    merged: list[Interval] = []
    for row in rows:
        start, end = float(row["start_s"]), float(row["end_s"])
        if merged and start <= merged[-1].end_s + _EPS:
            merged[-1] = Interval(start_s=merged[-1].start_s, end_s=max(merged[-1].end_s, end))
            continue
        merged.append(Interval(start_s=start, end_s=end))
    return merged


def uncovered_ranges(interval: Interval, covered: Sequence[Interval]) -> list[Interval]:
    """`interval` minus `covered`, as disjoint pieces in ascending order."""
    pieces = [interval]
    for done in covered:
        remaining: list[Interval] = []
        for piece in pieces:
            if done.end_s <= piece.start_s + _EPS or done.start_s >= piece.end_s - _EPS:
                remaining.append(piece)
                continue
            if done.start_s > piece.start_s + _EPS:
                remaining.append(Interval(start_s=piece.start_s, end_s=done.start_s))
            if done.end_s < piece.end_s - _EPS:
                remaining.append(Interval(start_s=done.end_s, end_s=piece.end_s))
        pieces = remaining
    return pieces


def analyze_range(
    store: ArtifactStore,
    artifact_path: Path | str,
    interval: Interval,
    *,
    analyze_fn: Any = None,
) -> dict[str, Any]:
    """Decode one range and shift its turns back onto the source timeline."""
    tmp = store.root / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    crop = tmp / f"analysis-{new_id('an')}.wav"
    materialize_keep_wav(artifact_path, [interval], crop)
    try:
        analysis = analyze_audio(crop, analyze_fn=analyze_fn)
    finally:
        crop.unlink(missing_ok=True)
    return offset_analysis(analysis, interval.start_s)


def offset_analysis(analysis: dict[str, Any], offset_s: float) -> dict[str, Any]:
    """Range-relative turns in, source-timeline turns out."""

    def shift(item: dict[str, Any]) -> dict[str, Any]:
        if "start_s" not in item:
            return item
        return {
            **item,
            "start_s": round(float(item["start_s"]) + offset_s, 6),
            "end_s": round(float(item["end_s"]) + offset_s, 6),
        }

    return {
        **analysis,
        "segments": [shift(seg) for seg in analysis.get("segments") or []],
        "overlaps": [shift(pair) for pair in analysis.get("overlaps") or []],
    }


def record_analysis(
    store: ArtifactStore, source_id: str, interval: Interval, analysis: dict[str, Any]
) -> str:
    """Persist one analyzed range and upsert its source-local speakers.

    A re-analysis refreshes labels and speaking time but never the operator's
    speaker-to-voice mapping.
    """
    analysis_id = new_id("sa")
    store.execute(
        """
        INSERT INTO source_analyses (
            id, source_id, start_s, end_s, processor, model_id, config_json,
            result_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            analysis_id,
            source_id,
            interval.start_s,
            interval.end_s,
            VIBEVOICE_PROCESSOR,
            VIBEVOICE_MODEL_ID,
            json.dumps(VIBEVOICE_CONFIG),
            json.dumps(analysis),
            _now(),
        ),
    )
    for speaker in analysis.get("speakers") or []:
        local_id = str(speaker.get("id") or "")
        if not local_id:
            continue
        store.execute(
            """
            INSERT INTO source_speakers (source_id, local_id, label, duration_s, mapped_voice_id, created_at)
            VALUES (?, ?, ?, ?, NULL, ?)
            ON CONFLICT(source_id, local_id) DO UPDATE SET
                label = excluded.label,
                duration_s = excluded.duration_s
            """,
            (
                source_id,
                local_id,
                str(speaker.get("label") or local_id),
                float(speaker.get("duration_s") or 0.0),
                _now(),
            ),
        )
    store.commit()
    return analysis_id


def speaker_rows(store: ArtifactStore, source_id: str) -> list[Any]:
    return store.execute(
        "SELECT * FROM source_speakers WHERE source_id = ? ORDER BY rowid ASC", (source_id,)
    ).fetchall()


def map_speaker(store: ArtifactStore, source_id: str, local_id: str, voice_id: str | None) -> None:
    """Bind a source-local speaker to a voice. `None` unmaps it."""
    row = store.execute(
        "SELECT 1 FROM source_speakers WHERE source_id = ? AND local_id = ?", (source_id, local_id)
    ).fetchone()
    if row is None:
        raise LookupError(local_id)
    store.execute(
        "UPDATE source_speakers SET mapped_voice_id = ? WHERE source_id = ? AND local_id = ?",
        (voice_id, source_id, local_id),
    )
    store.commit()


def clip_rows(store: ArtifactStore, where: str, params: tuple[Any, ...]) -> list[Any]:
    return store.execute(
        f"""
        SELECT clips.*, media_sources.title AS source_title, media_sources.kind AS source_kind
        FROM clips JOIN media_sources ON media_sources.id = clips.source_id
        WHERE {where}
        ORDER BY clips.created_at ASC, clips.rowid ASC
        """,
        params,
    ).fetchall()


def clip_json(row: Any, *, with_source: bool = False) -> dict[str, Any]:
    clip = {
        "id": str(row["id"]),
        "source_id": str(row["source_id"]),
        "speaker_local_id": str(row["speaker_local_id"]),
        "voice_id": None if row["voice_id"] is None else str(row["voice_id"]),
        "ranges": _load(row["ranges_json"], []),
        "segments": _load(row["segments_json"], []),
        "audio_artifact_id": str(row["audio_artifact_id"]),
        "clean_transcript": str(row["clean_transcript"] or ""),
        "created_at": str(row["created_at"]),
    }
    if with_source:
        clip["source_title"] = str(row["source_title"])
        clip["source_kind"] = str(row["source_kind"])
    return clip


def clips_for_voice(store: ArtifactStore, voice_id: str) -> list[dict[str, Any]]:
    rows = clip_rows(store, "clips.voice_id = ?", (voice_id,))
    return [clip_json(row, with_source=True) for row in rows]


def clips_for_source(store: ArtifactStore, source_id: str) -> list[dict[str, Any]]:
    return [clip_json(row) for row in clip_rows(store, "clips.source_id = ?", (source_id,))]


def source_detail(store: ArtifactStore, source_id: str) -> dict[str, Any]:
    row = source_row(store, source_id)
    analyses = store.execute(
        "SELECT * FROM source_analyses WHERE source_id = ? ORDER BY created_at ASC, rowid ASC",
        (source_id,),
    ).fetchall()
    return {
        "id": str(row["id"]),
        "kind": str(row["kind"]),
        "origin": str(row["origin"]),
        "title": str(row["title"]),
        "audio_artifact_id": str(row["audio_artifact_id"]),
        "waveform_artifact_id": (
            None if row["waveform_artifact_id"] is None else str(row["waveform_artifact_id"])
        ),
        "duration_s": float(row["duration_s"] or 0.0),
        "meta": _load(row["meta_json"], {}),
        "created_at": str(row["created_at"]),
        "coverage": [
            {"start_s": iv.start_s, "end_s": iv.end_s} for iv in coverage(store, source_id)
        ],
        "analyses": [
            {
                "id": str(item["id"]),
                "start_s": float(item["start_s"]),
                "end_s": float(item["end_s"]),
                "processor": str(item["processor"]),
                "model_id": item["model_id"],
                "config": _load(item["config_json"], {}),
                "result": _load(item["result_json"], {}),
                "created_at": str(item["created_at"]),
            }
            for item in analyses
        ],
        "speakers": [
            {
                "local_id": str(item["local_id"]),
                "label": str(item["label"]),
                "duration_s": float(item["duration_s"] or 0.0),
                "mapped_voice_id": (
                    None if item["mapped_voice_id"] is None else str(item["mapped_voice_id"])
                ),
            }
            for item in speaker_rows(store, source_id)
        ],
        "clips": clips_for_source(store, source_id),
    }


def select_ranges(ranges: Iterable[Interval], duration_s: float) -> list[Interval]:
    """Validate the operator's clip ranges. Overlap is refused, never merged."""
    ordered = sorted(ranges, key=lambda iv: (iv.start_s, iv.end_s))
    if not ordered:
        raise ValueError("at least one range is required")
    for left, right in zip(ordered, ordered[1:]):
        if right.start_s < left.end_s - _EPS:
            raise OverlappingRanges(
                f"ranges overlap at {right.start_s:.3f}s: {left.start_s}-{left.end_s} and "
                f"{right.start_s}-{right.end_s}"
            )
    for iv in ordered:
        if iv.end_s > duration_s + _EPS:
            raise ValueError(f"range {iv.start_s}-{iv.end_s} exceeds the source ({duration_s:.3f}s)")
    return ordered


def selected_segments(
    store: ArtifactStore, source_id: str, ranges: Sequence[Interval]
) -> list[dict[str, Any]]:
    """Every analyzed turn inside `ranges`, in source-timeline order."""
    chosen: list[dict[str, Any]] = []
    for row in store.execute(
        "SELECT result_json FROM source_analyses WHERE source_id = ? ORDER BY created_at ASC, rowid ASC",
        (source_id,),
    ).fetchall():
        result = _load(row["result_json"], {})
        for seg in result.get("segments") or []:
            start, end = float(seg["start_s"]), float(seg["end_s"])
            if any(iv.start_s - _EPS <= start and end <= iv.end_s + _EPS for iv in ranges):
                chosen.append(seg)
    chosen.sort(key=lambda seg: (float(seg["start_s"]), float(seg["end_s"]), str(seg["speaker_id"])))
    return chosen


def clean_transcript(segments: Iterable[dict[str, Any]]) -> str:
    """Selected turns as plain prose: no diarization markup reaches Breeze."""
    parts: list[str] = []
    for seg in segments:
        text = " ".join(_SPEAKER_MARKUP.sub(" ", str(seg.get("text") or "")).split())
        if text:
            parts.append(text)
    return " ".join(parts)


def extract_clip(
    store: ArtifactStore, source_id: str, speaker_local_id: str, ranges: Iterable[Interval]
) -> dict[str, Any]:
    """Crop the selected turns for one speaker and attach the clip to its voice.

    A mapped speaker also enrols the clip: one immutable voice source plus its
    ORIGINAL lineage root. The voice's source cap is checked before any audio is
    materialized, so a rejected enrolment writes nothing at all.
    """
    source = source_row(store, source_id)
    duration_s = source_duration_s(store, source_id)
    ordered = select_ranges(ranges, duration_s)
    speaker = store.execute(
        "SELECT mapped_voice_id FROM source_speakers WHERE source_id = ? AND local_id = ?",
        (source_id, speaker_local_id),
    ).fetchone()
    if speaker is None:
        raise LookupError(speaker_local_id)
    voice_id = None if speaker["mapped_voice_id"] is None else str(speaker["mapped_voice_id"])
    limit = None
    if voice_id is not None:
        limit = voice_source_limit(store, voice_id)
        if source_count(store, voice_id) >= limit:
            raise SourceLimitReached(limit)
    segments = selected_segments(store, source_id, ordered)
    transcript = clean_transcript(segments)
    tmp = store.root / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    crop = tmp / f"clip-{new_id('cl')}.wav"
    materialize_keep_wav(store.get(str(source["audio_artifact_id"])).path, ordered, crop)
    try:
        audio = store.import_audio(crop)
    finally:
        crop.unlink(missing_ok=True)
    clip_id = new_id("clip")
    store.execute(
        """
        INSERT INTO clips (
            id, source_id, speaker_local_id, voice_id, ranges_json, segments_json,
            audio_artifact_id, clean_transcript, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            clip_id,
            source_id,
            speaker_local_id,
            voice_id,
            json.dumps([iv.model_dump() for iv in ordered]),
            json.dumps(segments),
            audio.id,
            transcript,
            _now(),
        ),
    )
    store.pin(audio.id, reason=f"clip:{clip_id}")
    store.commit()
    row = clip_rows(store, "clips.id = ?", (clip_id,))[0]
    clip = clip_json(row)
    clip["voice_source_id"] = None
    clip["voice_artifact_id"] = None
    if voice_id is None:
        return clip
    whole_clip = [Interval(start_s=0.0, end_s=max(float(audio.duration_s or 0.0), 1e-3))]
    enrolled = add_voice_source(
        store,
        voice_id,
        artifact_id=audio.id,
        label=str(source["title"]),
        transcript=transcript,
        keep=whole_clip,
    )
    clip["voice_source_id"] = enrolled
    clip["voice_artifact_id"] = ensure_source_original_artifact(store, voice_id, enrolled)
    store.commit()
    return clip
