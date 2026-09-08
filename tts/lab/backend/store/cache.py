"""Canonical stream.fm cache identity. not on the voicecat path."""

from __future__ import annotations

from typing import Any

from tts.hashes import sha256_json
from tts.lab.backend.models import Interval
from tts.preprocess import PREPROCESS_VERSION, STREAMFM_CHECKPOINT, STREAMFM_SOLVER, STREAMFM_TASK

PROCESSOR = "streamfm"


def canonical_keep_intervals(intervals: list[Interval] | list[dict[str, Any]]) -> list[dict[str, float]]:
    parsed: list[Interval] = []
    for raw in intervals:
        parsed.append(raw if isinstance(raw, Interval) else Interval.model_validate(raw))
    parsed.sort(key=lambda iv: (iv.start_s, iv.end_s))
    return [{"start_s": iv.start_s, "end_s": iv.end_s} for iv in parsed]


def canonical_processor_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = dict(config or {})
    return {
        "processor": PROCESSOR,
        "task": str(payload.get("task") or STREAMFM_TASK),
        "solver": str(payload.get("solver") or STREAMFM_SOLVER),
        "checkpoint": str(payload.get("checkpoint") or STREAMFM_CHECKPOINT),
        "config": payload.get("config") or {},
        "preprocess_version": str(payload.get("preprocess_version") or PREPROCESS_VERSION),
    }


def streamfm_cache_key(
    *,
    source_sha256: str,
    keep_intervals: list[Interval] | list[dict[str, Any]],
    processor_config: dict[str, Any] | None = None,
) -> str:
    return sha256_json(
        {
            "source_sha256": source_sha256,
            "keep_intervals": canonical_keep_intervals(keep_intervals),
            **canonical_processor_config(processor_config),
        }
    )
