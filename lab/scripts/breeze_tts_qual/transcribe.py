"""not on the voicecat path.

Run Parakeet TDT 0.6B v2 through the existing transcribe.cpp CLI.
CPU only — do not steal Breeze VRAM.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
import wave
from pathlib import Path
from typing import Any

DEFAULT_CLI = (
    Path.home()
    / "workspace"
    / "external"
    / "transcribe.cpp"
    / "build"
    / "bin"
    / "transcribe-cli"
)
DEFAULT_MODEL = (
    Path.home()
    / "workspace"
    / "external"
    / "transcribe.cpp"
    / "models"
    / "parakeet-tdt-0.6b-v2"
    / "parakeet-tdt-0.6b-v2-Q8_0.gguf"
)
TARGET_SR = 16000


def cli_path() -> Path:
    raw = os.environ.get("TRANSCRIBE_CLI", str(DEFAULT_CLI))
    return Path(raw).expanduser()


def model_path() -> Path:
    raw = os.environ.get("PARAKEET_GGUF", str(DEFAULT_MODEL))
    return Path(raw).expanduser()


def parse_cli_text(stdout: str) -> str:
    found = ""
    saw = False
    for line in stdout.splitlines():
        if line.startswith("text: "):
            saw = True
            payload = line[6:]
            found = "" if payload == "(empty)" else payload
    if not saw:
        raise RuntimeError("transcribe-cli printed no text: line")
    return found.strip()


_TS_LINE = re.compile(r"^\s*\[\s*([0-9.]+)\s*->\s*([0-9.]+)\]\s*(.*)$")


def parse_cli_alignment(stdout: str) -> dict[str, Any]:
    """Full text plus source-timeline words/segments from transcribe-cli."""
    text = parse_cli_text(stdout)
    words: list[dict[str, Any]] = []
    mode: str | None = None
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("words:"):
            mode = "words"
            words = []
            continue
        if stripped.startswith("segments:"):
            if mode == "words":
                continue
            mode = "segments"
            words = []
            continue
        if stripped.startswith("tokens:"):
            break
        if mode not in {"words", "segments"}:
            continue
        match = _TS_LINE.match(line)
        if match is None:
            continue
        words.append(
            {
                "text": match.group(3),
                "start_s": float(match.group(1)),
                "end_s": float(match.group(2)),
            }
        )
    return {"text": text, "words": words}


def _as_mono_float(samples: Any) -> Any:
    import numpy as np

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
        # Gradio may emit (n, ch) or (ch, n)
        if arr.shape[0] <= 8 and arr.shape[1] > arr.shape[0]:
            arr = arr.mean(axis=0)
        else:
            arr = arr.mean(axis=1)
    return arr.reshape(-1)


def resample_mono(samples: Any, src_sr: int, dst_sr: int = TARGET_SR) -> Any:
    import numpy as np

    mono = _as_mono_float(samples)
    src_sr = int(src_sr)
    if src_sr <= 0:
        raise ValueError(f"invalid sample rate: {src_sr}")
    if src_sr == dst_sr or mono.size == 0:
        return mono.astype(np.float32)
    n_dst = int(round(mono.size * dst_sr / src_sr))
    if n_dst <= 0:
        return mono[:0].astype(np.float32)
    x_old = np.linspace(0.0, 1.0, mono.size, endpoint=False)
    x_new = np.linspace(0.0, 1.0, n_dst, endpoint=False)
    return np.interp(x_new, x_old, mono).astype(np.float32)


def write_16k_mono_wav(audio: Any, dest: Path) -> Path:
    import numpy as np

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    sample_rate, samples = coerce_audio(audio)
    mono = resample_mono(samples, sample_rate, TARGET_SR)
    pcm = np.clip(mono, -1.0, 1.0)
    pcm_i16 = (pcm * 32767.0).astype("<i2")
    with wave.open(str(dest), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(TARGET_SR)
        wav_file.writeframes(pcm_i16.tobytes())
    return dest


def coerce_audio(audio: Any) -> tuple[int, Any]:
    if audio is None:
        raise ValueError("no audio to transcribe")
    if isinstance(audio, (tuple, list)) and len(audio) == 2:
        sample_rate, samples = audio
        if sample_rate is None or samples is None:
            raise ValueError("empty audio tuple")
        try:
            rate = int(sample_rate)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid sample rate") from exc
        if rate <= 0:
            raise ValueError(f"invalid sample rate: {rate}")
        try:
            import numpy as np

            if np.asarray(samples).size == 0:
                raise ValueError("empty audio samples")
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError("unreadable audio samples") from exc
        return rate, samples
    if isinstance(audio, dict):
        path = audio.get("path") or audio.get("name") or audio.get("orig_name")
        if not path:
            raise ValueError("audio dict has no path")
        return _file_audio(Path(str(path)))
    if isinstance(audio, (str, Path)):
        if not str(audio).strip():
            raise ValueError("empty audio path")
        return _file_audio(Path(audio))
    raise ValueError(f"unsupported audio value: {type(audio)!r}")


def _file_audio(path: Path) -> tuple[int, Any]:
    path = Path(path)
    if not path.is_file():
        raise ValueError(f"audio file missing: {path}")
    try:
        return _wav_file_audio(path)
    except wave.Error:
        return _ffmpeg_decode(path)


def _wav_file_audio(path: Path) -> tuple[int, Any]:
    import numpy as np

    path = Path(path)
    with wave.open(str(path), "rb") as wav_file:
        channels = wav_file.getnchannels()
        sw = wav_file.getsampwidth()
        sr = wav_file.getframerate()
        frames = wav_file.readframes(wav_file.getnframes())
    if sw != 2:
        raise ValueError(f"{path}: expected 16-bit PCM, got {sw * 8}-bit")
    samples = np.frombuffer(frames, dtype="<i2")
    if samples.size == 0:
        raise ValueError(f"{path}: empty wav")
    if channels > 1:
        samples = samples.reshape(-1, channels)
    return int(sr), samples


def ffmpeg_path() -> Path:
    raw = os.environ.get("FFMPEG", shutil.which("ffmpeg") or "ffmpeg")
    return Path(raw).expanduser()


def _ffmpeg_decode(path: Path) -> tuple[int, Any]:
    import numpy as np

    ffmpeg = ffmpeg_path()
    if not shutil.which(str(ffmpeg)) and not ffmpeg.is_file():
        raise ValueError(f"{path}: not a wav, and ffmpeg is missing")
    cmd = [
        str(ffmpeg),
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(path),
        "-f",
        "s16le",
        "-acodec",
        "pcm_s16le",
        "-ac",
        "1",
        "-ar",
        str(TARGET_SR),
        "pipe:1",
    ]
    proc = subprocess.run(cmd, check=False, capture_output=True, timeout=60)
    if proc.returncode != 0 or not proc.stdout:
        detail = (proc.stderr or b"").decode("utf-8", "replace").strip()
        raise ValueError(f"{path}: could not decode audio ({detail or 'ffmpeg failed'})")
    samples = np.frombuffer(proc.stdout, dtype="<i2")
    if samples.size == 0:
        raise ValueError(f"{path}: empty audio after decode")
    return TARGET_SR, samples


def pick_audio(output_audio: Any, clone_audio: Any) -> tuple[Any, str]:
    if output_audio is not None:
        try:
            coerce_audio(output_audio)
            return output_audio, "output"
        except (ValueError, TypeError):
            pass
    if clone_audio is not None:
        coerce_audio(clone_audio)
        return clone_audio, "clone"
    raise ValueError("generate or upload audio first")


def transcribe_audio(
    audio: Any,
    *,
    cli: Path | None = None,
    model: Path | None = None,
    timeout_s: float = 120.0,
) -> dict[str, Any]:
    cli = Path(cli) if cli is not None else cli_path()
    model = Path(model) if model is not None else model_path()
    if not cli.is_file():
        raise FileNotFoundError(f"transcribe-cli missing: {cli}")
    if not os.access(cli, os.X_OK):
        raise PermissionError(f"transcribe-cli not executable: {cli}")
    if not model.is_file():
        raise FileNotFoundError(f"parakeet GGUF missing: {model}")

    with tempfile.TemporaryDirectory(prefix="breeze-parakeet-") as tmp:
        wav_path = Path(tmp) / "input-16k.wav"
        write_16k_mono_wav(audio, wav_path)
        cmd = [
            str(cli),
            "-m",
            str(model),
            "-q",
            "--backend",
            "cpu",
            "--timestamps",
            "word",
            "-l",
            "en",
            str(wav_path),
        ]
        t0 = time.perf_counter()
        proc = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        wall_s = time.perf_counter() - t0
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        if proc.returncode != 0:
            detail = (stderr or stdout).strip() or f"exit {proc.returncode}"
            raise RuntimeError(f"transcribe-cli failed: {detail}")
        aligned = parse_cli_alignment(stdout)
        return {
            "text": aligned["text"],
            "words": aligned["words"],
            "wall_s": wall_s,
            "cli": str(cli),
            "model": str(model),
            "backend": "cpu",
        }
