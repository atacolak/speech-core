"""Per-component weight residency for a resolved AuK pin.

This is the part of the AuK stack that is pure bookkeeping: read a component's
safetensors into memory, move it to the device, and let each component be
dropped without touching the others. It deliberately does not build the model
graph — the sampler port lives behind :mod:`tts.auk.engine`, which takes a
component's tensors and hands them back when the graph is dropped.

Nothing here is imported by the lab process; it runs only inside the AuK venv.
"""

from __future__ import annotations

import gc
from typing import Any

import torch
from safetensors import safe_open

from tts.auk.pin import COMPONENTS, AukPin


class UnknownComponent(ValueError):
    def __init__(self, component: str) -> None:
        super().__init__(f"unknown AuK component: {component}")
        self.code = "unknown_component"
        self.component = component


class ComponentStore:
    """Holds one component's tensors at a time, on demand.

    ``load`` is idempotent; ``offload`` frees the device memory and the host
    tensors. Residency is per component, so the API can drop the VAE while the
    encoder stays live — the plan requires them independently droppable even
    while the first implementation happens to load all three.
    """

    def __init__(self, pin: AukPin, *, device: str | torch.device = "cuda") -> None:
        self.pin = pin
        self.device = torch.device(device)
        self._tensors: dict[str, dict[str, torch.Tensor]] = {}
        self._meta: dict[str, dict[str, Any]] = {}
        self._held: dict[str, int] = {}

    # ---- introspection -------------------------------------------------

    def resident(self) -> dict[str, bool]:
        return {name: name in self._tensors or name in self._held for name in COMPONENTS}

    def sizes(self) -> dict[str, int]:
        sizes: dict[str, int] = {}
        for name in COMPONENTS:
            if name in self._tensors:
                sizes[name] = sum(t.numel() * t.element_size() for t in self._tensors[name].values())
            elif name in self._held:
                sizes[name] = self._held[name]
        return sizes

    def total_bytes(self) -> int:
        return sum(self.sizes().values())

    def metadata(self, component: str) -> dict[str, Any]:
        return dict(self._meta.get(component) or {})

    def peak_bytes(self) -> int:
        if self.device.type != "cuda":
            return self.total_bytes()
        return int(torch.cuda.max_memory_allocated(self.device))

    # ---- residency -----------------------------------------------------

    def load(self, component: str) -> dict[str, Any]:
        if component not in COMPONENTS:
            raise UnknownComponent(component)
        if component in self._tensors or component in self._held:
            # Held means a built graph owns the tensors; re-reading the checkpoint would
            # put a second copy of the component on the card.
            return {"component": component, "loaded": True, "bytes": self.sizes()[component]}
        path = self.pin.weight_path(component)
        handle = torch.device("cpu")
        tensors: dict[str, torch.Tensor] = {}
        with safe_open(str(path), framework="pt", device=str(handle)) as file:
            self._meta[component] = dict(file.metadata() or {})
            for key in file.keys():
                tensors[key] = file.get_tensor(key)
        if self.device.type != "cpu":
            tensors = {key: value.to(self.device) for key, value in tensors.items()}
        self._tensors[component] = tensors
        return {"component": component, "loaded": True, "bytes": self.sizes()[component]}

    def take(self, component: str) -> dict[str, torch.Tensor]:
        """Hand a component's tensors to the graph that will own them.

        The sampler folds the VAE's weight-norm pairs into fresh tensors, so leaving the
        originals in the store would keep a second copy of the codec on the card. The
        store remembers the bytes as held-by-someone-else until ``offload`` says the graph
        is gone, so ``resident``/``sizes`` keep telling the truth.
        """
        if component not in COMPONENTS:
            raise UnknownComponent(component)
        self.load(component)
        tensors = self._tensors.pop(component, None)
        if tensors is None:
            raise RuntimeError(f"the AuK {component} is already held by a built graph")
        self._held[component] = sum(t.numel() * t.element_size() for t in tensors.values())
        return tensors

    def offload(self, component: str) -> dict[str, Any]:
        if component not in COMPONENTS:
            raise UnknownComponent(component)
        self._tensors.pop(component, None)
        self._held.pop(component, None)
        gc.collect()
        if self.device.type == "cuda":
            torch.cuda.empty_cache()
        return {"component": component, "loaded": False}

    def offload_all(self) -> None:
        for component in COMPONENTS:
            self.offload(component)
