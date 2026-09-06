#!/usr/bin/env python3
"""not on the voicecat path.

Breeze hybrid lab benchmark harness for sc-breeze-hybrid-81p.
"""

from __future__ import annotations

import argparse
import gc
import json
import subprocess
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
from breeze_tts_qual.protocol import (
    compose_b_winner,
    compose_e_winner,
    measured_plan,
    should_stop_adding_graphs,
    tiny_rotation,
    would_exceed_hard_ceiling,
)

from breeze_tts_qual.utterances import DIRECTIONS, LONG, MEDIUM, SHORT

_DEFAULT_QUAL_ROOT = (
    Path.home() / ".cache" / "speech-out" / "breeze-tts-qual-sc-breeze-hybrid-81p"
)
_B_ARM_NAMES = (
    "B_depth",
    "B_codec",
    "B_backbone_decode",
    "B_backbone_prefill",
)
_C_LADDER_NAMES = (
    "C0",
    "C1",
    "C2",
    "C3",
    "C4",
)
_E_LADDER_NAMES = (
    "E1",
    "E2",
    "E3",
    "E4",
    "E5",
)
_FULL_CONFIGS = "A,B,C0,C1,C2,C3,C4,D,E1,E2,E3,E4,E5"



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
    if config.fast_text_encoder:
        flags.append("text_encoder")
    return flags


def _parse_config_names(raw: str) -> tuple[list[str], bool, bool, bool]:
    names = [part.strip() for part in raw.split(",") if part.strip()]
    if not names:
        raise SystemExit("no configs given")
    b_sweep = names == ["B"]
    if b_sweep:
        return ["A", *_B_ARM_NAMES], True, False, False
    if names == ["C"]:
        return list(_C_LADDER_NAMES), False, True, False
    if names == ["E"]:
        return list(_E_LADDER_NAMES), False, False, True
    known = _config_by_name()
    missing = [name for name in names if name not in known]
    if missing:
        raise SystemExit(f"unknown configs: {', '.join(missing)}")
    c_sweep = names == list(_C_LADDER_NAMES)
    e_sweep = names == list(_E_LADDER_NAMES)
    return names, False, c_sweep, e_sweep


def _gpu_util_power() -> dict[str, float | None]:
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=utilization.gpu,power.draw",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return {"gpu_util_pct": None, "gpu_power_w": None}
    if result.returncode != 0:
        return {"gpu_util_pct": None, "gpu_power_w": None}
    line = (result.stdout or "").strip().splitlines()
    if not line:
        return {"gpu_util_pct": None, "gpu_power_w": None}
    parts = [p.strip() for p in line[0].split(",")]
    if len(parts) < 2:
        return {"gpu_util_pct": None, "gpu_power_w": None}

    def _parse(raw: str) -> float | None:
        try:
            return float(raw)
        except ValueError:
            return None

    return {"gpu_util_pct": _parse(parts[0]), "gpu_power_w": _parse(parts[1])}


def _d_already_killed(qual_root: Path) -> dict[str, Any] | None:
    smoke_path = qual_root / "runs" / "smoke-D" / "metrics.json"
    if not smoke_path.is_file():
        return None
    try:
        payload = json.loads(smoke_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    row = (payload.get("configs") or {}).get("D")
    if not isinstance(row, dict):
        return None
    if not (row.get("d_killed") or payload.get("d_killed")):
        return None
    skipped = dict(row)
    skipped["name"] = "D"
    skipped["not_run"] = True
    skipped["d_killed"] = True
    skipped["stop_reason"] = skipped.get("stop_reason") or "d_killed"
    skipped["reused_from"] = "smoke-D"
    skipped["n"] = 0
    classes = skipped.get("classes")
    if isinstance(classes, dict):
        for cls in classes.values():
            if isinstance(cls, dict) and cls.get("n_measured") == 30:
                cls["n_measured"] = 0
    return skipped



def _class_texts(plan: dict[str, int]) -> dict[str, list[str]]:
    return {
        "tiny": tiny_rotation()[: int(plan["tiny"])],
        "short": [SHORT[0]] * int(plan["short"]),
        "medium": [MEDIUM[0]] * int(plan["medium"]),
        "long": [LONG[0]] * int(plan["long"]),
    }


def _summarize_class(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ttfa = [
        float(row["first_nonsilent_s"])
        for row in rows
        if row.get("first_nonsilent_s") is not None
    ]
    rtfs = [float(row["rtf"]) for row in rows if row.get("rtf") is not None]
    gaps: list[float] = []
    for row in rows:
        gaps.extend(float(g) for g in row.get("gaps") or [])
    summary: dict[str, Any] = {
        "n_measured": len(rows),
        "p50_ttfa_s": percentile(ttfa, 0.50) if ttfa else None,
        "p95_ttfa_s": percentile(ttfa, 0.95) if ttfa else None,
        "rtf": percentile(rtfs, 0.50) if rtfs else None,
        "gap_p50_s": percentile(gaps, 0.50) if gaps else 0.0,
        "gap_p95_s": percentile(gaps, 0.95) if gaps else 0.0,
        "max_stall_s": max(gaps) if gaps else 0.0,
    }
    return summary




def _persist_metrics(run_dir: Path, payload: dict[str, Any]) -> None:
    path = run_dir / "metrics.json"
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")



def _c0_p50_ttfa(qual_root: Path) -> float | None:
    c_incr = qual_root / "runs" / "c-incr" / "metrics.json"
    if c_incr.is_file():
        try:
            payload = json.loads(c_incr.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict):
            row = (payload.get("configs") or {}).get("C0")
            if isinstance(row, dict) and row.get("p50_ttfa_s") is not None:
                return float(row["p50_ttfa_s"])
    smoke = qual_root / "runs" / "smoke-C0" / "metrics.json"
    if smoke.is_file():
        try:
            payload = json.loads(smoke.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict):
            row = (payload.get("configs") or {}).get("C0")
            if isinstance(row, dict):
                if row.get("p50_ttfa_s") is not None:
                    return float(row["p50_ttfa_s"])
                if row.get("first_nonsilent_s") is not None:
                    return float(row["first_nonsilent_s"])
    return None


def _d_should_kill(row: dict[str, Any], c0_p50: float | None) -> bool:
    if row.get("not_run") or row.get("unsafe_vram"):
        return True
    p50 = row.get("p50_ttfa_s")
    if p50 is None or c0_p50 is None:
        return True
    return float(p50) >= float(c0_p50)


def _run_d_control(
    *,
    config: EngineConfig,
    qual_root: Path,
    run_dir: Path,
    ref_audio: Path,
    ref_text: str,
    n: int,
    warmup: int,
    full: bool = False,
) -> tuple[dict[str, Any], int, bool]:
    try:
        row, code = _run_measured_config(
            config=config,
            qual_root=qual_root,
            run_dir=run_dir,
            ref_audio=ref_audio,
            ref_text=ref_text,
            n=n,
            warmup=warmup,
            full=full,
        )
    except Exception as exc:
        row = {
            "name": config.name,
            "precision": config.precision,
            "backend": "OfficialBackend",
            "fast": _fast_flag_names(config),
            "unsafe_vram": False,
            "n": 0,
            "correctness_changed": True,
            "stop_reason": "correctness_changed",
            "traceback": traceback.format_exc(),
            "error": f"{type(exc).__name__}: {exc}",
        }
        code = 3
    peak = row.get("peak_allocated_gib")
    overflow = bool(row.get("unsafe_vram"))
    if peak is not None and would_exceed_hard_ceiling(float(peak)):
        overflow = True
    if overflow:
        row = _mark_c_not_run(row, reason="unsafe_vram")
    c0_p50 = _c0_p50_ttfa(qual_root)
    killed = _d_should_kill(row, c0_p50)
    row["d_killed"] = killed
    row["c0_p50_ttfa_s"] = c0_p50
    return row, code, killed





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
    instruction: str = "Speak clearly and naturally.",
    seed: int = 42,
    cfg_scale: float = 1.0,
) -> dict[str, Any]:
    chunks = []
    t0 = time.perf_counter()
    for chunk in engine.synthesize(
        text=text,
        reference_audio=reference_audio,
        reference_text=reference_text,
        instruction=instruction,
        seed=seed,
        cfg_scale=cfg_scale,
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

def _reuse_c0_metrics(qual_root: Path) -> dict[str, Any] | None:
    smoke_path = qual_root / "runs" / "smoke-C0" / "metrics.json"
    if not smoke_path.is_file():
        return None
    try:
        payload = json.loads(smoke_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    row = (payload.get("configs") or {}).get("C0")
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
    reused["name"] = "C0"
    reused["fast"] = list(row.get("fast") or [])
    reused["reused_from"] = "smoke-C0"
    return reused


def _skipped_c_row(config: EngineConfig, *, reason: str) -> dict[str, Any]:
    row = _skipped_arm_row(config, reason=reason)
    row["not_run"] = True
    return row


def _mark_c_not_run(row: dict[str, Any], *, reason: str) -> dict[str, Any]:
    marked = dict(row)
    marked["not_run"] = True
    marked["stop_reason"] = reason
    if reason == "unsafe_vram":
        marked["unsafe_vram"] = True
    marked["n"] = 0
    classes = marked.get("classes")
    if isinstance(classes, dict):
        for cls in classes.values():
            if isinstance(cls, dict):
                cls["n_measured"] = 0
    return marked



def _c_stop_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "p50_ttfa_s": row["p50_ttfa_s"],
        "gap_p95_s": row["gap_p95_s"],
        "peak_allocated_gib": float(row.get("peak_allocated_gib") or 0.0),
        "init_unreasonable": bool(row.get("init_unreasonable")),
        "correctness_changed": bool(row.get("correctness_changed")),
    }


def _run_c_sweep(
    *,
    by_name: dict[str, EngineConfig],
    qual_root: Path,
    run_dir: Path,
    ref_audio: Path,
    ref_text: str,
    n: int,
    warmup: int,
    full: bool = False,
) -> tuple[dict[str, Any], int]:
    configs: dict[str, Any] = {}
    exit_code = 0
    prev: dict[str, Any] | None = None
    stop_reason: str | None = None
    reused_c0 = None if full else _reuse_c0_metrics(qual_root)

    for name in _C_LADDER_NAMES:
        config = by_name[name]
        if config.fast_text_encoder:
            raise SystemExit(f"{name} must not enable fast_text_encoder")
        if stop_reason is not None:
            configs[name] = _skipped_c_row(config, reason=stop_reason)
            continue

        if name == "C0" and reused_c0 is not None:
            row = reused_c0
            code = 0
        else:
            try:
                row, code = _run_measured_config(
                    config=config,
                    qual_root=qual_root,
                    run_dir=run_dir,
                    ref_audio=ref_audio,
                    ref_text=ref_text,
                    n=n,
                    warmup=warmup,
                    full=full,
                )
            except Exception as exc:
                row = {
                    "name": config.name,
                    "precision": config.precision,
                    "backend": "OfficialBackend",
                    "fast": _fast_flag_names(config),
                    "unsafe_vram": False,
                    "n": 0,
                    "correctness_changed": True,
                    "stop_reason": "correctness_changed",
                    "traceback": traceback.format_exc(),
                    "error": f"{type(exc).__name__}: {exc}",
                }
                code = 3

        if code != 0 and exit_code == 0:
            exit_code = code

        peak = row.get("peak_allocated_gib")
        overflow = bool(row.get("unsafe_vram"))
        if peak is not None and would_exceed_hard_ceiling(float(peak)):
            overflow = True
        if overflow:
            configs[name] = _mark_c_not_run(row, reason="unsafe_vram")
            stop_reason = "unsafe_vram"
            continue

        if row.get("p50_ttfa_s") is None or row.get("gap_p95_s") is None:
            row = dict(row)
            row["correctness_changed"] = True
            row["stop_reason"] = "correctness_changed"
            configs[name] = row
            stop_reason = "correctness_changed"
            continue

        configs[name] = row
        if prev is not None:
            stop, reason = should_stop_adding_graphs(_c_stop_view(prev), _c_stop_view(row))
            if stop:
                stop_reason = reason
        elif row.get("init_unreasonable"):
            stop_reason = "init_unreasonable"
        elif row.get("correctness_changed"):
            stop_reason = "correctness_changed"
        prev = row

    return configs, exit_code



def _run_e_sweep(
    *,
    by_name: dict[str, EngineConfig],
    qual_root: Path,
    run_dir: Path,
    ref_audio: Path,
    ref_text: str,
    n: int,
    warmup: int,
    full: bool = False,
) -> tuple[dict[str, Any], int, bool]:
    configs: dict[str, Any] = {}
    exit_code = 0
    prev: dict[str, Any] | None = None
    stop_reason: str | None = None

    for name in _E_LADDER_NAMES:
        config = by_name[name]
        if stop_reason is not None:
            configs[name] = _skipped_c_row(config, reason=stop_reason)
            continue

        try:
            row, code = _run_measured_config(
                config=config,
                qual_root=qual_root,
                run_dir=run_dir,
                ref_audio=ref_audio,
                ref_text=ref_text,
                n=n,
                warmup=warmup,
                full=full,
            )
        except Exception as exc:
            row = {
                "name": config.name,
                "precision": config.precision,
                "backend": "OfficialBackend",
                "fast": _fast_flag_names(config),
                "unsafe_vram": False,
                "n": 0,
                "correctness_changed": True,
                "stop_reason": "correctness_changed",
                "traceback": traceback.format_exc(),
                "error": f"{type(exc).__name__}: {exc}",
            }
            code = 3

        if code != 0 and exit_code == 0:
            exit_code = code

        peak = row.get("peak_allocated_gib")
        overflow = bool(row.get("unsafe_vram"))
        if peak is not None and would_exceed_hard_ceiling(float(peak)):
            overflow = True
        if overflow:
            configs[name] = _mark_c_not_run(row, reason="unsafe_vram")
            stop_reason = "unsafe_vram"
            continue

        if row.get("p50_ttfa_s") is None or row.get("gap_p95_s") is None:
            row = dict(row)
            row["correctness_changed"] = True
            row["stop_reason"] = "correctness_changed"
            configs[name] = row
            stop_reason = "correctness_changed"
            continue

        configs[name] = row
        if prev is not None:
            stop, reason = should_stop_adding_graphs(_c_stop_view(prev), _c_stop_view(row))
            if stop:
                stop_reason = reason
        elif row.get("init_unreasonable"):
            stop_reason = "init_unreasonable"
        elif row.get("correctness_changed"):
            stop_reason = "correctness_changed"
        prev = row

    winner = compose_e_winner(configs)
    rejected = winner is None
    if winner is not None:
        winner = dict(winner)
        winner["name"] = winner.get("name") or "E_winner"
        configs["E_winner"] = winner
    return configs, exit_code, rejected




def _mark_unsafe(row: dict[str, Any], peak: dict[str, float] | None = None) -> dict[str, Any]:
    if peak:
        row.update(peak)
    row["unsafe_vram"] = True
    row["not_run"] = True
    row["stop_reason"] = "unsafe_vram"
    row["p50_ttfa_s"] = None
    row["gap_p95_s"] = None
    row["n"] = 0
    classes = row.get("classes")
    if isinstance(classes, dict):
        for cls in classes.values():
            if isinstance(cls, dict):
                cls["n_measured"] = 0
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
    full: bool = False,
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

        warmup_texts = [short_text]
        if full:
            warmup_texts = [tiny_rotation()[0], short_text]
        for i in range(max(warmup, 0)):
            _drain_utterance(
                engine,
                text=warmup_texts[i % len(warmup_texts)],
                reference_audio=ref_audio,
                reference_text=ref_text,
            )
            peak = _peak_vram()
            row.update(peak)
            if would_exceed_hard_ceiling(peak["peak_allocated_gib"]):
                return _mark_unsafe(row, peak), 0

        if full:
            plan = measured_plan()
            wav_dir = run_dir / "wavs"
            wav_dir.mkdir(parents=True, exist_ok=True)
            classes: dict[str, Any] = {}
            last: dict[str, Any] | None = None
            short_summary: dict[str, Any] | None = None
            all_gaps: list[float] = []
            for class_name, texts in _class_texts(plan).items():
                measured_rows: list[dict[str, Any]] = []
                print(
                    f"{config.name} class {class_name} n={len(texts)}",
                    flush=True,
                )
                for idx, text in enumerate(texts):
                    measured = _measure_utterance(
                        engine,
                        text=text,
                        reference_audio=ref_audio,
                        reference_text=ref_text,
                    )
                    peak = _peak_vram()
                    row.update(peak)
                    if would_exceed_hard_ceiling(peak["peak_allocated_gib"]):
                        return _mark_unsafe(row, peak), 0
                    last = measured
                    all_gaps.extend(float(g) for g in measured.get("gaps") or [])
                    measured_rows.append(measured)
                    wav_name = f"{config.name}_{class_name}_{idx}.wav"
                    _write_wav(
                        wav_dir / wav_name,
                        measured["pcm"],
                        measured["sample_rate"],
                    )
                    if idx == 0 and class_name == "short":
                        row["wav"] = f"wavs/{config.name}_short_0.wav"
                        row["n_chunks"] = measured["n_chunks"]
                        row["sample_rate"] = measured["sample_rate"]
                        row["first_pcm_s"] = measured["first_pcm_s"]
                        row["first_nonsilent_s"] = measured["first_nonsilent_s"]
                        row["first_codec_frame_s"] = measured["first_codec_frame_s"]
                        row["first_model_output_s"] = measured["first_model_output_s"]
                summary = _summarize_class(measured_rows)
                classes[class_name] = summary
                if class_name == "short":
                    short_summary = summary
            row["classes"] = classes
            row["n"] = int((short_summary or {}).get("n_measured") or 0)
            row["p50_ttfa_s"] = (short_summary or {}).get("p50_ttfa_s")
            row["p95_ttfa_s"] = (short_summary or {}).get("p95_ttfa_s")
            row["rtf"] = (short_summary or {}).get("rtf")
            row["gap_p50_s"] = (short_summary or {}).get("gap_p50_s")
            row["gap_p95_s"] = (short_summary or {}).get("gap_p95_s")
            row["max_stall_s"] = max(all_gaps) if all_gaps else 0.0
            row.update(_gpu_util_power())
            if last is not None and int(last["n_chunks"]) < 2:
                print("streaming contract failed", file=sys.stderr)
                return row, 2
            return row, 0

        ttfa: list[float] = []
        rtfs: list[float] = []
        all_gaps = []
        last = None
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
        row["max_stall_s"] = max(all_gaps) if all_gaps else 0.0
        if last is not None and int(last["n_chunks"]) < 2:
            print("streaming contract failed", file=sys.stderr)
            return row, 2
        return row, 0
    except Exception as cop:
        if _is_cuda_oom(cop):
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



def _direction_slug(phrase: str) -> str:
    lowered = phrase.lower()
    for token in ("calm", "amused", "urgent", "quiet"):
        if token in lowered:
            return token
    raise ValueError(f"unrecognized direction phrase: {phrase!r}")


def _executed_in_envelope(row: Any) -> bool:
    if not isinstance(row, dict):
        return False
    if row.get("not_run") or row.get("unsafe_vram"):
        return False
    peak = row.get("peak_allocated_gib")
    if peak is None or would_exceed_hard_ceiling(float(peak)):
        return False
    if row.get("p50_ttfa_s") is None:
        return False
    return True


def _hybrid_candidate_name(configs: dict[str, Any]) -> str:
    last = None
    for name in _C_LADDER_NAMES:
        if _executed_in_envelope(configs.get(name)):
            last = name
    return last or "C0"


def _e_candidate_name(configs: dict[str, Any]) -> str | None:
    last = None
    for name in _E_LADDER_NAMES:
        if _executed_in_envelope(configs.get(name)):
            last = name
    if last is not None:
        return last
    winner = configs.get("E_winner")
    if isinstance(winner, dict) and _executed_in_envelope(winner):
        return str(winner.get("name") or "E_winner")
    return None


def _direction_config_names(configs: dict[str, Any]) -> list[str]:
    names = ["A", _hybrid_candidate_name(configs)]
    e_name = _e_candidate_name(configs)
    if e_name is not None:
        names.append(e_name)
    seen: set[str] = set()
    ordered: list[str] = []
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        ordered.append(name)
    return ordered


def _run_direction_pass(
    *,
    by_name: dict[str, EngineConfig],
    configs: dict[str, Any],
    qual_root: Path,
    run_dir: Path,
    ref_audio: Path,
    ref_text: str,
) -> dict[str, Any]:
    wav_dir = run_dir / "wavs"
    wav_dir.mkdir(parents=True, exist_ok=True)
    dumps: dict[str, Any] = {}
    for name in _direction_config_names(configs):
        config = by_name[name]
        ckpt_dir = checkpoint_for_precision(qual_root, config.precision)
        engine: BreezeEngine | None = None
        try:
            print(f"direction start {name}", flush=True)
            backend = OfficialBackend(
                config,
                device="cuda:0",
                qual_root=qual_root,
                ckpt_dir=ckpt_dir,
            )
            if type(backend).__name__ != "OfficialBackend":
                raise SystemExit("direction pass requires OfficialBackend")
            engine = BreezeEngine(config, ckpt_dir=ckpt_dir, backend=backend)
            dumps[name] = {}
            for phrase in DIRECTIONS:
                slug = _direction_slug(phrase)
                measured = _measure_utterance(
                    engine,
                    text=SHORT[0],
                    reference_audio=ref_audio,
                    reference_text=ref_text,
                    instruction=phrase,
                    seed=42,
                    cfg_scale=4.0,
                )
                wav_name = f"{name}_dir_{slug}.wav"
                _write_wav(wav_dir / wav_name, measured["pcm"], measured["sample_rate"])
                dumps[name][slug] = f"wavs/{wav_name}"
                print(f"direction wrote {wav_name}", flush=True)
            print(f"direction done {name}", flush=True)
        finally:
            if engine is not None:
                engine.close()
            engine = None
            gc.collect()
            _empty_cuda()
    return dumps


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Breeze TTS 2 hybrid lab benchmark (not on the voicecat path)."
    )
    parser.add_argument(
        "--qual-root",
        default=os.environ.get("QUAL_ROOT", str(_DEFAULT_QUAL_ROOT)),
    )
    parser.add_argument("--configs", default=None)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--direction-only", action="store_true")
    parser.add_argument("--n", type=int, default=None)
    parser.add_argument("--run-id", default="smoke-A")
    parser.add_argument("--ref-audio", required=True)
    parser.add_argument("--ref-text", required=True)
    args = parser.parse_args(argv)

    if args.direction_only:
        qual_root = Path(args.qual_root)
        ref_audio = Path(args.ref_audio)
        if not ref_audio.is_file():
            raise SystemExit(f"missing ref audio: {ref_audio}")
        run_dir = qual_root / "runs" / args.run_id
        metrics_path = run_dir / "metrics.json"
        if not metrics_path.is_file():
            raise SystemExit(f"missing metrics for direction-only: {metrics_path}")
        payload = json.loads(metrics_path.read_text(encoding="utf-8"))
        configs = payload.get("configs") or {}
        dumps = _run_direction_pass(
            by_name=_config_by_name(),
            configs=configs,
            qual_root=qual_root,
            run_dir=run_dir,
            ref_audio=ref_audio,
            ref_text=args.ref_text,
        )
        payload["direction_wavs"] = dumps
        payload.setdefault("direction_notes", {})
        _persist_metrics(run_dir, payload)
        return 0

    if args.full and not args.configs:
        args.configs = _FULL_CONFIGS
    elif not args.configs:
        args.configs = "A"

    raw_names = [part.strip() for part in args.configs.split(",") if part.strip()]
    full_mixed = bool(args.full) and (
        "B" in raw_names or (set(raw_names) & {"A", *_C_LADDER_NAMES, *_E_LADDER_NAMES, "D"})
    )
    if full_mixed and not args.smoke:
        expanded: list[str] = []
        for name in raw_names:
            if name == "B":
                expanded.extend(["A", *_B_ARM_NAMES])
            else:
                expanded.append(name)
        seen: set[str] = set()
        names = []
        for name in expanded:
            if name in seen:
                continue
            seen.add(name)
            names.append(name)
        known = _config_by_name()
        missing = [name for name in names if name not in known]
        if missing:
            raise SystemExit(f"unknown configs: {', '.join(missing)}")
        b_sweep = False
        c_sweep = False
        e_sweep = False
        d_run = False
    else:
        names, b_sweep, c_sweep, e_sweep = _parse_config_names(args.configs)
        d_run = names == ["D"]
        if not args.smoke and not b_sweep and not c_sweep and not e_sweep and not d_run:
            parser.error(
                "only --smoke, --full, --configs B, --configs C0,C1,C2,C3,C4, --configs D, or --configs E1,E2,E3,E4,E5 is implemented"
            )

    qual_root = Path(args.qual_root)
    ref_audio = Path(args.ref_audio)
    if not ref_audio.is_file():
        raise SystemExit(f"missing ref audio: {ref_audio}")
    by_name = _config_by_name()
    run_dir = qual_root / "runs" / args.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    if args.full:
        (run_dir / "wavs").mkdir(parents=True, exist_ok=True)

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
        "smoke": bool(args.smoke) and not b_sweep and not e_sweep and not d_run and not args.full,
        "full": bool(args.full),
        "qual_root": str(qual_root).replace(str(Path.home()), "~"),
        "n": n_short if (b_sweep or c_sweep or e_sweep or d_run or args.full) else None,
        "configs": {},
    }
    exit_code = 0

    if full_mixed and not args.smoke:
        want_b = any(name in {"A", *_B_ARM_NAMES} for name in names)
        want_c = any(name in _C_LADDER_NAMES for name in names)
        want_e = any(name in _E_LADDER_NAMES for name in names)
        want_d = "D" in names
        if want_b:
            b_names = [name for name in ["A", *_B_ARM_NAMES] if name in names]
            for name in b_names:
                print(f"full-30 start {name}", flush=True)
                row, code = _run_measured_config(
                    config=by_name[name],
                    qual_root=qual_root,
                    run_dir=run_dir,
                    ref_audio=ref_audio,
                    ref_text=args.ref_text,
                    n=n_short,
                    warmup=warmup,
                    full=True,
                )
                payload["configs"][name] = row
                if code != 0 and exit_code == 0:
                    exit_code = code
                _persist_metrics(run_dir, payload)
                print(f"full-30 done {name} not_run={bool(row.get('not_run'))}", flush=True)
            for arm_name in _B_ARM_NAMES:
                payload["configs"].setdefault(
                    arm_name,
                    _skipped_arm_row(by_name[arm_name], reason="not-run"),
                )
            payload["configs"]["B_winner"] = _compose_b_payload(payload["configs"])
            _persist_metrics(run_dir, payload)
        if want_c:
            print("full-30 start C", flush=True)
            rows, code = _run_c_sweep(
                by_name=by_name,
                qual_root=qual_root,
                run_dir=run_dir,
                ref_audio=ref_audio,
                ref_text=args.ref_text,
                n=n_short,
                warmup=warmup,
                full=True,
            )
            payload["configs"].update(rows)
            if code != 0 and exit_code == 0:
                exit_code = code
            _persist_metrics(run_dir, payload)
            print("full-30 done C", flush=True)
        if want_e:
            print("full-30 start E", flush=True)
            rows, code, rejected = _run_e_sweep(
                by_name=by_name,
                qual_root=qual_root,
                run_dir=run_dir,
                ref_audio=ref_audio,
                ref_text=args.ref_text,
                n=n_short,
                warmup=warmup,
                full=True,
            )
            payload["configs"].update(rows)
            payload["e_rejected_for_our_purposes"] = rejected
            if code != 0 and exit_code == 0:
                exit_code = code
            _persist_metrics(run_dir, payload)
            print("full-30 done E", flush=True)
        if want_d:
            print("full-30 start D", flush=True)
            killed_row = _d_already_killed(qual_root)
            if killed_row is not None:
                payload["configs"]["D"] = killed_row
                payload["d_killed"] = True
            else:
                row, code, killed = _run_d_control(
                    config=by_name["D"],
                    qual_root=qual_root,
                    run_dir=run_dir,
                    ref_audio=ref_audio,
                    ref_text=args.ref_text,
                    n=n_short,
                    warmup=warmup,
                    full=True,
                )
                payload["configs"]["D"] = row
                payload["d_killed"] = killed
                if code != 0 and exit_code == 0:
                    exit_code = code
            _persist_metrics(run_dir, payload)
            print("full-30 done D", flush=True)
    elif e_sweep:
        rows, code, rejected = _run_e_sweep(
            by_name=by_name,
            qual_root=qual_root,
            run_dir=run_dir,
            ref_audio=ref_audio,
            ref_text=args.ref_text,
            n=n_short,
            warmup=warmup,
            full=bool(args.full),
        )
        payload["configs"] = rows
        payload["e_rejected_for_our_purposes"] = rejected
        exit_code = code
    elif c_sweep:
        rows, code = _run_c_sweep(
            by_name=by_name,
            qual_root=qual_root,
            run_dir=run_dir,
            ref_audio=ref_audio,
            ref_text=args.ref_text,
            n=n_short,
            warmup=warmup,
            full=bool(args.full),
        )
        payload["configs"] = rows
        exit_code = code

    elif b_sweep:
        reused_a = None if args.full else _reuse_a_metrics(qual_root)
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
                full=bool(args.full),
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
    elif d_run:
        killed_row = _d_already_killed(qual_root) if args.full else None
        if killed_row is not None:
            payload["configs"]["D"] = killed_row
            payload["d_killed"] = True
        else:
            row, code, killed = _run_d_control(
                config=by_name["D"],
                qual_root=qual_root,
                run_dir=run_dir,
                ref_audio=ref_audio,
                ref_text=args.ref_text,
                n=n_short,
                warmup=warmup,
                full=bool(args.full),
            )
            payload["configs"]["D"] = row
            payload["d_killed"] = killed
            exit_code = code
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
