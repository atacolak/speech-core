"""not on the voicecat path.

Warmup, n=30 protocol, and graph-stop rules for sc-breeze-hybrid-81p.
"""

from __future__ import annotations

UNSAFE_PEAK_GIB = 9.0  # hard ceiling; in-envelope is peak <= 9.0; overflow is peak > 9.0

_WARMUP_BUCKET = 32
_CLONE_PROMPT = (
    "And so, my fellow Americans, ask not what your country can do for you, "
    "ask what you can do for your country."
)
_CLONE_AUDIO_S_MAX = 15.0
_CODEC_HZ = 12.5
_WARMUP_INSTRUCTION = "Speak clearly and naturally."


def _round_up_32(n: int) -> int:
    n = max(int(n), 1)
    return ((n + _WARMUP_BUCKET - 1) // _WARMUP_BUCKET) * _WARMUP_BUCKET


def _text_token_guess(text: str) -> int:
    # Conservative stand-in: 2 tokens/word plus specials, rounded to 32.
    return _round_up_32(len(text.split()) * 2 + 16)


def voicecat_warmup_profile_dict(*, fast_codec: bool = False) -> dict:
    """Minimal VoiceCat CUDA-graph warmup profile. Never loads stock fast.json."""
    from .utterances import MEDIUM, SHORT, TINY

    texts = [*TINY, SHORT[0], MEDIUM[0], _CLONE_PROMPT, _WARMUP_INSTRUCTION]
    token_lengths = sorted({_text_token_guess(text) for text in texts})
    clone_audio = _round_up_32(int(_CLONE_AUDIO_S_MAX * _CODEC_HZ))
    prefill_lengths = sorted(
        {clone_audio} | {_round_up_32(clone_audio + length) for length in token_lengths}
    )
    return {
        "schema_version": 1,
        "name": "voicecat-tiny-short-medium",
        "service": {
            "concurrency": 1,
            "cfg_scales": [1.0, 4.0],
            "freeze_after_warmup": True,
        },
        "stages": {
            "text_encoder": {
                "graphs": [
                    {"batch_size": batch, "token_length": length}
                    for batch in (1, 2)
                    for length in token_lengths
                ]
            },
            "backbone_prefill": {
                "graphs": [
                    {"branch_batch_size": batch, "sequence_length": length}
                    for batch in (1, 2)
                    for length in prefill_lengths
                ]
            },
            "backbone_decode": {
                "graphs": [
                    {"branch_batch_size": 1},
                    {"branch_batch_size": 2},
                ]
            },
            "depth_decoder": {
                "graphs": [
                    {"batch_size": 1},
                    {"batch_size": 2},
                ]
            },
            "codec": {
                "graphs": [
                    {
                        "num_lanes": 1,
                        "chunk_frames": 1 if fast_codec else 2,
                    }
                ]
            },
        },
        "warmup_request": {
            "template": "ref_edit_tata",
            "text": "yeah.",
            "instruction": _WARMUP_INSTRUCTION,
            "speaker": "S0",
            "seed": 42,
        },
    }




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


_B_FLAG_ORDER: tuple[tuple[str, str], ...] = (
    ("B_depth", "depth"),
    ("B_codec", "codec"),
    ("B_backbone_decode", "backbone_decode"),
    ("B_backbone_prefill", "backbone_prefill"),
)


def compose_b_winner(arms: dict) -> dict:
    """Compose independent B fast-stage arms onto A.

    Starts from A and tries flags in order depth, codec, backbone_decode,
    backbone_prefill. Each independent arm is proposed as `curr` against A
    (`prev`), because arms were measured with exactly one fast flag — not as
    a stacked ladder. Arms that would exceed the 9.0 GiB hard ceiling are
    excluded. Prefill/jitter/latency stop rules reuse should_stop_adding_graphs.
    """

    baseline = dict(arms["A"])
    winner = {
        "name": "B_winner",
        "p50_ttfa_s": baseline.get("p50_ttfa_s"),
        "gap_p95_s": baseline.get("gap_p95_s"),
        "peak_allocated_gib": float(baseline.get("peak_allocated_gib") or 0.0),
        "fast": list(baseline.get("fast") or []),
        "excluded": [],
    }
    if (
        winner["p50_ttfa_s"] is None
        or winner["gap_p95_s"] is None
        or would_exceed_hard_ceiling(winner["peak_allocated_gib"])
        or baseline.get("unsafe_vram")
    ):
        winner["excluded"].append({"name": "A", "reason": "unsafe_vram"})
        winner["p50_ttfa_s"] = None
        winner["gap_p95_s"] = None
        winner["fast"] = []
        winner["peak_allocated_gib"] = 0.0
        return winner
    winner["p50_ttfa_s"] = float(winner["p50_ttfa_s"])
    winner["gap_p95_s"] = float(winner["gap_p95_s"])
    prev = {
        "p50_ttfa_s": winner["p50_ttfa_s"],
        "gap_p95_s": winner["gap_p95_s"],
        "peak_allocated_gib": winner["peak_allocated_gib"],
    }

    for arm_name, flag in _B_FLAG_ORDER:
        arm = arms.get(arm_name)
        if not arm:
            winner["excluded"].append({"name": arm_name, "reason": "missing"})
            continue
        peak = float(arm.get("peak_allocated_gib", 0.0))
        if would_exceed_hard_ceiling(peak) or arm.get("unsafe_vram"):
            winner["excluded"].append({"name": arm_name, "reason": "unsafe_vram"})
            continue
        if arm.get("p50_ttfa_s") is None or arm.get("gap_p95_s") is None:
            winner["excluded"].append({"name": arm_name, "reason": "not-run"})
            continue
        curr = {
            "p50_ttfa_s": float(arm["p50_ttfa_s"]),
            "gap_p95_s": float(arm["gap_p95_s"]),
            "peak_allocated_gib": peak,
            "init_unreasonable": bool(arm.get("init_unreasonable")),
            "correctness_changed": bool(arm.get("correctness_changed")),
        }
        stop, reason = should_stop_adding_graphs(prev, curr)
        if stop:
            winner["excluded"].append({"name": arm_name, "reason": reason})
            continue
        if flag not in winner["fast"]:
            winner["fast"].append(flag)
        winner["peak_allocated_gib"] = max(winner["peak_allocated_gib"], peak)
    return winner


_E_LADDER_NAMES: tuple[str, ...] = ("E1", "E2", "E3", "E4", "E5")


def compose_e_winner(rows: dict) -> dict | None:
    """Pick the executed in-envelope E row with lowest p50 TTFA."""
    best: dict | None = None
    best_ttfa: float | None = None
    for name in _E_LADDER_NAMES:
        row = rows.get(name)
        if not isinstance(row, dict):
            continue
        if row.get("not_run") or row.get("unsafe_vram"):
            continue
        peak = row.get("peak_allocated_gib")
        if peak is None or would_exceed_hard_ceiling(float(peak)):
            continue
        ttfa = row.get("p50_ttfa_s")
        if ttfa is None:
            continue
        ttfa_f = float(ttfa)
        if best is None or ttfa_f < best_ttfa:
            best = dict(row)
            best_ttfa = ttfa_f
    return best



