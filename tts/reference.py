"""not on the voicecat path.

Reference workbench: original audio + ordered edits -> effective reference,
then optional stream.fm. Never mutates the source file. Cache is internal.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tts.edits import EditSpec, transcript_status
from tts.hashes import sha256_file
from tts.paths import ensure_lab_dirs
from tts.preprocess import materialize_effective_wav, process_reference
from tts.wav import duration_s, is_riff_wav, read_wav


def _audio_path(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        return str(value.get("path") or value.get("name") or "")
    return str(value)


def materialize_effective(
    source: Path | str,
    spec: EditSpec | dict[str, Any] | None = None,
    *,
    lab_root: Path | None = None,
) -> dict[str, Any]:
    src = Path(source)
    if not src.is_file():
        raise FileNotFoundError(src)
    spec = spec if isinstance(spec, EditSpec) else EditSpec.from_dict(spec)
    dest = materialize_effective_wav(src, spec, lab_root=lab_root)
    sr, samples = read_wav(dest)
    if is_riff_wav(src):
        source_sr, source_samples = read_wav(src)
    else:
        source_sr, source_samples = sr, samples
    return {
        "source": str(src),
        "source_sha256": sha256_file(src),
        "edits": spec.to_dict(),
        "path": str(dest),
        "sample_rate": int(sr),
        "duration_s": duration_s(sr, samples),
        "source_duration_s": duration_s(source_sr, source_samples),
    }


class ReferenceWorkbench:
    """Session-side original + edits + optional stream.fm selection."""

    def __init__(self, lab: Path | None = None) -> None:
        self.lab = ensure_lab_dirs(lab)
        self.source_path: str | None = None
        self.source_hash: str | None = None
        self.source_transcript: str = ""
        self.effective_transcript: str = ""
        self.spec = EditSpec()
        self.streamfm_enabled = False
        self.active = "original"
        self.voice_id: str | None = None
        self.voice_name: str = ""
        self._streamfm: dict[str, Any] | None = None

    def snapshot(self) -> dict[str, Any]:
        materialized: dict[str, Any] | None = None
        streamfm = self._streamfm or {}
        if self.source_path:
            try:
                materialized = materialize_effective(
                    self.source_path, self.spec, lab_root=self.lab
                )
            except Exception as exc:
                materialized = {"error": str(exc)}
        status = transcript_status(
            self.source_transcript, self.effective_transcript, self.spec
        )
        effective_path = (materialized or {}).get("path")
        streamfm_path = (
            streamfm.get("output")
            if streamfm.get("status") in {"ok", "cache_hit"}
            else None
        )
        active_path = (
            streamfm_path if self.active == "streamfm" and streamfm_path else effective_path
        )
        active_label = (
            "stream.fm" if self.active == "streamfm" and streamfm_path else "original"
        )
        duration = None
        if self.active == "streamfm" and streamfm_path and Path(str(streamfm_path)).is_file():
            sr, samples = read_wav(streamfm_path)
            duration = duration_s(sr, samples)
        elif materialized and materialized.get("duration_s") is not None:
            duration = materialized.get("duration_s")
        return {
            "source_path": self.source_path,
            "source_hash": self.source_hash,
            "edits": self.spec.to_dict(),
            "source_transcript": self.source_transcript,
            "effective_transcript": self.effective_transcript,
            "transcript_status": status,
            "streamfm_enabled": self.streamfm_enabled,
            "streamfm": streamfm,
            "streamfm_path": streamfm_path,
            "effective_path": effective_path,
            "active": active_label,
            "active_path": active_path,
            "active_duration_s": duration,
            "voice_id": self.voice_id,
            "voice_name": self.voice_name,
            "source_duration_s": (materialized or {}).get("source_duration_s"),
            "effective_duration_s": (materialized or {}).get("duration_s"),
            "error": (materialized or {}).get("error"),
        }

    def card_text(self) -> str:
        snap = self.snapshot()
        voice = snap.get("voice_name") or snap.get("voice_id") or "(unsaved)"
        duration = snap.get("active_duration_s")
        dur = f"{float(duration):.1f}s" if duration is not None else "n/a"
        stale = ""
        if snap.get("transcript_status") == "stale":
            stale = " · transcript needs review"
        return (
            f"voice: {voice}  ·  reference: {snap.get('active')}  "
            f"·  duration: {dur}{stale}"
        )

    def load_source(
        self,
        audio: Any,
        transcript: str = "",
        *,
        reset: bool = True,
        voice_id: str | None = None,
        voice_name: str = "",
        edits: dict[str, Any] | None = None,
        active: str = "original",
    ) -> dict[str, Any]:
        path = _audio_path(audio)
        if not path:
            raise ValueError("upload reference audio first")
        src = Path(path)
        if not src.is_file():
            raise FileNotFoundError(src)
        if reset:
            self.spec = EditSpec.from_dict(edits)
            self.streamfm_enabled = str(active).lower().startswith("stream")
            self.active = "streamfm" if self.streamfm_enabled else "original"
            self._streamfm = None
            self.source_transcript = (transcript or "").strip()
            self.effective_transcript = self.source_transcript
        self.source_path = str(src)
        self.source_hash = sha256_file(src) if is_riff_wav(src) else sha256_file(src)
        self.voice_id = voice_id
        self.voice_name = voice_name
        if self.streamfm_enabled:
            self.enable_streamfm(True)
        return self.snapshot()

    def exclude(self, start_s: float, end_s: float) -> dict[str, Any]:
        self.spec = self.spec.exclude(start_s, end_s)
        self._after_edit()
        return self.snapshot()

    def keep(self, start_s: float | None, end_s: float | None) -> dict[str, Any]:
        self.spec = self.spec.keep(start_s, end_s)
        self._after_edit()
        return self.snapshot()

    def undo(self) -> dict[str, Any]:
        self.spec = self.spec.undo()
        self._after_edit()
        return self.snapshot()

    def clear(self) -> dict[str, Any]:
        self.spec = self.spec.clear()
        self._after_edit()
        return self.snapshot()

    def reset(self) -> dict[str, Any]:
        self.spec = EditSpec()
        self.streamfm_enabled = False
        self.active = "original"
        self._streamfm = None
        self.effective_transcript = self.source_transcript
        return self.snapshot()

    def set_transcripts(
        self,
        *,
        source: str | None = None,
        effective: str | None = None,
    ) -> dict[str, Any]:
        if source is not None:
            self.source_transcript = source.strip()
        if effective is not None:
            self.effective_transcript = effective.strip()
        return self.snapshot()

    def set_active(self, variant: str) -> dict[str, Any]:
        if str(variant).lower().startswith("stream"):
            return self.enable_streamfm(True)
        self.active = "original"
        self.streamfm_enabled = False
        return self.snapshot()

    def enable_streamfm(self, enabled: bool) -> dict[str, Any]:
        self.streamfm_enabled = bool(enabled)
        if not self.streamfm_enabled:
            self.active = "original"
            return self.snapshot()
        if not self.source_path:
            raise ValueError("upload reference audio first")
        self._streamfm = process_reference(
            self.source_path,
            lab_root=self.lab,
            edit_spec=self.spec.to_dict(),
        )
        if self._streamfm.get("status") in {"ok", "cache_hit"}:
            self.active = "streamfm"
        else:
            self.active = "original"
        return self.snapshot()

    def clone_inputs(self) -> dict[str, Any]:
        snap = self.snapshot()
        path = snap.get("active_path") or snap.get("effective_path") or self.source_path
        text = self.effective_transcript or self.source_transcript
        return {
            "path": path,
            "transcript": text,
            "variant": snap.get("active"),
            "edits": self.spec.to_dict(),
            "source_path": self.source_path,
        }

    def _after_edit(self) -> None:
        self._streamfm = None
        if self.streamfm_enabled:
            self.enable_streamfm(True)
        else:
            self.active = "original"


def dump_edits(spec: EditSpec) -> str:
    if not spec.ops:
        return "(no cuts — whole original)"
    return json.dumps(spec.to_dict(), indent=2)
