"""BreezeEngine facade. not on the voicecat path."""

from __future__ import annotations

import struct
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .configs import EngineConfig


@dataclass(frozen=True)
class PcmChunk:
    pcm: bytes
    sample_rate: int
    n_samples: int
    is_final: bool
    t_rel_s: float
    timing: dict = field(default_factory=dict)


class FakeBackend:
    sample_rate = 24000

    def synthesize(self, **kwargs: Any) -> Iterator[PcmChunk]:
        silent = struct.pack("<" + "h" * 200, *([0] * 200))
        audible = struct.pack("<" + "h" * 200, *([1000] * 200))
        yield PcmChunk(silent, 24000, 200, False, 0.050, {})
        yield PcmChunk(audible, 24000, 200, True, 0.120, {})

    def close(self) -> None:
        return None


class BreezeEngine:
    def __init__(
        self,
        config: EngineConfig,
        *,
        ckpt_dir: Path | str,
        device: str = "cuda:0",
        backend: Any | None = None,
    ) -> None:
        self.config = config
        self.ckpt_dir = Path(ckpt_dir)
        self.device = device
        self.backend = backend or FakeBackend()

    def synthesize(
        self,
        *,
        text: str,
        reference_audio: Path | str,
        reference_text: str,
        instruction: str = "Speak clearly and naturally.",
        seed: int = 42,
    ) -> Iterator[PcmChunk]:
        yield from self.backend.synthesize(
            text=text,
            reference_audio=reference_audio,
            reference_text=reference_text,
            instruction=instruction,
            seed=seed,
        )

    def close(self) -> None:
        close = getattr(self.backend, "close", None)
        if close is not None:
            close()
