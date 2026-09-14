"""Lab domain models. not on the voicecat path.

Canonical keep intervals live on the original source timeline.
Guidance is a discriminated union: single XOR dual. Mixed cfg is invalid.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

from tts.generation import UPSTREAM_DEFAULTS


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Interval(_Strict):
    start_s: float
    end_s: float

    @model_validator(mode="after")
    def _ordered_non_negative(self) -> "Interval":
        if self.start_s < 0 or self.end_s < 0:
            raise ValueError("negative coordinate")
        if self.end_s <= self.start_s:
            raise ValueError("zero-length interval")
        return self


class SingleGuidance(_Strict):
    mode: Literal["single"] = "single"
    cfg: float


class DualGuidance(_Strict):
    mode: Literal["dual"] = "dual"
    reference: float
    instruction: float


Guidance = Annotated[SingleGuidance | DualGuidance, Field(discriminator="mode")]
_GUIDANCE_ADAPTER: TypeAdapter[SingleGuidance | DualGuidance] = TypeAdapter(Guidance)


def parse_guidance(data: dict[str, Any] | SingleGuidance | DualGuidance) -> SingleGuidance | DualGuidance:
    if isinstance(data, (SingleGuidance, DualGuidance)):
        return data
    return _GUIDANCE_ADAPTER.validate_python(data)


class GenerationConfig(_Strict):
    guidance: Guidance
    seed: int = 42
    temperature: float = float(UPSTREAM_DEFAULTS["temperature"])
    depth_temperature: float = float(UPSTREAM_DEFAULTS["depth_temperature"])
    do_sample: bool = bool(UPSTREAM_DEFAULTS["do_sample"])
    top_k: int = int(UPSTREAM_DEFAULTS["top_k"])
    top_p: float = float(UPSTREAM_DEFAULTS["top_p"])
    max_new_tokens: int = int(UPSTREAM_DEFAULTS["max_new_tokens"])

    @field_validator("guidance", mode="before")
    @classmethod
    def _guidance(cls, value: Any) -> Any:
        if isinstance(value, (SingleGuidance, DualGuidance)):
            return value
        if isinstance(value, dict):
            return parse_guidance(value)
        return value


class PronunciationOverride(_Strict):
    id: str
    start_char: int
    end_char: int
    source_text: str
    intended_reading: str
    ipa: str | None = None
    backend_hint: str | None = None
    source: Literal["operator", "llm", "lexicon"] = "operator"

    @model_validator(mode="after")
    def _span(self) -> "PronunciationOverride":
        if self.start_char < 0 or self.end_char < 0:
            raise ValueError("negative character offset")
        if self.end_char <= self.start_char:
            raise ValueError("empty pronunciation span")
        return self


class VoiceProfile(_Strict):
    id: str
    name: str
    tags: list[str] = Field(default_factory=list)
    source_audio_artifact_id: str
    source_transcript: str
    keep_intervals: list[Interval]
    effective_transcript: str
    active_reference_variant_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ReferenceVariant(_Strict):
    id: str
    voice_profile_id: str
    kind: Literal["original", "resemble", "auk", "other"]
    audio_artifact_id: str
    processor_config: dict[str, Any] | None = None
    processor_cache_key: str | None = None
    duration_s: float
    pinned: bool = False
    # AuK candidate lineage. Null on original/resemble rows: they are not
    # generated from a parent candidate.
    parent_variant_id: str | None = None
    auk_task: str | None = None
    instruction: str | None = None
    model_variant: str | None = None
    auk_precision: str | None = None  # bf16 (preferred pin) | int8 (explicit fallback)
    encoder_precision: str | None = None  # w4a8
    seed: int | None = None
    settings: dict[str, Any] | None = None  # nfe / cfg / sway when non-default
    approved: bool = False
    # Read-side only: source bytes or keep no longer match the provenance above.
    stale: bool = False


# A media source is a file the operator owns or a youtube video ingested for it.
MediaSourceKind = Literal["file", "youtube"]


class SynthesisRequest(_Strict):
    text: str
    steer: str
    voice_profile_id: str
    reference_variant_id: str | None = None
    generation: GenerationConfig
    pronunciation_overrides: list[PronunciationOverride] = Field(default_factory=list)
    delivery_profile_id: str | None = None


class SynthesisRun(_Strict):
    id: str
    request_snapshot: SynthesisRequest
    output_artifact_id: str
    effective_reference_snapshot: dict[str, Any]
    latency_ms: float
    first_audio_ms: float | None = None
    duration_s: float
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    rating: str | None = None
    tags: list[str] = Field(default_factory=list)


class DeliveryProfile(_Strict):
    id: str
    name: str
    description: str = ""
    exemplar_ids: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class DeliveryExemplar(_Strict):
    id: str
    text: str
    steer: str
    run_id: str | None = None
    structured_delivery: dict[str, Any] | None = None
    tags: list[str] = Field(default_factory=list)
