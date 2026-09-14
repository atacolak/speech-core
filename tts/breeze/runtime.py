"""not on the voicecat path.

Selected Breeze runtime is E2. Legacy A/B/C/D/E1/E3+ stay in the
historical report, not on the normal synthesis surface.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

from breeze_tts_qual.configs import EngineConfig
from breeze_tts_qual.engine import BreezeEngine, OfficialBackend, PcmChunk, checkpoint_for_precision

from tts.generation import GenerationSettings
from tts.packets import UtterancePacket
from tts.paths import BREEZE_IMPLEMENTATION, BREEZE_PIN_COMMIT, SELECTED_RUNTIME, qual_root

E2_CONFIG = EngineConfig(
    name="E2",
    precision="bf16",
    fast_depth_decoder=True,
    fast_codec=True,
)


def selected_config() -> EngineConfig:
    return E2_CONFIG


def load_selected_engine(
    *,
    qual: Path | None = None,
    engine_factory: Any | None = None,
    on_progress: Any | None = None,
) -> BreezeEngine:
    root = Path(qual) if qual is not None else qual_root()
    if engine_factory is not None:
        return engine_factory(E2_CONFIG, qual_root=root)
    ckpt_dir = checkpoint_for_precision(root, E2_CONFIG.precision)
    backend = OfficialBackend(
        E2_CONFIG,
        device="cuda:0",
        qual_root=root,
        ckpt_dir=ckpt_dir,
        on_progress=on_progress,
    )
    return BreezeEngine(E2_CONFIG, ckpt_dir=ckpt_dir, backend=backend)


def synthesize(
    engine: BreezeEngine,
    *,
    packet: UtterancePacket,
    reference_audio: Path | str,
    reference_text: str,
    settings: GenerationSettings | None = None,
) -> Iterator[PcmChunk]:
    settings = settings or GenerationSettings()
    generation_config = None
    if settings.sampling_differs_from_e2_warmup() or settings.cfg_mode == "dual_experimental":
        generation_config = settings.to_breeze_generation_config()
    return engine.synthesize(
        text=packet.engine_text(),
        reference_audio=reference_audio,
        reference_text=reference_text,
        instruction=packet.steer,
        seed=settings.seed,
        cfg_scale=settings.cfg_scale,
        cfg_scale_ref=settings.cfg_scale_ref,
        cfg_scale_ins=settings.cfg_scale_ins,
        generation_config=generation_config,
    )


def runtime_record(settings: GenerationSettings) -> dict[str, Any]:
    return {
        "implementation": BREEZE_IMPLEMENTATION,
        "selected": SELECTED_RUNTIME,
        "breeze_commit": BREEZE_PIN_COMMIT,
        "fast": ["depth", "codec"],
        "precision": "bf16",
        "cfg_mode": settings.cfg_mode,
        "generation": settings.to_dict(),
        "dual_cfg_path": (
            "eager_generate" if settings.cfg_mode == "dual_experimental" else None
        ),
    }
