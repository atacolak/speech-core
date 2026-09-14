"""AuK sampler: instruction + reference audio in, 24 kHz mono candidates out.

This is the port the lab was missing: the three pin components are built into
real graphs inside the AuK venv, and one call to :meth:`AuKEngine.generate` runs
the published Base recipe — 32 Euler steps, guidance 2, sway -1 — through
BigVGAN decode. The behavioural oracle is the official ``AukInfer`` release as
wrapped by the MIT-licensed ComfyUI-AuK node pack; nothing here imports
``comfy`` and nothing here fabricates audio: an unbuildable engine raises
:class:`EngineUnavailable` so the lab still answers 501 instead of a silent wav.

Weights come from the :class:`~tts.auk.weights.ComponentStore` the worker already
filled, one component at a time, and each is owned by exactly one graph — the
store hands the tensors over rather than duplicating them on the card.
"""

from __future__ import annotations

import gc
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import torch
import torchaudio
import yaml

from tts.auk import ops
from tts.auk.backbone import AuKModel
from tts.auk.encoder import AuKEncoder
from tts.auk.pin import DEFAULT_SETTINGS, AukPin
from tts.auk.vae import BigVGANFlowVAE, fold_weight_norm
from tts.wav import as_mono_float, read_wav

SAMPLE_RATE = 24000
FRAMES_PER_SECOND = 50
LATENT_DIM = 64
MIN_REFERENCE_SAMPLES = 480


class EngineUnavailable(RuntimeError):
    """The port cannot run in this process at all (missing kernels, weights, config)."""

    code = "engine_unavailable"


class InvalidSettings(ValueError):
    """A generate asked for sampling values the published recipe never uses."""

    code = "invalid_settings"


class NonFiniteAudio(RuntimeError):
    """The sampler produced NaN/inf; the lab must not write that to a candidate."""

    code = "non_finite_audio"


@dataclass(frozen=True)
class SampleSettings:
    nfe: int
    cfg: float
    sway: float


@dataclass(frozen=True)
class AuKSample:
    samples: np.ndarray
    sample_rate: int
    duration_s: float
    wall_s: float
    vram_peak_bytes: int | None


def sample_settings(raw: Mapping[str, Any] | None) -> SampleSettings:
    """Merge a request over the pinned defaults and reject what cannot run."""
    merged = {**DEFAULT_SETTINGS, **(raw or {})}
    try:
        nfe, cfg, sway = merged["nfe"], merged["cfg"], merged["sway"]
    except KeyError as error:
        raise InvalidSettings(f"missing sampling setting: {error}") from error
    if isinstance(nfe, bool) or not isinstance(nfe, (int, float)) or int(nfe) != nfe:
        raise InvalidSettings(f"nfe must be a whole number, got {nfe!r}")
    if isinstance(cfg, bool) or not isinstance(cfg, (int, float)):
        raise InvalidSettings(f"cfg must be a number, got {cfg!r}")
    if isinstance(sway, bool) or not isinstance(sway, (int, float)):
        raise InvalidSettings(f"sway must be a number, got {sway!r}")
    if int(nfe) < 1:
        raise InvalidSettings(f"nfe must be at least 1, got {int(nfe)}")
    if float(cfg) < 0.0:
        raise InvalidSettings(f"cfg must not be negative, got {float(cfg)}")
    if not -1.0 <= float(sway) <= 0.0:
        raise InvalidSettings(f"sway must be within [-1, 0], got {float(sway)}")
    return SampleSettings(nfe=int(nfe), cfg=float(cfg), sway=float(sway))


def time_grid(settings: SampleSettings) -> list[float]:
    """Base schedule: uniform steps reshaped by sway sampling."""
    return [
        t + settings.sway * (math.cos(math.pi * t / 2) - 1 + t)
        for t in (index / settings.nfe for index in range(settings.nfe + 1))
    ]


def vram_peak_bytes(device: torch.device) -> int | None:
    if device.type != "cuda":
        return None
    return int(torch.cuda.max_memory_allocated(device))


class AuKEngine:
    """Owns the built graphs for one resolved pin and renders candidates."""

    def __init__(self, store: Any, *, device: str | torch.device = "cuda") -> None:
        self.store = store
        self.pin: AukPin = store.pin
        self.device = torch.device(device)
        self.compute = ops.compute_dtype(self.pin.precision, self.device)
        self._parts: dict[str, Any] = {}

    # ---- residency -----------------------------------------------------

    def drop(self, component: str) -> None:
        """Release a built graph so the store can hand the component out again."""
        self._parts.pop(component, None)
        gc.collect()
        _empty_cache(self.device)

    def _component(self, name: str) -> Any:
        part = self._parts.get(name)
        if part is not None:
            return part
        if name not in ("encoder", "model", "vae"):
            raise EngineUnavailable(f"unknown AuK component: {name}")
        state = self.store.take(name)
        try:
            if name == "encoder":
                part = AuKEncoder.build(self.pin.assets, state, device=self.device, compute=self.compute)
            elif name == "model":
                part = _build_backbone(self.pin, state, device=self.device, compute=self.compute)
            else:
                part = _build_vae(state, device=self.device)
        except (ImportError, RuntimeError, ValueError) as error:
            raise EngineUnavailable(f"cannot build the AuK {name}: {error}") from error
        finally:
            del state
        self._parts[name] = part
        return part

    # ---- sampling ------------------------------------------------------

    def generate(
        self,
        *,
        instruction: str,
        source_wav: Path | str,
        seed: int | None = None,
        settings: Mapping[str, Any] | None = None,
    ) -> AuKSample:
        """Render ``instruction`` over the keep wav as a fresh 24 kHz mono take."""
        resolved = sample_settings(settings)
        sample_rate, samples = read_wav(source_wav)
        waveform = torch.from_numpy(as_mono_float(samples))
        if waveform.numel() == 0:
            raise ValueError(f"AuK needs a non-empty reference: {source_wav}")
        target_seconds = waveform.shape[-1] / float(sample_rate)
        item_seed = int(seed or 0) % (2**64)

        if self.device.type == "cuda":
            torch.cuda.reset_peak_memory_stats(self.device)
        started = time.monotonic()

        backbone = self._component("model")
        encoder = self._component("encoder")
        vae = self._component("vae")

        reference = _encode_reference(vae, waveform, sample_rate, item_seed, self.device)
        hidden, mask = encoder.encode(
            instruction,
            waveform,
            sample_rate,
            backbone.layer_weights.detach(),
            backbone.layer_scale.detach(),
        )
        latent = self._diffuse(backbone, hidden, mask, reference, target_seconds, item_seed, resolved)
        audio = _decode_latent(vae, latent, self.device)
        if not bool(torch.isfinite(audio).all()):
            raise NonFiniteAudio(
                f"AuK produced non-finite audio at {self.pin.precision}; try the bf16 pin with fp32 compute"
            )
        keep = round(target_seconds * SAMPLE_RATE)
        rendered = audio[..., :keep][0, 0].to(torch.float32).cpu().numpy()
        if rendered.shape[-1] != keep:
            raise RuntimeError(
                f"the codec produced {rendered.shape[-1]} samples for a {keep}-sample target"
            )
        return AuKSample(
            samples=rendered,
            sample_rate=SAMPLE_RATE,
            duration_s=rendered.shape[-1] / float(SAMPLE_RATE),
            wall_s=time.monotonic() - started,
            vram_peak_bytes=vram_peak_bytes(self.device),
        )

    def _diffuse(
        self,
        backbone: AuKModel,
        hidden: torch.Tensor,
        mask: torch.Tensor,
        reference: torch.Tensor,
        target_seconds: float,
        item_seed: int,
        settings: SampleSettings,
    ) -> torch.Tensor:
        transformer = backbone.transformer
        guided = not backbone.is_flash and settings.cfg >= 1e-5
        reference = reference.to(device=self.device, dtype=self.compute)
        hidden = hidden.to(device=self.device, dtype=self.compute)
        mask = mask.to(self.device)
        context, prompt = transformer.prepare(hidden, reference, guided)
        length = max(1, math.ceil(target_seconds * FRAMES_PER_SECOND))
        generator = torch.Generator(device=self.device).manual_seed(item_seed)
        latent = torch.randn(
            (1, length, LATENT_DIM), device=self.device, dtype=torch.float32, generator=generator
        )
        times = time_grid(settings)
        for start, end in zip(times[:-1], times[1:]):
            velocity = transformer(
                latent.to(self.compute),
                torch.tensor(start, device=self.device, dtype=torch.float32),
                context,
                prompt,
                mask,
                guided,
            )
            if guided:
                conditional, unconditional = velocity.chunk(2)
                velocity = conditional + settings.cfg * (conditional - unconditional)
            latent = latent + (end - start) * velocity.float()
        return latent


def backbone_architecture(pin: AukPin) -> dict[str, int]:
    """The Flux2Edit geometry the pin's bundled config asks for."""
    config = yaml.safe_load(pin.arch_config.read_text(encoding="utf-8"))["model"]
    if config["name"] != "AuK" or config["backbone"] != "Flux2Edit":
        raise ValueError(f"incorrect bundled AuK config: {pin.arch_config}")
    keys = ("dim", "heads", "ff_mult", "text_hidden_dim", "num_layers", "num_single_layers")
    architecture = {key: config["arch"][key] for key in keys}
    # The backbone is resized to the codec, not to the config's own guess.
    architecture["latent_dim"] = config["vae"]["latent_dim"]
    return architecture


def _build_backbone(
    pin: AukPin, state: dict[str, torch.Tensor], *, device: torch.device, compute: torch.dtype
) -> AuKModel:
    with torch.device("meta"):
        model = AuKModel(
            variant="base",
            architecture=backbone_architecture(pin),
            encoder_layers=int(state["layer_weights"].shape[0]),
        )
    # The fusion weights are read in fp32 by the encoder, so they keep their stored dtype.
    ops.load_weights(
        model, state, compute=compute, device=device, keep_dtype=("layer_weights", "layer_scale")
    )
    model.eval()
    return model


def _build_vae(state: dict[str, torch.Tensor], *, device: torch.device) -> BigVGANFlowVAE:
    folded = fold_weight_norm(state)
    with torch.device("meta"):
        model = BigVGANFlowVAE()
    # The codec is fp32 on every pin: it is the decode path that carries AuK's volume edits.
    ops.load_weights(model, folded, compute=torch.float32, device=device)
    model.eval()
    return model


def _encode_reference(
    vae: BigVGANFlowVAE, waveform: torch.Tensor, sample_rate: int, seed: int, device: torch.device
) -> torch.Tensor:
    mono = waveform.reshape(1, 1, -1).to(device=device, dtype=torch.float32)
    if sample_rate != SAMPLE_RATE:
        mono = torchaudio.functional.resample(mono, sample_rate, SAMPLE_RATE)
    # The strided encoder needs at least one codec frame.
    if mono.shape[-1] < MIN_REFERENCE_SAMPLES:
        mono = torch.nn.functional.pad(mono, (0, MIN_REFERENCE_SAMPLES - mono.shape[-1]))
    generator = torch.Generator(device=device).manual_seed(seed)
    with torch.no_grad():
        return vae.encode(mono, generator).cpu()


def _decode_latent(vae: BigVGANFlowVAE, latent: torch.Tensor, device: torch.device) -> torch.Tensor:
    with torch.no_grad():
        decoded = vae.decode(latent.to(device=device, dtype=torch.float32))
        return decoded.cpu()


def _empty_cache(device: torch.device) -> None:
    if device.type == "cuda":
        torch.cuda.empty_cache()
