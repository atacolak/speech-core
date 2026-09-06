#!/usr/bin/env python3
"""not on the voicecat path.

Minimal Breeze config-A streaming smoke harness for sc-breeze-hybrid-81p.
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import sys
import time
import wave
from pathlib import Path
from typing import Any

if __package__ is None:
    _scripts = Path(__file__).resolve().parent.parent
    if str(_scripts) not in sys.path:
        sys.path.insert(0, str(_scripts))

from breeze_tts_qual.configs import CONFIGS, EngineConfig
from breeze_tts_qual.engine import BreezeEngine, OfficialBackend, checkpoint_for_precision
from breeze_tts_qual.metrics import bytes_to_gib, first_nonsilent_s as first_nonsilent_clock
from breeze_tts_qual.metrics import rtf as rtf_clock
from breeze_tts_qual.protocol import would_exceed_hard_ceiling
from breeze_tts_qual.utterances import SHORT

_DEFAULT_QUAL_ROOT = (
    Path.home() / ".cache" / "speech-out" / "breeze-tts-qual-sc-breeze-hybrid-81p"
)


def _config_by_name() -> dict[str, EngineConfig]:
    return {c.name: c for c in CONFIGS}


def _parse_config_names(raw: str) -> list[str]:
    names = [part.strip() for part in raw.split(",") if part.strip()]
    if not names:
        raise SystemExit("no configs given")
    known = _config_by_name()
    missing = [name for name in names if name not in known]
    if missing:
        raise SystemExit(f"unknown configs: {', '.join(missing)}")
    return names


def _peak_vram() -> dict[str, float]:
    try:
        import torch
    except ImportError:
        return {
            "peak_allocated_mb": 0.0,
            "peak_reserved_mb": 0.0,
            "peak_allocated_gib": 0.0,
        }
    if not torch.cuda.is_available():
        return {
            "peak_allocated_mb": 0.0,
            "peak_reserved_mb": 0.0,
            "peak_allocated_gib": 0.0,
        }
    allocated = float(torch.cuda.max_memory_allocated())
    reserved = float(torch.cuda.max_memory_reserved())
    return {
        "peak_allocated_mb": allocated / (1024.0 * 1024.0),
        "peak_reserved_mb": reserved / (1024.0 * 1024.0),
        "peak_allocated_gib": bytes_to_gib(allocated),
    }


def _reset_peak_vram() -> None:
    try:
        import torch
    except ImportError:
        return
    if torch.cuda.is_available():
        torch.cuda.reset_peak_memory_stats()


def _empty_cuda() -> None:
    try:
        import torch
    except ImportError:
        return
    if torch.cuda.is_available():
        torch.cuda.empty_cache()


def _write_wav(path: Path, pcm: bytes, sample_rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(int(sample_rate))
        handle.writeframes(pcm)


def _first_model_output_s(timing: dict[str, Any] | None) -> float | None:
    if not timing:
        return None
    if "prefill_gpu_ms" in timing:
        return float(timing["prefill_gpu_ms"]) / 1000.0
    if "ttfa_internal_ms" in timing:
        return float(timing["ttfa_internal_ms"]) / 1000.0
    return None


def _measure_utterance(
    engine: BreezeEngine,
    *,
    text: str,
    reference_audio: Path,
    reference_text: str,
) -> dict[str, Any]:
    chunks = []
    t0 = time.perf_counter()
    for chunk in engine.synthesize(
        text=text,
        reference_audio=reference_audio,
        reference_text=reference_text,
    ):
        chunks.append(chunk)
    wall_s = time.perf_counter() - t0

    sample_rate = int(getattr(engine.backend, "sample_rate", 24000))
    if chunks:
        sample_rate = int(chunks[0].sample_rate or sample_rate)

    pcm = b"".join(chunk.pcm for chunk in chunks)
    n_samples = sum(int(chunk.n_samples) for chunk in chunks)
    audio_s = (n_samples / float(sample_rate)) if sample_rate else 0.0

    first_pcm_s = None
    first_codec_frame_s = None
    first_model_output_s = None
    for chunk in chunks:
        timing = dict(chunk.timing or {})
        if first_model_output_s is None:
            first_model_output_s = _first_model_output_s(timing)
        if first_pcm_s is None and int(chunk.n_samples) > 0:
            first_pcm_s = float(chunk.t_rel_s)
        codec_frames = timing.get("codec_frames")
        if first_codec_frame_s is None:
            if codec_frames:
                first_codec_frame_s = float(chunk.t_rel_s)
            elif codec_frames is None and int(chunk.n_samples) > 0:
                first_codec_frame_s = float(chunk.t_rel_s)

    nonsilent_s, _ = first_nonsilent_clock(
        [(float(chunk.t_rel_s), chunk.pcm) for chunk in chunks],
        sample_rate=sample_rate,
    )
    return {
        "chunks": chunks,
        "pcm": pcm,
        "n_chunks": len(chunks),
        "n_samples": n_samples,
        "sample_rate": sample_rate,
        "wall_s": wall_s,
        "audio_s": audio_s,
        "rtf": rtf_clock(wall_s=wall_s, audio_s=audio_s),
        "first_pcm_s": first_pcm_s,
        "first_codec_frame_s": first_codec_frame_s,
        "first_model_output_s": first_model_output_s,
        "first_nonsilent_s": nonsilent_s,
    }


def _drain_utterance(
    engine: BreezeEngine,
    *,
    text: str,
    reference_audio: Path,
    reference_text: str,
) -> None:
    for _chunk in engine.synthesize(
        text=text,
        reference_audio=reference_audio,
        reference_text=reference_text,
    ):
        pass


def _is_cuda_oom(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "out of memory" in text or "cuda oom" in text


def _run_smoke_config(
    *,
    config: EngineConfig,
    qual_root: Path,
    run_dir: Path,
    ref_audio: Path,
    ref_text: str,
) -> tuple[dict[str, Any], int]:
    short_text = SHORT[0]
    ckpt_dir = checkpoint_for_precision(qual_root, config.precision)
    engine: BreezeEngine | None = None
    row: dict[str, Any] = {
        "name": config.name,
        "precision": config.precision,
        "backend": "OfficialBackend",
        "unsafe_vram": False,
    }
    exit_code = 0
    try:
        _reset_peak_vram()
        t_start = time.perf_counter()
        backend = OfficialBackend(
            config,
            device="cuda:0",
            qual_root=qual_root,
            ckpt_dir=ckpt_dir,
        )
        if type(backend).__name__ != "OfficialBackend":
            raise SystemExit("smoke requires OfficialBackend")
        engine = BreezeEngine(config, ckpt_dir=ckpt_dir, backend=backend)
        row["startup_s"] = time.perf_counter() - t_start

        peak = _peak_vram()
        row.update(peak)
        if would_exceed_hard_ceiling(peak["peak_allocated_gib"]):
            row["unsafe_vram"] = True
            row["stop_reason"] = "unsafe_vram"
            return row, 1

        _drain_utterance(
            engine,
            text=short_text,
            reference_audio=ref_audio,
            reference_text=ref_text,
        )
        peak = _peak_vram()
        row.update(peak)
        if would_exceed_hard_ceiling(peak["peak_allocated_gib"]):
            row["unsafe_vram"] = True
            row["stop_reason"] = "unsafe_vram"
            return row, 1

        measured = _measure_utterance(
            engine,
            text=short_text,
            reference_audio=ref_audio,
            reference_text=ref_text,
        )
        peak = _peak_vram()
        wav_name = f"{config.name}-short.wav"
        wav_path = run_dir / wav_name
        _write_wav(wav_path, measured["pcm"], measured["sample_rate"])
        row.update(
            {
                "startup_s": row["startup_s"],
                "first_model_output_s": measured["first_model_output_s"],
                "first_codec_frame_s": measured["first_codec_frame_s"],
                "first_pcm_s": measured["first_pcm_s"],
                "first_nonsilent_s": measured["first_nonsilent_s"],
                "rtf": measured["rtf"],
                "n_chunks": measured["n_chunks"],
                "sample_rate": measured["sample_rate"],
                "wav": wav_name,
                **peak,
            }
        )
        if would_exceed_hard_ceiling(peak["peak_allocated_gib"]):
            row["unsafe_vram"] = True
            row["stop_reason"] = "unsafe_vram"
            exit_code = 1
        if int(measured["n_chunks"]) < 2:
            print("streaming contract failed", file=sys.stderr)
            return row, 2
        return row, exit_code
    except RuntimeError as exc:
        if _is_cuda_oom(exc):
            peak = _peak_vram()
            row.update(peak)
            row["unsafe_vram"] = True
            row["stop_reason"] = "unsafe_vram"
            row["error"] = "cuda_oom"
            return row, 1
        raise
    finally:
        if engine is not None:
            engine.close()
        engine = None
        gc.collect()
        _empty_cuda()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Breeze TTS 2 hybrid lab benchmark (not on the voicecat path)."
    )
    parser.add_argument(
        "--qual-root",
        default=os.environ.get("QUAL_ROOT", str(_DEFAULT_QUAL_ROOT)),
    )
    parser.add_argument("--configs", default="A")
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--run-id", default="smoke-A")
    parser.add_argument("--ref-audio", required=True)
    parser.add_argument("--ref-text", required=True)
    args = parser.parse_args(argv)

    if not args.smoke:
        parser.error("only --smoke is implemented")

    qual_root = Path(args.qual_root)
    ref_audio = Path(args.ref_audio)
    if not ref_audio.is_file():
        raise SystemExit(f"missing ref audio: {ref_audio}")
    names = _parse_config_names(args.configs)
    by_name = _config_by_name()
    run_dir = qual_root / "runs" / args.run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    payload: dict[str, Any] = {
        "run_id": args.run_id,
        "smoke": True,
        "qual_root": str(qual_root).replace(str(Path.home()), "~"),
        "configs": {},
    }
    exit_code = 0
    for name in names:
        row, code = _run_smoke_config(
            config=by_name[name],
            qual_root=qual_root,
            run_dir=run_dir,
            ref_audio=ref_audio,
            ref_text=args.ref_text,
        )
        payload["configs"][name] = row
        if code != 0 and exit_code == 0:
            exit_code = code

    metrics_path = run_dir / "metrics.json"
    metrics_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
