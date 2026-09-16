"""Voice profiles: import plus the voice-lab source store contract.

A voice is a profile: many immutable sources, then experiments and references.
This module owns `voice_sources`; `services/candidates.py` owns
`voice_artifacts`. During the migrate window the voice's 1:1 columns still get
written (old clients and the keep/transcribe paths read them), so the primary
source row mirrors them here and reads take the voice columns for that entry.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tts.lab.backend.audio import decode_to_wav, suffix_of
from tts.lab.backend.models import Interval
from tts.lab.backend.services.references import (
    normalize_keep_intervals,
    slice_transcript,
)
from tts.lab.backend.store.artifacts import ArtifactStore
from tts.packets import new_id
from tts.wav import duration_s, is_riff_wav, read_wav


def source_rows(store: ArtifactStore, voice_id: str) -> list[Any]:
    """The voice's sources, primary first, in insertion order."""
    return store.execute(
        "SELECT * FROM voice_sources WHERE voice_id = ? ORDER BY created_at ASC, rowid ASC",
        (voice_id,),
    ).fetchall()


def primary_source_row(store: ArtifactStore, voice_id: str, legacy_artifact_id: str | None = None):
    """The source the voice's legacy 1:1 columns describe. None when it has none."""
    rows = source_rows(store, voice_id)
    if not rows:
        return None
    if legacy_artifact_id:
        for row in rows:
            if row["artifact_id"] == legacy_artifact_id:
                return row
    return rows[0]


def source_count(store: ArtifactStore, voice_id: str) -> int:
    """Rows already enrolled on the voice. Ingest into the library never counts."""
    row = store.execute(
        "SELECT COUNT(*) AS n FROM voice_sources WHERE voice_id = ?", (voice_id,)
    ).fetchone()
    return 0 if row is None else int(row["n"])


def voice_source_limit(store: ArtifactStore, voice_id: str) -> int:
    """The voice's `source_limit`, clamped to the same 1–50 window as take_limit."""
    row = store.execute("SELECT source_limit FROM voices WHERE id = ?", (voice_id,)).fetchone()
    if row is None or "source_limit" not in row.keys() or row["source_limit"] is None:
        return 5
    try:
        value = int(row["source_limit"])
    except (TypeError, ValueError):
        return 5
    return max(1, min(50, value))


def _source_label(store: ArtifactStore, voice_id: str) -> str:
    return f"Source {source_count(store, voice_id) + 1}"


def _insert_source(
    store: ArtifactStore,
    voice_id: str,
    *,
    artifact_id: str,
    transcript: str,
    words: list[Any] | None,
    keep: list[Interval],
    created_at: str,
    label: str | None = None,
) -> str:
    source_id = new_id("vs")
    store.execute(
        """
        INSERT INTO voice_sources
            (id, voice_id, label, artifact_id, transcript, words_json,
             keep_intervals_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source_id,
            voice_id,
            (label or "").strip() or _source_label(store, voice_id),
            artifact_id,
            transcript or "",
            json.dumps(words) if words else None,
            json.dumps([iv.model_dump() for iv in keep]),
            created_at,
        ),
    )
    return source_id


def add_voice_source(
    store: ArtifactStore,
    voice_id: str,
    *,
    artifact_id: str,
    label: str | None = None,
    transcript: str = "",
    words: list[Any] | None = None,
    keep: list[dict[str, Any]] | list[Interval] | None = None,
) -> str:
    """Append a source to a voice profile. Returns its id.

    A source is immutable once written: the audio artifact is pinned and never
    overwritten, only referenced. `keep` defaults to the whole file.
    """
    voice = store.execute("SELECT id FROM voices WHERE id = ?", (voice_id,)).fetchone()
    if voice is None:
        raise LookupError(f"voice not found: {voice_id}")
    artifact = store.get(artifact_id)
    duration = float(artifact.duration_s or 0.0)
    intervals = normalize_keep_intervals(
        list(keep or []) or [Interval(start_s=0.0, end_s=max(duration, 1e-3))],
        max(duration, 1e-3),
    )
    source_id = _insert_source(
        store,
        voice_id,
        artifact_id=artifact.id,
        transcript=transcript,
        words=words,
        keep=intervals,
        created_at=_now(),
        label=label,
    )
    store.pin(artifact.id, reason=f"voice:{voice_id}:source")
    store.commit()
    return source_id


def sync_primary_source(store: ArtifactStore, voice_id: str) -> None:
    """Mirror the voice's single-source columns onto its primary source row.

    Call after the voice row is written and before commit: the transcript and
    keep write paths still write `voices` during the migrate window, and the
    source row must not drift behind it.
    """
    row = store.execute(
        """
        SELECT source_artifact_id, source_transcript, source_words_json, keep_intervals_json
        FROM voices WHERE id = ?
        """,
        (voice_id,),
    ).fetchone()
    if row is None:
        return
    source = primary_source_row(store, voice_id, legacy_artifact_id=row["source_artifact_id"])
    if source is None:
        return
    store.execute(
        """
        UPDATE voice_sources SET transcript=?, words_json=?, keep_intervals_json=?
        WHERE id = ?
        """,
        (
            row["source_transcript"] or "",
            row["source_words_json"],
            row["keep_intervals_json"],
            source["id"],
        ),
    )


def patch_voice_source(
    store: ArtifactStore,
    voice_id: str,
    source_id: str,
    *,
    transcript: str | None = None,
    label: str | None = None,
) -> None:
    """Write one enrolled source row. A manual transcript locks that source alone.

    The voice's 1:1 columns describe its primary source, so only a write to that
    source mirrors them; every other source is its own truth.
    """
    source = store.execute(
        "SELECT * FROM voice_sources WHERE id = ? AND voice_id = ?", (source_id, voice_id)
    ).fetchone()
    if source is None:
        raise LookupError(f"source not found: {source_id} on {voice_id}")
    voice = store.execute(
        "SELECT source_artifact_id FROM voices WHERE id = ?", (voice_id,)
    ).fetchone()
    if voice is None:
        raise LookupError(f"voice not found: {voice_id}")
    primary = primary_source_row(
        store, voice_id, legacy_artifact_id=str(voice["source_artifact_id"])
    )
    is_primary = primary is not None and str(primary["id"]) == source_id
    locked = int(source["transcript_locked"] or 0)
    if transcript is not None:
        locked = 1
    store.execute(
        "UPDATE voice_sources SET label=?, transcript=?, transcript_locked=? WHERE id = ?",
        (
            str(source["label"]) if label is None else label,
            str(source["transcript"] or "") if transcript is None else transcript,
            locked,
            source_id,
        ),
    )
    if is_primary and transcript is not None:
        store.execute(
            """
            UPDATE voices SET source_transcript=?, effective_transcript=?, transcript_locked=?,
                updated_at=?
            WHERE id = ?
            """,
            (transcript, transcript, locked, _now(), voice_id),
        )
    else:
        store.execute("UPDATE voices SET updated_at=? WHERE id=?", (_now(), voice_id))
    store.commit()


def transcribe_alignment(path: Path | str) -> dict[str, object]:
    """Parakeet CPU transcript + word times. Empty on missing tooling; never raises."""
    try:
        from breeze_tts_qual.transcribe import transcribe_audio

        result = transcribe_audio(path)
        words = result.get("words") if isinstance(result.get("words"), list) else []
        return {
            "text": str(result.get("text") or "").strip(),
            "words": words,
        }
    except Exception:
        return {"text": "", "words": []}


class VoiceImportError(ValueError):
    """Operator-facing import failure."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def import_voice_from_path(
    store: ArtifactStore,
    src: Path | str,
    *,
    name: str,
    transcript: str = "",
    tags: list[str] | None = None,
    notes: str | None = None,
    voice_id: str | None = None,
    variant_id: str | None = None,
) -> str:
    source = Path(src)
    if not source.is_file():
        raise VoiceImportError(f"audio is required: {source}")
    tmp = store.root / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    original = store.import_audio(source)
    suffix = suffix_of(source.name)
    if is_riff_wav(source):
        working = original
        original_format = "wav"
        wav_path: Path | None = None
    else:
        wav_path = tmp / f"working-{new_id('up')}.wav"
        try:
            decode_to_wav(source, wav_path)
        except RuntimeError as exc:
            wav_path.unlink(missing_ok=True)
            raise VoiceImportError(str(exc)) from exc
        working = store.import_audio(wav_path)
        original_format = suffix.lstrip(".") or "audio"
    try:
        if not is_riff_wav(working.path):
            raise VoiceImportError("could not decode audio to WAV")
        sr, samples = read_wav(working.path)
        duration = duration_s(sr, samples)
        keep = normalize_keep_intervals(
            [Interval(start_s=0.0, end_s=max(duration, 1e-3))], max(duration, 1e-3)
        )
        supplied = (transcript or "").strip()
        words: list[object] = []
        if supplied:
            transcript = supplied
        else:
            aligned = transcribe_alignment(working.path)
            transcript = str(aligned.get("text") or "").strip()
            words = list(aligned.get("words") or [])
            if words:
                transcript = slice_transcript(words, keep) or transcript
        voice_id = voice_id or new_id("vp")
        variant_id = variant_id or new_id("rv")
        store.pin(original.id, reason=f"voice:{voice_id}:original")
        store.pin(working.id, reason=f"voice:{voice_id}")
        created = _now()
        store.execute(
            """
            INSERT INTO voices (
                id, name, tags_json, source_artifact_id, source_transcript,
                keep_intervals_json, effective_transcript, active_reference_variant_id,
                original_artifact_id, original_format, notes, created_at, updated_at,
                source_words_json, transcript_locked
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                voice_id,
                name,
                json.dumps(list(tags or [])),
                working.id,
                transcript,
                json.dumps([iv.model_dump() for iv in keep]),
                transcript,
                variant_id,
                original.id,
                original_format,
                notes,
                created,
                created,
                json.dumps(words) if words else None,
                # An import records a source's origin; the lock is written by a
                # per-source edit and mirrored here for the primary source only.
                0,
            ),
        )
        store.execute(
            """
            INSERT INTO reference_variants (
                id, voice_id, kind, audio_artifact_id, processor_config_json,
                processor_cache_key, duration_s, pinned, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (variant_id, voice_id, "original", working.id, None, None, float(duration), 1, created),
        )
        _insert_source(
            store,
            voice_id,
            artifact_id=working.id,
            transcript=transcript,
            words=words,
            keep=keep,
            created_at=created,
        )
        store.commit()
        return voice_id
    finally:
        if wav_path is not None:
            wav_path.unlink(missing_ok=True)
