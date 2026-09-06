"""not on the voicecat path.

Warmup, n=30 protocol, and graph-stop rules for sc-breeze-hybrid-81p.
"""

from __future__ import annotations

UNSAFE_PEAK_GIB = 9.0  # hard ceiling; in-envelope is peak <= 9.0; overflow is peak > 9.0


def would_exceed_hard_ceiling(peak_allocated_gib: float) -> bool:
    return float(peak_allocated_gib) > UNSAFE_PEAK_GIB


def should_run_measured_protocol(peak_allocated_gib: float) -> bool:
    """Do not run or optimize a stage whose peak would exceed 9 GB."""
    return not would_exceed_hard_ceiling(peak_allocated_gib)


def should_stop_adding_graphs(prev: dict, curr: dict) -> tuple[bool, str | None]:
    if curr.get("init_unreasonable"):
        return True, "init_unreasonable"
    if curr.get("correctness_changed"):
        return True, "correctness_changed"
    if would_exceed_hard_ceiling(curr.get("peak_allocated_gib", 0.0)):
        return True, "unsafe_vram"
    if float(curr["p50_ttfa_s"]) >= float(prev["p50_ttfa_s"]):
        return True, "latency_no_improve"
    if float(curr["gap_p95_s"]) > float(prev["gap_p95_s"]):
        return True, "jitter_worse"
    return False, None


def measured_plan() -> dict[str, int]:
    return {"warmup": 3, "tiny": 30, "short": 30, "medium": 3, "long": 3}


def tiny_rotation() -> list[str]:
    # 10 of each tiny text, fixed order, 30 total
    from .utterances import TINY

    return [TINY[i % 3] for i in range(30)]
