"""not on the voicecat path.

Optional Stream.FM reference preprocessing with content-addressed cache.
Never silently replace the original. Never write derivatives next to the
source or the operator Downloads folder. Cache identity is original source
bytes + edit spec + task + checkpoint + solver + config + implementation.
Processing runs on the effective (post-edit) reference.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from tts.edits import EditSpec, apply_edits
from tts.hashes import sha256_file, sha256_json
from tts.paths import ensure_lab_dirs
from tts.wav import is_riff_wav, read_wav, write_wav

STREAMFM_TASK = "se-predgen"
STREAMFM_SOLVER = "lrk4"
STREAMFM_CHECKPOINT = os.environ.get("STREAMFM_CHECKPOINT", "unavailable")
PREPROCESS_VERSION = "tts-streamfm-v2"


def cache_key(
    *,
    source_sha256: str,
    task: str = STREAMFM_TASK,
    checkpoint: str = STREAMFM_CHECKPOINT,
    solver: str = STREAMFM_SOLVER,
    config: dict[str, Any] | None = None,
    edit_spec: dict[str, Any] | None = None,
) -> str:
    return sha256_json(
        {
            "source_sha256": source_sha256,
            "edit_spec": EditSpec.from_dict(edit_spec).canonical(),
            "task": task,
            "checkpoint": checkpoint,
            "solver": solver,
            "config": config or {},
            "preprocess_version": PREPROCESS_VERSION,
        }
    )[:16]


def sidecar_name(source: Path, key: str) -> str:
    """Cache filename only. Do not join this onto the source directory."""
    del source
    return f"streamfm_{STREAMFM_TASK}_{STREAMFM_SOLVER}_{key}.wav"


def cache_dest(lab_root: Path | None, key: str) -> Path:
    root = ensure_lab_dirs(lab_root)
    dest = root / "cache" / f"streamfm_{STREAMFM_TASK}_{STREAMFM_SOLVER}_{key}.wav"
    dest.parent.mkdir(parents=True, exist_ok=True)
    return dest


def available() -> dict[str, Any]:
    cli = shutil.which("streamfm") or shutil.which("stream.fm")
    return {
        "cli": cli,
        "importable": False,
        "checkpoint": STREAMFM_CHECKPOINT,
        "task": STREAMFM_TASK,
        "solver": STREAMFM_SOLVER,
        "ready": bool(cli) and STREAMFM_CHECKPOINT != "unavailable",
    }


def _load_audio(src: Path) -> tuple[int, Any]:
    if is_riff_wav(src):
        return read_wav(src)
    from breeze_tts_qual.transcribe import coerce_audio

    return coerce_audio(src)


def materialize_effective_wav(
    source: Path | str,
    edit_spec: dict[str, Any] | EditSpec | None,
    *,
    lab_root: Path | None = None,
) -> Path:
    src = Path(source)
    spec = edit_spec if isinstance(edit_spec, EditSpec) else EditSpec.from_dict(edit_spec)
    source_sha = sha256_file(src)
    key = sha256_json(
        {
            "source_sha256": source_sha,
            "edits": spec.canonical(),
            "preprocess_version": PREPROCESS_VERSION,
        }
    )[:16]
    dest = ensure_lab_dirs(lab_root) / "cache" / f"effective_{key}.wav"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and is_riff_wav(dest):
        return dest
    sr, samples = _load_audio(src)
    audio = apply_edits(sr, samples, spec) if spec.ops else samples
    write_wav(dest, sr, audio)
    return dest


def process_reference(
    source: Path | str,
    *,
    lab_root: Path | None = None,
    config: dict[str, Any] | None = None,
    edit_spec: dict[str, Any] | None = None,
) -> dict[str, Any]:
    src = Path(source)
    if not src.is_file():
        raise FileNotFoundError(src)
    spec = EditSpec.from_dict(edit_spec)
    source_sha = sha256_file(src)
    key = cache_key(source_sha256=source_sha, config=config, edit_spec=spec.to_dict())
    dest = cache_dest(lab_root, key)
    meta_path = dest.with_suffix(".json")
    effective = materialize_effective_wav(src, spec, lab_root=lab_root)
    status = available()
    record = {
        "source": str(src),
        "source_sha256": source_sha,
        "edit_spec": spec.to_dict(),
        "effective": str(effective),
        "task": STREAMFM_TASK,
        "checkpoint": STREAMFM_CHECKPOINT,
        "solver": STREAMFM_SOLVER,
        "config": config or {},
        "preprocess_version": PREPROCESS_VERSION,
        "cache_key": key,
        "output": str(dest),
        "status": "unavailable",
        "processor": "stream.fm",
    }
    if dest.is_file() and is_riff_wav(dest):
        record["status"] = "cache_hit"
        record["output_sha256"] = sha256_file(dest)
        return record
    if not status["ready"]:
        record["detail"] = (
            "stream.fm is optional and not installed in this lab. "
            "Original/effective reference is preserved; do not treat missing "
            "enhancement as a clone."
        )
        meta_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        return record
    cmd = [
        str(status["cli"]),
        "--task",
        STREAMFM_TASK,
        "--solver",
        STREAMFM_SOLVER,
        "--checkpoint",
        STREAMFM_CHECKPOINT,
        "--input",
        str(effective),
        "--output",
        str(dest),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=False)
    if proc.returncode != 0 or not dest.is_file() or not is_riff_wav(dest):
        record["status"] = "failed"
        record["detail"] = (proc.stderr or proc.stdout or "stream.fm failed").strip()
        meta_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        return record
    record["status"] = "ok"
    record["output_sha256"] = sha256_file(dest)
    meta_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return record


def deepfilternet_available() -> bool:
    return shutil.which("deepFilter") is not None or shutil.which("deep-filter") is not None


def compare_references(
    source: Path | str,
    *,
    lab_root: Path | None = None,
    edit_spec: dict[str, Any] | None = None,
) -> dict[str, Any]:
    src = Path(source)
    spec = EditSpec.from_dict(edit_spec)
    effective = materialize_effective_wav(src, spec, lab_root=lab_root)
    arms = {
        "original": {
            "path": str(effective),
            "status": "ok" if Path(effective).is_file() else "missing",
            "variant": "original",
            "edits": spec.to_dict(),
        }
    }
    arms["stream.fm"] = process_reference(src, lab_root=lab_root, edit_spec=spec.to_dict())
    df = {
        "processor": "deepfilternet",
        "status": "unavailable",
        "detail": "optional cleaner; not wired unless deepFilter CLI is on PATH",
        "ready": deepfilternet_available(),
    }
    if df["ready"]:
        df["status"] = "cli-present-not-run"
        df["detail"] = "CLI present; batch comparison run is operator-triggered."
    arms["deepfilternet"] = df
    return {
        "source": str(src),
        "effective": str(effective),
        "arms": arms,
        "note": (
            "Do not assume cleaner audio is a better clone. Synthesize the same "
            "text/steer/seed/breeze settings against each arm."
        ),
    }
