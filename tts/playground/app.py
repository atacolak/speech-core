#!/usr/bin/env python3
"""not on the voicecat path.

Lab TTS playground over selected Breeze E2. not a pin swap.
Ordinary synthesis is short. Research controls live behind tabs.

Gradio Blocks UI is restored from frozen 3.10 bytecode (`_ui.cpython-310.pyc`)
after a truncated source write. PlaygroundSession.generate is defined below so
synthesis goes through `tts.lab.backend.services.breeze` without a second 3B.
"""

from __future__ import annotations

import marshal
from pathlib import Path
from typing import Any

_UI_PYC = Path(__file__).with_name("_ui.cpython-310.pyc")
_CODE = marshal.loads(_UI_PYC.read_bytes()[16:])
_SAVED_NAME = globals().get("__name__", "tts.playground.app")
globals()["__name__"] = "tts.playground._frozen_ui"
exec(_CODE, globals())
globals()["__name__"] = _SAVED_NAME

from tts.edits import EditSpec, spec_from_region
from tts.generation import GenerationSettings
from tts.lab.backend.services.breeze import SynthesisRequest, synthesize_e2
from tts.packets import PronunciationOverride, UtterancePacket, new_id
from tts.paths import SELECTED_RUNTIME
from tts.preprocess import process_reference
from tts.wav import crop_bounds


def generate(
    self,
    *,
    text: str,
    instruction: str,
    cfg_scale: float,
    seed: int | float,
    clone_audio: Any,
    clone_text: str,
    start_s: float | None = None,
    end_s: float | None = None,
    voice_profile_id: str | None = None,
    delivery_profile_id: str | None = None,
    settings: GenerationSettings | None = None,
    synthesis_text: str | None = None,
    pronunciation_overrides: list[PronunciationOverride] | None = None,
    edits: dict[str, Any] | None = None,
    reference_variant: str | None = None,
) -> tuple[tuple[int, Any], str, dict[str, Any]]:
    with self._lock:
        if self.engine is None:
            raise RuntimeError("engine is not loaded")
        spec = EditSpec.from_dict(edits)
        if not spec.ops:
            spec = spec_from_region(*crop_bounds(start_s, end_s))
        ref_text = (clone_text or "").strip()
        ref_audio = _audio_path(clone_audio)
        variant = reference_variant or "original"
        voice_name = ""
        if voice_profile_id:
            profile = self.profiles.load_voice(voice_profile_id)
            ref_audio = ref_audio or str(profile.original_audio)
            ref_text = ref_text or profile.transcript
            voice_name = profile.display_name
            if not EditSpec.from_dict(edits).ops:
                spec = EditSpec.from_dict(profile.edits) or spec
            if reference_variant is None:
                variant = profile.active_variant or profile.preferred_variant
        if not ref_audio or not ref_text:
            raise ValueError("clone audio and transcript are required together")
        from tts.preprocess import materialize_effective_wav as _eff

        effective = _eff(ref_audio, spec, lab_root=self.lab)
        used = str(effective)
        used_variant = "original"
        streamfm_record = None
        if str(variant).lower().startswith("stream"):
            streamfm_record = process_reference(
                ref_audio, lab_root=self.lab, edit_spec=spec.to_dict()
            )
            if streamfm_record.get("status") in {"ok", "cache_hit"}:
                used = str(streamfm_record["output"])
                used_variant = "stream.fm"
        packet = UtterancePacket(
            text=text,
            synthesis_text=synthesis_text,
            steer=instruction,
            pronunciation_overrides=list(pronunciation_overrides or []),
            voice_profile_id=voice_profile_id or None,
            delivery_profile_id=delivery_profile_id or None,
        )
        packet = self.lexicon.apply(packet)
        settings = settings or GenerationSettings(
            cfg_scale=float(cfg_scale), seed=int(seed)
        )
        settings.seed = int(seed)
        settings.cfg_scale = float(cfg_scale)
        result = synthesize_e2(
            SynthesisRequest(
                text=packet.text,
                steer=packet.steer,
                synthesis_text=packet.synthesis_text,
                pronunciation_overrides=list(packet.pronunciation_overrides),
                voice_profile_id=packet.voice_profile_id,
                delivery_profile_id=packet.delivery_profile_id,
                reference_audio=used,
                reference_text=ref_text,
                generation=settings,
            ),
            engine=self.engine,
        )
        chunks = result.chunks
        wall_s = result.wall_s
        sample_rate = result.sample_rate
        first_pcm_s = result.first_audio_s
        first_nonsilent, _ = first_nonsilent_s(
            [(float(chunk.t_rel_s), chunk.pcm) for chunk in chunks],
            sample_rate=sample_rate,
        )
        audio = result.samples
        wav_path = str(result.wav_path)
        duration = result.duration_s
        run = {
            "run_id": new_id("run"),
            "packet": packet.to_dict(),
            "voice_profile_id": packet.voice_profile_id,
            "voice_name": voice_name,
            "delivery_profile_id": packet.delivery_profile_id,
            "reference": {
                "path": used,
                "source": ref_audio,
                "transcript": ref_text,
                "edits": spec.to_dict(),
                "variant": used_variant,
                "streamfm": streamfm_record,
            },
            "runtime": result.runtime,
            "output_audio": {"path": wav_path, "kind": "generation_cache"},
            "generation_latency_s": wall_s,
            "first_audio_latency_s": first_pcm_s,
            "duration_s": duration,
            "rtf": (wall_s / duration) if duration else None,
            "peak_vram": _peak_vram_status(),
            "seed": int(seed),
            "cfg_scale": float(cfg_scale),
        }
        run_path = self.artifacts.write_run(run)
        self.last_run = run
        dual_note = ""
        if settings.cfg_mode == "dual_experimental":
            dual_note = " cfg=dual_experimental/eager "
        status = (
            f"leftover parked; loaded {SELECTED_RUNTIME}; "
            f"first_pcm={_fmt_s(first_pcm_s)} "
            f"first_nonsilent={_fmt_s(first_nonsilent)} "
            f"wall={_fmt_s(wall_s)} "
            f"{dual_note}"
            f"peak_vram={_peak_vram_status()} "
            f"gpu={_gpu_occupant()} "
            f"reference={used_variant} "
            f"run={run_path.name}"
        )
        return (sample_rate, audio), status, run


PlaygroundSession.generate = generate

if _SAVED_NAME == "__main__":
    raise SystemExit(main())
