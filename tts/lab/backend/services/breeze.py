"""E2 synthesis service. not on the voicecat path.

Adapter around the already-selected Breeze E2 runtime. The playground
must pass the warm engine so this module never loads a second 3B.
"""

from __future__ import annotations

import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from tts.generation import GenerationSettings
from tts.packets import PronunciationOverride, UtterancePacket
from tts.wav import duration_s, is_riff_wav, write_wav


@dataclass
class SynthesisRequest:
    text: str
    steer: str
    reference_audio: Path | str
    reference_text: str
    synthesis_text: str | None = None
    voice_profile_id: str | None = None
    delivery_profile_id: str | None = None
    pronunciation_overrides: list[PronunciationOverride] = field(default_factory=list)
    generation: GenerationSettings | None = None
    output_path: Path | str | None = None


@dataclass
class SynthesisResult:
    wav_path: Path
    sample_rate: int
    duration_s: float
    samples: np.ndarray
    pcm_s16le: bytes
    wall_s: float
    first_audio_s: float | None
    runtime: dict[str, Any]
    request_snapshot: dict[str, Any]
    chunks: list[Any] = field(default_factory=list)


def _reject_mixed_guidance(settings: GenerationSettings) -> None:
    ref = settings.cfg_scale_ref
    ins = settings.cfg_scale_ins
    if (ref is None) ^ (ins is None):
        raise ValueError(
            "dual guidance requires both cfg_scale_ref and cfg_scale_ins; "
            "mixed single/dual state is invalid"
        )


def synthesize_e2(
    request: SynthesisRequest,
    *,
    engine: Any | None = None,
) -> SynthesisResult:
    """Run selected E2 synthesis and write a real RIFF/WAVE file.

    Pass ``engine`` when a BreezeEngine is already loaded. Omitting it
    loads E2 for this call only and closes it afterwards — that path is
    for isolated tests, not the playground process.
    """
    from tts.breeze.runtime import load_selected_engine, runtime_record, synthesize

    settings = request.generation or GenerationSettings()
    _reject_mixed_guidance(settings)
    packet = UtterancePacket(
        text=request.text,
        steer=request.steer,
        synthesis_text=request.synthesis_text,
        pronunciation_overrides=list(request.pronunciation_overrides or []),
        voice_profile_id=request.voice_profile_id,
        delivery_profile_id=request.delivery_profile_id,
    )
    owns_engine = engine is None
    if owns_engine:
        engine = load_selected_engine()
    t0 = time.perf_counter()
    try:
        chunks = list(
            synthesize(
                engine,
                packet=packet,
                reference_audio=request.reference_audio,
                reference_text=request.reference_text,
                settings=settings,
            )
        )
    finally:
        if owns_engine:
            closer = getattr(engine, "close", None)
            if closer is not None:
                closer()
    wall_s = time.perf_counter() - t0
    if not chunks:
        raise RuntimeError("E2 synthesis produced no audio")
    sample_rate = int(chunks[0].sample_rate or 24000)
    pcm = b"".join(chunk.pcm for chunk in chunks)
    if not pcm:
        raise RuntimeError("E2 synthesis produced empty pcm")
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32767.0
    dest = (
        Path(request.output_path)
        if request.output_path is not None
        else Path(tempfile.mkdtemp(prefix="tts-lab-")) / "take.wav"
    )
    write_wav(dest, sample_rate, samples)
    if not is_riff_wav(dest):
        raise RuntimeError(f"E2 synthesis wrote non-RIFF output: {dest}")
    header = dest.read_bytes()[:12]
    if header[:4] != b"RIFF" or header[8:12] != b"WAVE":
        raise RuntimeError(f"E2 synthesis wrote invalid WAVE header: {dest}")
    first_audio_s = None
    for chunk in chunks:
        if int(chunk.n_samples) > 0:
            first_audio_s = float(chunk.t_rel_s)
            timing = dict(chunk.timing or {})
            if timing.get("first_pcm") is not None:
                first_audio_s = float(timing["first_pcm"])
            break
    return SynthesisResult(
        wav_path=dest,
        sample_rate=sample_rate,
        duration_s=duration_s(sample_rate, samples),
        samples=samples,
        pcm_s16le=pcm,
        wall_s=wall_s,
        first_audio_s=first_audio_s,
        runtime=runtime_record(settings),
        request_snapshot=packet.to_dict(),
        chunks=chunks,
    )
