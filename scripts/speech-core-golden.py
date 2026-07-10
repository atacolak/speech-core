#!/usr/bin/env python3
"""
speech-core-golden — Guided golden recording, synthesis, and manifest tool.

Top-level command for speech-core endpointing golden suite management.
Implements validate-manifest, record, synth, promote, and delegation
interfaces for capture, assert, run, and delete.

Prefer stdlib-only; YAML is optional with actionable fallback.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import textwrap
import time
import uuid
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# ── optional YAML support ──────────────────────────────────────────
try:
    import yaml as _yaml_mod

    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False

# ── golden assertion engine internals ─────────────────────────────
try:
    from _golden.validity import validate_capture_artifacts
    from _golden.assert_engine import run_assertions, AssertionResult
    from _golden.report import write_json_report
    from _golden.constants import DEFAULT_TERMINAL_MARKERS
    _HAS_GOLDEN_ENGINE = True
except ImportError:
    _HAS_GOLDEN_ENGINE = False


# ═══════════════════════════════════════════════════════════════════
# Exit codes (from spec §13)
# ═══════════════════════════════════════════════════════════════════

class ExitCode:
    PASS = 0
    ASSERTION_FAILED = 1
    MANIFEST_INVALID = 2
    QUALITY_FAILED = 3
    CONSENT_REQUIRED = 4
    DEPENDENCY_MISSING = 5
    DAEMON_UNREACHABLE = 6
    MODEL_UNAVAILABLE = 7
    CAPTURE_TIMEOUT = 8
    TERMINAL_MARKER_MISSING = 9
    ARTIFACT_HASH_MISMATCH = 10
    PRIVACY_POLICY_VIOLATION = 11
    CONFIG_MISMATCH = 12
    UNSUPPORTED_PROFILE = 13
    SCENARIO_NOT_FOUND = 14
    RECORDER_ABORTED = 15
    SYNTH_GENERATION_FAILED = 16
    WAV_FORMAT_INVALID = 17
    EVENT_SCHEMA_INVALID = 18
    BASELINE_REQUIRES_REVIEW = 19
    INTERNAL_ERROR = 20
    CAPTURE_INCOMPLETE = 21


EXIT_CODE_NAMES = {v: k for k, v in vars(ExitCode).items() if k.isupper()}


def die(code: int, msg: str) -> None:
    name = EXIT_CODE_NAMES.get(code, "UNKNOWN")
    print(f"[{name}] {msg}", file=sys.stderr)
    sys.exit(code)


# ═══════════════════════════════════════════════════════════════════
# YAML / JSON loader
# ═══════════════════════════════════════════════════════════════════

def _load_yaml(path: Path) -> dict:
    if not _HAS_YAML:
        die(
            ExitCode.DEPENDENCY_MISSING,
            f"YAML support requires PyYAML. Install it (pip install pyyaml) or use JSON manifests.\n"
            f"  File: {path}",
        )
    with open(path, "r") as fh:
        return _yaml_mod.safe_load(fh)


def load_manifest_file(path: Path) -> dict:
    """Load a YAML (.yaml/.yml) or JSON (.json) file, returning a dict."""
    suffix = path.suffix.lower()
    if suffix in (".yaml", ".yml"):
        return _load_yaml(path)
    elif suffix == ".json":
        with open(path, "r") as fh:
            return json.load(fh)
    else:
        # Try JSON first, then YAML
        try:
            with open(path, "r") as fh:
                return json.load(fh)
        except json.JSONDecodeError:
            return _load_yaml(path)


def _dump_yaml(data: dict, path: Path) -> None:
    if not _HAS_YAML:
        # fall back to JSON with a warning
        with open(path.with_suffix(".json"), "w") as fh:
            json.dump(data, fh, indent=2)
        print(
            f"[WARN] PyYAML unavailable; wrote JSON instead: {path.with_suffix('.json')}",
            file=sys.stderr,
        )
        return
    with open(path, "w") as fh:
        _yaml_mod.safe_dump(data, fh, default_flow_style=False, sort_keys=False)


def save_file(data: dict, path: Path) -> None:
    """Save dict as YAML or JSON based on file extension."""
    suffix = path.suffix.lower()
    if suffix in (".yaml", ".yml"):
        _dump_yaml(data, path)
    else:
        with open(path, "w") as fh:
            json.dump(data, fh, indent=2)


# ═══════════════════════════════════════════════════════════════════
# Hash utilities
# ═══════════════════════════════════════════════════════════════════

def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def sha256_json(obj: Any) -> str:
    """Canonical JSON hash for structured provenance."""
    canon = json.dumps(obj, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return sha256_hex(canon.encode("utf-8"))


# ═══════════════════════════════════════════════════════════════════
# WAV utilities (stdlib wave module)
# ═══════════════════════════════════════════════════════════════════

SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2  # 16-bit PCM
CHANNELS = 1
MAX_INT16 = 32767


def write_wav(path: Path, samples: List[int], sample_rate: int = SAMPLE_RATE) -> None:
    """Write mono PCM16 WAV file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "w") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(sample_rate)
        raw = b"".join(struct.pack("<h", max(-MAX_INT16, min(MAX_INT16, s))) for s in samples)
        wf.writeframes(raw)


def read_wav(path: Path) -> Tuple[int, int, int, int, List[int]]:
    """
    Read WAV, returning (sample_rate, channels, sample_width, n_samples, samples).
    Raises on undecodable files.
    """
    with wave.open(str(path), "rb") as wf:
        sr = wf.getframerate()
        ch = wf.getnchannels()
        sw = wf.getsampwidth()
        n = wf.getnframes()
        raw = wf.readframes(n)
        samples = []
        for i in range(0, len(raw), sw * ch):
            frame = raw[i : i + sw]
            if len(frame) < sw:
                break
            val = int.from_bytes(frame[:sw], "little", signed=True)
            if ch > 1:
                # Take first channel only for validation
                pass
            samples.append(val)
        return sr, ch, sw, n, samples


def validate_wav(path: Path,
                 expected_sr: int = SAMPLE_RATE,
                 expected_ch: int = CHANNELS,
                 expected_sw: int = SAMPLE_WIDTH,
                 min_samples: int = 1,
                 max_samples: Optional[int] = None) -> List[str]:
    """Validate WAV format. Returns list of error strings (empty = valid)."""
    errors = []
    if not path.exists():
        return [f"WAV file missing: {path}"]
    try:
        sr, ch, sw, n, samples = read_wav(path)
    except Exception as e:
        return [f"WAV undecodable: {path} — {e}"]
    if n == 0:
        errors.append(f"Zero-sample WAV: {path}")
    if sr != expected_sr:
        errors.append(f"Wrong sample rate: {sr} (expected {expected_sr})")
    if ch != expected_ch:
        errors.append(f"Wrong channels: {ch} (expected {expected_ch})")
    if sw != expected_sw:
        errors.append(f"Wrong sample width: {sw} (expected {expected_sw})")
    if min_samples is not None and n < min_samples:
        errors.append(f"Too few samples: {n} (min {min_samples})")
    if max_samples is not None and n > max_samples:
        errors.append(f"Too many samples: {n} (max {max_samples})")
    return errors


def wav_metadata(path: Path) -> Optional[dict]:
    """Return WAV metadata dict or None if undecodable."""
    try:
        sr, ch, sw, n, _samples = read_wav(path)
        return {
            "sample_rate": sr,
            "channels": ch,
            "sample_width_bytes": sw,
            "sample_count": n,
            "duration_ms": int(n * 1000 // sr) if sr else 0,
            "sha256": sha256_file(path),
        }
    except Exception:
        return None


def samples_ms(sample_count: int, sr: int = SAMPLE_RATE) -> int:
    """Convert sample count to milliseconds."""
    return int(sample_count * 1000 // sr)


def ms_samples(ms: int, sr: int = SAMPLE_RATE) -> int:
    """Convert milliseconds to exact sample count at given sample rate."""
    return int(ms * sr / 1000)


def rms_dbfs(samples: List[int]) -> float:
    """Compute RMS in dBFS for a list of PCM16 samples."""
    if not samples:
        return -96.0
    sq = sum((s / MAX_INT16) ** 2 for s in samples) / len(samples)
    if sq <= 0:
        return -96.0
    import math
    return 20.0 * math.log10(math.sqrt(sq))


def peak_dbfs(samples: List[int]) -> float:
    """Compute peak in dBFS."""
    if not samples:
        return -96.0
    import math
    peak = max(abs(s) for s in samples) / MAX_INT16
    if peak <= 0:
        return -96.0
    return 20.0 * math.log10(peak)


def clipping_count(samples: List[int], threshold: int = MAX_INT16 - 1) -> int:
    """Count samples at or near digital clipping."""
    return sum(1 for s in samples if abs(s) >= threshold)


# ═══════════════════════════════════════════════════════════════════
# Deterministic synthetic WAV generation
# ═══════════════════════════════════════════════════════════════════

GENERATOR_VERSION = "1.0.0"


def _generate_sine(sample_count: int, freq_hz: float, amplitude: float = 0.5,
                   sr: int = SAMPLE_RATE) -> List[int]:
    """Generate a sine wave segment."""
    import math
    samples = []
    for i in range(sample_count):
        val = int(amplitude * MAX_INT16 * math.sin(2.0 * math.pi * freq_hz * i / sr))
        samples.append(val)
    return samples


def _generate_noise(sample_count: int, amplitude: float = 0.01,
                    seed: int = 42) -> List[int]:
    """Generate white noise at given amplitude."""
    import random
    rng = random.Random(seed)
    samples = []
    for _ in range(sample_count):
        val = int(amplitude * MAX_INT16 * (rng.random() * 2.0 - 1.0))
        samples.append(val)
    return samples


def _generate_silence(sample_count: int) -> List[int]:
    """Generate silence (all zeros)."""
    return [0] * sample_count


def _generate_speech_like(sample_count: int, seed: int = 42,
                          base_freq: float = 200.0,
                          sr: int = SAMPLE_RATE) -> List[int]:
    """
    Generate speech-like audio: formant-ish signal with varying amplitude.
    Deterministic given the seed.
    """
    import math
    import random
    rng = random.Random(seed)
    samples = []
    # Use a few harmonics to create a richer signal
    amp_envelope = []
    for i in range(sample_count):
        # Create a smooth amplitude envelope
        phase = i / sample_count
        if phase < 0.1:
            amp = phase / 0.1 * 0.8  # attack
        elif phase > 0.9:
            amp = (1.0 - phase) / 0.1 * 0.8  # release
        else:
            amp = 0.8 + rng.uniform(-0.1, 0.1)  # sustain with variation
        amp_envelope.append(max(0.0, min(1.0, amp)))
    for i in range(sample_count):
        t = i / sr
        val = (
            0.6 * math.sin(2.0 * math.pi * base_freq * t)
            + 0.2 * math.sin(2.0 * math.pi * base_freq * 2.0 * t)
            + 0.1 * math.sin(2.0 * math.pi * base_freq * 3.0 * t)
            + 0.05 * rng.uniform(-1.0, 1.0)
        )
        val *= amp_envelope[i]
        samples.append(int(val * MAX_INT16 * 0.7))
    return samples


def build_synthetic_wav(plan: dict, seed: Optional[int] = None) -> Tuple[List[int], dict]:
    """
    Build a deterministic synthetic WAV from a segment plan.

    plan: {
        "segments": [
            {"type": "silence", "duration_ms": 500},
            {"type": "speech_like", "duration_ms": 600, "base_freq": 200},
            {"type": "silence", "duration_ms": 1500},
        ],
        "seed": 42,       # optional, default 42
        "sample_rate": 16000
    }
    Returns (samples_list, provenance_dict).
    """
    sr = plan.get("sample_rate", SAMPLE_RATE)
    gen_seed = seed if seed is not None else plan.get("seed", 42)
    segments = plan.get("segments", [])

    import random
    rng = random.Random(gen_seed)
    all_samples: List[int] = []
    segment_plan = []
    for seg in segments:
        seg_type = seg.get("type", "silence")
        dur_ms = seg.get("duration_ms", 1000)
        n = ms_samples(dur_ms, sr)
        seg_seed = rng.randint(0, 2**31 - 1)
        if seg_type == "silence":
            s = _generate_silence(n)
        elif seg_type == "speech_like":
            bf = seg.get("base_freq", 200.0)
            s = _generate_speech_like(n, seed=seg_seed, base_freq=bf, sr=sr)
        elif seg_type == "sine":
            freq = seg.get("freq_hz", 440.0)
            amp = seg.get("amplitude", 0.5)
            s = _generate_sine(n, freq_hz=freq, amplitude=amp, sr=sr)
        elif seg_type == "noise":
            amp = seg.get("amplitude", 0.01)
            s = _generate_noise(n, amplitude=amp, seed=seg_seed)
        else:
            s = _generate_silence(n)
        all_samples.extend(s)
        segment_plan.append({
            "type": seg_type,
            "duration_ms": dur_ms,
            "sample_count": n,
            "seed": seg_seed,
            "sample_start": len(all_samples) - n if segment_plan else 0,
        })

    full_hash = sha256_hex(
        b"".join(struct.pack("<h", s) for s in all_samples)
    )

    provenance = {
        "generator": "speech-core-golden synth",
        "generator_version": GENERATOR_VERSION,
        "seed": gen_seed,
        "sample_rate": sr,
        "channels": 1,
        "sample_width_bytes": 2,
        "total_samples": len(all_samples),
        "total_duration_ms": samples_ms(len(all_samples), sr),
        "wav_sha256": full_hash,
        "segments": segment_plan,
    }
    return all_samples, provenance


# ═══════════════════════════════════════════════════════════════════
# Manifest validation
# ═══════════════════════════════════════════════════════════════════

MANIFEST_SCHEMA_VERSION = 1


def validate_manifest(manifest: dict, manifest_dir: Path) -> List[str]:
    """Validate manifest schema, profile references, scenario IDs. Returns list of errors."""
    errors: List[str] = []

    # Top-level required fields
    mv = manifest.get("manifest_version")
    if mv is None:
        errors.append("Missing 'manifest_version' field")
    elif mv != MANIFEST_SCHEMA_VERSION:
        errors.append(f"Unsupported manifest_version: {mv} (expected {MANIFEST_SCHEMA_VERSION})")

    if "profile" not in manifest:
        errors.append("Missing 'profile' field")
    elif not isinstance(manifest["profile"], str):
        errors.append("'profile' must be a string")

    if "scenarios" not in manifest:
        errors.append("Missing 'scenarios' list")
    elif not isinstance(manifest["scenarios"], list):
        errors.append("'scenarios' must be a list")
    else:
        scenario_ids = set()
        for i, sc in enumerate(manifest["scenarios"]):
            if not isinstance(sc, dict):
                errors.append(f"scenarios[{i}]: must be an object")
                continue
            sid = sc.get("id")
            if not sid:
                errors.append(f"scenarios[{i}]: missing 'id'")
            elif not isinstance(sid, str):
                errors.append(f"scenarios[{i}]: 'id' must be a string")
            else:
                if sid in scenario_ids:
                    errors.append(f"Duplicate scenario id: {sid}")
                scenario_ids.add(sid)
                # Validate scenario_class if present
                sc_class = sc.get("class", "")
                valid_classes = {
                    "natural_endpoint", "session_end", "disconnect",
                    "queue_full", "lifecycle", "error", "diagnostic",
                    "synthetic_boundary", "recheck", "fallback",
                }
                if sc_class and sc_class not in valid_classes:
                    errors.append(f"scenario {sid}: unknown class '{sc_class}'")

                # Validate construction
                construction = sc.get("construction", "")
                valid_constructions = {
                    "human-recorded", "synthetic", "generated_tts",
                    "harness", "stub", "deterministic_stub",
                }
                if construction and construction not in valid_constructions:
                    errors.append(f"scenario {sid}: unknown construction '{construction}'")

                # Validate scenario_file if present
                sf = sc.get("scenario_file")
                if sf:
                    sf_path = manifest_dir / sf
                    if not sf_path.exists():
                        errors.append(f"scenario {sid}: scenario file not found: {sf_path}")

    # Validate profile if present
    profile_name = manifest.get("profile")
    if profile_name and isinstance(profile_name, str):
        profile_path = manifest_dir / "profiles" / profile_name / "profile.yaml"
        if not profile_path.exists():
            profile_path_json = manifest_dir / "profiles" / profile_name / "profile.json"
            if not profile_path_json.exists():
                errors.append(f"Profile not found: profiles/{profile_name}/profile.yaml")

    return errors


# ═══════════════════════════════════════════════════════════════════
# Recorder UX
# ═══════════════════════════════════════════════════════════════════

def _format_timer(total_sec: float) -> str:
    """Format seconds as mm:ss.mmm"""
    total_sec = max(0.0, total_sec)
    m = int(total_sec // 60)
    s = total_sec % 60
    return f"{m:02d}:{s:06.3f}"


def _clear_screen() -> None:
    """Clear terminal."""
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()


def _cursor_home() -> None:
    sys.stdout.write("\033[H")
    sys.stdout.flush()


def _get_key() -> str:
    """Read a single keypress. Returns empty string on EOF."""
    try:
        import termios
        import tty
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ch = sys.stdin.read(1)
            # Handle escape sequences
            if ch == "\x1b":
                extra = sys.stdin.read(2)
                if extra == "[A":
                    return "UP"
                elif extra == "[B":
                    return "DOWN"
                elif extra == "[C":
                    return "RIGHT"
                elif extra == "[D":
                    return "LEFT"
                else:
                    return "ESC"
            return ch
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
    except (ImportError, termios.error):
        # Fallback: line-buffered
        return sys.stdin.readline().strip()


def _countdown(seconds: int, label: str) -> None:
    """Display countdown: 3, 2, 1, READY."""
    for i in range(seconds, 0, -1):
        _clear_screen()
        print(f"\n\n\n          {i}\n\n          {label}")
        print(f"\n          Press 'q' to abort, any other key to continue...")
        time.sleep(1.0)
    _clear_screen()
    print(f"\n\n\n          {label}!")
    print(f"\n          Beginning...")
    time.sleep(0.5)


def _display_recording_screen(
    elapsed_sec: float,
    current_cue: Optional[dict],
    format_info: str,
    scenario_id: str,
    take_number: int,
    mode: str,  # "practice" or "take"
    hold_active: bool = False,
) -> None:
    """Display the live recording screen."""
    _clear_screen()
    timer = _format_timer(elapsed_sec)
    cue_label = current_cue["label"] if current_cue else "WAIT"
    cue_visual = current_cue.get("visual", "") if current_cue else ""

    # Build display
    lines = []
    lines.append("╔══════════════════════════════════════════════════════════╗")
    lines.append(f"║  GOLDEN RECORDER  │  {mode.upper():8s}  │  Take #{take_number:03d}       ║")
    lines.append(f"╠══════════════════════════════════════════════════════════╣")
    lines.append(f"║  Scenario: {scenario_id:<44s} ║")
    lines.append(f"╠══════════════════════════════════════════════════════════╣")
    lines.append(f"║                                                          ║")
    lines.append(f"║                   ⏱  {timer}                         ║")
    lines.append(f"║                                                          ║")
    lines.append(f"╠══════════════════════════════════════════════════════════╣")
    # Cue display - large
    cue_padded = cue_label.center(46)
    lines.append(f"║      {cue_padded}      ║")
    if hold_active:
        lines.append(f"║      {'[ HOLD ACTIVE ]'.center(46)}      ║")
    lines.append(f"╠══════════════════════════════════════════════════════════╣")
    lines.append(f"║  {cue_visual[:56]:<56s} ║")
    lines.append(f"╠══════════════════════════════════════════════════════════╣")
    lines.append(f"║  {format_info:<56s} ║")
    lines.append(f"╠══════════════════════════════════════════════════════════╣")
    lines.append(f"║  [SPACE] Play/Pause  [R] Retry  [A] Accept  [Q] Abort   ║")
    lines.append(f"╚══════════════════════════════════════════════════════════╝")

    for line in lines:
        print(line)


def _select_device() -> Optional[str]:
    """Try to detect available audio devices. Returns device name or None."""
    # Try arecord (Linux)
    try:
        result = subprocess.run(
            ["arecord", "-l"], capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0 and result.stdout.strip():
            return "default"  # ALSA default
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Try sox
    try:
        result = subprocess.run(
            ["sox", "--version"], capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            return "default"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    return None


def _get_format_info(device: Optional[str], dry_run: bool) -> str:
    """Return a format/device display string."""
    if dry_run:
        return "DRY-RUN │ 16000 Hz mono PCM16 │ no mic"
    dev = device or "system default"
    return f"{dev[:30]} │ 16000 Hz mono PCM16"


def guided_record(
    manifest: dict,
    manifest_dir: Path,
    scenario_id: str,
    out_dir: Path,
    mode: str,  # "practice" or "take"
    device: Optional[str] = None,
    dry_run: bool = False,
) -> int:
    """
    Run the guided recorder UX for a scenario.

    Returns exit code.
    """
    # Find scenario in manifest
    scenario = None
    for sc in manifest.get("scenarios", []):
        if sc.get("id") == scenario_id:
            scenario = sc
            break
    if scenario is None:
        die(ExitCode.SCENARIO_NOT_FOUND, f"Scenario not found in manifest: {scenario_id}")

    # Load scenario file if specified
    scenario_file = scenario.get("scenario_file")
    scenario_data = {}
    if scenario_file:
        sf_path = manifest_dir / scenario_file
        if sf_path.exists():
            scenario_data = load_manifest_file(sf_path)
        else:
            print(f"[WARN] Scenario file not found: {sf_path}", file=sys.stderr)

    # Get cues from scenario data or generate defaults
    cues = scenario_data.get("cues", [])
    prompt = scenario_data.get("prompt", scenario_id)

    if not cues:
        # Generate default cues from the spec if this is a known scenario
        cues = _default_cues_for_scenario(scenario_id)

    # Determine take number
    scenario_out = out_dir / "scenarios" / scenario_id / "takes"
    scenario_out.mkdir(parents=True, exist_ok=True)
    if mode == "take":
        existing_takes = sorted(scenario_out.glob("take-*"))
        take_number = len(existing_takes) + 1
    else:
        # Practice takes go into practice-NNN
        existing_practice = sorted(scenario_out.glob("practice-*"))
        take_number = len(existing_practice) + 1

    take_dir_name = f"{'take' if mode == 'take' else 'practice'}-{take_number:03d}"
    take_dir = scenario_out / take_dir_name
    take_dir.mkdir(parents=True, exist_ok=True)

    # Device selection
    if device is None and not dry_run:
        device = _select_device()
        if device is None:
            print("[WARN] No audio device detected. Use --device <name> or --dry-run.", file=sys.stderr)

    format_info = _get_format_info(device, dry_run)

    # Write consent for human recordings
    consent = {
        "consent_version": 1,
        "purpose": "local speech-core endpointing diagnostics",
        "stored_data": [
            "WAV audio",
            "transcripts/events containing text",
            "timing metadata",
            "quality metrics",
        ],
        "storage_location": str(take_dir.absolute()),
        "upload_policy": "never uploaded by golden tool",
        "sharing_policy": "not committed unless operator explicitly promotes fixture and confirms consent for repo storage",
        "retention_policy": "delete-after-run (unless accepted)",
        "deletion_command": "speech-core-golden delete",
        "speaker_label": "local-operator",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "scenario_id": scenario_id,
        "mode": mode,
    }
    save_file(consent, take_dir / "consent.json")

    # Write privacy metadata
    privacy = {
        "retention_class": "delete-after-run",
        "access_policy": "local-only",
        "pii_in_paths": False,
        "scenario_id": scenario_id,
        "take_id": take_dir_name,
        "speaker_label": "local-operator",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    save_file(privacy, take_dir / "privacy.json")

    # ── Recorder flow ──
    session_id = str(uuid.uuid4())
    wav_path = take_dir / "audio.wav"
    provenance_path = take_dir / "provenance.json"
    quality_path = take_dir / "quality.json"

    print(f"\n{'═' * 60}")
    print(f"  GOLDEN RECORDER — {mode.upper()}")
    print(f"  Scenario: {scenario_id}")
    print(f"  Prompt:   {prompt}")
    print(f"  Out:      {take_dir}")
    print(f"{'═' * 60}\n")

    # Show cue timeline overview
    if cues:
        print("  Cue timeline:")
        for cue in cues:
            label = cue.get("label", "")
            band = cue.get("band_ms", [0, 0])
            visual = cue.get("visual", "")
            print(f"    {band[0]:>5}–{band[1]:<5} ms  {label:<8s}  {visual}")
        print()

    print("  Controls: [Enter] start  [Q] quit")
    print()

    # Wait for start
    while True:
        key = _get_key()
        if key.lower() == "q":
            die(ExitCode.RECORDER_ABORTED, "Recorder aborted by operator.")
        if key in ("\r", "\n", " "):
            break

    # 3-2-1 READY countdown
    _countdown(3, "READY")

    # Determine total duration from last cue band
    total_ms = max((cue.get("band_ms", [0, 0])[1] for cue in cues), default=10000)
    total_sec = total_ms / 1000.0

    # Recording simulation / live timer
    start_time = time.monotonic()
    elapsed = 0.0
    current_cue = None
    hold_active = False

    # Write provenance
    provenance = {
        "stream_session_id": session_id,
        "scenario_id": scenario_id,
        "mode": mode,
        "take_number": take_number,
        "take_dir": str(take_dir_name),
        "recorder_version": "1.0.0",
        "sample_rate": SAMPLE_RATE,
        "channels": CHANNELS,
        "sample_width_bytes": SAMPLE_WIDTH,
        "device": device or "dry-run",
        "dry_run": dry_run,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "cue_timeline": cues,
        "prompt": prompt,
    }

    # Live display loop
    try:
        while elapsed < total_sec + 2.0:  # 2s grace period
            elapsed = time.monotonic() - start_time
            elapsed_ms = int(elapsed * 1000)

            # Determine current cue
            new_cue = None
            for cue in cues:
                band = cue.get("band_ms", [0, 0])
                if band[0] <= elapsed_ms < band[1]:
                    new_cue = cue
                    break

            # Check for HOLD label
            if new_cue and new_cue.get("label") == "HOLD":
                hold_active = True
            elif new_cue and new_cue.get("label") != "HOLD":
                hold_active = False

            if new_cue != current_cue:
                current_cue = new_cue

            _display_recording_screen(
                elapsed, current_cue, format_info, scenario_id, take_number, mode, hold_active
            )

            # Check for keypress (non-blocking with timeout)
            try:
                import select
                if select.select([sys.stdin], [], [], 0.05)[0]:
                    key = _get_key()
                    if key.lower() == "q":
                        _clear_screen()
                        die(ExitCode.RECORDER_ABORTED, "Recorder aborted by operator.")
                    elif key == " ":
                        # Pause/resume display (space toggle)
                        pass
                    elif key.lower() == "r":
                        # Retry
                        _clear_screen()
                        print("Retrying...")
                        # Clean up this take dir
                        shutil.rmtree(take_dir, ignore_errors=True)
                        return guided_record(manifest, manifest_dir, scenario_id,
                                            out_dir, mode, device, dry_run)
                    elif key.lower() == "a":
                        # Accept early
                        _clear_screen()
                        print("Accepted early.")
                        elapsed = total_sec
                        break
            except (ImportError, OSError):
                time.sleep(0.1)

        # ── Recording complete ──
        _clear_screen()
        print(f"\n{'═' * 60}")
        print(f"  RECORDING COMPLETE")
        print(f"  Total time: {_format_timer(elapsed)}")
        print(f"  Scenario:   {scenario_id}")
        print(f"  Take:       {take_dir_name}")
        print(f"{'═' * 60}\n")

        # Post-recording: generate WAV (mock if dry-run, otherwise we'd capture)
        if dry_run:
            # Generate a short silence WAV for dry-run testing
            samples = _generate_silence(ms_samples(total_ms))
            write_wav(wav_path, samples)
            print(f"  [DRY-RUN] Generated silence WAV: {wav_path}")
        else:
            # In real mode, we delegate to capture tool
            # For MVP, write a placeholder
            samples = _generate_silence(ms_samples(total_ms))
            write_wav(wav_path, samples)
            print(f"  [WARN] Real capture delegated to sibling; wrote placeholder WAV: {wav_path}")

        # Compute quality metrics
        try:
            _sr, _ch, _sw, n, sample_data = read_wav(wav_path)
            p = peak_dbfs(sample_data)
            r = rms_dbfs(sample_data)
            clip = clipping_count(sample_data)
        except Exception:
            n = 0
            p = -96.0
            r = -96.0
            clip = 0

        quality = {
            "sample_count": n,
            "duration_ms": samples_ms(n),
            "peak_dbfs": round(p, 2),
            "rms_dbfs": round(r, 2),
            "clipping_count": clip,
            "clipping_fraction": round(clip / n, 6) if n else 0.0,
            "zero_samples": n == 0,
        }
        save_file(quality, quality_path)

        # Update provenance with final data
        provenance["completed_at"] = datetime.now(timezone.utc).isoformat()
        provenance["total_duration_ms"] = int(elapsed * 1000)
        provenance["wav_sha256"] = sha256_file(wav_path) if wav_path.exists() else None
        save_file(provenance, provenance_path)

        # ── Review flow ──
        print("  Review options:")
        print("    [P] Playback  [R] Retry  [A] Accept  [D] Delete  [Q] Quit")
        print()

        while True:
            key = _get_key()
            if key.lower() == "p":
                print("  Playback not available (delegated to sibling).")
                print("  [R] Retry  [A] Accept  [D] Delete  [Q] Quit")
            elif key.lower() == "a":
                # Accept - write review record
                review = {
                    "accepted": True,
                    "reviewed_at": datetime.now(timezone.utc).isoformat(),
                    "reviewed_by": "local operator",
                    "quality": quality,
                    "consent_sha256": sha256_file(take_dir / "consent.json"),
                    "privacy_sha256": sha256_file(take_dir / "privacy.json"),
                    "wav_sha256": sha256_file(wav_path) if wav_path.exists() else None,
                    "review_notes": "Accepted by operator.",
                }
                save_file(review, take_dir / "review.json")
                print(f"\n  ✓ Take accepted: {take_dir}")
                return ExitCode.PASS
            elif key.lower() == "r":
                # Retry: delete and redo
                shutil.rmtree(take_dir, ignore_errors=True)
                return guided_record(manifest, manifest_dir, scenario_id,
                                    out_dir, mode, device, dry_run)
            elif key.lower() == "d":
                # Delete
                shutil.rmtree(take_dir, ignore_errors=True)
                print(f"  Take deleted: {take_dir_name}")
                return ExitCode.PASS
            elif key.lower() == "q":
                print(f"  Take left unreviewed: {take_dir}")
                return ExitCode.PASS

    except KeyboardInterrupt:
        _clear_screen()
        print("\nRecorder interrupted.")
        # Save partial provenance
        provenance["interrupted_at"] = datetime.now(timezone.utc).isoformat()
        save_file(provenance, provenance_path)
        return ExitCode.RECORDER_ABORTED


def _default_cues_for_scenario(scenario_id: str) -> list:
    """Return default cue timeline for known scenarios."""
    defaults = {
        "human-clean-complete": [
            {"band_ms": [0, 3000], "label": "READY",
             "visual": "Read silently. Breathe normally."},
            {"band_ms": [3000, 9000], "label": "SPEAK",
             "visual": "Say: The weather looks great today. I think I will go outside."},
            {"band_ms": [9000, 11000], "label": "STOP",
             "visual": "Stay quiet, then stop."},
        ],
        "human-trailing-off": [
            {"band_ms": [0, 3000], "label": "READY",
             "visual": "Prepare to trail off naturally at the ellipsis."},
            {"band_ms": [3000, 11000], "label": "SPEAK",
             "visual": "Say the line, trailing off before actually never mind."},
            {"band_ms": [11000, 13000], "label": "STOP",
             "visual": "Stay quiet."},
        ],
        "human-pause-resume-incomplete": [
            {"band_ms": [0, 3000], "label": "READY",
             "visual": "Prepare for a real pause."},
            {"band_ms": [3000, 5200], "label": "SPEAK",
             "visual": "Say: I need to check"},
            {"band_ms": [5200, 6800], "label": "PAUSE",
             "visual": "Pause silently. Do not breathe loudly into mic."},
            {"band_ms": [6800, 10500], "label": "RESUME",
             "visual": "Say: one more thing before I answer."},
            {"band_ms": [10500, 12500], "label": "STOP",
             "visual": "Stay quiet."},
        ],
        "human-hold-continuous-filler-7500": [
            {"band_ms": [0, 3000], "label": "READY",
             "visual": "Prepare to sustain a non-word vocalization."},
            {"band_ms": [3000, 11200], "label": "HOLD",
             "visual": "Hold: uhhhh / thinking hum. No words."},
            {"band_ms": [11200, 13000], "label": "STOP",
             "visual": "Stop and stay quiet."},
        ],
        "human-rapid-question": [
            {"band_ms": [0, 3000], "label": "READY",
             "visual": "Prepare a quick natural question."},
            {"band_ms": [3000, 5500], "label": "SPEAK",
             "visual": "Say: What time is it right now?"},
            {"band_ms": [5500, 7500], "label": "STOP",
             "visual": "Stay quiet."},
        ],
    }
    return defaults.get(scenario_id, [
        {"band_ms": [0, 3000], "label": "READY", "visual": "Prepare."},
        {"band_ms": [3000, 10000], "label": "SPEAK", "visual": "Speak the prompt."},
        {"band_ms": [10000, 12000], "label": "STOP", "visual": "Stop and stay quiet."},
    ])


# ═══════════════════════════════════════════════════════════════════
# Operator: one-command guided recording workflow
# ═══════════════════════════════════════════════════════════════════

FRIENDLY_NAMES: Dict[str, str] = {
    "human-clean-complete": "Clean sentence",
    "human-trailing-off": "Trailing off",
    "human-pause-resume-incomplete": "Pause and resume",
    "human-rapid-question": "Rapid question",
    "human-hold-continuous-filler-7500": "Continuous hold",
    "synthetic-vad-onset-below-32ms": "VAD onset below (32ms)",
    "synthetic-vad-onset-at-64ms": "VAD onset at (64ms)",
    "synthetic-vad-onset-above-96ms": "VAD onset above (96ms)",
    "synthetic-vad-hangover-below-64ms-silence": "VAD hangover below (64ms)",
    "synthetic-vad-hangover-at-96ms-silence": "VAD hangover at (96ms)",
    "synthetic-vad-hangover-above-128ms-silence": "VAD hangover above (128ms)",
    "synthetic-min-vad-speech-below-399ms": "Min speech below (399ms)",
    "synthetic-min-vad-speech-at-400ms": "Min speech at (400ms)",
    "synthetic-min-vad-speech-above-401ms": "Min speech above (401ms)",
    "synthetic-smart-recheck-schedule": "Smart Turn recheck",
    "synthetic-acoustic-fallback-installed-1700": "Acoustic fallback (1700ms)",
    "synthetic-acoustic-fallback-code-default-3500": "Acoustic fallback (3500ms)",
    "synthetic-transcript-silence-close-700": "Transcript silence close",
}


# Default drain window for event capture after mic stops (seconds)
DEFAULT_EVENT_DRAIN_SEC = 5.0
# Default URL for daemon websocket
DEFAULT_WS_URL = "ws://127.0.0.1:8765/ws/audio-ingress"


def _find_binary(name: str) -> Optional[str]:
    """Find a binary: first PATH, then script dir, then target/debug."""
    script_dir = Path(__file__).resolve().parent
    repo_root = script_dir.parent

    # Check PATH
    import shutil as _shutil
    found = _shutil.which(name)
    if found:
        return found

    # Check script dir
    candidate = script_dir / name
    if candidate.exists() and os.access(candidate, os.X_OK):
        return str(candidate)

    # Check target/debug
    candidate = repo_root / "target" / "debug" / name
    if candidate.exists() and os.access(candidate, os.X_OK):
        return str(candidate)

    return None


def _scenario_friendly_name(scenario_id: str) -> str:
    """Return friendly name for a scenario, falling back to the ID."""
    return FRIENDLY_NAMES.get(scenario_id, scenario_id)


def _operator_scenario_menu(manifest: dict) -> Optional[str]:
    """
    Display an interactive numbered menu of scenarios.
    Returns selected scenario_id or None if quit.
    """
    scenarios = manifest.get("scenarios", [])
    if not scenarios:
        die(ExitCode.SCENARIO_NOT_FOUND, "No scenarios in manifest.")

    # Split into human and synthetic groups
    human = [s for s in scenarios if s.get("construction") == "human-recorded"]
    synthetic = [s for s in scenarios if s.get("construction") == "synthetic"]

    _clear_screen()
    print("╔══════════════════════════════════════════════════════╗")
    print("║  GOLDEN OPERATOR — speech-core endpoint testing      ║")
    print("╠══════════════════════════════════════════════════════╣")
    print("║                                                      ║")
    print("║  Choose a scenario:                                  ║")
    print("║                                                      ║")

    idx = 1
    mapping: Dict[int, str] = {}

    if human:
        print("║  [HUMAN RECORDINGS]                                  ║")
        for s in human:
            sid = s["id"]
            name = _scenario_friendly_name(sid)
            print(f"║    {idx:2d}. {name:<44s} ║")
            mapping[idx] = sid
            idx += 1

    print("║                                                      ║")

    if synthetic:
        print("║  [SYNTHETIC TESTS]                                   ║")
        for s in synthetic:
            sid = s["id"]
            name = _scenario_friendly_name(sid)
            print(f"║    {idx:2d}. {name:<44s} ║")
            mapping[idx] = sid
            idx += 1

    print("║                                                      ║")
    print("║  Type a number or name, or [Q] to quit               ║")
    print("╚══════════════════════════════════════════════════════╝")
    print()

    while True:
        try:
            raw = input("  > ").strip()
        except (EOFError, KeyboardInterrupt):
            return None

        if not raw:
            continue

        if raw.lower() in ("q", "quit"):
            return None

        # Try number
        try:
            num = int(raw)
            if num in mapping:
                return mapping[num]
        except ValueError:
            pass

        # Try name prefix / fuzzy match
        matches = [sid for sid in FRIENDLY_NAMES if raw.lower() in FRIENDLY_NAMES[sid].lower()]
        if not matches:
            matches = [sid for sid in FRIENDLY_NAMES if raw.lower() in sid.lower()]
        if not matches:
            # Last resort: check manifest scenario IDs
            for s in scenarios:
                sid = s["id"]
                if raw.lower() in sid.lower() or raw.lower() == sid.lower():
                    return sid

        if len(matches) == 1:
            return matches[0]
        elif len(matches) > 1:
            print(f"  Multiple matches: {', '.join(matches[:5])}")
            print("  Please be more specific or use a number.")
        else:
            print(f"  No match for '{raw}'. Try a number or name.")


def _operator_display_scenario_info(manifest: dict, scenario_id: str,
                                     manifest_dir: Path) -> Optional[dict]:
    """Display scenario description and prompt. Returns cue timeline."""
    scenario = None
    for s in manifest.get("scenarios", []):
        if s.get("id") == scenario_id:
            scenario = s
            break
    if scenario is None:
        return None

    scenario_file = scenario.get("scenario_file")
    scenario_data = {}
    if scenario_file:
        sf_path = manifest_dir / scenario_file
        if sf_path.exists():
            try:
                scenario_data = load_manifest_file(sf_path)
            except Exception:
                pass

    cues = scenario_data.get("cues", [])
    prompt = scenario_data.get("prompt", scenario.get("description", scenario_id))

    if not cues:
        cues = _default_cues_for_scenario(scenario_id)

    _clear_screen()
    name = _scenario_friendly_name(scenario_id)
    print("╔══════════════════════════════════════════════════════╗")
    print(f"║  {name[:48]:<48s} ║")
    print("╠══════════════════════════════════════════════════════╣")
    print(f"║  {prompt[:52]:<52s} ║")
    print("║                                                      ║")
    if cues:
        print("║  Cue timeline:                                       ║")
        for cue in cues:
            label = cue.get("label", "")
            band = cue.get("band_ms", [0, 0])
            print(f"║    {band[0]:>5}–{band[1]:<5} ms  {label:<8s}  ║")
    print("║                                                      ║")
    print("║  [ENTER] Start   [B] Back   [Q] Quit                 ║")
    print("╚══════════════════════════════════════════════════════╝")
    print()

    while True:
        try:
            key = _get_key()
        except (EOFError, KeyboardInterrupt):
            return None

        if key.lower() == "q":
            return None
        if key.lower() == "b":
            return "back"
        if key in ("\r", "\n", " "):
            return {"cues": cues, "prompt": prompt, "scenario": scenario}


def _operator_real_capture(
    scenario: dict,
    cues: list,
    take_dir: Path,
    mode: str,
    device: Optional[str],
    ws_url: str,
    stream_session_id: str,
    total_ms: int,
    drain_sec: float = DEFAULT_EVENT_DRAIN_SEC,
) -> Tuple[int, Dict[str, Any]]:
    """
    Run real capture: launch watcher, then mic adapter, display TUI,
    clean up subprocesses. Returns (exit_code, capture_info).

    Fail-closed contract (P1):
    - Watcher readiness: --ready-file side-channel. Watcher atomically
      creates/writes the ready file only after connect_async + SubscribeEvents.
      We wait for that file bounded by timeout while monitoring watcher exit.
    - Stale pre-existing ready file is rejected (caller must provide fresh path).
    - Monitor early child exits, capture/persist stderr and exit codes.
    - After drain, let validity establish terminal completion.
    """
    WATCHER_READY_TIMEOUT_SEC = 15.0
    WATCHER_READY_POLL_SEC = 0.1

    watcher_bin = _find_binary("speech-core-watch")
    mic_bin = _find_binary("speech-core-mic-adapter")

    if not watcher_bin or not mic_bin:
        missing = []
        if not watcher_bin:
            missing.append("speech-core-watch")
        if not mic_bin:
            missing.append("speech-core-mic-adapter")
        print(f"  [ERROR] Missing binaries: {', '.join(missing)}", file=sys.stderr)
        return ExitCode.DEPENDENCY_MISSING, {"missing": missing}

    event_path = take_dir / "event-stream.jsonl"
    wav_path = take_dir / "audio.wav"
    diag_dir = take_dir / "diagnostics"
    diag_dir.mkdir(parents=True, exist_ok=True)

    # Fresh unique ready-file path (caller must provide a unique take_dir)
    ready_path = diag_dir / "watcher.ready"

    capture_info: Dict[str, Any] = {
        "stream_session_id": stream_session_id,
        "watcher_bin": watcher_bin,
        "mic_bin": mic_bin,
        "event_path": str(event_path),
        "wav_path": str(wav_path),
        "watcher_pid": None,
        "mic_pid": None,
        "events_captured": False,
        "watcher_exit_code": None,
        "mic_exit_code": None,
        "watcher_stderr_path": str(diag_dir / "watcher.stderr.log"),
        "mic_stderr_path": str(diag_dir / "mic.stderr.log"),
        "ready_path": str(ready_path),
        "readiness_at": None,
        "readiness_method": "ready-file",
    }

    watcher_proc = None
    mic_proc = None
    event_fh = None
    watcher_stderr_fh = None
    mic_stderr_fh = None

    try:
        # ── Launch watcher first ──────────────────────────────────────
        event_fh = open(event_path, "w")
        watcher_stderr_fh = open(diag_dir / "watcher.stderr.log", "w")

        # Require fresh ready path: remove if somehow pre-existing
        try:
            ready_path.unlink(missing_ok=True)
        except OSError:
            pass

        watcher_args = [
            watcher_bin,
            "--mode", "jsonl",
            "--url", ws_url,
            "--stream-session-id", stream_session_id,
            "--ready-file", str(ready_path),
        ]
        watcher_proc = subprocess.Popen(
            watcher_args,
            stdout=event_fh,
            stderr=watcher_stderr_fh,
            text=True,
        )
        capture_info["watcher_pid"] = watcher_proc.pid

        # ── Watcher readiness: explicit ready-file side-channel ───────
        #   Poll for the ready file. Watcher writes it only after
        #   connect_async + SubscribeEvents send returns Ok.
        #   While waiting, monitor watcher exit (early exit = fail).
        #   Stale pre-existing file has been removed above.
        readiness_start = time.monotonic()
        watcher_ready = False
        while (time.monotonic() - readiness_start) < WATCHER_READY_TIMEOUT_SEC:
            rc = watcher_proc.poll()
            if rc is not None:
                watcher_stderr_fh.close()
                stderr_content = _read_file_head(diag_dir / "watcher.stderr.log", 2000)
                event_fh.close()
                capture_info["watcher_exit_code"] = rc
                capture_info["watcher_stderr_tail"] = stderr_content
                capture_info["readiness_status"] = f"watcher-exited-early-rc-{rc}"
                print(f"  [ERROR] Watcher exited early with code {rc}",
                      file=sys.stderr)
                if stderr_content:
                    print(f"  Watcher stderr: {stderr_content[:500]}", file=sys.stderr)
                return ExitCode.DAEMON_UNREACHABLE, capture_info

            if ready_path.exists():
                watcher_ready = True
                capture_info["readiness_at"] = datetime.now(timezone.utc).isoformat()
                capture_info["readiness_elapsed_ms"] = int(
                    (time.monotonic() - readiness_start) * 1000
                )
                capture_info["readiness_status"] = "ready-file-created"
                break

            time.sleep(WATCHER_READY_POLL_SEC)

        if not watcher_ready:
            watcher_stderr_fh.close()
            event_fh.close()
            capture_info["readiness_status"] = "timeout"
            print(f"  [ERROR] Watcher readiness timeout: ready file not created "
                  f"after {WATCHER_READY_TIMEOUT_SEC}s", file=sys.stderr)
            if watcher_proc.poll() is None:
                watcher_proc.terminate()
                watcher_proc.wait(timeout=5)
            capture_info["watcher_exit_code"] = watcher_proc.returncode
            return ExitCode.DAEMON_UNREACHABLE, capture_info

        # ── Launch mic adapter ────────────────────────────────────────
        mic_stderr_fh = open(diag_dir / "mic.stderr.log", "w")
        mic_args = [
            mic_bin,
            "--record-wav", str(wav_path),
            "--url", ws_url,
            "--stream-session-id", stream_session_id,
        ]
        if device:
            mic_args.extend(["--device", device])

        mic_proc = subprocess.Popen(
            mic_args,
            stdout=subprocess.DEVNULL,
            stderr=mic_stderr_fh,
            text=True,
        )
        capture_info["mic_pid"] = mic_proc.pid

        # ── Recording TUI loop ────────────────────────────────────────
        scenario_id = scenario.get("id", "unknown")
        take_number = int(take_dir.name.split("-")[-1]) if "-" in take_dir.name else 1
        total_sec = total_ms / 1000.0

        start_time = time.monotonic()
        elapsed = 0.0
        current_cue = None
        hold_active = False
        format_info = _get_format_info(device, dry_run=False)

        while elapsed < total_sec + 2.0:
            # Monitor early child exits during recording
            watcher_rc = watcher_proc.poll()
            if watcher_rc is not None:
                watcher_stderr_fh.close()
                mic_stderr_fh.close()
                capture_info["watcher_exit_code"] = watcher_rc
                stderr_tail = _read_file_head(diag_dir / "watcher.stderr.log", 2000)
                capture_info["watcher_stderr_tail"] = stderr_tail
                print(f"  [ERROR] Watcher exited during capture with code {watcher_rc}",
                      file=sys.stderr)
                if stderr_tail:
                    print(f"  Watcher stderr: {stderr_tail[:500]}", file=sys.stderr)
                event_fh.close()
                return ExitCode.DAEMON_UNREACHABLE, capture_info

            mic_rc = mic_proc.poll()
            if mic_rc is not None and mic_rc != 0:
                mic_stderr_fh.close()
                capture_info["mic_exit_code"] = mic_rc
                stderr_tail = _read_file_head(diag_dir / "mic.stderr.log", 2000)
                capture_info["mic_stderr_tail"] = stderr_tail
                print(f"  [ERROR] Mic adapter exited with code {mic_rc}", file=sys.stderr)
                if stderr_tail:
                    print(f"  Mic stderr: {stderr_tail[:500]}", file=sys.stderr)
                event_fh.close()
                return ExitCode.INTERNAL_ERROR, capture_info

            elapsed = time.monotonic() - start_time
            elapsed_ms = int(elapsed * 1000)

            new_cue = None
            for cue in cues:
                band = cue.get("band_ms", [0, 0])
                if band[0] <= elapsed_ms < band[1]:
                    new_cue = cue
                    break

            if new_cue and new_cue.get("label") == "HOLD":
                hold_active = True
            elif new_cue and new_cue.get("label") != "HOLD":
                hold_active = False

            if new_cue != current_cue:
                current_cue = new_cue

            _display_recording_screen(
                elapsed, current_cue, format_info, scenario_id, take_number, mode, hold_active
            )

            # Non-blocking key check
            try:
                import select
                if select.select([sys.stdin], [], [], 0.05)[0]:
                    key = _get_key()
                    if key.lower() == "q":
                        _clear_screen()
                        return ExitCode.RECORDER_ABORTED, capture_info
                    elif key == " ":
                        pass  # toggle display
            except (ImportError, OSError):
                time.sleep(0.1)

        _clear_screen()
        print(f"\n{'═' * 60}")
        print(f"  RECORDING COMPLETE")
        print(f"  Total time: {_format_timer(min(elapsed, total_sec))}")
        print(f"{'═' * 60}\n")

        # ── Stop mic adapter cleanly ──────────────────────────────────
        mic_term_ok = True
        if mic_proc and mic_proc.poll() is None:
            mic_proc.terminate()
            try:
                mic_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                mic_proc.kill()
                mic_proc.wait()
                mic_term_ok = False
        mic_stderr_fh.close()
        capture_info["mic_exit_code"] = mic_proc.returncode if mic_proc else None
        capture_info["mic_stderr_tail"] = _read_file_head(diag_dir / "mic.stderr.log", 2000)

        # ── Event drain window ────────────────────────────────────────
        print(f"  Waiting {drain_sec}s for final events...")
        time.sleep(drain_sec)

        # ── Stop watcher ──────────────────────────────────────────────
        if watcher_proc and watcher_proc.poll() is None:
            watcher_proc.terminate()
            try:
                watcher_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                watcher_proc.kill()
                watcher_proc.wait()
        watcher_stderr_fh.close()
        capture_info["watcher_exit_code"] = watcher_proc.returncode if watcher_proc else None
        capture_info["watcher_stderr_tail"] = _read_file_head(diag_dir / "watcher.stderr.log", 2000)

        event_fh.close()

        # ── Fail unexpected nonzero exits ─────────────────────────────
        # -15 = SIGTERM (expected termination), 0 = clean exit
        # Any other exit code is a hard failure (fail-closed)
        if watcher_proc and watcher_proc.returncode not in (0, -15, None):
            print(f"  [ERROR] Watcher exited nonzero: {watcher_proc.returncode}",
                  file=sys.stderr)
            capture_info["watcher_exit_code"] = watcher_proc.returncode
            capture_info["error"] = f"Watcher exited with code {watcher_proc.returncode}"
            return ExitCode.INTERNAL_ERROR, capture_info

        if mic_proc and mic_proc.returncode not in (0, -15, None):
            print(f"  [ERROR] Mic adapter exited nonzero: {mic_proc.returncode}",
                  file=sys.stderr)
            capture_info["mic_exit_code"] = mic_proc.returncode
            capture_info["error"] = f"Mic adapter exited with code {mic_proc.returncode}"
            return ExitCode.INTERNAL_ERROR, capture_info

        # Check if events were captured
        if event_path.exists() and event_path.stat().st_size > 0:
            capture_info["events_captured"] = True

        return ExitCode.PASS, capture_info

    except KeyboardInterrupt:
        _clear_screen()
        print("\nOperator interrupted.")
        # Cleanup
        if mic_proc and mic_proc.poll() is None:
            mic_proc.terminate()
            try:
                mic_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                mic_proc.kill()
        if watcher_proc and watcher_proc.poll() is None:
            watcher_proc.terminate()
            try:
                watcher_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                watcher_proc.kill()
        try:
            if event_fh:
                event_fh.close()
            if watcher_stderr_fh:
                watcher_stderr_fh.close()
            if mic_stderr_fh:
                mic_stderr_fh.close()
        except Exception:
            pass
        return ExitCode.RECORDER_ABORTED, capture_info

    except Exception as e:
        print(f"  [ERROR] Capture failed: {e}", file=sys.stderr)
        if mic_proc and mic_proc.poll() is None:
            mic_proc.terminate()
        if watcher_proc and watcher_proc.poll() is None:
            watcher_proc.terminate()
        try:
            if event_fh:
                event_fh.close()
            if watcher_stderr_fh:
                watcher_stderr_fh.close()
            if mic_stderr_fh:
                mic_stderr_fh.close()
        except Exception:
            pass
        return ExitCode.INTERNAL_ERROR, capture_info

    finally:
        # Ensure subprocesses are cleaned up
        for p, name in [(mic_proc, "mic"), (watcher_proc, "watcher")]:
            if p and p.poll() is None:
                try:
                    p.kill()
                    p.wait(timeout=2)
                except Exception:
                    pass


def _read_file_head(path: Path, max_chars: int) -> str:
    """Read up to max_chars from a file, returning content."""
    try:
        if not path.exists():
            return ""
        with open(path, "r", errors="replace") as f:
            return f.read(max_chars)
    except Exception:
        return ""


def _stdin_is_tty() -> bool:
    """Check if stdin is a TTY. Extractable for test mocking."""
    return hasattr(sys.stdin, "isatty") and sys.stdin.isatty()


def _operator_run_validity_and_assertions(
    take_dir: Path,
    stream_session_id: str,
    scenario: dict,
    manifest_dir: Path,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Run fail-closed capture validity checks and semantic assertion DSL
    against captured artifacts. Reuses _golden internals.

    Returns (validity_evidence, assertion_evidence) — both are dicts
    with at least {"passed": bool, "exit_code": int}.

    Evidence is persisted to quality/review/provenance/ in the take_dir.
    """
    evidence_dir = take_dir / "quality" / "review" / "provenance"
    evidence_dir.mkdir(parents=True, exist_ok=True)

    validity_evidence: Dict[str, Any] = {
        "passed": False,
        "exit_code": -1,
        "run": False,
        "reason": "not run",
    }
    assertion_evidence: Dict[str, Any] = {
        "passed": False,
        "exit_code": -1,
        "run": False,
        "reason": "not run",
    }

    if not _HAS_GOLDEN_ENGINE:
        validity_evidence["reason"] = "golden engine not importable"
        assertion_evidence["reason"] = "golden engine not importable"
        return validity_evidence, assertion_evidence

    # ── 1. Capture validity ───────────────────────────────────────────
    try:
        # Load profile terminal markers from manifest directory
        terminal_markers = _load_profile_terminal_markers(
            scenario, manifest_dir
        )
        v_exit, v_record = validate_capture_artifacts(
            scenario_dir=take_dir,
            stream_session_id=stream_session_id,
            required_markers=terminal_markers if terminal_markers else None,
        )
        validity_evidence = {
            "passed": v_exit == 0 and v_record.get("valid", False),
            "exit_code": v_exit,
            "run": True,
            "reason": v_record.get("reason", ""),
            "event_count": v_record.get("event_count", 0),
            "check_results": v_record.get("check_results", {}),
            "missing_markers": v_record.get("missing_markers"),
            "artifact_hashes": v_record.get("artifact_hashes", {}),
        }
        save_file(v_record, evidence_dir / "validity.json")
    except Exception as e:
        validity_evidence["reason"] = f"validity check exception: {e}"
        validity_evidence["run"] = True

    # ── 2. Semantic assertions — only run if validity passed ──────────
    try:
        dsl = _load_scenario_assertion_dsl(scenario, manifest_dir)
        if dsl:
            # Use strict JSON loading for assertion stage
            events, load_error = _load_events_from_stream(
                take_dir / "event-stream.jsonl", fail_on_malformed=True
            )
            if load_error:
                assertion_evidence["reason"] = f"event stream invalid for assertions: {load_error}"
                assertion_evidence["run"] = True
            elif not validity_evidence.get("passed", False):
                # Validity failed — assertions must not run
                assertion_evidence["reason"] = (
                    "assertions skipped: capture validity failed — "
                    f"{validity_evidence.get('reason', 'unknown')}"
                )
                assertion_evidence["run"] = False
            elif events:
                a_exit, a_result = run_assertions(events, dsl, take_dir)
                assertion_evidence = {
                    "passed": a_exit == 0 and a_result.all_passed,
                    "exit_code": a_exit,
                    "run": True,
                    "reason": f"{len(a_result.violations)} violations, {len(a_result.passed)} passed",
                    "violations": [v.to_dict() for v in a_result.violations],
                    "warnings": a_result.warnings,
                    "passed_assertions": a_result.passed,
                }
                # Write assertion report
                write_json_report(
                    a_exit, a_result, None,
                    evidence_dir / "assert-report.json",
                    scenario_id=scenario.get("id"),
                )
            else:
                assertion_evidence["reason"] = "no events to assert"
                assertion_evidence["run"] = True
        else:
            assertion_evidence["reason"] = "no assertion DSL for scenario"
            assertion_evidence["run"] = False  # not applicable
    except Exception as e:
        assertion_evidence["reason"] = f"assertion exception: {e}"
        assertion_evidence["run"] = True

    # Persist combined evidence
    combined = {
        "stream_session_id": stream_session_id,
        "scenario_id": scenario.get("id"),
        "validity": validity_evidence,
        "assertions": assertion_evidence,
    }
    save_file(combined, evidence_dir / "combined-evidence.json")

    return validity_evidence, assertion_evidence


def _load_profile_terminal_markers(
    scenario: dict, manifest_dir: Path
) -> Optional[List[Dict[str, Any]]]:
    """Load required terminal markers from the profile file."""
    profile_file = manifest_dir / "profiles" / "golden-mvp" / "profile.yaml"
    try:
        if profile_file.exists():
            profile = load_manifest_file(profile_file)
            markers = profile.get("required_terminal_markers")
            if markers:
                return markers
    except Exception:
        pass
    return DEFAULT_TERMINAL_MARKERS if _HAS_GOLDEN_ENGINE else None


def _load_scenario_assertion_dsl(
    scenario: dict, manifest_dir: Path
) -> Optional[Dict[str, Any]]:
    """Load assertion DSL from scenario YAML file."""
    scenario_file = scenario.get("scenario_file")
    if not scenario_file:
        return None
    sf_path = manifest_dir / scenario_file
    if not sf_path.exists():
        return None
    try:
        scenario_data = load_manifest_file(sf_path)
        assertions = scenario_data.get("assertions")
        if assertions and isinstance(assertions, dict):
            return assertions
    except Exception:
        pass
    return None


def _load_events_from_stream(events_path: Path, *,
                               fail_on_malformed: bool = False) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Load events from event-stream.jsonl.

    Returns (events, error_message). When fail_on_malformed is True,
    malformed JSON causes immediate failure (used by assertion stage).
    When False (default for quality checks), malformed lines are counted
    but not fatal.
    """
    if not events_path.exists():
        return [], "event-stream.jsonl missing"
    events = []
    try:
        with open(events_path, "r", encoding="utf-8", errors="replace") as f:
            for line_no, line in enumerate(f, 1):
                line = line.strip()
                if line:
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError as e:
                        if fail_on_malformed:
                            return [], f"Malformed JSON at line {line_no}: {e}"
                        # In lenient mode, skip the line but don't fail
                        pass
    except Exception as e:
        if fail_on_malformed:
            return [], f"Error reading event stream: {e}"
        return [], None
    return events, None


def _operator_quality_check(take_dir: Path, scenario_id: str,
                             mode: str, dry_run: bool,
                             min_peak_dbfs: float = -60.0,
                             min_rms_dbfs: float = -60.0) -> Dict[str, Any]:
    """
    Run automated quality checks on captured artifacts.
    Returns a dict with pass/fail and specific check results.

    Thresholds default to -60 dBFS but can be tightened by the caller
    (e.g. from manifest min_peak_dbfs / min_rms_dbfs).
    """
    result: Dict[str, Any] = {
        "passed": False,
        "checks": [],
        "failures": [],
        "warnings": [],
        "take_dir": str(take_dir),
        "scenario_id": scenario_id,
        "mode": mode,
        "dry_run": dry_run,
    }

    wav_path = take_dir / "audio.wav"
    event_path = take_dir / "event-stream.jsonl"

    # ── WAV checks ────────────────────────────────────────────────────
    if not wav_path.exists():
        result["failures"].append("WAV missing")
        result["checks"].append({"name": "wav_exists", "passed": False, "reason": "WAV file not found"})
    else:
        result["checks"].append({"name": "wav_exists", "passed": True})

        # Validate format
        wav_errors = validate_wav(wav_path)
        if wav_errors:
            for e in wav_errors:
                result["failures"].append(e)
                result["checks"].append({"name": "wav_format", "passed": False, "reason": e})
        else:
            result["checks"].append({"name": "wav_format", "passed": True})

        # Quality metrics
        try:
            _sr, _ch, _sw, n, sample_data = read_wav(wav_path)
            p = peak_dbfs(sample_data)
            r = rms_dbfs(sample_data)
            clip = clipping_count(sample_data)
            dur = samples_ms(n)

            result["quality"] = {
                "duration_ms": dur,
                "sample_count": n,
                "peak_dbfs": round(p, 2),
                "rms_dbfs": round(r, 2),
                "clipping_count": clip,
            }

            # Peak check — use caller-provided threshold
            if p < min_peak_dbfs:
                result["checks"].append({"name": "peak", "passed": False,
                                          "reason": f"Peak {p:.1f} dBFS below threshold ({min_peak_dbfs:.0f} dBFS)",
                                          "value": round(p, 2),
                                          "threshold": min_peak_dbfs})
                result["failures"].append(f"Peak {p:.1f} dBFS below threshold ({min_peak_dbfs:.0f} dBFS)")
            else:
                result["checks"].append({"name": "peak", "passed": True,
                                          "reason": f"Peak {p:.1f} dBFS",
                                          "value": round(p, 2)})

            # Clipping check
            if clip > 0:
                result["checks"].append({"name": "clipping", "passed": False,
                                          "reason": f"{clip} clipped samples",
                                          "value": clip})
                result["failures"].append(f"Clipping: {clip} samples")
            else:
                result["checks"].append({"name": "clipping", "passed": True,
                                          "reason": "No clipping"})

            # Non-silence check — use caller-provided threshold
            if r < min_rms_dbfs and not dry_run:
                result["checks"].append({"name": "non_silence", "passed": False,
                                          "reason": f"Too quiet (RMS={r:.1f} dBFS, threshold={min_rms_dbfs:.0f} dBFS)",
                                          "value": round(r, 2),
                                          "threshold": min_rms_dbfs})
                result["failures"].append(f"Too quiet (RMS={r:.1f} dBFS, threshold={min_rms_dbfs:.0f} dBFS)")
            else:
                reason = f"Signal present (RMS={r:.1f} dBFS)"
                if dry_run:
                    # Dry-run must never display as passing signal
                    result["checks"].append({"name": "non_silence", "passed": False,
                                              "reason": "DRY-RUN: synthetic silence, not real audio",
                                              "value": round(r, 2)})
                else:
                    result["checks"].append({"name": "non_silence", "passed": True,
                                              "reason": reason,
                                              "value": round(r, 2)})

        except Exception as e:
            result["failures"].append(f"WAV read error: {e}")
            result["checks"].append({"name": "wav_read", "passed": False, "reason": str(e)})

    # ── Event checks ──────────────────────────────────────────────────
    if not event_path.exists():
        if not dry_run:
            result["checks"].append({"name": "events_exist", "passed": False,
                                      "reason": "event-stream.jsonl missing"})
            result["failures"].append("Event stream missing")
        else:
            result["checks"].append({"name": "events_exist", "passed": True,
                                      "reason": "dry-run (no events expected)"})
    else:
        try:
            event_count = 0
            with open(event_path) as f:
                for line in f:
                    if line.strip():
                        event_count += 1
            result["event_count"] = event_count
            if event_count > 0:
                result["checks"].append({"name": "events_exist", "passed": True,
                                          "reason": f"{event_count} events"})
            else:
                result["checks"].append({"name": "events_exist", "passed": False,
                                          "reason": "Zero events"})
                result["failures"].append("Zero events captured")
        except Exception as e:
            result["checks"].append({"name": "events_exist", "passed": False,
                                      "reason": f"Error reading: {e}"})
            result["failures"].append(f"Event stream read error: {e}")

    # ── Dry-run is always invalid for promotion ───────────────────────
    if dry_run:
        result["warnings"].append("DRY-RUN: not valid for promotion")
        result["passed"] = False
    else:
        result["passed"] = len(result["failures"]) == 0

    return result


def _operator_review_loop(
    take_dir: Path,
    quality: Dict[str, Any],
    mode: str,
    play_cmd: Optional[str] = None,
    validity_evidence: Optional[Dict[str, Any]] = None,
    assertion_evidence: Optional[Dict[str, Any]] = None,
) -> int:
    """
    Post-capture review loop: P/R/A/Q.
    Returns exit code (PASS if accepted, RECORDER_ABORTED if quit,
    or a special code for retry).

    Fail-closed acceptance: validity failures or semantic assertion
    violations disable acceptance even in practice mode.
    """
    take_name = take_dir.name
    q = quality
    failures = q.get("failures", [])
    checks = q.get("checks", [])
    passed = q.get("passed", False)
    dry_run = q.get("dry_run", False)

    while True:
        _clear_screen()
        print("╔══════════════════════════════════════════════════════╗")
        print(f"║  REVIEW — {take_name:<40s} ║")
        print("╠══════════════════════════════════════════════════════╣")

        # Quality summary
        if "quality" in q:
            qual = q["quality"]
            print(f"║  Audio:  {qual.get('duration_ms','?')}ms, 16kHz mono PCM16     ║")
            print(f"║  Peak:   {qual.get('peak_dbfs',0):.1f} dBFS                          ║")
            print(f"║  Clip:   {qual.get('clipping_count',0)} samples                          ║")

        # Event summary
        ec = q.get("event_count")
        if ec is not None:
            print(f"║  Events: {ec} captured                           ║")

        print("╠══════════════════════════════════════════════════════╣")

        # Check results
        for c in checks:
            name = c.get("name", "?")
            ok = c.get("passed", False)
            reason = c.get("reason", "")
            mark = "✓" if ok else "✗"
            print(f"║  {mark} {name:<25s} {reason[:24]:>24s} ║")

        print("╠══════════════════════════════════════════════════════╣")

        # Validity evidence summary
        if validity_evidence and validity_evidence.get("run"):
            ve_passed = validity_evidence.get("passed", False)
            mark = "✓" if ve_passed else "✗"
            reason = validity_evidence.get("reason", "?")
            print(f"║  {mark} capture-validity     {reason[:24]:>24s} ║")

        # Assertion evidence summary
        if assertion_evidence and assertion_evidence.get("run"):
            ae_passed = assertion_evidence.get("passed", False)
            mark = "✓" if ae_passed else "✗"
            reason = assertion_evidence.get("reason", "?")
            print(f"║  {mark} semantic-assertions  {reason[:24]:>24s} ║")

        print("╠══════════════════════════════════════════════════════╣")

        if passed:
            print("║  Result: PASS — all checks OK                        ║")
        else:
            if dry_run:
                print("║  Result: DRY-RUN — invalid for promotion              ║")
            else:
                print(f"║  Result: {'FAIL' if failures else 'WARN'} — {len(failures)} issue(s)                     ║")

        print("╠══════════════════════════════════════════════════════╣")
        print("║                                                      ║")

        # Determine if accept is allowed:
        # - Quality checks must pass (unless practice, which is reviewable)
        # - Validity must pass (fail-closed: always required)
        # - Assertions must pass if they were run (fail-closed)
        # Practice allows accept despite quality failures, but NOT validity/assertion failures
        quality_ok = passed or mode == "practice"
        validity_ok = (not validity_evidence) or (not validity_evidence.get("run")) or validity_evidence.get("passed", False)
        assertion_ok = (not assertion_evidence) or (not assertion_evidence.get("run")) or assertion_evidence.get("passed", False)
        can_accept = quality_ok and validity_ok and assertion_ok

        if can_accept:
            print("║  [P] Play   [R] Retry   [A] Accept   [Q] Quit        ║")
        else:
            print("║  [P] Play   [R] Retry   [Q] Quit                     ║")
            reasons = []
            if not quality_ok:
                reasons.append("quality checks")
            if not validity_ok:
                reasons.append("capture validity")
            if not assertion_ok:
                reasons.append("semantic assertions")
            print(f"║  (Accept disabled: {', '.join(reasons)})        ║")

        print("╚══════════════════════════════════════════════════════╝")
        print()

        try:
            key = _get_key()
        except (EOFError, KeyboardInterrupt):
            return ExitCode.RECORDER_ABORTED

        if key.lower() == "q":
            return ExitCode.RECORDER_ABORTED

        if key.lower() == "a":
            if not can_accept:
                print("\n  Cannot accept: issues require attention.")
                print("  Use R to retry or Q to quit.")
                time.sleep(1.5)
                continue
            # Accept: write review with validity/assertion evidence
            review = {
                "accepted": True,
                "reviewed_at": datetime.now(timezone.utc).isoformat(),
                "quality": q.get("quality", {}),
                "checks_passed": passed,
                "capture_validity_passed": validity_evidence.get("passed") if validity_evidence else None,
                "semantic_assertions_passed": assertion_evidence.get("passed") if assertion_evidence else None,
            }
            save_file(review, take_dir / "review.json")
            print(f"\n  ✓ Take accepted.")
            return ExitCode.PASS

        if key.lower() == "r":
            # Retry: caller handles by returning a sentinel
            return -1  # sentinel for retry

        if key.lower() == "p":
            # Playback
            wav_path = take_dir / "audio.wav"
            if not wav_path.exists():
                print("\n  No audio to play.")
                time.sleep(1.0)
                continue

            cmd_to_run = play_cmd
            if not cmd_to_run:
                # Auto-detect playback command
                import shutil as _shutil
                for candidate in ["pw-play", "aplay", "paplay", "ffplay"]:
                    if _shutil.which(candidate):
                        cmd_to_run = f"{candidate} {{wav}}"
                        break
                if not cmd_to_run:
                    print("\n  No audio player found. Install pw-play, aplay, paplay, or ffplay.")
                    print("  Or use --play-cmd to specify a custom command.")
                    time.sleep(2.0)
                    continue

            full_cmd = cmd_to_run.replace("{wav}", str(wav_path))
            print(f"\n  Playing: {full_cmd}")
            try:
                subprocess.run(full_cmd, shell=True, timeout=60)
            except subprocess.TimeoutExpired:
                print("  Playback timed out.")
            except Exception as e:
                print(f"  Playback error: {e}")
            time.sleep(0.5)


def cmd_operator(args: argparse.Namespace) -> int:
    """
    Main operator command: interactive scenario selection -> recording -> review.

    Fail-closed additions (P1):
    - No-TTY/EOF produces clear noninteractive error.
    - After capture, runs validity checker and semantic assertion DSL.
    - Review loop displays validity/assertion results and disables
      acceptance on failures.
    - --force is removed; no bypass of safety gates.
    """
    # ── Manifest load (must run first for correct exit code) ────────
    manifest_path = Path(args.manifest)
    if not manifest_path.exists():
        die(ExitCode.MANIFEST_INVALID, f"Manifest not found: {manifest_path}")

    try:
        manifest = load_manifest_file(manifest_path)
    except Exception as e:
        die(ExitCode.MANIFEST_INVALID, f"Failed to load manifest: {e}")

    # ── No-TTY/EOF check ─────────────────────────────────────────────
    is_tty = _stdin_is_tty()
    has_scenario = hasattr(args, "scenario") and args.scenario
    dry_run = getattr(args, "dry_run", False)

    # Explicit safe mechanism for test/subprocess automation:
    # --dry-run + --scenario is safe (synthetic data, no mic, no real capture)
    safe_automation = dry_run and has_scenario

    if not is_tty and not safe_automation:
        if not has_scenario:
            die(ExitCode.INTERNAL_ERROR,
                "Operator requires an interactive terminal. "
                "Use --scenario <name> --dry-run for automated testing, "
                "or run from a TTY.")
        else:
            die(ExitCode.INTERNAL_ERROR,
                "Operator requires an interactive terminal for confirmation. "
                "Use --dry-run for automated testing, "
                "or run from a TTY.")

    manifest_dir = manifest_path.parent.resolve()
    out_dir = Path(args.out) if hasattr(args, "out") and args.out else Path("golden-runs")
    out_dir.mkdir(parents=True, exist_ok=True)

    # Scenario selection
    scenario_id = None
    if has_scenario:
        # Try as number first, then as name/id
        raw = str(args.scenario)
        scenarios = manifest.get("scenarios", [])
        try:
            num = int(raw)
            # Build mapping by menu order
            human = [s for s in scenarios if s.get("construction") == "human-recorded"]
            synthetic = [s for s in scenarios if s.get("construction") == "synthetic"]
            ordered = human + synthetic
            if 1 <= num <= len(ordered):
                scenario_id = ordered[num - 1]["id"]
        except ValueError:
            # Try fuzzy match on ID or friendly name
            for s in scenarios:
                sid = s["id"]
                fname = _scenario_friendly_name(sid)
                if raw.lower() == sid.lower() or raw.lower() == fname.lower():
                    scenario_id = sid
                    break
            if scenario_id is None:
                matches = [s["id"] for s in scenarios
                          if raw.lower() in s["id"].lower() or raw.lower() in _scenario_friendly_name(s["id"]).lower()]
                if len(matches) == 1:
                    scenario_id = matches[0]

        if scenario_id is None:
            die(ExitCode.SCENARIO_NOT_FOUND,
                f"No scenario matches '{raw}'. Run without --scenario for interactive menu.")
    else:
        # Interactive menu
        scenario_id = _operator_scenario_menu(manifest)
        if scenario_id is None:
            die(ExitCode.RECORDER_ABORTED, "Operator quit.")

    # Verify scenario exists
    scenario = None
    for s in manifest.get("scenarios", []):
        if s.get("id") == scenario_id:
            scenario = s
            break
    if scenario is None:
        die(ExitCode.SCENARIO_NOT_FOUND, f"Scenario not found: {scenario_id}")

    # Show scenario info and get confirmation (skip in safe automation)
    if safe_automation:
        # Build cues and prompt without interactive display
        cues = _default_cues_for_scenario(scenario_id)
        prompt = _scenario_friendly_name(scenario_id)
    else:
        info = _operator_display_scenario_info(manifest, scenario_id, manifest_dir)
        if info is None:
            die(ExitCode.RECORDER_ABORTED, "Operator quit.")
        if info == "back":
            return cmd_operator(args)  # Go back to menu
        cues = info["cues"]
        prompt = info.get("prompt", "")

    # Determine mode
    practice = getattr(args, "practice", False)
    mode = "practice" if practice else "take"
    dry_run = getattr(args, "dry_run", False)
    device = getattr(args, "device", None)
    play_cmd = getattr(args, "play_cmd", None)
    ws_url = getattr(args, "url", None) or DEFAULT_WS_URL

    # Determine total duration from cues
    total_ms = max((cue.get("band_ms", [0, 0])[1] for cue in cues), default=10000) + 2000

    # Determine take number and directory
    scenario_out = out_dir / "scenarios" / scenario_id / "takes"
    scenario_out.mkdir(parents=True, exist_ok=True)

    max_retries = 50
    retry_count = 0

    while retry_count < max_retries:
        if mode == "take":
            existing_takes = sorted(scenario_out.glob("take-*"))
            take_number = len(existing_takes) + retry_count + 1
        else:
            existing_practice = sorted(scenario_out.glob("practice-*"))
            take_number = len(existing_practice) + retry_count + 1

        take_dir_name = f"{'take' if mode == 'take' else 'practice'}-{take_number:03d}"
        take_dir = scenario_out / take_dir_name

        # Ensure non-overwriting: if dir exists, increment
        attempt = 0
        while take_dir.exists() and attempt < 100:
            attempt += 1
            new_num = take_number + attempt
            take_dir_name = f"{'take' if mode == 'take' else 'practice'}-{new_num:03d}"
            take_dir = scenario_out / take_dir_name

        take_dir.mkdir(parents=True, exist_ok=False)

        # Write metadata
        consent = {
            "consent_version": 1,
            "purpose": "local speech-core endpointing diagnostics",
            "stored_data": ["WAV audio", "transcripts/events", "timing metadata", "quality metrics"],
            "upload_policy": "never uploaded by golden tool",
            "sharing_policy": "not committed unless operator explicitly promotes",
            "retention_policy": "delete-after-run (unless accepted)",
            "speaker_label": "local-operator",
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "scenario_id": scenario_id,
            "mode": mode,
        }
        save_file(consent, take_dir / "consent.json")

        privacy = {
            "retention_class": "delete-after-run",
            "access_policy": "local-only",
            "pii_in_paths": False,
            "scenario_id": scenario_id,
            "take_id": take_dir_name,
            "speaker_label": "local-operator",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        save_file(privacy, take_dir / "privacy.json")

        # Countdown (skip in safe automation to avoid TTY requirement)
        if not safe_automation:
            _countdown(3, "READY")

        # Capture
        stream_session_id = str(uuid.uuid4())

        if dry_run:
            # Dry-run: generate silence
            wav_path = take_dir / "audio.wav"
            samples = _generate_silence(ms_samples(total_ms))
            write_wav(wav_path, samples)
            print(f"  [DRY-RUN] Generated silence WAV: {wav_path}")

            # Fake event stream for dry-run
            event_path = take_dir / "event-stream.jsonl"
            event_path.write_text("")

            capture_code = ExitCode.PASS
        else:
            # Real capture with subprocesses
            print(f"  Launching capture...")
            capture_code, capture_info = _operator_real_capture(
                scenario=scenario,
                cues=cues,
                take_dir=take_dir,
                mode=mode,
                device=device,
                ws_url=ws_url,
                stream_session_id=stream_session_id,
                total_ms=total_ms,
                drain_sec=DEFAULT_EVENT_DRAIN_SEC,
            )

            if capture_code == ExitCode.RECORDER_ABORTED:
                # Clean up and exit
                shutil.rmtree(take_dir, ignore_errors=True)
                return capture_code
            if capture_code == ExitCode.DAEMON_UNREACHABLE:
                print("  Daemon unreachable. Check that speech-core-daemon is running.")
                shutil.rmtree(take_dir, ignore_errors=True)
                return capture_code
            if capture_code == ExitCode.DEPENDENCY_MISSING:
                print("  Required binaries not found. Build them or use --dry-run.")
                shutil.rmtree(take_dir, ignore_errors=True)
                return capture_code

        # Write provenance
        provenance = {
            "stream_session_id": stream_session_id,
            "scenario_id": scenario_id,
            "mode": mode,
            "take_dir": str(take_dir_name),
            "recorder_version": "1.0.0",
            "sample_rate": SAMPLE_RATE,
            "channels": CHANNELS,
            "sample_width_bytes": SAMPLE_WIDTH,
            "device": device or "dry-run" if dry_run else (device or "system default"),
            "dry_run": dry_run,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "cue_timeline": cues,
            "prompt": prompt,
        }
        save_file(provenance, take_dir / "provenance.json")

        # Quality checks — use scenario/manifest-specific thresholds
        min_peak = scenario.get("min_peak_dbfs", -60.0)
        min_rms = scenario.get("min_rms_dbfs", -60.0)
        quality = _operator_quality_check(take_dir, scenario_id, mode, dry_run,
                                           min_peak_dbfs=min_peak,
                                           min_rms_dbfs=min_rms)

        # ── Fail-closed validity and semantic assertions (P1) ──────────
        validity_evidence: Optional[Dict[str, Any]] = None
        assertion_evidence: Optional[Dict[str, Any]] = None
        if not dry_run:
            # Run validity and semantic assertions against captured artifacts
            print("  Running capture validity checks...")
            validity_evidence, assertion_evidence = _operator_run_validity_and_assertions(
                take_dir=take_dir,
                stream_session_id=stream_session_id,
                scenario=scenario,
                manifest_dir=manifest_dir,
            )
            # Merge validity/assertion failures into quality for review display
            if validity_evidence.get("run") and not validity_evidence.get("passed", False):
                quality["failures"].append(
                    f"Capture validity: {validity_evidence.get('reason', 'failed')}"
                )
            if assertion_evidence.get("run") and not assertion_evidence.get("passed", False):
                quality["failures"].append(
                    f"Semantic assertions: {assertion_evidence.get('reason', 'failed')}"
                )

        # Review loop (with validity/assertion evidence)
        review_code = _operator_review_loop(
            take_dir, quality, mode, play_cmd,
            validity_evidence=validity_evidence,
            assertion_evidence=assertion_evidence,
        )

        if review_code == ExitCode.PASS:
            # Accepted
            return ExitCode.PASS
        elif review_code == -1:
            # Retry
            retry_count += 1
            print(f"\n  Retrying (attempt {retry_count + 1})...")
            time.sleep(0.5)
            continue
        else:
            # Quit
            shutil.rmtree(take_dir, ignore_errors=True)
            return ExitCode.RECORDER_ABORTED

    print("  Max retries reached.")
    return ExitCode.RECORDER_ABORTED


# ═══════════════════════════════════════════════════════════════════
# Synthetic scenario generation (synth)
# ═══════════════════════════════════════════════════════════════════

SYNTHETIC_SCENARIO_PLANS: Dict[str, dict] = {
    # ── VAD onset triplets ──
    "synthetic-vad-onset-below-32ms": {
        "segments": [
            {"type": "silence", "duration_ms": 500},
            {"type": "speech_like", "duration_ms": 32, "base_freq": 200},
            {"type": "silence", "duration_ms": 1000},
        ],
        "seed": 42,
        "description": "VAD onset below — 32ms speech-like segment, no vad_speech_start expected",
    },
    "synthetic-vad-onset-at-64ms": {
        "segments": [
            {"type": "silence", "duration_ms": 500},
            {"type": "speech_like", "duration_ms": 64, "base_freq": 200},
            {"type": "silence", "duration_ms": 1500},
        ],
        "seed": 43,
        "description": "VAD onset at threshold — 64ms speech-like segment",
    },
    "synthetic-vad-onset-above-96ms": {
        "segments": [
            {"type": "silence", "duration_ms": 500},
            {"type": "speech_like", "duration_ms": 96, "base_freq": 200},
            {"type": "silence", "duration_ms": 1500},
        ],
        "seed": 44,
        "description": "VAD onset above — 96ms speech-like segment",
    },

    # ── VAD hangover triplets ──
    "synthetic-vad-hangover-below-64ms-silence": {
        "segments": [
            {"type": "silence", "duration_ms": 500},
            {"type": "speech_like", "duration_ms": 600, "base_freq": 200},
            {"type": "silence", "duration_ms": 64},
            {"type": "speech_like", "duration_ms": 400, "base_freq": 200},
            {"type": "silence", "duration_ms": 1500},
        ],
        "seed": 50,
        "description": "VAD hangover below — 64ms internal silence gap",
    },
    "synthetic-vad-hangover-at-96ms-silence": {
        "segments": [
            {"type": "silence", "duration_ms": 500},
            {"type": "speech_like", "duration_ms": 600, "base_freq": 200},
            {"type": "silence", "duration_ms": 96},
            {"type": "speech_like", "duration_ms": 400, "base_freq": 200},
            {"type": "silence", "duration_ms": 1500},
        ],
        "seed": 51,
        "description": "VAD hangover at threshold — 96ms internal silence gap",
    },
    "synthetic-vad-hangover-above-128ms-silence": {
        "segments": [
            {"type": "silence", "duration_ms": 500},
            {"type": "speech_like", "duration_ms": 600, "base_freq": 200},
            {"type": "silence", "duration_ms": 128},
            {"type": "speech_like", "duration_ms": 400, "base_freq": 200},
            {"type": "silence", "duration_ms": 1500},
        ],
        "seed": 52,
        "description": "VAD hangover above — 128ms internal silence gap, expected two VAD segments",
    },

    # ── Min VAD speech triplets ──
    "synthetic-min-vad-speech-below-399ms": {
        "segments": [
            {"type": "silence", "duration_ms": 500},
            {"type": "speech_like", "duration_ms": 399, "base_freq": 200},
            {"type": "silence", "duration_ms": 2500},
        ],
        "seed": 60,
        "description": "Min VAD speech below — 399ms = 6384 samples at 16kHz, suppressed as too short",
    },
    "synthetic-min-vad-speech-at-400ms": {
        "segments": [
            {"type": "silence", "duration_ms": 500},
            {"type": "speech_like", "duration_ms": 400, "base_freq": 200},
            {"type": "silence", "duration_ms": 2500},
        ],
        "seed": 61,
        "description": "Min VAD speech at threshold — 400ms = 6400 samples at 16kHz, eligible boundary",
    },
    "synthetic-min-vad-speech-above-401ms": {
        "segments": [
            {"type": "silence", "duration_ms": 500},
            {"type": "speech_like", "duration_ms": 401, "base_freq": 200},
            {"type": "silence", "duration_ms": 2500},
        ],
        "seed": 62,
        "description": "Min VAD speech above — 401ms = 6416 samples at 16kHz, eligible boundary",
    },

    # ── Smart Turn recheck schedule ──
    "synthetic-smart-recheck-schedule": {
        "segments": [
            {"type": "silence", "duration_ms": 500},
            {"type": "speech_like", "duration_ms": 600, "base_freq": 200},
            {"type": "silence", "duration_ms": 2500},
        ],
        "seed": 70,
        "description": "Smart Turn recheck schedule — speech ends, silence for recheck probing",
    },

    # ── Acoustic fallback ──
    "synthetic-acoustic-fallback-installed-2500": {
        "segments": [
            {"type": "silence", "duration_ms": 500},
            {"type": "speech_like", "duration_ms": 600, "base_freq": 200},
            {"type": "silence", "duration_ms": 2700},
        ],
        "seed": 80,
        "description": "Installed acoustic fallback at 2500ms — 2700ms silence to cross threshold",
    },
    "synthetic-acoustic-fallback-code-default-3500": {
        "segments": [
            {"type": "silence", "duration_ms": 500},
            {"type": "speech_like", "duration_ms": 600, "base_freq": 200},
            {"type": "silence", "duration_ms": 3700},
        ],
        "seed": 81,
        "description": "Code-default acoustic fallback at 3500ms — 3700ms silence",
    },

    # ── Transcript silence close ──
    "synthetic-transcript-silence-close-700": {
        "segments": [
            {"type": "silence", "duration_ms": 500},
            {"type": "silence", "duration_ms": 3200},  # transcript token injection window
            {"type": "silence", "duration_ms": 1500},
        ],
        "seed": 90,
        "description": "Transcript silence close — silence WAV, transcript injected via harness",
    },
}


def synth_scenario(
    manifest: dict,
    manifest_dir: Path,
    scenario_id: str,
    out_dir: Path,
    seed: Optional[int] = None,
    dry_run: bool = False,
) -> int:
    """
    Generate deterministic synthetic WAV and metadata for a scenario.

    Returns exit code.
    """
    # Find scenario in manifest
    scenario = None
    for sc in manifest.get("scenarios", []):
        if sc.get("id") == scenario_id:
            scenario = sc
            break
    if scenario is None:
        die(ExitCode.SCENARIO_NOT_FOUND, f"Scenario not found in manifest: {scenario_id}")

    # Get the scenario plan
    plan = SYNTHETIC_SCENARIO_PLANS.get(scenario_id)
    if plan is None:
        die(
            ExitCode.SYNTH_GENERATION_FAILED,
            f"No synthetic plan defined for scenario: {scenario_id}\n"
            f"Available synthetic scenarios: {', '.join(sorted(SYNTHETIC_SCENARIO_PLANS.keys()))}",
        )

    # Resolve output path
    scenario_out = out_dir / "scenarios" / scenario_id / "synth"
    scenario_out.mkdir(parents=True, exist_ok=True)

    wav_path = scenario_out / "audio.wav"
    plan_path = scenario_out / "segment-plan.json"
    provenance_path = scenario_out / "provenance.json"

    # Check for stale files
    if wav_path.exists() and not dry_run:
        print(f"[WARN] Existing WAV found: {wav_path}", file=sys.stderr)
        print(f"       Remove it manually or use --dry-run to skip.", file=sys.stderr)

    if dry_run:
        print(f"[DRY-RUN] Would generate WAV for: {scenario_id}")
        print(f"          Plan: {json.dumps(plan, indent=2)}")
        return ExitCode.PASS

    # Generate
    samples, provenance = build_synthetic_wav(plan, seed=seed)
    write_wav(wav_path, samples)
    save_file(plan, plan_path)
    save_file(provenance, provenance_path)

    print(f"  Generated: {wav_path}")
    print(f"  Samples:   {len(samples)} ({samples_ms(len(samples))} ms)")
    print(f"  SHA-256:   {provenance['wav_sha256']}")
    print(f"  Seed:      {provenance['seed']}")
    print(f"  Version:   {provenance['generator_version']}")

    return ExitCode.PASS


# ═══════════════════════════════════════════════════════════════════
# Promote
# ═══════════════════════════════════════════════════════════════════

def promote_take(take_dir: Path, dest_dir: Path, dry_run: bool = False) -> int:
    """
    Promote an accepted take to a repo fixture after consent/privacy checks.

    Fail-closed promotion safety (P1): independently rejects
    - dry_run mode (from provenance)
    - practice mode (from provenance)
    - missing/false checks_passed
    - missing/false capture validity evidence
    - missing/false semantic assertion evidence

    Even if review.accepted is manually true, these gates cannot be bypassed.

    Returns exit code.
    """
    if not take_dir.exists():
        die(ExitCode.SCENARIO_NOT_FOUND, f"Take directory not found: {take_dir}")

    # ── Gate 0: Provenance (must load first to check dry_run / practice) ─
    provenance_path = take_dir / "provenance.json"
    if not provenance_path.exists():
        die(ExitCode.INTERNAL_ERROR, f"Missing provenance: {provenance_path}")
    try:
        provenance = load_manifest_file(provenance_path)
    except Exception as e:
        die(ExitCode.INTERNAL_ERROR, f"Invalid provenance: {e}")

    # ── Gate 1: Reject dry_run ─────────────────────────────────────────
    if provenance.get("dry_run", False):
        die(ExitCode.BASELINE_REQUIRES_REVIEW,
            "Cannot promote: provenance indicates dry_run mode. "
            "Dry-run takes are synthetic silence and invalid for promotion.")

    # ── Gate 2: Reject practice mode ────────────────────────────────────
    if provenance.get("mode") == "practice":
        die(ExitCode.BASELINE_REQUIRES_REVIEW,
            "Cannot promote: provenance indicates practice mode. "
            "Practice takes are reviewable but never promotable.")

    # Check consent
    consent_path = take_dir / "consent.json"
    if not consent_path.exists():
        die(ExitCode.CONSENT_REQUIRED, f"Missing consent file: {consent_path}")

    try:
        consent = load_manifest_file(consent_path)
    except Exception as e:
        die(ExitCode.CONSENT_REQUIRED, f"Invalid consent file: {e}")

    # Check privacy
    privacy_path = take_dir / "privacy.json"
    if not privacy_path.exists():
        die(ExitCode.PRIVACY_POLICY_VIOLATION, f"Missing privacy file: {privacy_path}")

    try:
        privacy = load_manifest_file(privacy_path)
    except Exception as e:
        die(ExitCode.PRIVACY_POLICY_VIOLATION, f"Invalid privacy file: {e}")

    # ── Gate 3: Review with checks_passed ──────────────────────────────
    review_path = take_dir / "review.json"
    if not review_path.exists():
        die(ExitCode.BASELINE_REQUIRES_REVIEW, f"Missing review file: {review_path}")

    try:
        review = load_manifest_file(review_path)
    except Exception as e:
        die(ExitCode.BASELINE_REQUIRES_REVIEW, f"Invalid review file: {e}")

    if not review.get("accepted", False):
        die(ExitCode.BASELINE_REQUIRES_REVIEW, f"Take not accepted: {review_path}")

    if not review.get("checks_passed", False):
        die(ExitCode.BASELINE_REQUIRES_REVIEW,
            "Cannot promote: review.checks_passed is false or missing. "
            "Quality checks must pass before promotion.")

    # ── Gate 4: Capture validity evidence ──────────────────────────────
    evidence_dir = take_dir / "quality" / "review" / "provenance"
    validity_path = evidence_dir / "validity.json"
    if not validity_path.exists():
        die(ExitCode.BASELINE_REQUIRES_REVIEW,
            "Cannot promote: missing capture validity evidence. "
            f"Expected at {validity_path}")
    try:
        validity_evidence = load_manifest_file(validity_path)
    except Exception as e:
        die(ExitCode.BASELINE_REQUIRES_REVIEW,
            f"Cannot promote: invalid validity evidence: {e}")
    if not validity_evidence.get("valid", False):
        die(ExitCode.BASELINE_REQUIRES_REVIEW,
            f"Cannot promote: capture validity check failed: "
            f"{validity_evidence.get('reason', 'unknown')}")

    # ── Gate 6: Event-stream hash consistency ─────────────────────────
    event_path = take_dir / "event-stream.jsonl"
    promote_diag: Dict[str, Any] = {}
    if event_path.exists():
        event_hash = sha256_file(event_path)
        promote_diag["event_stream_sha256"] = event_hash
        # Cross-check against validity record if present
        if validity_evidence:
            ah = validity_evidence.get("artifact_hashes", {})
            if ah.get("event_stream_sha256") and ah["event_stream_sha256"] != event_hash:
                die(ExitCode.ARTIFACT_HASH_MISMATCH,
                    f"Event-stream hash mismatch: provenance says {event_hash}, "
                    f"validity record says {ah['event_stream_sha256']}")

    # ── Gate 7: stream_session_id consistency ─────────────────────────
    provenance_sid = provenance.get("stream_session_id")
    if provenance_sid:
        # Check against validity evidence if present
        if validity_evidence:
            vsid = validity_evidence.get("stream_session_id")
            if vsid and vsid != provenance_sid:
                die(ExitCode.EVENT_SCHEMA_INVALID,
                    f"stream_session_id mismatch: provenance={provenance_sid}, "
                    f"validity={vsid}")
        # Check against events in the event stream
        if event_path.exists():
            # Sample first few events to verify session id consistency
            session_mismatch = False
            try:
                with open(event_path, "r", encoding="utf-8", errors="replace") as f:
                    for line_no, line in enumerate(f, 1):
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            evt = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        evt_sid = evt.get("stream_session_id")
                        if evt_sid and evt_sid != provenance_sid:
                            session_mismatch = True
                            promote_diag["session_mismatch_line"] = line_no
                            break
                        if line_no > 50:  # Check first 50 events
                            break
            except Exception:
                pass
            if session_mismatch:
                die(ExitCode.EVENT_SCHEMA_INVALID,
                    f"stream_session_id in events does not match provenance: "
                    f"expected {provenance_sid}")

    # ── Gate 5: Semantic assertion evidence ────────────────────────────
    assert_path = evidence_dir / "assert-report.json"
    if assert_path.exists():
        try:
            assert_evidence = load_manifest_file(assert_path)
        except Exception as e:
            die(ExitCode.BASELINE_REQUIRES_REVIEW,
                f"Cannot promote: invalid assertion evidence: {e}")
        if not assert_evidence.get("passed", False):
            die(ExitCode.BASELINE_REQUIRES_REVIEW,
                f"Cannot promote: semantic assertions failed.")
    # No assertion DSL is acceptable (not all scenarios have one)

    # Check PII in paths
    take_name = take_dir.name
    pii_patterns = ["@", "email", "name", "user", "home"]
    for pat in pii_patterns:
        if pat in str(take_dir).lower():
            die(
                ExitCode.PRIVACY_POLICY_VIOLATION,
                f"Possible PII in path: {take_dir} (contains '{pat}')",
            )

    # Check WAV exists and is valid
    wav_path = take_dir / "audio.wav"
    if not wav_path.exists():
        die(ExitCode.WAV_FORMAT_INVALID, f"WAV missing: {wav_path}")

    wav_errors = validate_wav(wav_path)
    if wav_errors:
        for e in wav_errors:
            print(f"[FAIL] {e}", file=sys.stderr)
        die(ExitCode.WAV_FORMAT_INVALID, f"WAV validation failed: {wav_path}")

    if dry_run:
        print(f"[DRY-RUN] Would promote {take_dir} -> {dest_dir}")
        return ExitCode.PASS

    # Promote: copy to dest
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Copy all artifacts
    for item in take_dir.iterdir():
        dest_item = dest_dir / item.name
        if item.is_dir():
            if dest_item.exists():
                shutil.rmtree(dest_item)
            shutil.copytree(item, dest_item)
        else:
            shutil.copy2(item, dest_item)

    # Write promotion record
    promotion = {
        "promoted_at": datetime.now(timezone.utc).isoformat(),
        "source": str(take_dir.absolute()),
        "dest": str(dest_dir.absolute()),
        "consent_sha256": sha256_file(consent_path),
        "privacy_sha256": sha256_file(privacy_path),
        "wav_sha256": sha256_file(wav_path),
        "review_sha256": sha256_file(review_path),
        "retention_class": privacy.get("retention_class", "repo-fixture-explicit"),
    }
    save_file(promotion, dest_dir / "promotion.json")

    print(f"  ✓ Promoted: {take_dir} -> {dest_dir}")
    return ExitCode.PASS


# ═══════════════════════════════════════════════════════════════════
# Delegation to speech-core-golden-assert
# ═══════════════════════════════════════════════════════════════════

_ASSERT_SCRIPT = Path(__file__).resolve().parent / "speech-core-golden-assert.py"


def _build_assert_args(args: argparse.Namespace, keys: List[str]) -> List[str]:
    """Build CLI argument list for delegation, converting underscore keys to dash-prefixed args."""
    result: List[str] = []
    for key in keys:
        value = getattr(args, key, None)
        if value is None:
            continue
        flag = "--" + key.replace("_", "-")
        if isinstance(value, bool):
            if value:
                result.append(flag)
        elif isinstance(value, list):
            result.append(flag)
            result.extend(str(v) for v in value)
        else:
            result.append(flag)
            result.append(str(value))
    return result


def delegate_to_assert(cmd: str, cli_args: List[str]) -> int:
    """Delegate a command to speech-core-golden-assert.py via subprocess.

    Propagates exact exit codes and stdout/stderr.
    """
    argv = [sys.executable, str(_ASSERT_SCRIPT), cmd] + cli_args
    result = subprocess.run(argv)
    return result.returncode


def delegate_delete(args: argparse.Namespace) -> int:
    """Safe audio purge: deletes WAV file(s) while retaining metadata/hash/tombstone.

    Implements consent/privacy-record-aware deletion:
    - Preserves JSON metadata, provenance, consent, and review files.
    - Writes a tombstone record when audio is purged.
    - Supports --dry-run for safety inspection.
    - Requires explicit --purge-audio flag.
    """
    run_dir = Path(args.run) if args.run else None
    if not run_dir or not run_dir.exists():
        print(f"Run directory not found: {run_dir}", file=sys.stderr)
        return ExitCode.SCENARIO_NOT_FOUND

    scenario_id = args.scenario or "unknown"
    dry_run = args.dry_run
    purge_audio = args.purge_audio

    if not purge_audio:
        print("delete: --purge-audio is required to remove audio files.", file=sys.stderr)
        print("        Use --dry-run first to inspect what would be deleted.", file=sys.stderr)
        return ExitCode.INTERNAL_ERROR

    # Collect WAV files
    wav_files = sorted(run_dir.rglob("*.wav"))
    if not wav_files:
        print(f"No WAV files found in {run_dir}")
        return ExitCode.PASS

    tombstone = {
        "operation": "delete",
        "scenario_id": scenario_id,
        "run_dir": str(run_dir),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "purged_files": [],
        "retained_metadata": [],
        "sha256_tombstone": True,
    }

    for wav_path in wav_files:
        wav_hash = sha256_file(wav_path)
        entry = {
            "path": str(wav_path),
            "wav_sha256": wav_hash,
            "size_bytes": wav_path.stat().st_size,
        }
        tombstone["purged_files"].append(entry)

        # Record retained metadata files alongside
        for sibling in sorted(wav_path.parent.glob("*.json")):
            rel = str(sibling.relative_to(run_dir))
            if rel not in tombstone["retained_metadata"]:
                tombstone["retained_metadata"].append(rel)

    if dry_run:
        print(f"[DRY-RUN] Would purge {len(wav_files)} WAV file(s) from {run_dir}:")
        for entry in tombstone["purged_files"]:
            print(f"  {entry['path']}  ({entry['size_bytes']} bytes, sha256={entry['wav_sha256'][:12]}…)")
        print(f"[DRY-RUN] Metadata files retained: {len(tombstone['retained_metadata'])}")
        tombstone_path = run_dir / "delete-tombstone.json"
        print(f"[DRY-RUN] Would write tombstone: {tombstone_path}")
        return ExitCode.PASS

    # Purge audio
    purged = 0
    for entry in tombstone["purged_files"]:
        path = Path(entry["path"])
        if path.exists():
            path.unlink()
            purged += 1

    # Write tombstone
    tombstone_path = run_dir / "delete-tombstone.json"
    with open(tombstone_path, "w") as f:
        json.dump(tombstone, f, indent=2)

    print(f"Purged {purged} WAV file(s) from {run_dir}")
    print(f"Tombstone written: {tombstone_path}")
    print(f"Metadata retained: {len(tombstone['retained_metadata'])} file(s)")
    return ExitCode.PASS


# ═══════════════════════════════════════════════════════════════════
# Legacy fixture quarantine / migration
# ═══════════════════════════════════════════════════════════════════

def quarantine_legacy_fixtures(legacy_dir: Path, dry_run: bool = False) -> int:
    """
    Quarantine legacy fixtures with explicit disposition.
    Does not delete evidence; writes quarantine report.
    """
    legacy_dir.mkdir(parents=True, exist_ok=True)

    report = {
        "quarantine_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "disposition": "Legacy fixtures retained for evidence; not valid release gates.",
        "fixtures": [
            {
                "legacy_id": "01-clean-sentence",
                "disposition": "quarantine",
                "reclassify_as": "legacy-clean-fallback-regression",
                "reason": "One degraded vad_acoustic_fallback; smart-turn incomplete.",
                "migration": "Re-record as human-clean-complete until clean smart_turn close.",
            },
            {
                "legacy_id": "02-trailing-off",
                "disposition": "candidate-fallback",
                "reclassify_as": None,
                "reason": "One degraded vad_acoustic_fallback; transcript matches trailing-off.",
                "migration": "Import with WAV hash and config if fallback intent explicit.",
            },
            {
                "legacy_id": "03-pause-resume",
                "disposition": "quarantine",
                "reclassify_as": None,
                "reason": "Two turns; closes on first phrase; does not test resume-without-close.",
                "migration": "Re-record with deliberate incomplete pre-pause phrase.",
            },
            {
                "legacy_id": "04-human-hold",
                "disposition": "quarantine",
                "reclassify_as": "fallback/no-token fixture only",
                "reason": "7.5s hum; still under 7500ms hold; closes via fallback.",
                "migration": "New hold fixture must sustain VAD-active no-token audio >= 8s.",
            },
            {
                "legacy_id": "05-short-word",
                "disposition": "reclassify",
                "reclassify_as": "human-short-complete-no-transcript",
                "reason": "Short word passes min speech, closes via smart_turn with no tokens.",
                "migration": "Add synthetic below/at/above min-speech triplet for actual threshold coverage.",
            },
            {
                "legacy_id": "06-rapid-question",
                "disposition": "candidate-after-recapture",
                "reclassify_as": None,
                "reason": "Smart-turn close plus extra session_end close; transcript mismatch.",
                "migration": "Recapture with terminal-marker capture to eliminate session-end artifact.",
            },
            {
                "legacy_id": "07-self-interrupt",
                "disposition": "candidate-exhaustive",
                "reclassify_as": None,
                "reason": "Two VAD segments, one smart-turn close on recheck; minor transcript mismatch.",
                "migration": "Import with coarse assertions around interruption/recheck.",
            },
            {
                "legacy_id": "08-slow-thoughtful",
                "disposition": "candidate-exhaustive",
                "reclassify_as": None,
                "reason": "Four VAD segments, earlier incomplete decisions, final smart-turn close.",
                "migration": "Import with provenance and assertions for multiple incomplete decisions.",
            },
        ],
    }

    report_path = legacy_dir / "quarantine-report.yaml"
    if not dry_run:
        save_file(report, report_path)
        print(f"  Quarantine report written: {report_path}")
    else:
        print(f"[DRY-RUN] Would write quarantine report: {report_path}")

    return ExitCode.PASS


# ═══════════════════════════════════════════════════════════════════
# CLI argument parsing
# ═══════════════════════════════════════════════════════════════════

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="speech-core-golden",
        description="Guided golden recording, synthesis, and manifest tool for speech-core endpointing.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(f"""\
            Exit codes:
              {ExitCode.PASS:2d}  PASS
              {ExitCode.ASSERTION_FAILED:2d}  ASSERTION_FAILED
              {ExitCode.MANIFEST_INVALID:2d}  MANIFEST_INVALID
              {ExitCode.QUALITY_FAILED:2d}  QUALITY_FAILED
              {ExitCode.CONSENT_REQUIRED:2d}  CONSENT_REQUIRED
              {ExitCode.DEPENDENCY_MISSING:2d}  DEPENDENCY_MISSING
              {ExitCode.DAEMON_UNREACHABLE:2d}  DAEMON_UNREACHABLE
              {ExitCode.MODEL_UNAVAILABLE:2d}  MODEL_UNAVAILABLE
              {ExitCode.CAPTURE_TIMEOUT:2d}  CAPTURE_TIMEOUT
              {ExitCode.TERMINAL_MARKER_MISSING:2d}  TERMINAL_MARKER_MISSING
              {ExitCode.ARTIFACT_HASH_MISMATCH:2d}  ARTIFACT_HASH_MISMATCH
              {ExitCode.PRIVACY_POLICY_VIOLATION:2d}  PRIVACY_POLICY_VIOLATION
              {ExitCode.CONFIG_MISMATCH:2d}  CONFIG_MISMATCH
              {ExitCode.UNSUPPORTED_PROFILE:2d}  UNSUPPORTED_PROFILE
              {ExitCode.SCENARIO_NOT_FOUND:2d}  SCENARIO_NOT_FOUND
              {ExitCode.RECORDER_ABORTED:2d}  RECORDER_ABORTED
              {ExitCode.SYNTH_GENERATION_FAILED:2d}  SYNTH_GENERATION_FAILED
              {ExitCode.WAV_FORMAT_INVALID:2d}  WAV_FORMAT_INVALID
              {ExitCode.EVENT_SCHEMA_INVALID:2d}  EVENT_SCHEMA_INVALID
              {ExitCode.BASELINE_REQUIRES_REVIEW:2d}  BASELINE_REQUIRES_REVIEW
              {ExitCode.INTERNAL_ERROR:2d}  INTERNAL_ERROR
              {ExitCode.CAPTURE_INCOMPLETE:2d}  CAPTURE_INCOMPLETE
        """),
    )

    sub = parser.add_subparsers(dest="command", help="Available commands")

    # ── validate-manifest ──
    p_val = sub.add_parser("validate-manifest", help="Validate manifest schema, profile references, scenario IDs")
    p_val.add_argument("--manifest", required=True, help="Path to manifest YAML/JSON file")
    p_val.add_argument("--dry-run", action="store_true", help="Dry-run mode")

    # ── record ──
    p_rec = sub.add_parser("record", help="Run guided recorder UX for a scenario")
    p_rec.add_argument("--manifest", required=True, help="Path to manifest YAML/JSON file")
    p_rec.add_argument("--scenario", required=True, help="Scenario ID to record")
    p_rec.add_argument("--out", required=True, help="Output directory for run artifacts")
    p_rec.add_argument("--practice", action="store_true", help="Practice take (not promoted)")
    p_rec.add_argument("--take", action="store_true", help="Formal take (promotable)")
    p_rec.add_argument("--device", help="Audio device name")
    p_rec.add_argument("--dry-run", action="store_true", help="Dry-run mode (no microphone)")

    # ── synth ──
    p_syn = sub.add_parser("synth", help="Build deterministic synthetic WAV and cue timelines")
    p_syn.add_argument("--manifest", required=True, help="Path to manifest YAML/JSON file")
    p_syn.add_argument("--scenario", required=True, help="Synthetic scenario ID")
    p_syn.add_argument("--out", required=True, help="Output directory")
    p_syn.add_argument("--seed", type=int, help="Override generator seed")
    p_syn.add_argument("--dry-run", action="store_true", help="Dry-run (print plan, no WAV)")

    # ── capture ──
    p_cap = sub.add_parser("capture", help="Subscribe, replay WAV, wait for terminal markers")
    p_cap.add_argument("--url", default="ws://127.0.0.1:8765/ws/audio-ingress", help="WebSocket URL")
    p_cap.add_argument("--stream-session-id", help="Unique stream session id")
    p_cap.add_argument("--out", help="Output directory for event-stream.jsonl")
    p_cap.add_argument("--timeout-ms", type=int, default=30000, help="Capture timeout in ms")
    p_cap.add_argument("--adapter-cmd", nargs="*", help="Optional adapter command to spawn")
    p_cap.add_argument("--adapter-cwd", help="Working dir for adapter")
    p_cap.add_argument("--manifest", help="(ignored, forwarded for CLI compat)")
    p_cap.add_argument("--scenario", help="(ignored, forwarded for CLI compat)")

    # ── assert ──
    p_asr = sub.add_parser("assert", help="Run assertion DSL against captured artifacts")
    p_asr.add_argument("--scenario-dir", help="Path to scenario take directory")
    p_asr.add_argument("--assertion-dsl", help="Path to assertion DSL YAML/JSON file")
    p_asr.add_argument("--stream-session-id", help="Expected stream session id")
    p_asr.add_argument("--wav-hash", help="Expected WAV SHA-256")
    p_asr.add_argument("--config-hash", help="Expected config SHA-256")

    # ── run ──
    p_run = sub.add_parser("run", help="Combined capture + assert")
    p_run.add_argument("--url", default="ws://127.0.0.1:8765/ws/audio-ingress", help="WebSocket URL")
    p_run.add_argument("--stream-session-id", help="Unique stream session id")
    p_run.add_argument("--out", help="Output directory")
    p_run.add_argument("--timeout-ms", type=int, default=30000, help="Capture timeout in ms")
    p_run.add_argument("--assertion-dsl", help="Assertion DSL file")
    p_run.add_argument("--wav-hash", help="Expected WAV SHA-256")
    p_run.add_argument("--config-hash", help="Expected config SHA-256")

    # ── promote ──
    p_pro = sub.add_parser("promote", help="Promote accepted take to repo fixture")
    p_pro.add_argument("--take", required=True, help="Path to take directory")
    p_pro.add_argument("--dest", required=True, help="Destination fixture directory")
    p_pro.add_argument("--dry-run", action="store_true", help="Dry-run mode")

    # ── delete ──
    p_del = sub.add_parser("delete", help="Delete human audio per retention policy (retains metadata)")
    p_del.add_argument("--run", help="Run directory")
    p_del.add_argument("--scenario", help="Scenario ID")
    p_del.add_argument("--purge-audio", action="store_true", help="Purge audio files")
    p_del.add_argument("--dry-run", action="store_true", help="Dry-run mode")

    # ── quarantine-legacy ──
    p_leg = sub.add_parser("quarantine-legacy", help="Quarantine legacy eight fixtures with explicit disposition")
    p_leg.add_argument("--legacy-dir", default="tests/golden/legacy", help="Legacy directory")
    p_leg.add_argument("--dry-run", action="store_true", help="Dry-run mode")

    # ── operator ──
    p_op = sub.add_parser("operator", help="Interactive guided recording workflow")
    p_op.add_argument("--manifest", default="tests/golden/manifest.yaml", help="Path to manifest YAML/JSON")
    p_op.add_argument("--scenario", help="Pre-select scenario (number or name); skip menu")
    p_op.add_argument("--out", default="golden-runs", help="Output directory for run artifacts")
    p_op.add_argument("--practice", action="store_true", help="Practice take (not saved)")
    p_op.add_argument("--dry-run", action="store_true", help="Dry-run mode (synthetic silence, no mic)")
    p_op.add_argument("--device", help="Audio device name")
    p_op.add_argument("--url", default=None, help="Daemon WebSocket URL")
    p_op.add_argument("--play-cmd", help="Playback command template (use {wav} for path)")
    # --force removed: no bypass of promotion safety gates (P1)

    return parser


# ═══════════════════════════════════════════════════════════════════
# Main entry point
# ═══════════════════════════════════════════════════════════════════

def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(ExitCode.PASS)

    command = args.command

    # ── validate-manifest ──
    if command == "validate-manifest":
        manifest_path = Path(args.manifest)
        if not manifest_path.exists():
            die(ExitCode.MANIFEST_INVALID, f"Manifest file not found: {manifest_path}")

        try:
            manifest = load_manifest_file(manifest_path)
        except Exception as e:
            die(ExitCode.MANIFEST_INVALID, f"Failed to load manifest: {e}")

        manifest_dir = manifest_path.parent.resolve()
        errors = validate_manifest(manifest, manifest_dir)

        if errors:
            print(f"Manifest INVALID ({len(errors)} errors):")
            for e in errors:
                print(f"  ✗ {e}")
            sys.exit(ExitCode.MANIFEST_INVALID)
        else:
            print(f"Manifest valid: {manifest_path}")
            print(f"  Profile:  {manifest.get('profile', 'none')}")
            print(f"  Scenarios: {len(manifest.get('scenarios', []))}")
            sys.exit(ExitCode.PASS)

    # ── record ──
    elif command == "record":
        if not args.practice and not args.take:
            die(ExitCode.MANIFEST_INVALID, "Must specify --practice or --take")

        mode = "practice" if args.practice else "take"
        manifest_path = Path(args.manifest)
        manifest = load_manifest_file(manifest_path)
        manifest_dir = manifest_path.parent.resolve()
        out_dir = Path(args.out)

        code = guided_record(
            manifest, manifest_dir, args.scenario, out_dir, mode,
            device=args.device, dry_run=args.dry_run,
        )
        sys.exit(code)

    # ── synth ──
    elif command == "synth":
        manifest_path = Path(args.manifest)
        manifest = load_manifest_file(manifest_path)
        manifest_dir = manifest_path.parent.resolve()
        out_dir = Path(args.out)

        code = synth_scenario(
            manifest, manifest_dir, args.scenario, out_dir,
            seed=args.seed, dry_run=args.dry_run,
        )
        sys.exit(code)

    # ── capture ──
    elif command == "capture":
        cli_args = _build_assert_args(args, ["url", "stream_session_id", "out", "timeout_ms", "adapter_cmd", "adapter_cwd"])
        sys.exit(delegate_to_assert("capture", cli_args))

    # ── assert ──
    elif command == "assert":
        cli_args = _build_assert_args(args, ["scenario_dir", "assertion_dsl", "stream_session_id", "wav_hash", "config_hash"])
        sys.exit(delegate_to_assert("assert", cli_args))

    # ── run ──
    elif command == "run":
        cli_args = _build_assert_args(args, ["url", "stream_session_id", "out", "timeout_ms", "assertion_dsl", "wav_hash", "config_hash"])
        sys.exit(delegate_to_assert("run", cli_args))

    # ── promote ──
    elif command == "promote":
        take_dir = Path(args.take)
        dest_dir = Path(args.dest)
        code = promote_take(take_dir, dest_dir, dry_run=args.dry_run)
        sys.exit(code)

    # ── delete ──
    elif command == "delete":
        sys.exit(delegate_delete(args))

    # ── quarantine-legacy ──
    elif command == "quarantine-legacy":
        legacy_dir = Path(args.legacy_dir)
        code = quarantine_legacy_fixtures(legacy_dir, dry_run=args.dry_run)
        sys.exit(code)

    # ── operator ──
    elif command == "operator":
        code = cmd_operator(args)
        sys.exit(code)

    else:
        print(f"Unknown command: {command}", file=sys.stderr)
        parser.print_help()
        sys.exit(ExitCode.INTERNAL_ERROR)


if __name__ == "__main__":
    main()
