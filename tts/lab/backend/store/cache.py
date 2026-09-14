"""Canonical processor cache identity. not on the voicecat path.

A processor key covers the source bytes, the keep intervals it ran on, and the
processor's own identity. An analysis key covers source bytes and processor
identity only: diarization always reads the whole source.

An AuK key also covers candidate provenance (`AUK_PROVENANCE_FIELDS`): the same
source and keep rendered with a different task, instruction, pin, seed or
sampling settings is a different candidate.
"""

from __future__ import annotations

from typing import Any

from tts.hashes import sha256_json
from tts.lab.backend.models import Interval

AUK_PROCESSOR = "auk"

# The `kind` of an AuK candidate is also its processor name. Its provenance is
# the processor config (services/candidates.py), so these are the fields the key
# forks on beyond source bytes and keep.
AUK_PROVENANCE_FIELDS: tuple[str, ...] = (
    "parent_variant_id",
    "auk_task",
    "instruction",
    "model_variant",
    "auk_precision",
    "encoder_precision",
    "seed",
    "settings",
)


def canonical_keep_intervals(intervals: list[Interval] | list[dict[str, Any]]) -> list[dict[str, float]]:
    parsed: list[Interval] = []
    for raw in intervals:
        parsed.append(raw if isinstance(raw, Interval) else Interval.model_validate(raw))
    parsed.sort(key=lambda iv: (iv.start_s, iv.end_s))
    return [{"start_s": iv.start_s, "end_s": iv.end_s} for iv in parsed]


def canonical_processor_config(processor: str, config: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = dict(config or {})
    canonical = {
        "processor": processor,
        "checkpoint": str(payload.get("checkpoint") or ""),
        "config": payload.get("config") or {},
        "preprocess_version": str(payload.get("preprocess_version") or ""),
        "model_id": str(payload.get("model_id") or ""),
        "model_revision": str(payload.get("model_revision") or ""),
    }
    if processor == AUK_PROCESSOR:
        # Only AuK forks on provenance: resemble and vibevoice keys keep their
        # existing shape (and so their existing hashes).
        canonical["provenance"] = {field: payload.get(field) for field in AUK_PROVENANCE_FIELDS}
    return canonical


def processor_cache_key(
    processor: str,
    source_sha256: str,
    keep_intervals: list[Interval] | list[dict[str, Any]],
    processor_config: dict[str, Any] | None = None,
) -> str:
    return sha256_json(
        {
            "source_sha256": source_sha256,
            "keep_intervals": canonical_keep_intervals(keep_intervals),
            **canonical_processor_config(processor, processor_config),
        }
    )


def analysis_cache_key(
    source_sha256: str,
    processor: str,
    processor_config: dict[str, Any] | None = None,
) -> str:
    return sha256_json(
        {
            "source_sha256": source_sha256,
            **canonical_processor_config(processor, processor_config),
        }
    )
