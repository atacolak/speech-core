"""Resemble denoise-only reference cleanup. not on the voicecat path.

Denoise only, never `enhance()`: the enhancer regenerates the performance, and
the operator wants this take with less noise behind it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

import numpy as np

from tts.wav import as_mono_float, read_wav, write_wav

PROCESSOR = "resemble"
PROCESSOR_CONFIG: dict[str, Any] = {
    "checkpoint": "resemble-denoise",
    "preprocess_version": "v1",
}
DEVICE = "cuda"


class ProcessorUnavailable(RuntimeError):
    """Resemble weights or its torch stack are missing. Fail closed, never fake clean."""


class DenoiseFn(Protocol):
    """Mono float samples + rate + device in, denoised samples + rate out."""

    def __call__(self, dwav: np.ndarray, sr: int, device: str) -> tuple[np.ndarray, int]: ...


def default_denoise(dwav: np.ndarray, sr: int, device: str = DEVICE) -> tuple[np.ndarray, int]:
    """`resemble_enhance.enhancer.inference.denoise` — cleanup, not `enhance()`."""
    try:
        import torch
        from resemble_enhance.enhancer.inference import denoise
    except ImportError as exc:
        raise ProcessorUnavailable(f"resemble denoise is not installed: {exc}") from exc
    wav = torch.as_tensor(as_mono_float(dwav), dtype=torch.float32)
    denoised, out_sr = denoise(wav, int(sr), device)
    return np.asarray(denoised.detach().cpu(), dtype=np.float32), int(out_sr)


# Module hook. Tests swap in a fake denoiser: no GPU, no weights.
denoise_hook: DenoiseFn = default_denoise


def denoise_wav(
    src: Path | str,
    dest: Path | str,
    *,
    denoise_fn: DenoiseFn | None = None,
) -> Path:
    """Denoise the keep wav at `src` into `dest`. `src` is left untouched."""
    sr, samples = read_wav(src)
    denoised, out_sr = (denoise_fn or denoise_hook)(samples, sr, DEVICE)
    return write_wav(dest, int(out_sr), as_mono_float(denoised))
