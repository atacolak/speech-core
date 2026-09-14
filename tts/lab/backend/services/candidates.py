"""AuK candidate rows and the voice artifact table. not on the voicecat path.

An AuK run renders the voice's current keep into a new artifact and records it
here as a `kind=auk` reference variant: a lineage-bearing candidate, never an
overwrite of the source or of the original variant row.

`voice_artifacts` is the voice-lab table these rows are being migrated onto: one
row per experiment or approved reference, keyed by the same id as its
`reference_variants` counterpart while the legacy table is still dual-written.
`original` variants are not artifacts: that audio is a `voice_sources` row
(services/voices.py). Roles are the profile's vocabulary; `kind` is provenance.

The provenance columns are the one source of truth for an AuK candidate. Its
`processor_config` *is* that provenance (`AUK_PROVENANCE_FIELDS` in store.cache),
which is why `processor_config_json` stays empty for `kind=auk` and the cache
key is recomputed from the columns on read.

The AuK runtime that fills these rows lands later; this module is the store
contract it writes through.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from tts.lab.backend.models import Interval
from tts.lab.backend.services.voices import primary_source_row
from tts.lab.backend.store.artifacts import ArtifactStore
from tts.lab.backend.store.cache import (
    AUK_PROCESSOR,
    AUK_PROVENANCE_FIELDS,
    processor_cache_key,
)
from tts.packets import new_id

ORIGINAL_KIND = "original"
EXPERIMENT = "experiment"
REFERENCE = "reference"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def auk_processor_config(provenance: dict[str, Any]) -> dict[str, Any]:
    """The processor config an AuK candidate's cache key hashes."""
    return {field: provenance.get(field) for field in AUK_PROVENANCE_FIELDS}


def auk_provenance_from_row(row: Any) -> dict[str, Any]:
    """A stored variant or artifact row's provenance, as the API hands it out.

    Variants carry the edge as `parent_variant_id`, artifacts as `parent_id`.
    """
    keys = row.keys()
    return {
        "parent_variant_id": row["parent_variant_id"] if "parent_variant_id" in keys else row["parent_id"],
        "auk_task": row["auk_task"],
        "instruction": row["instruction"],
        "model_variant": row["model_variant"],
        "auk_precision": row["auk_precision"],
        "encoder_precision": row["encoder_precision"],
        "seed": row["seed"],
        "settings": json.loads(row["settings_json"] or "null"),
    }


def set_default_reference(store: ArtifactStore, voice_id: str, variant_id: str | None) -> None:
    """Point the profile's default at an approved artifact, mirroring `is_default`.

    The active variant may be the source audio (`original`), which is not an
    artifact: then the profile has no default reference at all.
    """
    target = None
    if variant_id is not None:
        target = store.execute(
            "SELECT id, role FROM voice_artifacts WHERE id = ? AND voice_id = ?",
            (variant_id, voice_id),
        ).fetchone()
    chosen = None if target is None or target["role"] != REFERENCE else target["id"]
    store.execute("UPDATE voices SET default_reference_id = ? WHERE id = ?", (chosen, voice_id))
    store.execute(
        "UPDATE voice_artifacts SET is_default = CASE WHEN id = ? THEN 1 ELSE 0 END WHERE voice_id = ?",
        (chosen, voice_id),
    )


def record_voice_artifact(
    store: ArtifactStore,
    voice_id: str,
    *,
    artifact_id: str,
    role: str,
    kind: str,
    name: str,
    audio_artifact_id: str,
    parent_id: str | None = None,
    source_id: str | None = None,
    keep_intervals: list[Interval] | None = None,
    processor_config: dict[str, Any] | None = None,
    processor_cache_key: str | None = None,
    provenance: dict[str, Any] | None = None,
    created_at: str | None = None,
) -> str:
    """Upsert one voice artifact and return its id.

    `artifact_id` is the lineage key: a processor that re-renders the same row
    reuses it, so identity, role, approvals and tags survive while the audio and
    provenance are refreshed. Never touches the source audio.
    """
    if role not in (EXPERIMENT, REFERENCE):
        raise ValueError(f"unknown artifact role: {role}")
    fields = provenance or {}
    settings = fields.get("settings")
    values = (
        name,
        audio_artifact_id,
        None if keep_intervals is None else json.dumps([iv.model_dump() for iv in keep_intervals]),
        None if processor_config is None else json.dumps(processor_config),
        processor_cache_key,
        fields.get("auk_task"),
        fields.get("instruction"),
        fields.get("model_variant"),
        fields.get("auk_precision"),
        fields.get("encoder_precision"),
        fields.get("seed"),
        None if settings is None else json.dumps(settings),
    )
    exists = store.execute(
        "SELECT 1 FROM voice_artifacts WHERE id = ? AND voice_id = ?", (artifact_id, voice_id)
    ).fetchone()
    if exists:
        store.execute(
            """
            UPDATE voice_artifacts SET
                name = ?, audio_artifact_id = ?, keep_intervals_json = ?,
                processor_config_json = ?, processor_cache_key = ?, auk_task = ?,
                instruction = ?, model_variant = ?, auk_precision = ?,
                encoder_precision = ?, seed = ?, settings_json = ?
            WHERE id = ?
            """,
            (*values, artifact_id),
        )
        return artifact_id
    store.execute(
        """
        INSERT INTO voice_artifacts (
            id, voice_id, role, kind, name, audio_artifact_id, keep_intervals_json,
            processor_config_json, processor_cache_key, auk_task, instruction,
            model_variant, auk_precision, encoder_precision, seed, settings_json,
            parent_id, source_id, approved_at, is_default, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            artifact_id,
            voice_id,
            role,
            kind,
            *values,
            parent_id,
            source_id,
            None,
            0,
            created_at or _now(),
        ),
    )
    return artifact_id


def record_auk_candidate(
    store: ArtifactStore,
    voice_id: str,
    *,
    audio_artifact_id: str,
    auk_task: str,
    instruction: str,
    model_variant: str,
    auk_precision: str,
    encoder_precision: str,
    seed: int | None = None,
    settings: dict[str, Any] | None = None,
    parent_variant_id: str | None = None,
) -> str:
    """Insert a `kind=auk` candidate for `audio_artifact_id`. Returns its variant id.

    Source artifact and existing variant rows are read, never written. The cache
    key covers the voice's current source bytes and keep plus the provenance, so
    a later keep change stales this row instead of deleting it.
    """
    voice = store.execute(
        "SELECT source_artifact_id, keep_intervals_json FROM voices WHERE id = ?", (voice_id,)
    ).fetchone()
    if voice is None:
        raise LookupError(f"voice not found: {voice_id}")
    parent = None
    if parent_variant_id is not None:
        parent = store.execute(
            "SELECT id, kind FROM reference_variants WHERE id = ? AND voice_id = ?",
            (parent_variant_id, voice_id),
        ).fetchone()
        if parent is None:
            raise LookupError(f"parent variant not found: {parent_variant_id}")
    artifact = store.get(audio_artifact_id)
    source = store.get(str(voice["source_artifact_id"]))
    keep = [Interval.model_validate(item) for item in json.loads(voice["keep_intervals_json"])]
    provenance = auk_processor_config(
        {
            "parent_variant_id": parent_variant_id,
            "auk_task": auk_task,
            "instruction": instruction,
            "model_variant": model_variant,
            "auk_precision": auk_precision,
            "encoder_precision": encoder_precision,
            "seed": seed,
            "settings": settings,
        }
    )
    cache_key = processor_cache_key(AUK_PROCESSOR, source.sha256, keep, provenance)
    variant_id = new_id("rv")
    store.execute(
        """
        INSERT INTO reference_variants (
            id, voice_id, kind, audio_artifact_id, processor_config_json,
            processor_cache_key, duration_s, pinned, created_at,
            parent_variant_id, auk_task, instruction, model_variant,
            auk_precision, encoder_precision, seed, settings_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            variant_id,
            voice_id,
            AUK_PROCESSOR,
            artifact.id,
            None,
            cache_key,
            float(artifact.duration_s or 0.0),
            0,
            _now(),
            provenance["parent_variant_id"],
            provenance["auk_task"],
            provenance["instruction"],
            provenance["model_variant"],
            provenance["auk_precision"],
            provenance["encoder_precision"],
            provenance["seed"],
            None if settings is None else json.dumps(settings),
        ),
    )
    store.pin(artifact.id, reason=f"voice:{voice_id}:{AUK_PROCESSOR}")
    source_row = primary_source_row(
        store, voice_id, legacy_artifact_id=str(voice["source_artifact_id"])
    )
    record_voice_artifact(
        store,
        voice_id,
        artifact_id=variant_id,
        role=EXPERIMENT,
        kind=AUK_PROCESSOR,
        name=f"AuK {provenance['auk_task']}",
        audio_artifact_id=artifact.id,
        # A candidate rendered from the source itself has no artifact parent:
        # the source row is its lineage.
        parent_id=None if parent is None or parent["kind"] == ORIGINAL_KIND else parent["id"],
        source_id=None if source_row is None else source_row["id"],
        keep_intervals=keep,
        processor_cache_key=cache_key,
        provenance=provenance,
    )
    store.commit()
    return variant_id


def set_candidate_approved(
    store: ArtifactStore,
    voice_id: str,
    variant_id: str,
    *,
    approved: bool = True,
) -> None:
    """Mark a candidate as reference-bank eligible. Audio and provenance untouched."""
    cursor = store.execute(
        "UPDATE reference_variants SET approved = ? WHERE id = ? AND voice_id = ?",
        (1 if approved else 0, variant_id, voice_id),
    )
    if cursor.rowcount == 0:
        raise LookupError(f"reference variant not found: {variant_id}")
    store.execute(
        "UPDATE voice_artifacts SET role = ?, approved_at = ? WHERE id = ? AND voice_id = ?",
        (REFERENCE if approved else EXPERIMENT, _now() if approved else None, variant_id, voice_id),
    )
    if not approved:
        # A demoted row cannot stay the profile's default.
        store.execute(
            "UPDATE voices SET default_reference_id = NULL WHERE id = ? AND default_reference_id = ?",
            (voice_id, variant_id),
        )
        store.execute(
            "UPDATE voice_artifacts SET is_default = 0 WHERE id = ? AND voice_id = ?",
            (variant_id, voice_id),
        )
    store.commit()
