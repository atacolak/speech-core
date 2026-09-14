"""AuK pin: which weights make a run, and whether they are actually there.

Torch-free on purpose: the lab process imports this to gate a load before it
ever spawns the AuK worker in the AuK venv.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from tts.paths import auk_pin_root

BF16 = "bf16"
INT8 = "int8"
PRECISIONS: tuple[str, ...] = (BF16, INT8)
DEFAULT_PRECISION = BF16

# The diffusion checkpoint is what the precision selects. Encoder and VAE are
# shared: w4a8 encoder + unquantized fp32 VAE, exactly as published.
AUK_PIN_COMPONENTS: dict[str, dict[str, str]] = {
    BF16: {
        "diffusion": "auk_base_bf16.safetensors",
        "encoder": "qwen_omni_w4a8.safetensors",
        "vae": "auk_vae.safetensors",
    },
    INT8: {
        "diffusion": "auk_base_int8.safetensors",
        "encoder": "qwen_omni_w4a8.safetensors",
        "vae": "auk_vae.safetensors",
    },
}

# Published byte sizes. A short or long file is a failed download, not a pin.
AUK_PIN_SIZES: dict[str, int] = {
    "auk_base_bf16.safetensors": 3_062_397_724,
    "auk_base_int8.safetensors": 1_545_616_604,
    "qwen_omni_w4a8.safetensors": 3_178_989_504,
    "auk_vae.safetensors": 637_322_604,
}

# Provenance words a candidate row records.
AUK_MODEL_VARIANT = "auk-base"
AUK_ENCODER_PRECISION = "w4a8"

# Base sampling the cookbook pins; a request may override any of them.
DEFAULT_SETTINGS: dict[str, float] = {"nfe": 32, "cfg": 2.0, "sway": -1.0}

ASSETS_DIRNAME = "qwen2.5-omni-3b"
ARCH_DIRNAME = "auk_base"
COMPONENTS: tuple[str, ...] = ("encoder", "model", "vae")
# Runtime component -> pin slot. The diffusion checkpoint is the "model".
AUK_COMPONENT_WEIGHTS: dict[str, str] = {"encoder": "encoder", "model": "diffusion", "vae": "vae"}


class UnknownPrecision(ValueError):
    def __init__(self, precision: str) -> None:
        super().__init__(f"unknown AuK precision: {precision}")
        self.code = "unknown_precision"
        self.precision = precision


@dataclass(frozen=True)
class AukPin:
    """One resolved checkpoint set. ``missing`` is the gate, not a warning."""

    root: Path
    precision: str
    weights: dict[str, Path]

    @property
    def name(self) -> str:
        """The diffusion checkpoint stem, e.g. ``auk_base_bf16``."""
        return Path(AUK_PIN_COMPONENTS[self.precision]["diffusion"]).stem

    @property
    def assets(self) -> Path:
        return self.root / "assets" / ASSETS_DIRNAME

    @property
    def arch_config(self) -> Path:
        return self.root / "assets" / ARCH_DIRNAME / "config.yaml"

    def weight_path(self, component: str) -> Path:
        """The checkpoint file behind a runtime component."""
        return self.weights[AUK_COMPONENT_WEIGHTS[component]]

    def sizes(self) -> dict[str, int]:
        return {
            component: AUK_PIN_SIZES[path.name] for component, path in self.weights.items()
        }

    def missing(self) -> list[str]:
        """What the pin still needs: weight filenames, then the bundle config."""
        absent: list[str] = []
        for path in self.weights.values():
            expected = AUK_PIN_SIZES[path.name]
            try:
                actual = path.stat().st_size
            except OSError:
                absent.append(path.name)
                continue
            if actual != expected:
                absent.append(path.name)
        if not self.arch_config.is_file() or not (self.assets / "config.json").is_file():
            absent.append(str(self.arch_config.relative_to(self.root)))
        return absent

    def is_complete(self) -> bool:
        return not self.missing()


def resolve_pin(precision: str = DEFAULT_PRECISION, root: Path | None = None) -> AukPin:
    """The pin for ``precision``. Never guesses a fallback precision."""
    if precision not in AUK_PIN_COMPONENTS:
        raise UnknownPrecision(precision)
    base = Path(root) if root is not None else auk_pin_root()
    weights = {
        component: base / "weights" / name
        for component, name in AUK_PIN_COMPONENTS[precision].items()
    }
    return AukPin(root=base, precision=precision, weights=weights)
