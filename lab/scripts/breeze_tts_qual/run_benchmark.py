#!/usr/bin/env python3
"""not on the voicecat path.

Minimal Breeze config-A streaming smoke harness for sc-breeze-hybrid-81p.
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import traceback
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
from breeze_tts_qual.metrics import inter_chunk_gaps, percentile
from breeze_tts_qual.metrics import rtf as rtf_clock
from breeze_tts_qual.protocol import compose_b_winner, measured_plan, would_exceed_hard_ceiling
from breeze_tts_qual.utterances import SHORT

_DEFAULT_QUAL_ROOT = (
    Path.home() / ".cache" / "speech-out" / "breeze-tts-qual-sc-breeze-hybrid-81p"
)
_B_ARM_NAMES = (
    "B_depth",
    "B_codec",
    "B_backbone_decode",
    "B_backbone_prefill",
)


def _config_by_name() -> dict[str, EngineConfig]:
    return {c.name: c for c in CONFIGS}


def _fast_flag_names(config: EngineConfig) -> list[str]:
    flags: list[str] = []
    if config.fast_depth_decoder:
        flags.append("depth")
    if config.fast_codec:
        flags.append("codec")
    if config.fast_backbone_decode:
        flags.append("backbone_decode")
    if config.fast_backbone_prefill:
        flags.append("backbone_prefill")
    return flags


def _parse_config_names(raw: str) -> tuple[list[str], bool]:
    names = [part.strip() for part in raw.split(",") if part.strip()]
    if not names:
        raise SystemExit("no configs given")
    b_sweep = names == ["B"]
    if b_sweep:
        return ["A", *_B_ARM_NAMES], True
    known = _config_by_name()
    missing = [name for name in names if name not in known]
    if missing:
        raise SystemExit(f"unknown configs: {', '.join(missing)}")
    return names, False



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

    chunk_rows = [
        {
            "t_rel_s": float(chunk.t_rel_s),
            "duration_s": (int(chunk.n_samples) / float(sample_rate)) if sample_rate else 0.0,
        }
        for chunk in chunks
    ]
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
        "gaps": inter_chunk_gaps(chunk_rows),
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
    except Exception as exc:
        if _is_cuda_oom(exc):
            peak = _peak_vram()
            row.update(peak)
            row["unsafe_vram"] = True
            row["stop_reason"] = "unsafe_vram"
            row["error"] = "cuda_oom"
            return row, 1
        row["correctness_changed"] = True
        row["traceback"] = traceback.format_exc()
        row["error"] = f"{type(exc).__name__}: {exc}"
        row["stop_reason"] = "correctness_changed"
        return row, 3
    finally:
        if engine is not None:
            engine.close()
        engine = None
        gc.collect()
        _empty_cuda()


def _skipped_arm_row(config: EngineConfig, *, reason: str) -> dict[str, Any]:
    return {
        "name": config.name,
        "precision": config.precision,
        "backend": "OfficialBackend",
        "fast": _fast_flag_names(config),
        "unsafe_vram": reason == "unsafe_vram",
        "stop_reason": reason,
        "p50_ttfa_s": None,
        "gap_p95_s": None,
        "peak_allocated_gib": None,
        "n": 0,
    }

def _reuse_a_metrics(qual_root: Path) -> dict[str, Any] | None:
    smoke_path = qual_root / "runs" / "smoke-A" / "metrics.json"
    if not smoke_path.is_file():
        return None
    try:
        payload = json.loads(smoke_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    row = (payload.get("configs") or {}).get("A")
    if not isinstance(row, dict):
        return None
    if row.get("unsafe_vram"):
        return None
    if row.get("p50_ttfa_s") is None or row.get("gap_p95_s") is None:
        return None
    peak = row.get("peak_allocated_gib")
    if peak is None:
        return None
    if would_exceed_hard_ceiling(float(peak)):
        return None
    reused = dict(row)
    reused["name"] = "A"
    reused["fast"] = list(row.get("fast") or [])
    reused["reused_from"] = "smoke-A"
    return reused


def _mark_unsafe(row: dict[str, Any], peak: dict[str, float] | None = None) -> dict[str, Any]:
    if peak:
        row.update(peak)
    row["unsafe_vram"] = True
    row["stop_reason"] = "unsafe_vram"
    row["p50_ttfa_s"] = None
    row["gap_p95_s"] = None
    return row


def _run_measured_config(
    *,
    config: EngineConfig,
    qual_root: Path,
    run_dir: Path,
    ref_audio: Path,
    ref_text: str,
    n: int,
    warmup: int,
) -> tuple[dict[str, Any], int]:
    short_text = SHORT[0]
    ckpt_dir = checkpoint_for_precision(qual_root, config.precision)
    engine: BreezeEngine | None = None
    row: dict[str, Any] = {
        "name": config.name,
        "precision": config.precision,
        "backend": "OfficialBackend",
        "fast": _fast_flag_names(config),
        "unsafe_vram": False,
        "n": 0,
    }
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
            raise SystemExit("B sweep requires OfficialBackend")
        engine = BreezeEngine(config, ckpt_dir=ckpt_dir, backend=backend)
        row["startup_s"] = time.perf_counter() - t_start

        peak = _peak_vram()
        row.update(peak)
        if would_exceed_hard_ceiling(peak["peak_allocated_gib"]):
            return _mark_unsafe(row, peak), 0

        for _ in range(max(warmup, 0)):
            _drain_utterance(
                engine,
                text=short_text,
                reference_audio=ref_audio,
                reference_text=ref_text,
            )
            peak = _peak_vram()
            row.update(peak)
            if would_exceed_hard_ceiling(peak["peak_allocated_gib"]):
                return _mark_unsafe(row, peak), 0

        ttfa: list[float] = []
        rtfs: list[float] = []
        all_gaps: list[float] = []
        last: dict[str, Any] | None = None
        for i in range(n):
            measured = _measure_utterance(
                engine,
                text=short_text,
                reference_audio=ref_audio,
                reference_text=ref_text,
            )
            peak = _peak_vram()
            row.update(peak)
            if would_exceed_hard_ceiling(peak["peak_allocated_gib"]):
                return _mark_unsafe(row, peak), 0
            last = measured
            if measured["first_nonsilent_s"] is not None:
                ttfa.append(float(measured["first_nonsilent_s"]))
            if measured["rtf"] is not None:
                rtfs.append(float(measured["rtf"]))
            all_gaps.extend(float(g) for g in measured.get("gaps") or [])
            if i == 0:
                wav_name = f"{config.name}-short.wav"
                _write_wav(run_dir / wav_name, measured["pcm"], measured["sample_rate"])
                row["wav"] = wav_name
                row["n_chunks"] = measured["n_chunks"]
                row["sample_rate"] = measured["sample_rate"]
                row["first_pcm_s"] = measured["first_pcm_s"]
                row["first_nonsilent_s"] = measured["first_nonsilent_s"]
                row["first_codec_frame_s"] = measured["first_codec_frame_s"]
                row["first_model_output_s"] = measured["first_model_output_s"]

        row["n"] = n
        row["p50_ttfa_s"] = percentile(ttfa, 0.50) if ttfa else (last or {}).get("first_nonsilent_s")
        row["p95_ttfa_s"] = percentile(ttfa, 0.95) if ttfa else None
        row["rtf"] = percentile(rtfs, 0.50) if rtfs else (last or {}).get("rtf")
        row["gap_p50_s"] = percentile(all_gaps, 0.50) if all_gaps else 0.0
        row["gap_p95_s"] = percentile(all_gaps, 0.95) if all_gaps else 0.0
        if last is not None and int(last["n_chunks"]) < 2:
            print("streaming contract failed", file=sys.stderr)
            return row, 2
        return row, 0
    except RuntimeError as exc:
        if _is_cuda_oom(exc):
            peak = _peak_vram()
            row.update(peak)
            row["error"] = "cuda_oom"
            return _mark_unsafe(row), 0
        raise
    finally:
        if engine is not None:
            engine.close()
        engine = None
        gc.collect()
        _empty_cuda()


def _compose_b_payload(configs: dict[str, Any]) -> dict[str, Any]:
    arms = {}
    for name, row in configs.items():
        arms[name] = {
            "p50_ttfa_s": row.get("p50_ttfa_s"),
            "gap_p95_s": row.get("gap_p95_s"),
            "peak_allocated_gib": row.get("peak_allocated_gib") or 0.0,
            "fast": list(row.get("fast") or []),
            "unsafe_vram": bool(row.get("unsafe_vram")),
            "init_unreasonable": bool(row.get("init_unreasonable")),
            "correctness_changed": bool(row.get("correctness_changed")),
        }
    winner = compose_b_winner(arms)
    winner["name"] = "B_winner"
    winner["precision"] = "bf16"
    winner["backend"] = "OfficialBackend"
    winner["unsafe_vram"] = would_exceed_hard_ceiling(float(winner.get("peak_allocated_gib") or 0.0))
    return winner



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
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--n", type=int, default=None)
    parser.add_argument("--run-id", default="smoke-A")
    parser.add_argument("--ref-audio", required=True)
    parser.add_argument("--ref-text", required=True)
    args = parser.parse_args(argv)

    names, b_sweep = _parse_config_names(args.configs)
    if not args.smoke and not b_sweep:
        parser.error("only --smoke or --configs B is implemented")

    qual_root = Path(args.qual_root)
    ref_audio = Path(args.ref_audio)
    if not ref_audio.is_file():
        raise SystemExit(f"missing ref audio: {ref_audio}")
    by_name = _config_by_name()
    run_dir = qual_root / "runs" / args.run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    plan = measured_plan()
    warmup = int(plan["warmup"])
    if args.full:
        n_short = int(plan["short"])
    elif args.n is not None:
        n_short = int(args.n)
    else:
        n_short = int(plan["short"])

    payload: dict[str, Any] = {
        "run_id": args.run_id,
        "smoke": bool(args.smoke) and not b_sweep,
        "qual_root": str(qual_root).replace(str(Path.home()), "~"),
        "n": n_short if b_sweep else None,
        "configs": {},
    }
    exit_code = 0

    if b_sweep:
        reused_a = _reuse_a_metrics(qual_root)
        for name in names:
            if name == "A" and reused_a is not None:
                payload["configs"]["A"] = reused_a
                continue
            row, code = _run_measured_config(
                config=by_name[name],
                qual_root=qual_root,
                run_dir=run_dir,
                ref_audio=ref_audio,
                ref_text=args.ref_text,
                n=n_short,
                warmup=warmup,
            )
            payload["configs"][name] = row
            if code != 0 and exit_code == 0:
                exit_code = code
        for arm_name in _B_ARM_NAMES:
            payload["configs"].setdefault(
                arm_name,
                _skipped_arm_row(by_name[arm_name], reason="not-run"),
            )
        payload["configs"]["B_winner"] = _compose_b_payload(payload["configs"])
    else:
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
