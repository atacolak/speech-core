"""not on the voicecat path.

Utterance packet: WHAT is said vs HOW it is rendered.
Steer is authoritative for Breeze. Structured delivery is optional metadata.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from typing import Any


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@dataclass
class DeliveryAnnotations:
    intent: str | None = None
    affect: str | None = None
    energy: str | None = None
    pace: str | None = None
    articulation: str | None = None
    phonation: str | None = None
    emphasis: list[Any] = field(default_factory=list)
    pauses: list[Any] = field(default_factory=list)
    trajectory: list[Any] = field(default_factory=list)
    vocal_events: list[Any] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "DeliveryAnnotations":
        if not data:
            return cls()
        known = {k: data.get(k) for k in cls.__dataclass_fields__}
        for key in ("emphasis", "pauses", "trajectory", "vocal_events"):
            if known.get(key) is None:
                known[key] = []
        return cls(**known)


@dataclass
class PronunciationOverride:
    span: str
    context: str | None = None
    intended_reading: str | None = None
    ipa: str | None = None
    backend_hint: str | None = None
    source: str = "operator"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PronunciationOverride":
        return cls(
            span=str(data["span"]),
            context=data.get("context") or data.get("occurrence/context"),
            intended_reading=data.get("intended_reading"),
            ipa=data.get("ipa"),
            backend_hint=data.get("backend_hint"),
            source=str(data.get("source") or "operator"),
        )


@dataclass
class UtterancePacket:
    text: str
    steer: str
    synthesis_text: str | None = None
    delivery: DeliveryAnnotations = field(default_factory=DeliveryAnnotations)
    pronunciation_overrides: list[PronunciationOverride] = field(default_factory=list)
    voice_profile_id: str | None = None
    delivery_profile_id: str | None = None
    packet_id: str = field(default_factory=lambda: new_id("up"))
    provenance: dict[str, Any] = field(default_factory=dict)

    def display_text(self) -> str:
        return self.text

    def engine_text(self) -> str:
        return (self.synthesis_text or self.text).strip()

    def to_dict(self) -> dict[str, Any]:
        return {
            "packet_id": self.packet_id,
            "text": self.text,
            "synthesis_text": self.synthesis_text,
            "steer": self.steer,
            "delivery": self.delivery.to_dict(),
            "pronunciation_overrides": [item.to_dict() for item in self.pronunciation_overrides],
            "voice_profile_id": self.voice_profile_id,
            "delivery_profile_id": self.delivery_profile_id,
            "provenance": dict(self.provenance),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "UtterancePacket":
        overrides = [
            PronunciationOverride.from_dict(item)
            for item in data.get("pronunciation_overrides") or []
        ]
        return cls(
            text=str(data.get("text") or ""),
            steer=str(data.get("steer") or ""),
            synthesis_text=data.get("synthesis_text"),
            delivery=DeliveryAnnotations.from_dict(data.get("delivery")),
            pronunciation_overrides=overrides,
            voice_profile_id=data.get("voice_profile_id"),
            delivery_profile_id=data.get("delivery_profile_id"),
            packet_id=str(data.get("packet_id") or new_id("up")),
            provenance=dict(data.get("provenance") or {}),
        )
