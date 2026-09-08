"""not on the voicecat path.

Lossless wav helpers. Never confuse raw PCM with a .wav container.
"""

from __future__ import annotations

import wave
from pathlib import Path
from typing import Any

import numpy as np

PCM_DTYPE = np.dtype("<i2")


def as_mono_float(samples: Any) -> np.ndarray:
    arr = np.asarray(samples)
    if arr.size == 0:
        return arr.astype(np.float32).reshape(-1)
    if arr.dtype == np.int16:
        arr = arr.astype(np.float32) / 32767.0
    else:
        arr = arr.astype(np.float32)
        peak = float(np.max(np.abs(arr))) if arr.size else 0.0
        if peak > 1.5:
            arr = arr / 32767.0
    if arr.ndim == 2:
        if arr.shape[0] <= 8 and arr.shape[1] > arr.shape[0]:
            arr = arr.mean(axis=0)
        else:
            arr = arr.mean(axis=1)
    return arr.reshape(-1)


def as_pcm16(samples: Any) -> np.ndarray:
    mono = as_mono_float(samples)
    clipped = np.clip(mono, -1.0, 1.0)
    return (clipped * 32767.0).astype(PCM_DTYPE)


def write_wav(path: Path | str, sample_rate: int, samples: Any) -> Path:
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    rate = int(sample_rate)
    if rate <= 0:
        raise ValueError(f"invalid sample rate: {rate}")
    pcm = as_pcm16(samples)
    with wave.open(str(dest), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(rate)
        wav_file.writeframes(pcm.tobytes())
    if not is_riff_wav(dest):
        raise RuntimeError(f"wrote non-RIFF output: {dest}")
    return dest


def read_wav(path: Path | str) -> tuple[int, np.ndarray]:
    src = Path(path)
    if not src.is_file():
        raise ValueError(f"audio file missing: {src}")
    with wave.open(str(src), "rb") as wav_file:
        channels = wav_file.getnchannels()
        sw = wav_file.getsampwidth()
        sr = wav_file.getframerate()
        frames = wav_file.readframes(wav_file.getnframes())
    if sw != 2:
        raise ValueError(f"{src}: expected 16-bit PCM, got {sw * 8}-bit")
    samples = np.frombuffer(frames, dtype=PCM_DTYPE)
    if samples.size == 0:
        raise ValueError(f"{src}: empty wav")
    if channels > 1:
        samples = samples.reshape(-1, channels)
        samples = samples.mean(axis=1).astype(PCM_DTYPE)
    return int(sr), samples


def is_riff_wav(path: Path | str) -> bool:
    src = Path(path)
    if not src.is_file() or src.stat().st_size < 12:
        return False
    header = src.read_bytes()[:12]
    return header[:4] == b"RIFF" and header[8:12] == b"WAVE"


def crop_bounds(
    start_s: float | None | Any,
    end_s: float | None | Any,
) -> tuple[float | None, float | None]:
    """Treat unset and Gradio's 0/0 Number default as 'whole clip'."""

    def _opt(value: Any) -> float | None:
        if value in (None, "", False):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if number != number:
            return None
        return number

    start = _opt(start_s)
    end = _opt(end_s)
    if start is None and end is None:
        return None, None
    if (start or 0.0) == 0.0 and (end or 0.0) == 0.0:
        return None, None
    return start, end


def crop_seconds(
    sample_rate: int,
    samples: Any,
    start_s: float | None,
    end_s: float | None,
) -> np.ndarray:
    start_s, end_s = crop_bounds(start_s, end_s)
    mono = as_mono_float(samples)
    n = int(mono.size)
    start = 0 if start_s is None else max(0, int(float(start_s) * sample_rate))
    end = n if end_s is None else min(n, int(float(end_s) * sample_rate))
    if end <= start:
        raise ValueError("empty crop region")
    return mono[start:end]


def duration_s(sample_rate: int, samples: Any) -> float:
    return float(np.asarray(samples).size) / float(sample_rate)
