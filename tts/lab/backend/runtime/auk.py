"""AuK runtime: the isolated audio editor, occupant ``auk`` on the GPU lease.

The lab process never imports torch or the AuK weights. It resolves a pin,
takes the GPU lease from E2 with ``restore_leftover=False``, and drives a worker
in the AuK venv over JSON lines. Everything that can be answered before a
subprocess exists — unknown precision, an incomplete pin, a live call, a busy
E2 — is answered synchronously so HTTP can fail closed with the right status.

int8 exists as an explicit second pin. Nothing here ever selects it by
omission, and a missing int8 pin is an error, not a quiet bf16 run.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tts.auk.pin import (
    AUK_ENCODER_PRECISION,
    AUK_MODEL_VARIANT,
    COMPONENTS,
    DEFAULT_PRECISION,
    DEFAULT_SETTINGS,
    INT8,
    PRECISIONS,
    AukPin,
    UnknownPrecision,
    resolve_pin,
)
from tts.lab.backend.runtime.config import (
    AUK_GENERATE_TIMEOUT_S,
    AUK_GPU_INDEX,
    AUK_LOAD_TIMEOUT_S,
    AUK_REQUIRED_VRAM_BYTES,
    AUK_UNLOAD_TIMEOUT_S,
    AUK_VRAM_MARGIN_BYTES,
)
from tts.lab.backend.runtime.processors import ProcessorLease
from tts.lab.backend.runtime.types import (
    InsufficientVram,
    LiveCallActive,
    RuntimeBusy,
    RuntimeErrorInfo,
    RuntimeState,
    RuntimeUnloaded,
)
from tts.lab.backend.runtime.vram import NvidiaSmiVramProbe, VramProbe
from tts.lab.backend.runtime.worker import SubprocessWorker, WorkerHandle
from tts.paths import auk_pin_root, auk_venv_python

PROCESSOR = "auk"
WORKER_MODULE = "tts.lab.backend.runtime.auk_worker"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class WeightsMissing(Exception):
    """A pin component is absent or not the published size. Never downgraded."""

    def __init__(self, precision: str, missing: list[str]) -> None:
        super().__init__(f"AuK {precision} pin incomplete: {', '.join(missing)}")
        self.code = "weights_missing"
        self.precision = precision
        self.missing = list(missing)


class UnknownComponent(ValueError):
    def __init__(self, component: str) -> None:
        super().__init__(f"unknown AuK component: {component}")
        self.code = "unknown_component"
        self.component = component


class PrecisionMismatch(Exception):
    """A generate asked for a precision that is not the loaded pin."""

    def __init__(self, requested: str, loaded: str) -> None:
        super().__init__(f"AuK is loaded at {loaded}, not {requested}")
        self.code = "precision_mismatch"
        self.requested = requested
        self.loaded = loaded


class EngineUnavailable(RuntimeError):
    """Weights are resident but inference cannot run (sampler port incomplete)."""

    code = "engine_unavailable"


class AukRuntimeManager:
    def __init__(
        self,
        *,
        lease: ProcessorLease,
        vram: VramProbe | None = None,
        worker_factory: Any | None = None,
        pin_root: Path | str | None = None,
        python: str | Path | None = None,
        env: dict[str, str] | None = None,
        gpu_index: int = AUK_GPU_INDEX,
        required_vram_bytes: int = AUK_REQUIRED_VRAM_BYTES,
        vram_margin_bytes: int = AUK_VRAM_MARGIN_BYTES,
        load_timeout_s: float = AUK_LOAD_TIMEOUT_S,
        generate_timeout_s: float = AUK_GENERATE_TIMEOUT_S,
        unload_timeout_s: float = AUK_UNLOAD_TIMEOUT_S,
    ) -> None:
        self._lock = threading.RLock()
        self._cv = threading.Condition(self._lock)
        self._lease = lease
        self._vram = vram or NvidiaSmiVramProbe()
        self._worker_factory = worker_factory
        self._pin_root = Path(pin_root) if pin_root is not None else auk_pin_root()
        self._python = str(python) if python is not None else str(auk_venv_python())
        self._env = dict(env or {})
        self.required_vram_bytes = int(required_vram_bytes)
        self.vram_margin_bytes = int(vram_margin_bytes)
        self.gpu_index = int(gpu_index)
        self.load_timeout_s = float(load_timeout_s)
        self.generate_timeout_s = float(generate_timeout_s)
        self.unload_timeout_s = float(unload_timeout_s)
        self._state: RuntimeState = "unloaded"
        self._precision = DEFAULT_PRECISION
        self._worker: WorkerHandle | None = None
        self._loaded = {name: False for name in COMPONENTS}
        self._last_error: RuntimeErrorInfo | None = None
        self._loaded_at: datetime | None = None
        self._vram_peak_bytes: int | None = None
        self._load_phase: str | None = None
        self._load_started_at: datetime | None = None
        self._load_started_mono: float | None = None
        self._generate_inflight = 0

    # ---- introspection -------------------------------------------------

    @property
    def lease(self) -> ProcessorLease:
        return self._lease

    @property
    def spawn_count(self) -> int:
        factory = self._worker_factory
        count = getattr(factory, "spawn_count", None)
        if count is not None:
            return int(count)
        return 0 if self._worker is None else 1

    def worker_alive(self) -> bool:
        worker = self._worker
        if worker is None:
            return False
        try:
            return bool(worker.is_alive())
        except Exception:
            return False

    def pin(self, precision: str | None = None) -> AukPin:
        return resolve_pin(self._precision if precision is None else precision, self._pin_root)

    def status(self) -> dict[str, Any]:
        with self._lock:
            if self._state == "ready" and not self.worker_alive():
                self._state = "error"
                self._worker = None
                self._loaded = {name: False for name in COMPONENTS}
                self._last_error = RuntimeErrorInfo(
                    code="worker_crash", message="AuK worker exited"
                )
                self._lease.release()
            state = self._state
            pin = resolve_pin(self._precision, self._pin_root)
            missing = pin.missing()
            elapsed = None
            if state == "loading" and self._load_started_mono is not None:
                elapsed = max(0.0, time.monotonic() - self._load_started_mono)
            remaining = self._lease.manager.live_call_remaining_s()
            return {
                "state": state,
                "status": state,
                "precision": self._precision,
                "default_precision": DEFAULT_PRECISION,
                "pin": pin.name,
                "pin_root": str(pin.root),
                "venv_python": self._python,
                "engine": PROCESSOR,
                "weights_present": not missing,
                "missing_weights": missing,
                "int8_selected": state == "ready" and self._precision == INT8,
                "occupant": self._lease.manager.status().processor,
                "loaded": dict(self._loaded),
                "gpu_index": self.gpu_index,
                "worker_pid": None if self._worker is None else self._worker.pid,
                "required_vram_bytes": self.required_vram_bytes + self.vram_margin_bytes,
                "free_vram_bytes": self._probe_free(),
                "used_vram_bytes": self._probe_used(),
                "vram_peak_bytes": self._vram_peak_bytes,
                "live_call_active": remaining > 0,
                "live_call_remaining_s": remaining,
                "leftover_parked": self._lease.manager.status().leftover_parked,
                "loaded_at": None if self._loaded_at is None else self._loaded_at.isoformat(),
                "load_phase": self._load_phase,
                "load_elapsed_s": elapsed,
                "last_error": None if self._last_error is None else self._last_error.to_dict(),
            }

    def wait_until_not(self, *states: RuntimeState, timeout: float = 30.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        with self._cv:
            while self._state in states:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cv.wait(timeout=remaining)
        return self.status()

    # ---- load / unload -------------------------------------------------

    def _normalize_precision(self, precision: str | None) -> str:
        if precision is None:
            return DEFAULT_PRECISION
        if precision not in PRECISIONS:
            raise UnknownPrecision(precision)
        return precision

    def begin_load(self, precision: str | None = None) -> dict[str, Any]:
        with self._cv:
            wanted = self._normalize_precision(precision)
            if self._state == "loading":
                return self.status()
            if self._state == "unloading":
                raise RuntimeBusy("unloading")
            if self._state == "ready" and self.worker_alive() and wanted == self._precision:
                return self.status()
            pin = resolve_pin(wanted, self._pin_root)
            missing = pin.missing()
            if missing:
                raise WeightsMissing(wanted, missing)
            self._lease.take(PROCESSOR)
            self._state = "loading"
            self._precision = wanted
            self._last_error = None
            self._load_phase = "starting worker"
            self._load_started_at = _now()
            self._load_started_mono = time.monotonic()
            self._cv.notify_all()
            threading.Thread(
                target=self._load_thread_main,
                kwargs={"pin": pin},
                name="auk-load",
                daemon=True,
            ).start()
            return self.status()

    def load(self, precision: str | None = None, *, timeout: float | None = None) -> dict[str, Any]:
        status = self.begin_load(precision)
        if status["state"] != "loading":
            return status
        return self.wait_until_not("loading", timeout=timeout or self.load_timeout_s)

    def _spawn_worker(self) -> WorkerHandle:
        factory = self._worker_factory
        if factory is None:
            worker: WorkerHandle = SubprocessWorker(
                python=self._python, env=self._env, module=WORKER_MODULE
            )
        else:
            spawn = getattr(factory, "spawn", None)
            worker = spawn() if callable(spawn) and not callable(factory) else factory()
        worker.start()
        return worker

    def _load_thread_main(self, *, pin: AukPin) -> None:
        required = self.required_vram_bytes + self.vram_margin_bytes
        worker: WorkerHandle | None = None
        try:
            self._set_phase("unloading previous")
            self._teardown_worker()
            self._set_phase("checking VRAM")
            free = self._probe_free()
            if free is not None and free < required:
                raise InsufficientVram(required_bytes=required, free_bytes=free)
            self._set_phase("starting worker")
            worker = self._spawn_worker()
            if not worker.is_alive():
                raise RuntimeError("AuK worker died during start")
            self._set_phase("loading weights")
            reply = worker.rpc(
                {
                    "cmd": "load",
                    "precision": pin.precision,
                    "pin_root": str(pin.root),
                },
                timeout=self.load_timeout_s,
                on_progress=self._on_load_progress,
            )
            if not reply.get("ok"):
                code = str(reply.get("code") or "error")
                if code == "cuda_oom":
                    raise InsufficientVram(
                        required_bytes=required, free_bytes=self._probe_free() or 0
                    )
                if code == "weights_missing":
                    raise WeightsMissing(pin.precision, list(reply.get("missing") or []))
                raise RuntimeError(str(reply.get("message") or "AuK load failed"))
            result = dict(reply.get("result") or {})
            reported = dict(result.get("components") or {})
            with self._cv:
                self._worker = worker
                self._state = "ready"
                self._loaded = {name: bool(reported.get(name, True)) for name in COMPONENTS}
                peak = result.get("vram_peak_bytes")
                self._vram_peak_bytes = None if peak is None else int(peak)
                self._loaded_at = _now()
                self._last_error = None
                self._clear_load_progress()
                self._cv.notify_all()
        except InsufficientVram as exc:
            self._fail_load(worker, exc.to_info())
        except Exception as exc:
            self._fail_load(
                worker,
                RuntimeErrorInfo(code=str(getattr(exc, "code", "error")), message=str(exc)),
            )

    def _fail_load(self, worker: WorkerHandle | None, info: RuntimeErrorInfo) -> None:
        """A failed load holds no GPU: the worker dies and the lease goes back."""
        self._abort_worker(worker)
        with self._cv:
            self._worker = None
            self._loaded = {name: False for name in COMPONENTS}
            self._state = "insufficient_vram" if info.code == "insufficient_vram" else "error"
            self._last_error = info
            self._clear_load_progress()
            self._lease.release()
            self._cv.notify_all()

    def begin_unload(self) -> dict[str, Any]:
        with self._cv:
            if self._state in {"unloaded", "insufficient_vram", "error"} and self._worker is None:
                self._state = "unloaded"
                self._loaded = {name: False for name in COMPONENTS}
                self._last_error = None
                self._lease.release()
                self._cv.notify_all()
                return self.status()
            if self._state == "unloading":
                return self.status()
            self._state = "unloading"
            self._cv.notify_all()
            threading.Thread(target=self._unload_thread_main, name="auk-unload", daemon=True).start()
            return self.status()

    def unload(self, *, timeout: float | None = None) -> dict[str, Any]:
        """Blocking unload. Cheap (kill the worker); the route can wait for it."""
        status = self.begin_unload()
        if status["state"] != "unloading":
            return status
        return self.wait_until_not("unloading", timeout=timeout or (self.unload_timeout_s + 5.0))

    def _unload_thread_main(self) -> None:
        self._teardown_worker()
        with self._cv:
            self._state = "unloaded"
            self._loaded = {name: False for name in COMPONENTS}
            self._loaded_at = None
            self._last_error = None
            self._vram_peak_bytes = None
            self._clear_load_progress()
            self._lease.release()
            self._cv.notify_all()

    def _teardown_worker(self) -> None:
        deadline = time.monotonic() + max(1.0, self.unload_timeout_s)
        while time.monotonic() < deadline:
            with self._lock:
                if self._generate_inflight == 0:
                    break
            time.sleep(0.05)
        with self._lock:
            worker = self._worker
            self._worker = None
            self._loaded = {name: False for name in COMPONENTS}
        if worker is None:
            return
        try:
            worker.rpc({"cmd": "shutdown"}, timeout=min(2.0, self.unload_timeout_s))
        except Exception:
            pass
        try:
            worker.terminate(timeout=self.unload_timeout_s)
        except Exception:
            pass
        try:
            if worker.is_alive():
                worker.kill()
        except Exception:
            pass

    def _abort_worker(self, worker: WorkerHandle | None) -> None:
        if worker is None:
            return
        try:
            worker.terminate(timeout=1.0)
        except Exception:
            pass
        try:
            worker.kill()
        except Exception:
            pass

    # ---- components ----------------------------------------------------

    def offload_component(self, component: str) -> dict[str, Any]:
        """Drop one component's residency. The others stay; the state stays ready."""
        if component not in COMPONENTS:
            raise UnknownComponent(component)
        worker = self._claim_worker()
        try:
            reply = worker.rpc(
                {"cmd": "offload_component", "component": component},
                timeout=self.generate_timeout_s,
            )
            if not reply.get("ok"):
                raise RuntimeError(str(reply.get("message") or "offload failed"))
            result = dict(reply.get("result") or {})
        finally:
            with self._lock:
                self._generate_inflight = max(0, self._generate_inflight - 1)
        reported = dict(result.get("components") or {})
        with self._cv:
            for name in COMPONENTS:
                if name in reported:
                    self._loaded[name] = bool(reported[name])
            self._loaded[component] = False
        return self.status()

    # ---- generate ------------------------------------------------------

    def generate(
        self,
        *,
        task: str,
        instruction: str,
        source_wav: Path | str,
        output_path: Path | str,
        seed: int | None = None,
        settings: dict[str, Any] | None = None,
        precision: str | None = None,
    ) -> dict[str, Any]:
        if precision is not None:
            wanted = self._normalize_precision(precision)
            if wanted != self._precision:
                raise PrecisionMismatch(wanted, self._precision)
        worker = self._claim_worker()
        payload = {
            "cmd": "generate",
            "request": {
                "task": str(task),
                "instruction": str(instruction),
                "source_wav": str(source_wav),
                "output_path": str(output_path),
                "seed": seed,
                "settings": {**DEFAULT_SETTINGS, **(settings or {})},
                "precision": self._precision,
                "model_variant": AUK_MODEL_VARIANT,
                "encoder_precision": AUK_ENCODER_PRECISION,
            },
        }
        try:
            reply = worker.rpc(payload, timeout=self.generate_timeout_s)
            if not reply.get("ok"):
                code = str(reply.get("code") or "error")
                if code == "cuda_oom":
                    raise InsufficientVram(
                        required_bytes=self.required_vram_bytes + self.vram_margin_bytes,
                        free_bytes=self._probe_free() or 0,
                    )
                if code == "engine_unavailable":
                    raise EngineUnavailable(str(reply.get("message") or "AuK inference unavailable"))
                raise RuntimeError(str(reply.get("message") or "AuK generate failed"))
            result = dict(reply.get("result") or {})
            peak = result.get("vram_peak_bytes")
            if peak is not None:
                with self._lock:
                    self._vram_peak_bytes = max(self._vram_peak_bytes or 0, int(peak))
            return result
        finally:
            with self._lock:
                self._generate_inflight = max(0, self._generate_inflight - 1)

    def _claim_worker(self) -> WorkerHandle:
        with self._lock:
            if self._state != "ready":
                raise RuntimeUnloaded(self._state)
            remaining = self._lease.manager.live_call_remaining_s()
            if remaining > 0:
                raise LiveCallActive(remaining)
            worker = self._worker
            if worker is None or not self.worker_alive():
                self._state = "error"
                self._worker = None
                self._last_error = RuntimeErrorInfo(
                    code="worker_crash", message="AuK worker exited"
                )
                self._lease.release()
                raise RuntimeUnloaded(self._state)
            self._generate_inflight += 1
            return worker

    # ---- helpers -------------------------------------------------------

    def _on_load_progress(self, payload: dict[str, Any]) -> None:
        phase = str(payload.get("phase") or "").strip()
        if phase:
            self._set_phase(phase)

    def _set_phase(self, phase: str) -> None:
        with self._cv:
            if self._state == "loading":
                self._load_phase = phase
                self._cv.notify_all()

    def _clear_load_progress(self) -> None:
        self._load_phase = None
        self._load_started_at = None
        self._load_started_mono = None

    def _probe_free(self) -> int | None:
        try:
            return int(self._vram.free_bytes(self.gpu_index))
        except Exception:
            return None

    def _probe_used(self) -> int | None:
        try:
            return int(self._vram.used_bytes(self.gpu_index))
        except Exception:
            return None
