"""Canonical keep-interval math. not on the voicecat path.

Intervals are on the original source timeline. Exclude/keep-only never
shift later coordinates. Sequential EditSpec ops stay in tts.edits for
the Gradio rollback path.
"""

from __future__ import annotations

from tts.lab.backend.models import Interval

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
