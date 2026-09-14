"""Canonical keep-interval math. not on the voicecat path.

Intervals are on the original source timeline. Exclude/keep-only never
shift later coordinates. Sequential EditSpec ops stay in tts.edits for
the Gradio rollback path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tts.lab.backend.models import Interval
from tts.lab.backend.store.cache import processor_cache_key

_EPS = 1e-9


def _as_interval(value: Interval) -> Interval:
    return value if isinstance(value, Interval) else Interval.model_validate(value)


def normalize_keep_intervals(
    intervals: list[Interval],
    duration_s: float,
) -> list[Interval]:
    if duration_s <= 0:
        raise ValueError("duration must be positive")
    if not intervals:
        raise ValueError("keep intervals must not be empty")
    ordered: list[Interval] = []
    for raw in intervals:
        interval = _as_interval(raw)
        if interval.end_s > duration_s + _EPS:
            raise ValueError("end > duration")
        if interval.start_s > duration_s + _EPS:
            raise ValueError("end > duration")
        ordered.append(interval)
    ordered.sort(key=lambda iv: (iv.start_s, iv.end_s))
    merged: list[Interval] = [ordered[0]]
    for interval in ordered[1:]:
        prev = merged[-1]
        if interval.start_s <= prev.end_s + _EPS:
            merged[-1] = Interval(
                start_s=prev.start_s,
                end_s=max(prev.end_s, interval.end_s),
            )
        else:
            merged.append(interval)
    return merged


def exclude_interval(keep: list[Interval], excluded: Interval) -> list[Interval]:
    cut = _as_interval(excluded)
    if not keep:
        raise ValueError("keep intervals must not be empty")
    duration = max(max(iv.end_s for iv in keep), cut.end_s)
    pieces: list[Interval] = []
    for interval in keep:
        interval = _as_interval(interval)
        if cut.end_s <= interval.start_s + _EPS or cut.start_s >= interval.end_s - _EPS:
            pieces.append(interval)
            continue
        if interval.start_s < cut.start_s - _EPS:
            pieces.append(Interval(start_s=interval.start_s, end_s=cut.start_s))
        if interval.end_s > cut.end_s + _EPS:
            pieces.append(Interval(start_s=cut.end_s, end_s=interval.end_s))
    return normalize_keep_intervals(pieces, duration)


def keep_only_interval(selected: Interval, duration_s: float) -> list[Interval]:
    return normalize_keep_intervals([_as_interval(selected)], duration_s)


def exclude_intervals(keep: list[Interval], excluded: list[Interval]) -> list[Interval]:
    """Cut many sections. Each cut is on the original timeline; later cuts see earlier holes."""
    current = list(keep)
    for item in excluded:
        current = exclude_interval(current, item)
    return current


def materialize_keep_wav(
    source: Path | str,
    intervals: list[Interval],
    dest: Path | str,
) -> Path:
    from pathlib import Path as _Path

    import numpy as np

    from tts.wav import read_wav, write_wav

    src = _Path(source)
    out = _Path(dest)
    sr, samples = read_wav(src)
    pieces = []
    n = int(samples.size)
    for raw in normalize_keep_intervals(list(intervals), max(n / float(sr), 1e-3)):
        start = max(0, min(n, int(round(raw.start_s * sr))))
        end = max(0, min(n, int(round(raw.end_s * sr))))
        if end > start:
            pieces.append(samples[start:end])
    if not pieces:
        raise ValueError("effective reference is empty")
    write_wav(out, sr, np.concatenate(pieces))
    return out


def processed_variant_is_current(
    variant: dict[str, Any] | None,
    source_sha256: str,
    keep_intervals: list[Interval] | list[dict[str, Any]],
) -> bool:
    """True when a processed variant still matches the source bytes and keep.

    `original` is not a processed artifact: it reports False so callers
    materialize the keep crop from the source instead of cloning raw audio.
    """
    if not variant or variant.get("kind") in (None, "original"):
        return False
    stored = str(variant.get("processor_cache_key") or "")
    if not stored:
        return False
    expected = processor_cache_key(
        str(variant["kind"]),
        source_sha256,
        keep_intervals,
        variant.get("processor_config") or {},
    )
    return stored == expected


def keep_duration_s(intervals: list[Interval] | list[dict[str, float]]) -> float:
    total = 0.0
    for raw in intervals:
        interval = _as_interval(raw)
        total += max(0.0, interval.end_s - interval.start_s)
    return total


def _word_midpoint(word: dict[str, object]) -> float:
    start = float(word.get("start_s") or 0.0)
    end = float(word.get("end_s") or start)
    if end <= start:
        return start
    return 0.5 * (start + end)


def _word_in_keep(word: dict[str, object], keep: list[Interval]) -> bool:
    mid = _word_midpoint(word)
    for interval in keep:
        if interval.start_s - _EPS <= mid <= interval.end_s + _EPS:
            return True
    return False


def join_words(words: list[dict[str, object]]) -> str:
    parts: list[str] = []
    for word in words:
        text = str(word.get("text") or "")
        if not text:
            continue
        if parts and not text[:1].isspace() and not text.startswith(("'", ",", ".", "!", "?", ";", ":")):
            if not parts[-1].endswith((" ", "\n")):
                parts.append(" ")
        parts.append(text)
    return "".join(parts).strip()


def slice_transcript(
    words: list[dict[str, object]] | None,
    keep: list[Interval] | list[dict[str, float]],
) -> str:
    """Crop a cached source-aligned transcript to the keep windows.

    One ASR pass on the original file. Keep/exclude never re-runs Parakeet.
    """
    if not words:
        return ""
    windows = [_as_interval(item) for item in keep]
    kept = [word for word in words if _word_in_keep(word, windows)]
    return join_words(kept)

