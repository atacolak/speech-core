#!/usr/bin/env python3
"""Isolated CosyVoice GPU qualification harness for sc-e71.4.

Does not touch Supertonic venvs or stop speech-core/speech-out services.
Writes JSON metrics + optional PCM/WAV artifacts for independent review.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import resource
import signal
import subprocess
import sys
import time
import traceback
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def host_snapshot() -> Dict[str, Any]:
    snap: Dict[str, Any] = {"ts": utc_now()}
    try:
        out = subprocess.check_output(
            ["ps", "-o", "pid=,etime=,cmd=", "-C", "speech-core-daemon,speech-out"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        snap["speech_processes"] = [ln.strip() for ln in out.splitlines() if ln.strip()]
    except Exception as exc:  # noqa: BLE001
        snap["speech_processes_error"] = str(exc)
        # fallback: pgrep style
        try:
            out = subprocess.check_output(["ps", "-eo", "pid=,etime=,cmd="], text=True)
            snap["speech_processes"] = [
                ln.strip()
                for ln in out.splitlines()
                if "speech-core-daemon" in ln or ("speech-out" in ln and "daemon" in ln)
            ]
        except Exception as exc2:  # noqa: BLE001
            snap["speech_processes_fallback_error"] = str(exc2)

    try:
        out = subprocess.check_output(["ss", "-ltnp"], text=True, stderr=subprocess.DEVNULL)
        snap["listen_ports"] = [
            ln.strip() for ln in out.splitlines() if ":8788" in ln or ":7788" in ln
        ]
    except Exception as exc:  # noqa: BLE001
        snap["listen_ports_error"] = str(exc)

    try:
        with open("/proc/driver/nvidia/version") as f:
            snap["nvidia_kernel"] = f.read().strip()
    except Exception as exc:  # noqa: BLE001
        snap["nvidia_kernel_error"] = str(exc)

    try:
        gpu = Path("/proc/driver/nvidia/gpus")
        if gpu.exists():
            info = next(gpu.glob("*/information"))
            text = info.read_text()
            for key in ("Model:", "GPU UUID:", "Bus Location:", "GPU Firmware:"):
                for ln in text.splitlines():
                    if ln.strip().startswith(key):
                        snap.setdefault("gpu_info", {})[key.rstrip(":")] = ln.split(":", 1)[
                            1
                        ].strip()
    except Exception as exc:  # noqa: BLE001
        snap["gpu_info_error"] = str(exc)

    return snap


def vram_mb() -> Dict[str, float]:
    import torch

    if not torch.cuda.is_available():
        return {"available": 0.0}
    free, total = torch.cuda.mem_get_info()
    allocated = torch.cuda.memory_allocated()
    reserved = torch.cuda.memory_reserved()
    peak = torch.cuda.max_memory_allocated()
    return {
        "free_mb": free / (1024 * 1024),
        "total_mb": total / (1024 * 1024),
        "allocated_mb": allocated / (1024 * 1024),
        "reserved_mb": reserved / (1024 * 1024),
        "peak_allocated_mb": peak / (1024 * 1024),
        "used_mb": (total - free) / (1024 * 1024),
    }


@dataclass
class ChunkMetric:
    index: int
    t_rel_s: float
    n_samples: int
    duration_s: float


@dataclass
class UtteranceResult:
    name: str
    mode: str
    text: str
    ok: bool
    error: Optional[str] = None
    t_start: Optional[str] = None
    first_pcm_s: Optional[float] = None
    total_s: Optional[float] = None
    audio_duration_s: Optional[float] = None
    rtf: Optional[float] = None
    n_chunks: int = 0
    n_samples: int = 0
    sample_rate: int = 0
    vram_before_mb: Dict[str, float] = field(default_factory=dict)
    vram_after_mb: Dict[str, float] = field(default_factory=dict)
    vram_peak_mb: Dict[str, float] = field(default_factory=dict)
    chunks: List[Dict[str, Any]] = field(default_factory=list)
    wav_path: Optional[str] = None
    cancelled: bool = False
    cancel_after_chunks: Optional[int] = None
    cancel_ack_s: Optional[float] = None
    post_cancel_chunks: int = 0


def save_wav(path: Path, speech, sample_rate: int) -> None:
    import torch
    import torchaudio

    if not isinstance(speech, torch.Tensor):
        speech = torch.tensor(speech)
    if speech.dim() == 1:
        speech = speech.unsqueeze(0)
    path.parent.mkdir(parents=True, exist_ok=True)
    torchaudio.save(str(path), speech.cpu().float(), sample_rate)


def run_inference(
    cosyvoice,
    *,
    name: str,
    text: str,
    prompt_text: str,
    prompt_wav: str,
    stream: bool,
    artifacts_dir: Path,
    cancel_after_chunks: Optional[int] = None,
) -> UtteranceResult:
    import torch

    result = UtteranceResult(
        name=name,
        mode="stream" if stream else "buffered",
        text=text,
        ok=False,
        sample_rate=int(cosyvoice.sample_rate),
        cancel_after_chunks=cancel_after_chunks,
    )
    result.t_start = utc_now()
    torch.cuda.reset_peak_memory_stats()
    result.vram_before_mb = vram_mb()

    pieces = []
    chunks: List[ChunkMetric] = []
    t0 = time.perf_counter()
    first_pcm_s = None
    cancel_t = None
    post_cancel = 0
    stopped = False

    try:
        gen = cosyvoice.inference_zero_shot(
            text,
            prompt_text,
            prompt_wav,
            stream=stream,
            text_frontend=True,
        )
        for i, out in enumerate(gen):
            t_rel = time.perf_counter() - t0
            speech = out["tts_speech"]
            n = int(speech.shape[-1])
            if first_pcm_s is None and n > 0:
                first_pcm_s = t_rel
            if stopped:
                post_cancel += 1
                continue
            pieces.append(speech.detach().cpu())
            chunks.append(
                ChunkMetric(
                    index=i,
                    t_rel_s=t_rel,
                    n_samples=n,
                    duration_s=n / float(cosyvoice.sample_rate),
                )
            )
            if cancel_after_chunks is not None and (i + 1) >= cancel_after_chunks:
                cancel_t = time.perf_counter()
                stopped = True
                # consumer-side stop only — documents native lack of cancel
                break
        total_s = time.perf_counter() - t0
        if cancel_t is not None:
            result.cancel_ack_s = cancel_t - t0
            result.cancelled = True
        result.first_pcm_s = first_pcm_s
        result.total_s = total_s
        result.n_chunks = len(chunks)
        result.chunks = [asdict(c) for c in chunks]
        result.post_cancel_chunks = post_cancel

        if pieces:
            import torch as _torch

            audio = _torch.cat(pieces, dim=-1)
            result.n_samples = int(audio.shape[-1])
            result.audio_duration_s = result.n_samples / float(cosyvoice.sample_rate)
            if result.audio_duration_s > 0:
                result.rtf = total_s / result.audio_duration_s
            wav_path = artifacts_dir / f"{name}.wav"
            save_wav(wav_path, audio, int(cosyvoice.sample_rate))
            result.wav_path = str(wav_path)
        result.ok = True
    except Exception as exc:  # noqa: BLE001
        result.error = f"{type(exc).__name__}: {exc}"
        result.ok = False
        traceback.print_exc()
    finally:
        result.vram_after_mb = vram_mb()
        result.vram_peak_mb = {
            "peak_allocated_mb": vram_mb().get("peak_allocated_mb", 0.0),
            **{k: v for k, v in vram_mb().items() if k == "peak_allocated_mb"},
        }
        # prefer true peak from torch
        import torch as _torch

        if _torch.cuda.is_available():
            result.vram_peak_mb = {
                "peak_allocated_mb": _torch.cuda.max_memory_allocated() / (1024 * 1024),
                "peak_reserved_mb": _torch.cuda.max_memory_reserved() / (1024 * 1024),
                "used_mb": vram_mb().get("used_mb", 0.0),
            }
    return result


def process_per_utterance_cancel(
    *,
    python: str,
    repo_root: Path,
    model_dir: Path,
    prompt_wav: Path,
    prompt_text: str,
    text: str,
    artifacts_dir: Path,
    kill_after_s: float,
) -> Dict[str, Any]:
    """Qualify hard cancel via process-per-utterance (recommended adaptation)."""
    worker = artifacts_dir / "_cancel_worker.py"
    worker.write_text(
        f"""#!/usr/bin/env python3
import sys, time
from pathlib import Path
sys.path.insert(0, {str(repo_root)!r})
sys.path.insert(0, {str(repo_root / 'third_party' / 'Matcha-TTS')!r})
from cosyvoice.cli.cosyvoice import AutoModel
import torch

model_dir = {str(model_dir)!r}
prompt_wav = {str(prompt_wav)!r}
prompt_text = {prompt_text!r}
text = {text!r}
out_path = Path({str(artifacts_dir / 'cancel_process_partial.json')!r})

t0 = time.perf_counter()
cv = AutoModel(model_dir=model_dir)
load_s = time.perf_counter() - t0
chunks = []
first = None
t1 = time.perf_counter()
for i, out in enumerate(cv.inference_zero_shot(text, prompt_text, prompt_wav, stream=True)):
    t_rel = time.perf_counter() - t1
    n = int(out['tts_speech'].shape[-1])
    if first is None and n > 0:
        first = t_rel
    chunks.append({{"i": i, "t_rel_s": t_rel, "n": n}})
    # heartbeat for parent
    print(f"CHUNK {{i}} {{t_rel:.4f}} {{n}}", flush=True)
out_path.write_text(__import__('json').dumps({{
    "load_s": load_s,
    "first_pcm_s": first,
    "chunks": chunks,
    "completed": True,
}}))
print("DONE", flush=True)
"""
    )
    t_start = time.perf_counter()
    proc = subprocess.Popen(
        [python, str(worker)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    saw_chunk = False
    lines: List[str] = []
    kill_s = None
    deadline = time.perf_counter() + 180.0
    try:
        assert proc.stdout is not None
        while time.perf_counter() < deadline:
            line = proc.stdout.readline()
            if line:
                lines.append(line.rstrip())
                if line.startswith("CHUNK") and not saw_chunk:
                    saw_chunk = True
                    # allow kill_after_s after first chunk
                    time.sleep(max(0.0, kill_after_s))
                    os.killpg(proc.pid, signal.SIGKILL)
                    kill_s = time.perf_counter() - t_start
                    break
            elif proc.poll() is not None:
                break
            else:
                time.sleep(0.01)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": str(exc),
            "lines": lines,
            "method": "process-per-utterance-SIGKILL",
        }

    # drain remaining
    try:
        rest, _ = proc.communicate(timeout=2)
        if rest:
            lines.extend(rest.splitlines())
    except Exception:
        pass

    return {
        "ok": True,
        "method": "process-per-utterance-SIGKILL",
        "saw_first_chunk": saw_chunk,
        "kill_after_first_chunk_s": kill_after_s,
        "t_kill_s": kill_s,
        "returncode": proc.returncode,
        "stdout_tail": lines[-30:],
        "note": (
            "Process kill is the practical cancel boundary; in-process generator break "
            "does not stop the LLM worker thread (p.join runs to completion)."
        ),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--qual-root",
        default=os.environ.get(
            "COSYVOICE_QUAL_ROOT",
            os.path.expanduser("~/.cache/speech-out/cosyvoice-qual-sc-e71.4"),
        ),
    )
    ap.add_argument("--run-id", default=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    ap.add_argument("--skip-process-cancel", action="store_true")
    args = ap.parse_args()

    qual_root = Path(args.qual_root)
    repo_root = qual_root / "src" / "CosyVoice"
    model_dir = qual_root / "models" / "Fun-CosyVoice3-0.5B"
    prompt_wav = repo_root / "asset" / "zero_shot_prompt.wav"
    run_dir = qual_root / "runs" / args.run_id
    artifacts_dir = run_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    report: Dict[str, Any] = {
        "bead": "sc-e71.4",
        "run_id": args.run_id,
        "started_at": utc_now(),
        "pin": {
            "code_repo": "https://github.com/QwenAudio/CosyVoice",
            "code_commit": None,
            "model_id": "FunAudioLLM/Fun-CosyVoice3-0.5B-2512",
            "model_revision": "29e01c4e8d000f4bcd70751be16fa94bf3d85a18",
            "model_dir": str(model_dir),
            "python": sys.version,
            "qual_root": str(qual_root),
        },
        "host_before": host_snapshot(),
        "legal": {
            "code_license_tag": "Apache-2.0",
            "model_card_license_tag": "apache-2.0",
            "model_card_disclaimer": (
                "The content provided above is for academic purposes only and is "
                "intended to demonstrate technical capabilities."
            ),
            "ambiguity": (
                "License tag is Apache-2.0 but model card disclaimer restricts to "
                "academic purposes. Legal review required before product use."
            ),
        },
        "gates": {
            "warm_first_pcm_ms": "300-500",
            "cancel_hop": "<=1 hop after cancel wrapper",
            "vram_solo_gb": "<=8-9",
            "rtf": "<1",
            "host_impact": "no existing service replaced/stopped",
        },
        "results": {},
        "failures": [],
    }

    # code commit
    try:
        report["pin"]["code_commit"] = subprocess.check_output(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"], text=True
        ).strip()
        report["pin"]["submodule_matcha"] = subprocess.check_output(
            ["git", "-C", str(repo_root / "third_party" / "Matcha-TTS"), "rev-parse", "HEAD"],
            text=True,
        ).strip()
    except Exception as exc:  # noqa: BLE001
        report["failures"].append(f"git_pin: {exc}")

    # file hashes
    key_files = [
        "llm.pt",
        "flow.pt",
        "hift.pt",
        "speech_tokenizer_v3.onnx",
        "campplus.onnx",
        "CosyVoice-BlankEN/model.safetensors",
        "cosyvoice3.yaml",
    ]
    hashes = {}
    for rel in key_files:
        p = model_dir / rel
        if p.exists():
            hashes[rel] = {
                "sha256": sha256_file(p),
                "bytes": p.stat().st_size,
            }
        else:
            report["failures"].append(f"missing_model_file: {rel}")
    if prompt_wav.exists():
        hashes["asset/zero_shot_prompt.wav"] = {
            "sha256": sha256_file(prompt_wav),
            "bytes": prompt_wav.stat().st_size,
            "path": str(prompt_wav),
        }
    report["pin"]["hashes"] = hashes

    # env
    try:
        import torch

        report["env"] = {
            "torch": torch.__version__,
            "cuda": torch.version.cuda,
            "cuda_available": torch.cuda.is_available(),
            "device_name": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
            "device_capability": (
                list(torch.cuda.get_device_capability(0)) if torch.cuda.is_available() else None
            ),
            "vram_idle_mb": vram_mb() if torch.cuda.is_available() else {},
        }
        if not torch.cuda.is_available():
            report["failures"].append("cuda_not_available")
            (run_dir / "report.json").write_text(json.dumps(report, indent=2))
            print(json.dumps(report, indent=2))
            return 2
    except Exception as exc:  # noqa: BLE001
        report["failures"].append(f"torch_import: {exc}")
        (run_dir / "report.json").write_text(json.dumps(report, indent=2))
        return 2

    sys.path.insert(0, str(repo_root))
    sys.path.insert(0, str(repo_root / "third_party" / "Matcha-TTS"))

    # cold load
    print(f"[qual] cold load AutoModel from {model_dir}", flush=True)
    t_load0 = time.perf_counter()
    try:
        from cosyvoice.cli.cosyvoice import AutoModel

        cosyvoice = AutoModel(model_dir=str(model_dir))
        cold_load_s = time.perf_counter() - t_load0
        report["results"]["cold_load_s"] = cold_load_s
        report["results"]["sample_rate"] = int(cosyvoice.sample_rate)
        report["results"]["vram_after_load_mb"] = vram_mb()
        print(
            f"[qual] cold_load_s={cold_load_s:.3f} sr={cosyvoice.sample_rate} "
            f"vram_used_mb={vram_mb().get('used_mb', 0):.1f}",
            flush=True,
        )
    except Exception as exc:  # noqa: BLE001
        report["failures"].append(f"model_load: {type(exc).__name__}: {exc}")
        report["results"]["load_traceback"] = traceback.format_exc()
        report["finished_at"] = utc_now()
        report["host_after"] = host_snapshot()
        (run_dir / "report.json").write_text(json.dumps(report, indent=2))
        print(traceback.format_exc())
        return 3

    # warm reload timing (second construction not free; instead time a no-op cuda sync + measure
    # first inference as cold-infer and second as warm)
    prompt_text = "You are a helpful assistant.<|endofprompt|>希望你以后能够做的比我还好呦。"
    utterances = [
        {
            "name": "en_zero_shot_stream_cold",
            "text": (
                "CosyVoice is undergoing a comprehensive upgrade, providing more accurate, "
                "stable, faster, and better voice generation capabilities."
            ),
            "stream": True,
        },
        {
            "name": "en_zero_shot_stream_warm",
            "text": (
                "CosyVoice is undergoing a comprehensive upgrade, providing more accurate, "
                "stable, faster, and better voice generation capabilities."
            ),
            "stream": True,
        },
        {
            "name": "en_short_stream_warm",
            "text": "Hello. This is a short warm-path latency probe.",
            "stream": True,
        },
        {
            "name": "zh_zero_shot_stream_warm",
            "text": "八百标兵奔北坡，北坡炮兵并排跑，炮兵怕把标兵碰，标兵怕碰炮兵炮。",
            "stream": True,
        },
        {
            "name": "en_zero_shot_buffered_warm",
            "text": "Buffered path check. This utterance disables stream mode on purpose.",
            "stream": False,
        },
        {
            "name": "en_cancel_break_after_1_chunk",
            "text": (
                "This utterance is intentionally long so cancellation can be observed after the "
                "first progressive PCM chunk arrives from the streaming synthesis path. "
                "We keep speaking to give the worker thread more tokens to produce."
            ),
            "stream": True,
            "cancel_after_chunks": 1,
        },
    ]

    utt_results: List[Dict[str, Any]] = []
    for spec in utterances:
        print(f"[qual] run {spec['name']} stream={spec['stream']}", flush=True)
        r = run_inference(
            cosyvoice,
            name=spec["name"],
            text=spec["text"],
            prompt_text=prompt_text,
            prompt_wav=str(prompt_wav),
            stream=spec["stream"],
            artifacts_dir=artifacts_dir,
            cancel_after_chunks=spec.get("cancel_after_chunks"),
        )
        utt_results.append(asdict(r))
        print(
            f"[qual]   ok={r.ok} first_pcm_s={r.first_pcm_s} total_s={r.total_s} "
            f"audio_s={r.audio_duration_s} rtf={r.rtf} chunks={r.n_chunks} "
            f"peak_mb={r.vram_peak_mb.get('peak_allocated_mb')} err={r.error}",
            flush=True,
        )

    report["results"]["utterances"] = utt_results

    # summarize gate evaluation (evidence only; no self-certify)
    warm = next((u for u in utt_results if u["name"] == "en_zero_shot_stream_warm"), None)
    short = next((u for u in utt_results if u["name"] == "en_short_stream_warm"), None)
    cancel = next(
        (u for u in utt_results if u["name"] == "en_cancel_break_after_1_chunk"), None
    )
    peak_used = max(
        (u.get("vram_peak_mb", {}) or {}).get("used_mb") or 0.0 for u in utt_results
    ) if utt_results else 0.0
    report["results"]["summary"] = {
        "warm_first_pcm_s": warm.get("first_pcm_s") if warm else None,
        "warm_rtf": warm.get("rtf") if warm else None,
        "short_first_pcm_s": short.get("first_pcm_s") if short else None,
        "cancel_consumer_break": {
            "first_pcm_s": cancel.get("first_pcm_s") if cancel else None,
            "cancel_ack_s": cancel.get("cancel_ack_s") if cancel else None,
            "n_chunks_kept": cancel.get("n_chunks") if cancel else None,
            "post_cancel_chunks_observed_after_break": (
                cancel.get("post_cancel_chunks") if cancel else None
            ),
            "note": (
                "break stops consuming generator; CosyVoice worker thread still joins. "
                "Not a true synthesis cancel."
            ),
        },
        "peak_vram_used_mb_across_utts": peak_used,
        "cold_load_s": report["results"].get("cold_load_s"),
    }

    if not args.skip_process_cancel:
        print("[qual] process-per-utterance cancel probe", flush=True)
        # Use a separate process that loads model again — expensive but documents real cancel.
        # To keep wall time bounded, only run if env allows; default on.
        try:
            report["results"]["process_cancel"] = process_per_utterance_cancel(
                python=sys.executable,
                repo_root=repo_root,
                model_dir=model_dir,
                prompt_wav=prompt_wav,
                prompt_text=prompt_text,
                text=(
                    "Process cancel probe. Keep generating progressive audio long enough "
                    "for the parent to observe the first chunk and send SIGKILL."
                ),
                artifacts_dir=artifacts_dir,
                kill_after_s=0.05,
            )
        except Exception as exc:  # noqa: BLE001
            report["results"]["process_cancel"] = {
                "ok": False,
                "error": str(exc),
                "traceback": traceback.format_exc(),
            }

    # cleanup attempt
    try:
        import torch
        import gc

        del cosyvoice
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            torch.cuda.synchronize()
        report["results"]["vram_after_cleanup_mb"] = vram_mb()
    except Exception as exc:  # noqa: BLE001
        report["failures"].append(f"cleanup: {exc}")

    report["host_after"] = host_snapshot()
    report["finished_at"] = utc_now()
    report["resource_ru_maxrss_kb"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    # service intact check
    before_p = {
        ln.split()[0]
        for ln in report["host_before"].get("speech_processes", [])
        if ln.split()
    }
    after_p = {
        ln.split()[0]
        for ln in report["host_after"].get("speech_processes", [])
        if ln.split()
    }
    report["results"]["services_intact"] = {
        "before_pids": sorted(before_p),
        "after_pids": sorted(after_p),
        "pids_unchanged": before_p == after_p and len(before_p) > 0,
        "port_8788_before": report["host_before"].get("listen_ports"),
        "port_8788_after": report["host_after"].get("listen_ports"),
    }

    out_json = run_dir / "report.json"
    out_json.write_text(json.dumps(report, indent=2, default=str))
    print(f"[qual] wrote {out_json}", flush=True)
    print(json.dumps(report["results"].get("summary", {}), indent=2, default=str))
    return 0 if not report["failures"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
