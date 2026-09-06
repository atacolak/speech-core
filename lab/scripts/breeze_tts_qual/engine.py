"""BreezeEngine facade. not on the voicecat path."""

from __future__ import annotations

import struct
import os
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .configs import EngineConfig


@dataclass(frozen=True)
class PcmChunk:
    pcm: bytes
    sample_rate: int
    n_samples: int
    is_final: bool
    t_rel_s: float
    timing: dict = field(default_factory=dict)


class FakeBackend:
    sample_rate = 24000

    def synthesize(self, **kwargs: Any) -> Iterator[PcmChunk]:
        silent = struct.pack("<" + "h" * 200, *([0] * 200))
        audible = struct.pack("<" + "h" * 200, *([1000] * 200))
        yield PcmChunk(silent, 24000, 200, False, 0.050, {})
        yield PcmChunk(audible, 24000, 200, True, 0.120, {})

    def close(self) -> None:
        return None



_DEFAULT_QUAL_ROOT = Path.home() / ".cache" / "speech-out" / "breeze-tts-qual-sc-breeze-hybrid-81p"
_INT8_PRECISIONS = ("hybrid_int8", "full_int8")


def fast_streaming_kwargs(config: EngineConfig) -> dict[str, Any]:
    return {
        "fast_depth_decoder": config.fast_depth_decoder,
        "fast_codec": config.fast_codec,
        "fast_backbone_decode": config.fast_backbone_decode,
        "fast_backbone_prefill": config.fast_backbone_prefill,
        "fast_all": None,
        "fast_text_encoder": config.fast_text_encoder,
        "collect_timing": True,
    }


def _official_has_shards(official: Path) -> bool:
    if (official / "model.safetensors.index.json").is_file():
        return True
    return any(official.glob("model-*-of-*.safetensors"))


def checkpoint_for_precision(qual_root: Path | str, precision: str) -> Path:
    root = Path(qual_root)
    deriv = root / "models" / "comfyui-deriv"
    official = root / "models" / "official"
    if precision == "hybrid_int8":
        return deriv / "Breeze-TTS-2-int8-hybrid.safetensors"
    if precision == "full_int8":
        return deriv / "Breeze-TTS-2-int8-convrot.safetensors"
    if precision == "bf16":
        if _official_has_shards(official):
            return official
        return deriv / "Breeze-TTS-2-bf16.safetensors"
    raise ValueError(f"unknown precision: {precision!r}")


def _default_qual_root() -> Path:
    return Path(os.environ.get("QUAL_ROOT", str(_DEFAULT_QUAL_ROOT)))


def _float_audio_to_s16le(audio: Any) -> bytes:
    import numpy as np

    samples = np.asarray(audio, dtype=np.float32).reshape(-1)
    clipped = np.clip(samples, -1.0, 1.0)
    return (clipped * 32767.0).astype("<i2").tobytes()


def attach_clone_reference(request: dict[str, Any], qual_root: Path | str) -> dict[str, Any]:
    root = Path(qual_root)
    out = dict(request)
    out["ref_audio_path"] = str(root / "fixtures" / "ref.wav")
    out["ref_text"] = (root / "fixtures" / "ref.txt").read_text(encoding="utf-8").strip()
    return out


def _any_fast(config: EngineConfig) -> bool:
    return any(
        (
            config.fast_depth_decoder,
            config.fast_codec,
            config.fast_backbone_decode,
            config.fast_backbone_prefill,
            config.fast_text_encoder,
        )
    )


class OfficialBackend:
    sample_rate = 24000

    def __init__(
        self,
        config: EngineConfig,
        *,
        device: str = "cuda:0",
        qual_root: Path | str | None = None,
        ckpt_dir: Path | str | None = None,
    ) -> None:
        self.config = config
        self.device = device
        self.qual_root = Path(qual_root) if qual_root is not None else _default_qual_root()
        breeze_src = self.qual_root / "src" / "breeze-tts"
        src = str(breeze_src)
        if src not in sys.path:
            sys.path.insert(0, src)

        ckpt = Path(ckpt_dir) if ckpt_dir is not None else checkpoint_for_precision(
            self.qual_root, config.precision
        )
        load_dir = self._runtime_load_dir(ckpt)

        from breeze_infer.runtime import load_runtime, update_generation_config_for_breeze
        from models.fast_streaming import FastBreezeStreamingRuntime, FastStreamingConfig

        tokenizer, model, audio_tokenizer = load_runtime(
            load_dir,
            device=device,
            attn_implementation="eager",
        )
        if config.precision in _INT8_PRECISIONS:
            weight_file = ckpt if ckpt.is_file() else checkpoint_for_precision(
                self.qual_root, config.precision
            )
            self._apply_int8(model, weight_file)
        elif ckpt.is_file() and load_dir != ckpt:
            self._load_state_dict(model, ckpt)
        update_generation_config_for_breeze(model)

        stream_config = FastStreamingConfig(**fast_streaming_kwargs(config))
        runtime = FastBreezeStreamingRuntime(
            model, audio_tokenizer, stream_config, tokenizer=tokenizer
        )
        self._maybe_warmup(runtime, breeze_src)
        self.tokenizer = tokenizer
        self.model = model
        self.audio_tokenizer = audio_tokenizer
        self.runtime = runtime
        self.sample_rate = int(getattr(runtime, "sample_rate", 24000))

    def _runtime_load_dir(self, ckpt: Path) -> Path:
        official = self.qual_root / "models" / "official"
        if ckpt.is_dir():
            return ckpt
        if _official_has_shards(official):
            return official
        return ckpt.parent

    def _apply_int8(self, model: Any, weight_file: Path) -> None:
        from .int8_convrot import replace_quantized_linears, scan_checkpoint_quantization

        quant_map = scan_checkpoint_quantization(weight_file)
        replace_quantized_linears(model, quant_map)
        self._load_state_dict(model, weight_file)

    def _load_state_dict(self, model: Any, weight_file: Path) -> None:
        from safetensors.torch import load_file

        state = load_file(str(weight_file))
        model.load_state_dict(state, strict=False)

    def _maybe_warmup(self, runtime: Any, breeze_src: Path) -> None:
        if not _any_fast(self.config):
            return
        if self.config.name.startswith("E"):
            from models.warmup_profile import parse_warmup_profile

            import breeze_infer.templates as templates

            from .protocol import voicecat_warmup_profile_dict

            profile = parse_warmup_profile(
                voicecat_warmup_profile_dict(fast_codec=self.config.fast_codec),
                source="voicecat",
            )
            orig_prepare = templates.prepare_inputs

            def _prepare_with_clone(*args: Any, **kwargs: Any):
                seq = list(args)
                if len(seq) >= 4 and isinstance(seq[3], list):
                    seq[3] = [
                        attach_clone_reference(req, self.qual_root) for req in seq[3]
                    ]
                elif isinstance(kwargs.get("requests"), list):
                    kwargs["requests"] = [
                        attach_clone_reference(req, self.qual_root)
                        for req in kwargs["requests"]
                    ]
                return orig_prepare(*seq, **kwargs)

            templates.prepare_inputs = _prepare_with_clone
            try:
                runtime.warmup_from_profile(profile)
            finally:
                templates.prepare_inputs = orig_prepare
            return
        from dataclasses import replace

        from models.warmup_profile import load_warmup_profile

        profile = load_warmup_profile(breeze_src / "configs" / "fast.json")
        profile = replace(profile, codec_chunk_frames=runtime.codec_chunk_frames)
        runtime.warmup_from_profile(profile)



    def synthesize(self, **kwargs: Any) -> Iterator[PcmChunk]:
        from breeze_infer.runtime import set_all_seeds
        from breeze_infer.templates import get_template, prepare_inputs

        text = kwargs["text"]
        reference_audio = Path(kwargs["reference_audio"])
        reference_text = kwargs["reference_text"]
        instruction = kwargs.get("instruction", "Speak clearly and naturally.")
        seed = int(kwargs.get("seed", 42))

        request = {
            "id": "qual-request",
            "text": text,
            "instruction": instruction,
            "speaker": "S0",
            "ref_audio_path": str(reference_audio),
            "ref_text": reference_text,
        }
        set_all_seeds(seed)
        t0 = time.perf_counter()
        inputs = prepare_inputs(
            self.tokenizer,
            self.audio_tokenizer,
            self.model,
            [request],
            get_template("ref_edit_tata"),
            guidance_scale=1.0,
            guidance_scale_ref=None,
            guidance_scale_ins=None,
        )
        chunks = self.runtime.iter_audio_chunks(
            inputs, request_id="qual-request", seed=seed
        )
        for chunk in chunks:
            pcm = _float_audio_to_s16le(chunk.audio)
            n_samples = len(pcm) // 2
            yield PcmChunk(
                pcm=pcm,
                sample_rate=int(chunk.sample_rate),
                n_samples=n_samples,
                is_final=bool(chunk.is_final),
                t_rel_s=time.perf_counter() - t0,
                timing=dict(chunk.timing or {}),
            )

    def close(self) -> None:
        runtime = getattr(self, "runtime", None)
        if runtime is not None:
            closer = getattr(runtime, "close", None)
            if closer is not None:
                closer()
        self.runtime = None
        self.model = None
        self.audio_tokenizer = None
        self.tokenizer = None


class BreezeEngine:
    def __init__(
        self,
        config: EngineConfig,
        *,
        ckpt_dir: Path | str,
        device: str = "cuda:0",
        backend: Any | None = None,
    ) -> None:
        self.config = config
        self.ckpt_dir = Path(ckpt_dir)
        self.device = device
        self.backend = backend or FakeBackend()

    def synthesize(
        self,
        *,
        text: str,
        reference_audio: Path | str,
        reference_text: str,
        instruction: str = "Speak clearly and naturally.",
        seed: int = 42,
    ) -> Iterator[PcmChunk]:
        yield from self.backend.synthesize(
            text=text,
            reference_audio=reference_audio,
            reference_text=reference_text,
            instruction=instruction,
            seed=seed,
        )

    def close(self) -> None:
        close = getattr(self.backend, "close", None)
        if close is not None:
            close()
