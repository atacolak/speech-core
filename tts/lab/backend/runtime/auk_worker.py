"""AuK worker: the only process that touches AuK weights, CUDA or torch.

Launched as ``python -m tts.lab.backend.runtime.auk_worker`` from the AuK venv
by :class:`tts.lab.backend.runtime.worker.SubprocessWorker`. It speaks the same
JSON-lines protocol as the E2 worker and reuses its line helpers, so the lab
process needs neither torch nor the AuK venv on its own path.

Commands: ``ping``, ``load``, ``offload_component``, ``generate``, ``status``,
``shutdown``. Weight residency is real per component; ``generate`` builds the
sampler graphs on demand and fails closed with ``engine_unavailable`` when the port
cannot run here, rather than returning audio it did not synthesize.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

REPO = str(Path(__file__).resolve().parents[4])
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from tts.auk.pin import COMPONENTS, resolve_pin  # noqa: E402
from tts.lab.backend.runtime.worker import decode_rpc_line, install_cancel_handler, write_reply  # noqa: E402


class AukWorker:
    """Owns the pin and the per-component weight residency for one process."""

    def __init__(self) -> None:
        self.pin_root: Path | None = None
        self.precision: str | None = None
        self.store: Any | None = None
        self.engine: Any | None = None
        self.device = os.environ.get("AUK_DEVICE", "cuda")

    # ---- commands ------------------------------------------------------

    def ping(self) -> dict[str, Any]:
        return {"ok": True, "state": "ready", "pid": os.getpid()}

    def status(self) -> dict[str, Any]:
        loaded = {name: False for name in COMPONENTS}
        if self.store is not None:
            loaded = self.store.resident()
        return {
            "ok": True,
            "result": {
                "precision": self.precision,
                "components": loaded,
                "device": self.device,
            },
        }

    def load(self, precision: str | None, pin_root: str | None) -> dict[str, Any]:
        import torch

        from tts.auk.weights import ComponentStore

        pin = resolve_pin(precision or "bf16", Path(pin_root) if pin_root else None)
        missing = pin.missing()
        if missing:
            return {"ok": False, "code": "weights_missing", "missing": missing}
        if self.store is None:
            device = self.device
            if device.startswith("cuda") and not torch.cuda.is_available():
                return {"ok": False, "code": "cuda_unavailable", "message": "no CUDA device"}
            self.store = ComponentStore(pin, device=device)
            self.engine = None
        elif self.store.pin.precision != pin.precision:
            self.store.offload_all()
            self.store = ComponentStore(pin, device=self.device)
            # A pin swap invalidates every built graph: the tensors underneath are gone.
            self.engine = None
        if torch.cuda.is_available():
            torch.cuda.reset_peak_memory_stats()
        components: dict[str, bool] = {}
        loaded_bytes = 0
        for name in COMPONENTS:
            try:
                report = self.store.load(name)
            except torch.cuda.OutOfMemoryError as exc:
                self.store.offload_all()
                return {"ok": False, "code": "cuda_oom", "message": str(exc)}
            components[name] = bool(report.get("loaded"))
            loaded_bytes += int(report.get("bytes") or 0)
        self.precision = pin.precision
        self.pin_root = pin.root
        return {
            "ok": True,
            "result": {
                "precision": pin.precision,
                "pin": pin.name,
                "components": components,
                "loaded_bytes": loaded_bytes,
                "vram_peak_bytes": self.store.peak_bytes() if self.store is not None else None,
            },
        }

    def offload_component(self, component: str) -> dict[str, Any]:
        if self.store is None:
            return {"ok": False, "code": "runtime_unloaded", "message": "AuK is not loaded"}
        if self.engine is not None:
            # The graph owns the tensors the store handed over; freeing only the store's
            # own copy would leave the component resident and the API would lie.
            self.engine.drop(component)
        try:
            self.store.offload(component)
        except ValueError as exc:
            return {"ok": False, "code": getattr(exc, "code", "unknown_component"), "message": str(exc)}
        return {"ok": True, "result": {"components": self.store.resident()}}

    def generate(self, request: dict[str, Any]) -> dict[str, Any]:
        if self.store is None:
            return {"ok": False, "code": "runtime_unloaded", "message": "AuK is not loaded"}
        import torch

        from tts.auk.engine import AuKEngine, EngineUnavailable
        from tts.wav import write_wav

        output_path = Path(str(request["output_path"]))
        try:
            if self.engine is None:
                self.engine = AuKEngine(self.store, device=self.device)
            sample = self.engine.generate(
                instruction=str(request.get("instruction") or ""),
                source_wav=request["source_wav"],
                seed=request.get("seed"),
                settings=request.get("settings"),
            )
            write_wav(output_path, sample.sample_rate, sample.samples)
        except torch.cuda.OutOfMemoryError as exc:
            return {"ok": False, "code": "cuda_oom", "message": str(exc)}
        except EngineUnavailable as exc:
            # The port cannot run here at all; the lab answers 501 rather than a fake wav.
            return {"ok": False, "code": "engine_unavailable", "message": str(exc)}
        return {
            "ok": True,
            "result": {
                "output_path": str(output_path),
                "sample_rate": sample.sample_rate,
                "duration_s": sample.duration_s,
                "wall_s": sample.wall_s,
                "vram_peak_bytes": sample.vram_peak_bytes,
            },
        }


def handle(worker: AukWorker, payload: dict[str, Any]) -> dict[str, Any]:
    cmd = str(payload.get("cmd") or "")
    if cmd == "ping":
        return worker.ping()
    if cmd == "status":
        return worker.status()
    if cmd == "load":
        return worker.load(payload.get("precision"), payload.get("pin_root"))
    if cmd == "offload_component":
        return worker.offload_component(str(payload.get("component") or ""))
    if cmd == "generate":
        return worker.generate(dict(payload.get("request") or {}))
    return {"ok": False, "code": "error", "message": f"unknown cmd: {cmd}"}


def main() -> int:
    install_cancel_handler()
    worker = AukWorker()
    for raw in sys.stdin:
        payload = decode_rpc_line(raw)
        if payload is None:
            continue
        request_id = payload.get("id")
        cmd = str(payload.get("cmd") or "")
        if cmd == "shutdown":
            write_reply({"ok": True, "state": "unloaded"}, request_id)
            if worker.store is not None:
                worker.store.offload_all()
            return 0
        try:
            reply = handle(worker, payload)
        except Exception as exc:  # fail closed with a code the lab maps to HTTP
            reply = {"ok": False, "code": "error", "message": f"{type(exc).__name__}: {exc}"}
        write_reply(reply, request_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
