"""Desk talker: leftover hop synthesizes through the loaded Breeze worker.

Leftover v1 stays the mouth. This module never talks to the parked legacy :18091.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

from tts.generation import GenerationSettings
from tts.hashes import sha256_json
from tts.lab.backend.models import DualGuidance, Interval, parse_guidance
from tts.lab.backend.routes.voices import get_voice_or_404
from tts.lab.backend.runtime.types import RuntimeBusy, RuntimeUnloaded
from tts.lab.backend.services.references import (
    materialize_keep_wav,
    processed_variant_is_current,
)
from tts.lab.backend.store.cache import canonical_keep_intervals
from tts.wav import as_pcm16, read_wav

# leftover speak.voice still sends legacy voice aliases. those are not lab ids.
DEFAULT_STEER = (
    "Fast and informal, drop the politeness, keep moving, no pauses."
)
LEFTOVER_VOICE_ALIASES = {
    "",
    "default",
    "m1",
    "ryan",
    "lock-informal",
    "informal",
    "female-ish",
}


def _settings_from_generation(raw: dict[str, Any] | None) -> GenerationSettings:
    if not raw:
        return GenerationSettings()
    if "guidance" in raw:
        guidance = parse_guidance(raw["guidance"])
        settings = GenerationSettings(seed=int(raw.get("seed", 42)))
        if isinstance(guidance, DualGuidance):
            settings.cfg_scale_ref = float(guidance.reference)
            settings.cfg_scale_ins = float(guidance.instruction)
        else:
            settings.cfg_scale = float(guidance.cfg)
        return settings
    return GenerationSettings.from_dict(raw)


def resolve_talker_voice_id(
    store: Any,
    *,
    requested: str | None,
    talker_id: str | None,
) -> str | None:
    raw = (requested or "").strip()
    if raw and raw.lower() not in LEFTOVER_VOICE_ALIASES:
        row = store.execute("SELECT id FROM voices WHERE id = ?", (raw,)).fetchone()
        if row is not None:
            return raw
    if isinstance(talker_id, str) and talker_id.strip():
        return talker_id.strip()
    return None


def _talker_request(
    state: Any,
    *,
    text: str,
    voice_id: str,
    steer: str | None = None,
) -> dict[str, Any]:
    runtime_state = state.runtime.status().state
    if runtime_state != "ready":
        if runtime_state in {"loading", "unloading"}:
            raise RuntimeBusy(runtime_state)
        raise RuntimeUnloaded(runtime_state)
    voice = get_voice_or_404(state.store, voice_id)
    source = state.store.get(voice["source_audio_artifact_id"])
    variant = voice.get("active_variant")
    if variant is not None and processed_variant_is_current(
        variant, source.sha256, voice["keep_intervals"]
    ):
        reference_path = state.store.get(variant["audio_artifact_id"]).path
    else:
        keep = [Interval.model_validate(item) for item in voice["keep_intervals"]]
        digest = sha256_json(canonical_keep_intervals(keep))[:16]
        dest = state.store.root / "tmp" / f"{voice_id}-talker-ref-{digest}.wav"
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.is_file():
            materialize_keep_wav(source.path, keep, dest)
        reference_path = dest
    reference_text = str(voice.get("effective_transcript") or voice.get("source_transcript") or "").strip()
    if not reference_text:
        raise RuntimeError("clone needs a transcript of the reference audio")
    settings = _settings_from_generation(voice.get("generation"))
    return {
        "text": text,
        "steer": (steer or "").strip() or DEFAULT_STEER,
        "voice_profile_id": voice_id,
        "reference_audio": str(reference_path),
        "reference_text": reference_text,
        "generation": settings.to_dict(),
    }


def iter_talker_pcm(
    state: Any,
    *,
    text: str,
    voice_id: str,
    steer: str | None = None,
) -> Iterator[bytes]:
    """s16le PCM chunks from the loaded Breeze worker. Fail closed if not ready."""
    yield from state.runtime.synthesize_stream(
        _talker_request(state, text=text, voice_id=voice_id, steer=steer)
    )


def synthesize_talker_pcm(
    state: Any,
    *,
    text: str,
    voice_id: str,
    steer: str | None = None,
) -> bytes:
    """PCM from the already-loaded Breeze worker. Fail closed if not ready."""
    pcm = b"".join(iter_talker_pcm(state, text=text, voice_id=voice_id, steer=steer))
    if pcm:
        return pcm
    payload = state.runtime.synthesize(
        _talker_request(state, text=text, voice_id=voice_id, steer=steer)
    )
    wav_path = Path(str(payload["wav_path"]))
    _sr, samples = read_wav(wav_path)
    return as_pcm16(samples).tobytes()
