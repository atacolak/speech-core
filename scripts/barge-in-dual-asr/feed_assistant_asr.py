"""Feed captured assistant PCM into Nemotron B via speech-core-file-adapter.

Harness helper only. Uses stream_id=assistant.self_asr (separate from user mic).
Does not modify protocol schemas.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any


ASSISTANT_STREAM_ID = "assistant.self_asr"
DEFAULT_CORE_WS = os.environ.get(
    "SPEECH_CORE_WS_URL", "ws://127.0.0.1:8765/ws/audio-ingress"
)


def find_file_adapter(repo_root: Path) -> str | None:
    candidates = [
        repo_root / "target" / "release" / "speech-core-file-adapter",
        repo_root / "target" / "debug" / "speech-core-file-adapter",
    ]
    which = shutil.which("speech-core-file-adapter")
    if which:
        return which
    for c in candidates:
        if c.is_file() and os.access(c, os.X_OK):
            return str(c)
    return None


def _wav_probe(path: Path) -> tuple[int, int, int] | None:
    """Return (nchannels, framerate, sampwidth) or None."""
    import wave

    try:
        with wave.open(str(path), "rb") as w:
            return w.getnchannels(), w.getframerate(), w.getsampwidth()
    except Exception:
        return None


def ensure_pcm16_mono_16k(wav_path: Path, out_dir: Path) -> tuple[Path, dict[str, Any]]:
    """Normalize assistant capture to 16 kHz mono PCM16 for speech-core-file-adapter.

    Supertonic emits 44.1 kHz; Nemotron ingress requires 16 kHz mono.
    Prefer ffmpeg when available; fall back to stdlib wave linear resample.
    """
    meta: dict[str, Any] = {"source": str(wav_path)}
    probe = _wav_probe(wav_path)
    if probe is not None:
        ch, rate, width = probe
        meta.update({"src_channels": ch, "src_rate": rate, "src_width": width})
        if ch == 1 and rate == 16_000 and width == 2:
            meta["resampled"] = False
            return wav_path, meta

    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / "assistant_played_16k.wav"
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        cmd = [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(wav_path),
            "-ac",
            "1",
            "-ar",
            "16000",
            "-sample_fmt",
            "s16",
            str(dest),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        meta["ffmpeg_rc"] = proc.returncode
        if proc.returncode == 0 and dest.is_file() and dest.stat().st_size > 44:
            meta["resampled"] = True
            meta["method"] = "ffmpeg"
            meta["dest"] = str(dest)
            return dest, meta
        meta["ffmpeg_err"] = (proc.stderr or proc.stdout or "").strip()[:500]

    # stdlib fallback: linear resample PCM16/mono-or-mix
    import audioop
    import wave

    with wave.open(str(wav_path), "rb") as src:
        ch = src.getnchannels()
        rate = src.getframerate()
        width = src.getsampwidth()
        nframes = src.getnframes()
        raw = src.readframes(nframes)
    if ch > 1:
        raw = audioop.tomono(raw, width, 0.5, 0.5)
        ch = 1
    if width != 2:
        raw = audioop.lin2lin(raw, width, 2)
        width = 2
    if rate != 16_000:
        raw, _ = audioop.ratecv(raw, width, 1, rate, 16_000, None)
        rate = 16_000
    with wave.open(str(dest), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16_000)
        out.writeframes(raw)
    meta["resampled"] = True
    meta["method"] = "audioop"
    meta["dest"] = str(dest)
    return dest, meta


def feed_wav_to_assistant_stream(
    wav_path: Path,
    *,
    repo_root: Path,
    core_ws: str = DEFAULT_CORE_WS,
    stream_id: str = ASSISTANT_STREAM_ID,
    stream_session_id: str | None = None,
    adapter_id: str = "assistant.self_asr.feed",
    frame_ms: int = 20,
    realtime: bool = True,
    append_silence_ms: int = 800,
    hold_open_ms: int = 400,
    out_dir: Path | None = None,
) -> dict[str, Any]:
    """Run file-adapter against a captured WAV as the assistant.self_asr stream."""
    adapter = find_file_adapter(repo_root)
    if adapter is None:
        return {
            "ok": False,
            "error": "speech-core-file-adapter binary not found",
            "hint": (
                "cargo build -p speech-core-file-adapter "
                "(debug or release) or install client tools"
            ),
        }
    if not wav_path.is_file():
        return {"ok": False, "error": f"wav not found: {wav_path}"}

    session = stream_session_id or f"assistant-self-asr-{int(time.time())}-{uuid.uuid4().hex[:8]}"
    out_dir = out_dir or wav_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = out_dir / "file-adapter.out"
    stderr_path = out_dir / "file-adapter.err"

    feed_wav, resample_meta = ensure_pcm16_mono_16k(wav_path, out_dir)
    (out_dir / "resample_meta.json").write_text(
        json.dumps(resample_meta, indent=2) + "\n", encoding="utf-8"
    )

    cmd = [
        adapter,
        "--url",
        core_ws,
        "--stream-id",
        stream_id,
        "--stream-session-id",
        session,
        "--adapter-id",
        adapter_id,
        "--frame-ms",
        str(frame_ms),
        "--append-silence-ms",
        str(append_silence_ms),
        "--hold-open-ms",
        str(hold_open_ms),
        str(feed_wav),
    ]
    if realtime:
        cmd.insert(-1, "--realtime")

    started = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "error": "file-adapter timed out",
            "cmd": cmd,
            "stream_session_id": session,
            "resample": resample_meta,
            "feed_wav": str(feed_wav),
        }
    elapsed_ms = int((time.monotonic() - started) * 1000)
    stdout_path.write_text(proc.stdout or "", encoding="utf-8")
    stderr_path.write_text(proc.stderr or "", encoding="utf-8")

    result = {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "cmd": cmd,
        "stream_id": stream_id,
        "stream_session_id": session,
        "adapter_id": adapter_id,
        "wav": str(wav_path),
        "feed_wav": str(feed_wav),
        "resample": resample_meta,
        "elapsed_ms": elapsed_ms,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "note": (
            "Nemotron B feed only. User mic remains on a separate stream_id / process."
        ),
    }
    (out_dir / "feed_result.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8"
    )
    return result


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Feed assistant PCM WAV into speech-core as assistant.self_asr"
    )
    p.add_argument("wav", type=Path, help="Captured assistant PCM WAV")
    p.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
    )
    p.add_argument("--url", default=DEFAULT_CORE_WS)
    p.add_argument("--stream-id", default=ASSISTANT_STREAM_ID)
    p.add_argument(
        "--stream-session-id",
        default=None,
        help="Optional stream_session_id for the assistant.self_asr feed",
    )
    p.add_argument("--out-dir", type=Path, default=None)
    p.add_argument("--no-realtime", action="store_true")
    args = p.parse_args(argv)
    result = feed_wav_to_assistant_stream(
        args.wav,
        repo_root=args.repo_root,
        core_ws=args.url,
        stream_id=args.stream_id,
        stream_session_id=args.stream_session_id,
        realtime=not args.no_realtime,
        out_dir=args.out_dir,
    )
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
