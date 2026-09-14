"""Benchmark clocks for sc-breeze-hybrid-81p. not on the voicecat path."""

from __future__ import annotations

import struct
from typing import Iterable, Optional

SILENCE_ABS_S16 = 512


def percentile(values: list[float], q: float) -> Optional[float]:
    if not values:
        return None
    xs = sorted(float(v) for v in values)
    if len(xs) == 1:
        return xs[0]
    k = (len(xs) - 1) * q
    f = int(k)
    c = min(f + 1, len(xs) - 1)
    return xs[f] * (c - k) + xs[c] * (k - f)


def rtf(*, wall_s: float, audio_s: float) -> Optional[float]:
    if audio_s <= 0:
        return None
    return wall_s / audio_s


def inter_chunk_gaps(chunks: list[dict]) -> list[float]:
    gaps: list[float] = []
    for prev, cur in zip(chunks, chunks[1:]):
        gaps.append(float(cur["t_rel_s"]) - (float(prev["t_rel_s"]) + float(prev["duration_s"])))
    return gaps


def first_nonsilent_s(
    chunks: Iterable[tuple[float, bytes]],
    *,
    sample_rate: int,
) -> tuple[Optional[float], Optional[int]]:
    """Return (time_s, global_sample_index) of first |s16| >= 512."""
    sample_index = 0
    for t_rel_s, pcm in chunks:
        n = len(pcm) // 2
        samples = struct.unpack("<" + "h" * n, pcm[: n * 2])
        for i, s in enumerate(samples):
            if abs(s) >= SILENCE_ABS_S16:
                return t_rel_s + (i / float(sample_rate)), sample_index + i
        sample_index += n
    return None, None


def vram_mb() -> dict[str, float]:
    try:
        import torch
    except ImportError:
        return {"available": 0.0}
    if not torch.cuda.is_available():
        return {"available": 0.0}
    free, total = torch.cuda.mem_get_info()
    return {
        "free_mb": free / (1024 * 1024),
        "total_mb": total / (1024 * 1024),
        "allocated_mb": torch.cuda.memory_allocated() / (1024 * 1024),
        "reserved_mb": torch.cuda.memory_reserved() / (1024 * 1024),
        "peak_allocated_mb": torch.cuda.max_memory_allocated() / (1024 * 1024),
        "peak_reserved_mb": torch.cuda.max_memory_reserved() / (1024 * 1024),
        "used_mb": (total - free) / (1024 * 1024),
    }


def bytes_to_gib(n_bytes: float) -> float:
    return float(n_bytes) / (1024.0 ** 3)
