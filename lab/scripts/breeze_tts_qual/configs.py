"""EngineConfig table for sc-breeze-hybrid-81p. not on the voicecat path."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EngineConfig:
    name: str
    precision: str
    fast_depth_decoder: bool = False
    fast_codec: bool = False
    fast_backbone_prefill: bool = False
    fast_backbone_decode: bool = False
    fast_text_encoder: bool = False


def _cfg(
    name: str,
    precision: str,
    *,
    depth: bool = False,
    codec: bool = False,
    backbone_decode: bool = False,
    backbone_prefill: bool = False,
    text_encoder: bool = False,
) -> EngineConfig:
    return EngineConfig(
        name=name,
        precision=precision,
        fast_depth_decoder=depth,
        fast_codec=codec,
        fast_backbone_decode=backbone_decode,
        fast_backbone_prefill=backbone_prefill,
        fast_text_encoder=text_encoder,
    )


CONFIGS: tuple[EngineConfig, ...] = (
    _cfg("A", "bf16"),
    _cfg("B_depth", "bf16", depth=True),
    _cfg("B_codec", "bf16", codec=True),
    _cfg("B_backbone_decode", "bf16", backbone_decode=True),
    _cfg("B_backbone_prefill", "bf16", backbone_prefill=True),
    _cfg("C0", "hybrid_int8"),
    _cfg("C1", "hybrid_int8", depth=True),
    _cfg("C2", "hybrid_int8", depth=True, codec=True),
    _cfg("C3", "hybrid_int8", depth=True, codec=True, backbone_decode=True),
    _cfg("C4", "hybrid_int8", depth=True, codec=True, backbone_decode=True, backbone_prefill=True),
    _cfg("D", "full_int8"),
    _cfg("E1", "bf16", depth=True),
    _cfg("E2", "bf16", depth=True, codec=True),
    _cfg("E3", "bf16", depth=True, codec=True, backbone_decode=True),
    _cfg("E4", "bf16", depth=True, codec=True, backbone_decode=True, backbone_prefill=True),
    _cfg("E5", "bf16", depth=True, codec=True, backbone_decode=True, backbone_prefill=True, text_encoder=True),
)
