"""not on the voicecat path.

Upstream Breeze generation defaults. Do not invent a slider zoo.
E2 graphs were warmed at these values; changing them is experimental.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

UPSTREAM_DEFAULTS = {
    "temperature": 0.9,
    "depth_temperature": 0.9,
    "do_sample": True,
    "top_p": 1.0,
    "top_k": 50,
    "max_new_tokens": 750,
    "repetition_penalty": 1.1,
    "cfg_scale": 1.0,
    "cfg_mode": "single",
}


@dataclass
class GenerationSettings:
    temperature: float = 0.9
    depth_temperature: float = 0.9
    do_sample: bool = True
    top_p: float = 1.0
    top_k: int = 50
    max_new_tokens: int = 750
    repetition_penalty: float = 1.1
    cfg_scale: float = 1.0
    cfg_scale_ref: float | None = None
    cfg_scale_ins: float | None = None
    seed: int = 42

    @property
    def cfg_mode(self) -> str:
        if self.cfg_scale_ref is not None and self.cfg_scale_ins is not None:
            return "dual_experimental"
        if float(self.cfg_scale) != 1.0:
            return "single"
        return "none_or_unity"

    def non_default(self) -> dict[str, Any]:
        current = self.to_dict()
        out = {}
        for key, default in UPSTREAM_DEFAULTS.items():
            if key in {"cfg_mode"}:
                continue
            if key not in current:
                continue
            if current[key] != default:
                out[key] = current[key]
        if self.cfg_scale_ref is not None:
            out["cfg_scale_ref"] = self.cfg_scale_ref
        if self.cfg_scale_ins is not None:
            out["cfg_scale_ins"] = self.cfg_scale_ins
        return out

    def sampling_differs_from_e2_warmup(self) -> bool:
        return any(
            getattr(self, key) != UPSTREAM_DEFAULTS[key]
            for key in (
                "temperature",
                "depth_temperature",
                "do_sample",
                "top_p",
                "top_k",
                "max_new_tokens",
                "repetition_penalty",
            )
        )

    def to_breeze_generation_config(self) -> dict[str, Any]:
        return {
            "depth_decoder_do_sample": self.do_sample,
            "depth_decoder_temperature": self.depth_temperature,
            "depth_decoder_top_p": self.top_p,
            "depth_decoder_top_k": self.top_k,
            "do_sample": self.do_sample,
            "top_p": self.top_p,
            "top_k": self.top_k,
            "max_new_tokens": self.max_new_tokens,
            "temperature": self.temperature,
        }

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["cfg_mode"] = self.cfg_mode
        payload["upstream_defaults"] = dict(UPSTREAM_DEFAULTS)
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "GenerationSettings":
        data = dict(data or {})
        data.pop("cfg_mode", None)
        data.pop("upstream_defaults", None)
        known = {k: data[k] for k in cls.__dataclass_fields__ if k in data}
        return cls(**known)
