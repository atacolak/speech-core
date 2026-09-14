"""not on the voicecat path.

Non-destructive reference edit list. Source bytes stay intact.
Ordered ops replay on the current effective buffer. Identity is
source hash + this spec, not a rewritten original file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from tts.wav import as_mono_float, crop_bounds

EDIT_VERSION = "tts-edits-v1"
VALID_OPS = {"exclude", "trim_start", "trim_end"}


@dataclass(frozen=True)
class EditOp:
    op: str
    start_s: float | None = None
    end_s: float | None = None

    def __post_init__(self) -> None:
        if self.op not in VALID_OPS:
            raise ValueError(f"unknown edit op: {self.op}")
        if self.op == "exclude":
            if self.start_s is None or self.end_s is None:
                raise ValueError("exclude needs start_s and end_s")
            if float(self.end_s) <= float(self.start_s):
                raise ValueError("empty crop region")
        if self.op == "trim_start" and self.start_s is None:
            raise ValueError("trim_start needs start_s")
        if self.op == "trim_end" and self.end_s is None:
            raise ValueError("trim_end needs end_s")

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"op": self.op}
        if self.start_s is not None:
            payload["start_s"] = float(self.start_s)
        if self.end_s is not None:
            payload["end_s"] = float(self.end_s)
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EditOp":
        return cls(
            op=str(data["op"]),
            start_s=data.get("start_s"),
            end_s=data.get("end_s"),
        )


@dataclass
class EditSpec:
    ops: list[EditOp] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"version": EDIT_VERSION, "ops": [op.to_dict() for op in self.ops]}

    def canonical(self) -> dict[str, Any]:
        return self.to_dict()

    def identity(self) -> dict[str, Any]:
        return self.canonical()

    @classmethod
    def from_dict(cls, data: dict[str, Any] | list[Any] | None) -> "EditSpec":
        if not data:
            return cls()
        if isinstance(data, list):
            return cls(ops=[EditOp.from_dict(item) for item in data])
        return cls(ops=[EditOp.from_dict(item) for item in data.get("ops") or []])

    def with_op(self, op: EditOp) -> "EditSpec":
        return EditSpec(ops=[*self.ops, op])

    def exclude(self, start_s: float, end_s: float) -> "EditSpec":
        return self.with_op(EditOp("exclude", start_s=float(start_s), end_s=float(end_s)))

    def keep(self, start_s: float | None, end_s: float | None) -> "EditSpec":
        """Keep only [start, end] on the current effective buffer.

        trim_end first so trim_start still refers to the pre-cut timeline.
        """
        start, end = crop_bounds(start_s, end_s)
        spec = self
        if end is not None:
            spec = spec.with_op(EditOp("trim_end", end_s=float(end)))
        if start is not None:
            spec = spec.with_op(EditOp("trim_start", start_s=float(start)))
        return spec

    def undo(self) -> "EditSpec":
        if not self.ops:
            return EditSpec()
        return EditSpec(ops=list(self.ops[:-1]))

    def clear(self) -> "EditSpec":
        return EditSpec()


def spec_from_region(start_s: float | None, end_s: float | None) -> EditSpec:
    return EditSpec().keep(start_s, end_s)


def apply_edits(sample_rate: int, samples: Any, spec: EditSpec | dict[str, Any] | None) -> np.ndarray:
    spec = spec if isinstance(spec, EditSpec) else EditSpec.from_dict(spec)
    mono = as_mono_float(samples)
    sr = int(sample_rate)
    if sr <= 0:
        raise ValueError(f"invalid sample rate: {sr}")
    for op in spec.ops:
        n = int(mono.size)
        if n == 0:
            raise ValueError("empty crop region")
        if op.op == "exclude":
            start = max(0, int(float(op.start_s) * sr))
            end = min(n, int(float(op.end_s) * sr))
            if end <= start:
                raise ValueError("empty crop region")
            mono = np.concatenate([mono[:start], mono[end:]])
        elif op.op == "trim_start":
            start = max(0, int(float(op.start_s) * sr))
            if start >= n:
                raise ValueError("empty crop region")
            mono = mono[start:]
        elif op.op == "trim_end":
            end = min(n, int(float(op.end_s) * sr))
            if end <= 0:
                raise ValueError("empty crop region")
            mono = mono[:end]
        else:
            raise ValueError(f"unknown edit op: {op.op}")
    if int(mono.size) == 0:
        raise ValueError("empty crop region")
    return mono


def transcript_status(source: str, effective: str, spec: EditSpec | None) -> str:
    spec = spec or EditSpec()
    source_text = (source or "").strip()
    effective_text = (effective or "").strip()
    if not spec.ops:
        return "match" if effective_text == source_text else "edited"
    if not effective_text:
        return "stale"
    if effective_text == source_text:
        return "stale"
    return "edited"
