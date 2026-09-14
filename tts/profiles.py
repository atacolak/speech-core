"""not on the voicecat path.

Voice identity is not delivery persona. Keep them separate objects.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from tts.edits import EditSpec, spec_from_region
from tts.hashes import sha256_file
from tts.packets import PronunciationOverride, new_id
from tts.paths import SELECTED_RUNTIME, TTS_ROOT, ensure_lab_dirs
from tts.preprocess import materialize_effective_wav, process_reference
from tts.wav import read_wav, write_wav


@dataclass
class AudioRegion:
    start_s: float | None = None
    end_s: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"start_s": self.start_s, "end_s": self.end_s}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "AudioRegion":
        data = data or {}
        return cls(start_s=data.get("start_s"), end_s=data.get("end_s"))


def _edits_from_profile_data(data: dict[str, Any]) -> dict[str, Any]:
    raw = data.get("edits")
    spec = EditSpec.from_dict(raw)
    if spec.ops:
        return spec.to_dict()
    region = AudioRegion.from_dict(data.get("region"))
    return spec_from_region(region.start_s, region.end_s).to_dict()


@dataclass
class VoiceProfile:
    display_name: str
    transcript: str
    original_audio: str
    id: str = field(default_factory=lambda: new_id("vp"))
    region: AudioRegion = field(default_factory=AudioRegion)
    edits: dict[str, Any] = field(default_factory=lambda: EditSpec().to_dict())
    source_transcript: str = ""
    derived: dict[str, str] = field(default_factory=dict)
    hashes: dict[str, str] = field(default_factory=dict)
    source_provenance: dict[str, Any] = field(default_factory=dict)
    preferred_variant: str = "original"
    active_variant: str = "original"
    default_breeze: dict[str, Any] = field(
        default_factory=lambda: {"implementation": SELECTED_RUNTIME}
    )
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["region"] = self.region.to_dict()
        payload["edits"] = EditSpec.from_dict(self.edits).to_dict()
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "VoiceProfile":
        transcript = str(data.get("transcript") or "")
        source_transcript = str(data.get("source_transcript") or transcript)
        variant = str(
            data.get("active_variant")
            or data.get("preferred_variant")
            or "original"
        )
        return cls(
            display_name=str(data.get("display_name") or data.get("id") or "voice"),
            transcript=transcript,
            original_audio=str(data["original_audio"]),
            id=str(data.get("id") or new_id("vp")),
            region=AudioRegion.from_dict(data.get("region")),
            edits=_edits_from_profile_data(data),
            source_transcript=source_transcript,
            derived=dict(data.get("derived") or {}),
            hashes=dict(data.get("hashes") or {}),
            source_provenance=dict(data.get("source_provenance") or {}),
            preferred_variant=str(data.get("preferred_variant") or variant),
            active_variant=variant,
            default_breeze=dict(data.get("default_breeze") or {"implementation": SELECTED_RUNTIME}),
            notes=str(data.get("notes") or ""),
        )


@dataclass
class DeliveryExemplar:
    text: str
    steer: str
    packet_id: str | None = None
    audio_id: str | None = None
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DeliveryExemplar":
        return cls(
            text=str(data.get("text") or ""),
            steer=str(data.get("steer") or ""),
            packet_id=data.get("packet_id"),
            audio_id=data.get("audio_id"),
            notes=str(data.get("notes") or ""),
        )


@dataclass
class DeliveryProfile:
    name: str
    base_description: str = ""
    priors: str = ""
    id: str = field(default_factory=lambda: new_id("dp"))
    exemplars: list[DeliveryExemplar] = field(default_factory=list)
    observations: list[str] = field(default_factory=list)
    default_vocal_events: list[Any] = field(default_factory=list)
    pronunciation_preferences: list[PronunciationOverride] = field(default_factory=list)
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "base_description": self.base_description,
            "priors": self.priors,
            "exemplars": [item.to_dict() for item in self.exemplars],
            "observations": list(self.observations),
            "default_vocal_events": list(self.default_vocal_events),
            "pronunciation_preferences": [
                item.to_dict() for item in self.pronunciation_preferences
            ],
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DeliveryProfile":
        return cls(
            name=str(data.get("name") or "persona"),
            base_description=str(data.get("base_description") or ""),
            priors=str(data.get("priors") or ""),
            id=str(data.get("id") or new_id("dp")),
            exemplars=[
                DeliveryExemplar.from_dict(item) for item in data.get("exemplars") or []
            ],
            observations=list(data.get("observations") or []),
            default_vocal_events=list(data.get("default_vocal_events") or []),
            pronunciation_preferences=[
                PronunciationOverride.from_dict(item)
                for item in data.get("pronunciation_preferences") or []
            ],
            notes=str(data.get("notes") or ""),
        )


class ProfileStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = ensure_lab_dirs(root)
        self.voices_dir = self.root / "voices"
        self.delivery_dir = self.root / "delivery_profiles"

    def _write(self, path: Path, payload: dict[str, Any]) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return path

    def save_voice(self, profile: VoiceProfile) -> Path:
        return self._write(self.voices_dir / f"{profile.id}.json", profile.to_dict())

    def load_voice(self, voice_id: str) -> VoiceProfile:
        path = self.voices_dir / f"{voice_id}.json"
        return VoiceProfile.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def list_voices(self) -> list[VoiceProfile]:
        items = []
        for path in sorted(self.voices_dir.glob("vp_*.json")):
            items.append(VoiceProfile.from_dict(json.loads(path.read_text(encoding="utf-8"))))
        return items

    def save_delivery(self, profile: DeliveryProfile) -> Path:
        return self._write(self.delivery_dir / f"{profile.id}.json", profile.to_dict())

    def load_delivery(self, delivery_id: str) -> DeliveryProfile:
        path = self.delivery_dir / f"{delivery_id}.json"
        return DeliveryProfile.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def list_delivery(self) -> list[DeliveryProfile]:
        items = []
        for path in sorted(self.delivery_dir.glob("dp_*.json")):
            items.append(
                DeliveryProfile.from_dict(json.loads(path.read_text(encoding="utf-8")))
            )
        return items

    def add_exemplar(self, delivery_id: str, exemplar: DeliveryExemplar) -> DeliveryProfile:
        profile = self.load_delivery(delivery_id)
        profile.exemplars.append(exemplar)
        self.save_delivery(profile)
        return profile

    def materialize_reference(self, profile: VoiceProfile) -> Path:
        """Return the wav Breeze should clone from (edits, then optional stream.fm)."""
        src = Path(profile.original_audio)
        if not src.is_file():
            raise FileNotFoundError(f"voice reference missing: {src}")
        spec = EditSpec.from_dict(profile.edits)
        if not spec.ops:
            spec = spec_from_region(profile.region.start_s, profile.region.end_s)
        effective = materialize_effective_wav(src, spec, lab_root=self.root)
        variant = profile.active_variant or profile.preferred_variant
        chosen = Path(effective)
        if str(variant).lower().startswith("stream"):
            record = process_reference(src, lab_root=self.root, edit_spec=spec.to_dict())
            if record.get("status") in {"ok", "cache_hit"} and record.get("output"):
                chosen = Path(str(record["output"]))
        dest = self.root / "voices" / f"{profile.id}_active.wav"
        sr, samples = read_wav(chosen)
        write_wav(dest, sr, samples)
        return dest


def import_voice_from_audio(
    store: ProfileStore,
    *,
    audio_path: Path | str,
    transcript: str,
    display_name: str,
    region: AudioRegion | None = None,
    edits: dict[str, Any] | None = None,
    source_transcript: str | None = None,
    active_variant: str = "original",
    source_provenance: dict[str, Any] | None = None,
) -> VoiceProfile:
    src = Path(audio_path)
    sr, samples = read_wav(src)
    dest_dir = store.root / "references"
    dest_dir.mkdir(parents=True, exist_ok=True)
    voice_id = new_id("vp")
    original = dest_dir / f"{voice_id}_original.wav"
    write_wav(original, sr, samples)
    region = region or AudioRegion()
    spec = EditSpec.from_dict(edits)
    if not spec.ops:
        spec = spec_from_region(region.start_s, region.end_s)
    text = transcript.strip()
    profile = VoiceProfile(
        id=voice_id,
        display_name=display_name,
        transcript=text,
        original_audio=str(original),
        region=region,
        edits=spec.to_dict(),
        source_transcript=(source_transcript or text).strip(),
        hashes={"sha256": sha256_file(original), "sample_rate": str(sr)},
        preferred_variant=active_variant,
        active_variant=active_variant,
        source_provenance=dict(source_provenance or {"imported_from": str(src)}),
    )
    store.save_voice(profile)
    return profile


def seed_default_delivery(store: ProfileStore) -> DeliveryProfile:
    existing = store.list_delivery()
    if existing:
        return existing[0]
    profile = DeliveryProfile(
        name="lab-default",
        base_description=(
            "Technically confident, dry, faintly amused. Utterance-specific "
            "natural-language steers over generic emotion enums."
        ),
        priors="Do not flatten delivery to a single affect label.",
    )
    fixtures_path = TTS_ROOT / "experiments" / "fixtures" / "steers.json"
    if fixtures_path.is_file():
        for item in json.loads(fixtures_path.read_text(encoding="utf-8")):
            profile.exemplars.append(
                DeliveryExemplar(
                    text=str(item.get("text") or ""),
                    steer=str(item.get("steer") or ""),
                    notes=str(item.get("id") or "fixture"),
                )
            )
    store.save_delivery(profile)
    return profile
