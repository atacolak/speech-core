"""Aligner backend protocol and dispatcher.

Usage (CLI-friendly):
  python -m barge_in_align.backend \\
    --backend ctc_forced \\
    --wav played.wav \\
    --intended "This is a long assistant reply..." \\
    --played-ms 2500
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass
class AlignCursor:
    """Where TTS is estimated to be in the intended text."""

    spoken_prefix: str
    word_index: int  # number of fully spoken words (prefix length in words)
    intended_text: str
    backend_id: str
    confidence: float
    align_latency_ms: float
    played_ms: int
    audio_path: str | None = None
    detail: dict[str, Any] | None = None

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        return d


def list_backends() -> list[str]:
    return ["approx_wallclock", "ctc_forced"]


def _words(text: str) -> list[str]:
    return text.split()


def approx_wallclock(
    intended_text: str,
    played_ms: int,
    *,
    speed: float = 1.0,
    **_kwargs: Any,
) -> AlignCursor:
    t0 = time.perf_counter()
    words = _words(intended_text)
    if not words:
        prefix, n = "", 0
    else:
        # ~2.5 wps at speed 1.0 (matches live-session provisional)
        wps = max(0.8, 2.5 * max(0.5, float(speed)))
        n = int(round((max(0, played_ms) / 1000.0) * wps))
        n = max(1, min(len(words), n)) if played_ms > 0 else 0
        prefix = " ".join(words[:n])
    ms = (time.perf_counter() - t0) * 1000.0
    return AlignCursor(
        spoken_prefix=prefix,
        word_index=n if words else 0,
        intended_text=intended_text,
        backend_id="approx_wallclock",
        confidence=0.2,
        align_latency_ms=ms,
        played_ms=played_ms,
        detail={"speed": speed, "wps": max(0.8, 2.5 * max(0.5, float(speed)))},
    )


def _load_ctc():
    from .ctc_forced import align_ctc_forced

    return align_ctc_forced


BACKENDS: dict[str, Callable[..., AlignCursor]] = {
    "approx_wallclock": approx_wallclock,
}


def align_played_clip(
    *,
    backend: str,
    intended_text: str,
    played_ms: int,
    wav_path: str | Path | None = None,
    speed: float = 1.0,
    **kwargs: Any,
) -> AlignCursor:
    backend = (backend or "approx_wallclock").strip().lower()
    if backend == "ctc_forced":
        fn = _load_ctc()
        return fn(
            intended_text=intended_text,
            played_ms=played_ms,
            wav_path=wav_path,
            speed=speed,
            **kwargs,
        )
    if backend not in BACKENDS:
        raise ValueError(f"unknown backend {backend!r}; choose from {list_backends()}")
    return BACKENDS[backend](
        intended_text=intended_text,
        played_ms=played_ms,
        wav_path=wav_path,
        speed=speed,
        **kwargs,
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Forced-align played TTS clip to intended text")
    p.add_argument("--backend", default="ctc_forced", choices=list_backends())
    p.add_argument("--wav", type=Path, default=None)
    p.add_argument("--intended", required=True)
    p.add_argument("--played-ms", type=int, required=True)
    p.add_argument("--speed", type=float, default=1.0)
    p.add_argument("--out", type=Path, default=None)
    args = p.parse_args(argv)

    cursor = align_played_clip(
        backend=args.backend,
        intended_text=args.intended,
        played_ms=args.played_ms,
        wav_path=args.wav,
        speed=args.speed,
    )
    payload = cursor.to_json()
    text = json.dumps(payload, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
