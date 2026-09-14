"""Decode imported audio to a canonical working WAV. not on the voicecat path."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from tts.wav import is_riff_wav

ACCEPT_SUFFIXES = {".wav", ".mp3", ".flac", ".m4a", ".ogg", ".aac", ".opus", ".webm", ".wma"}
WORKING_RATE = 24000


def suffix_of(name: str) -> str:
    return Path(name).suffix.lower() or ".bin"


def decode_to_wav(src: Path | str, dest: Path | str, *, sample_rate: int = WORKING_RATE) -> Path:
    source = Path(src)
    out = Path(dest)
    out.parent.mkdir(parents=True, exist_ok=True)
    if is_riff_wav(source) and sample_rate <= 0:
        if Path(source) != out:
            shutil.copy2(source, out)
        return out
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required to import non-WAV audio")
    result = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-i",
            str(source),
            "-ac",
            "1",
            "-ar",
            str(int(sample_rate)),
            "-c:a",
            "pcm_s16le",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not is_riff_wav(out):
        detail = (result.stderr or result.stdout or "ffmpeg failed").strip().splitlines()
        raise RuntimeError(detail[-1] if detail else "ffmpeg failed")
    return out
