"""E2RuntimeManager: one owner for GPU residency. not on the voicecat path."""

from __future__ import annotations

import tempfile
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tts.lab.backend.runtime.config import (
    E2_GPU_INDEX,
    E2_LIVE_CALL_LEASE_S,
    E2_LIVE_CALL_SESSION_LEASE_S,
    E2_LOAD_TIMEOUT_S,
    E2_REQUIRED_VRAM_BYTES,
    E2_SYNTH_TIMEOUT_S,
    E2_UNLOAD_TIMEOUT_S,
    E2_VRAM_MARGIN_BYTES,
)
from tts.lab.backend.runtime.leftover import LeftoverController, NoopLeftover
from tts.lab.backend.runtime.types import (
    InsufficientVram,
    LiveCallActive,
    RuntimeBusy,
    RuntimeErrorInfo,
    RuntimeState,
    RuntimeStatus,
    RuntimeUnloaded,
)
from tts.lab.backend.runtime.vram import NvidiaSmiVramProbe, VramProbe
from tts.lab.backend.runtime.worker import (
    SubprocessWorker,
    WorkerChannelDirty,
    WorkerHandle,
)
from tts.paths import BREEZE_IMPLEMENTATION, BREEZE_PIN_COMMIT, SELECTED_RUNTIME


def _now() -> datetime:
    return datetime.now(timezone.utc)


class E2RuntimeManager:
    def __init__(
        self,
        *,
        vram: VramProbe | None = None,
        leftover: LeftoverController | None = None,
        worker_factory: Any | None = None,
        required_vram_bytes: int = E2_REQUIRED_VRAM_BYTES,
        vram_margin_bytes: int = E2_VRAM_MARGIN_BYTES,
        gpu_index: int = E2_GPU_INDEX,
        load_timeout_s: float = E2_LOAD_TIMEOUT_S,
        synth_timeout_s: float = E2_SYNTH_TIMEOUT_S,
        unload_timeout_s: float = E2_UNLOAD_TIMEOUT_S,
        live_call_lease_s: float = E2_LIVE_CALL_LEASE_S,
        live_call_session_lease_s: float = E2_LIVE_CALL_SESSION_LEASE_S,
        autoload: bool = False,
    ) -> None:
        self._lock = threading.RLock()
        self._cv = threading.Condition(self._lock)
        self._state: RuntimeState = "unloaded"
        self._worker: WorkerHandle | None = None
        self._processor: str | None = None
        self._last_error: RuntimeErrorInfo | None = None
        self._loaded_at: datetime | None = None
        self._vram = vram or NvidiaSmiVramProbe()
        self._leftover = leftover if leftover is not None else NoopLeftover()
        self._worker_factory = worker_factory
        self.required_vram_bytes = int(required_vram_bytes)
        self.vram_margin_bytes = int(vram_margin_bytes)
        self.gpu_index = int(gpu_index)
        self.load_timeout_s = float(load_timeout_s)
        self.synth_timeout_s = float(synth_timeout_s)
        self.unload_timeout_s = float(unload_timeout_s)
        self.live_call_lease_s = float(live_call_lease_s)
        self.live_call_session_lease_s = float(live_call_session_lease_s)
        self._synth_inflight = 0
        self._stream_observer: Any | None = None
        self._load_phase: str | None = None
        self._load_started_at: datetime | None = None
        self._load_started_mono: float | None = None
        self._hop_lease_until: float | None = None
        self._session_lease_until: float | None = None
        self._reloading = False
        if autoload:
            self.begin_load()

    @property
    def worker_spawn_count(self) -> int:
        factory = self._worker_factory
        count = getattr(factory, "spawn_count", None)
        if count is not None:
            return int(count)
        return 0 if self._worker is None else 1

    def _spawn_worker(self) -> WorkerHandle:
        factory = self._worker_factory
        if factory is None:
            worker = SubprocessWorker()
            worker.start()
            return worker
        spawn = getattr(factory, "spawn", None)
        if callable(spawn) and not callable(factory):
            worker = spawn()
        else:
            worker = factory()
        worker.start()
        return worker

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

    def _pid(self) -> int | None:
        worker = self._worker
        if worker is None:
            return None
        return worker.pid

    def _remaining_until(self, until: float | None) -> float:
        if until is None:
            return 0.0
        return max(0.0, until - time.monotonic())

    def _hop_remaining_unlocked(self) -> float:
        return self._remaining_until(self._hop_lease_until)

    def _session_remaining_unlocked(self) -> float:
        return self._remaining_until(self._session_lease_until)

    def _live_call_remaining_unlocked(self) -> float:
        return max(self._hop_remaining_unlocked(), self._session_remaining_unlocked())

    def _live_call_holder_unlocked(self) -> str | None:
        session = self._session_remaining_unlocked()
        hop = self._hop_remaining_unlocked()
        if session > 0:
            return "session"
        if hop > 0:
            return "hop"
        return None

    def refresh_live_call_lease(self, ttl_s: float | None = None) -> None:
        """Hop safety net. VoiceCat session presence uses hold_session_lease."""
        with self._lock:
            self._hop_lease_until = time.monotonic() + float(
                self.live_call_lease_s if ttl_s is None else ttl_s
            )

    def hold_session_lease(self, ttl_s: float | None = None) -> None:
        with self._lock:
            self._session_lease_until = time.monotonic() + float(
                self.live_call_session_lease_s if ttl_s is None else ttl_s
            )

    def clear_session_lease(self) -> None:
        with self._lock:
            self._session_lease_until = None

    def live_call_remaining_s(self) -> float:
        with self._lock:
            return self._live_call_remaining_unlocked()

    def live_call_active(self) -> bool:
        return self.live_call_remaining_s() > 0

    def _set_phase(self, phase: str) -> None:
        with self._cv:
            if self._state == "loading":
                self._load_phase = phase
                self._cv.notify_all()

    def _clear_load_progress(self) -> None:
        self._load_phase = None
        self._load_started_at = None
        self._load_started_mono = None

    def set_processor(self, name: str | None) -> None:
        """GPU occupant, owned by ProcessorLease. E2 never sets this itself."""
        with self._lock:
            self._processor = None if name is None else str(name)

    def set_stream_observer(self, observer: Any | None) -> None:
        """Optional wrap(request, chunks) tap on synthesize_stream. Default None."""
        self._stream_observer = observer

    def status(self) -> RuntimeStatus:
        with self._lock:
            if (
                self._state == "ready"
                and not self._reloading
                and self._worker is not None
                and not self._worker.is_alive()
            ):
                self._state = "error"
                self._last_error = RuntimeErrorInfo(code="worker_crash", message="E2 worker exited")
                self._worker = None
                self._loaded_at = None
                try:
                    self._leftover.restore()
                except Exception:
                    pass
            elapsed = None
            started_at = None
            phase = None
            if self._state == "loading":
                phase = self._load_phase
                started_at = self._load_started_at
                if self._load_started_mono is not None:
                    elapsed = max(0.0, time.monotonic() - self._load_started_mono)
            remaining = self._live_call_remaining_unlocked()
            return RuntimeStatus(
                state=self._state,
                gpu_index=self.gpu_index,
                worker_pid=self._pid(),
                required_vram_bytes=self.required_vram_bytes + self.vram_margin_bytes,
                vram_margin_bytes=self.vram_margin_bytes,
                free_vram_bytes=self._probe_free(),
                used_vram_bytes=self._probe_used(),
                last_error=self._last_error,
                loaded_at=self._loaded_at,
                model_revision=BREEZE_PIN_COMMIT,
                leftover_parked=self._leftover.is_parked(),
                selected=SELECTED_RUNTIME,
                implementation=BREEZE_IMPLEMENTATION,
                load_phase=phase,
                load_started_at=started_at,
                load_elapsed_s=elapsed,
                live_call_active=remaining > 0,
                live_call_remaining_s=remaining,
                live_call_holder=self._live_call_holder_unlocked(),
                processor=self._processor,
            )

    def wait_until_not(self, *states: RuntimeState, timeout: float = 30.0) -> RuntimeStatus:
        deadline = time.monotonic() + timeout
        with self._cv:
            while self._state in states:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cv.wait(timeout=remaining)
            return self.status()

    def begin_load(self) -> RuntimeStatus:
        with self._cv:
            if self._state == "ready" and self._worker is not None and self._worker.is_alive():
                return self.status()
            if self._state == "loading":
                return self.status()
            if self._state == "unloading":
                raise RuntimeBusy("unloading")
            self._state = "loading"
            self._last_error = None
            self._load_phase = "parking leftover"
            self._load_started_at = _now()
            self._load_started_mono = time.monotonic()
            self._cv.notify_all()
            threading.Thread(target=self._load_thread_main, name="e2-load", daemon=True).start()
            return self.status()

    def load(self, *, timeout: float | None = None) -> RuntimeStatus:
        status = self.begin_load()
        if status.state != "loading":
            return status
        return self.wait_until_not("loading", timeout=timeout or self.load_timeout_s)

    def _load_thread_main(self) -> None:
        required = self.required_vram_bytes + self.vram_margin_bytes
        worker: WorkerHandle | None = None
        try:
            self._set_phase("parking leftover")
            self._leftover.park()
            self._set_phase("checking VRAM")
            free = self._probe_free()
            if free is not None and free < required:
                raise InsufficientVram(required_bytes=required, free_bytes=free)
            self._set_phase("starting worker")
            worker = self._spawn_worker()
            if not worker.is_alive():
                raise RuntimeError("E2 worker died during start")
            self._set_phase("loading weights")

            def _on_progress(payload: dict[str, Any]) -> None:
                phase = str(payload.get("phase") or "").strip()
                if phase:
                    self._set_phase(phase)

            reply = worker.rpc(
                {"cmd": "load"},
                timeout=self.load_timeout_s,
                on_progress=_on_progress,
            )
            if not reply.get("ok"):
                code = str(reply.get("code") or "error")
                if code == "cuda_oom":
                    free_after = self._probe_free() or 0
                    raise InsufficientVram(required_bytes=required, free_bytes=free_after)
                raise RuntimeError(str(reply.get("message") or "E2 load failed"))
            with self._cv:
                self._worker = worker
                self._state = "ready"
                self._loaded_at = _now()
                self._last_error = None
                self._clear_load_progress()
                self._cv.notify_all()
        except InsufficientVram as exc:
            self._abort_worker(worker)
            try:
                self._leftover.restore()
            except Exception:
                pass
            with self._cv:
                self._state = "insufficient_vram"
                self._last_error = exc.to_info()
                self._clear_load_progress()
                self._cv.notify_all()
        except Exception as exc:
            self._abort_worker(worker)
            try:
                self._leftover.restore()
            except Exception:
                pass
            with self._cv:
                self._state = "error"
                self._last_error = RuntimeErrorInfo(code="error", message=str(exc))
                self._clear_load_progress()
                self._cv.notify_all()

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

    def begin_unload(self, *, restore_leftover: bool = True) -> RuntimeStatus:
        with self._cv:
            if self._state in {"unloaded", "insufficient_vram", "error"} and self._worker is None:
                self._state = "unloaded"
                self._last_error = None
                self._hop_lease_until = None
                self._session_lease_until = None
                self._cv.notify_all()
                return self.status()
            if self._state == "unloading":
                return self.status()
            self._state = "unloading"
            self._cv.notify_all()
            threading.Thread(
                target=self._unload_thread_main,
                kwargs={"restore_leftover": restore_leftover},
                name="e2-unload",
                daemon=True,
            ).start()
            return self.status()

    def unload(
        self,
        *,
        timeout: float | None = None,
        restore_leftover: bool = True,
    ) -> RuntimeStatus:
        status = self.begin_unload(restore_leftover=restore_leftover)
        if status.state != "unloading":
            return status
        return self.wait_until_not("unloading", timeout=timeout or (self.unload_timeout_s + 5))

    def _unload_thread_main(self, *, restore_leftover: bool = True) -> None:
        deadline = time.monotonic() + max(1.0, self.unload_timeout_s)
        while time.monotonic() < deadline:
            with self._lock:
                inflight = self._synth_inflight
            if inflight == 0:
                break
            time.sleep(0.05)
        worker: WorkerHandle | None
        with self._lock:
            worker = self._worker
            self._worker = None
            self._hop_lease_until = None
            self._session_lease_until = None
        if worker is not None:
            try:
                worker.rpc({"cmd": "shutdown"}, timeout=min(2.0, self.unload_timeout_s))
            except Exception:
                pass
            try:
                worker.terminate(timeout=self.unload_timeout_s)
            except Exception:
                pass
            if worker.is_alive():
                worker.kill()
        if restore_leftover:
            try:
                self._leftover.restore()
            except Exception:
                pass
        with self._cv:
            self._state = "unloaded"
            self._loaded_at = None
            self._last_error = None
            self._cv.notify_all()

    def _claim_ready_worker(self) -> WorkerHandle:
        if self._state in {"unloaded", "insufficient_vram", "error"}:
            raise RuntimeUnloaded(self._state)
        if self._state in {"loading", "unloading"}:
            raise RuntimeBusy(self._state)
        worker = self._worker
        if worker is None or not worker.is_alive():
            self._state = "error"
            self._last_error = RuntimeErrorInfo(code="worker_crash", message="E2 worker exited")
            raise RuntimeUnloaded(self._state)
        self._synth_inflight += 1
        return worker

    def _reload_dirty_worker(self, worker: WorkerHandle) -> WorkerHandle:
        """Kill a desynced pipe worker and load a replacement. Leftover stays parked."""
        self._reloading = True
        try:
            self._abort_worker(worker)
            with self._cv:
                if self._worker is worker:
                    self._worker = None
            replacement = self._spawn_worker()
            try:
                reply = replacement.rpc({"cmd": "load"}, timeout=self.load_timeout_s)
                if not reply.get("ok"):
                    raise RuntimeError(str(reply.get("message") or "E2 reload failed"))
            except Exception:
                self._abort_worker(replacement)
                raise
            with self._cv:
                self._worker = replacement
                self._state = "ready"
                self._loaded_at = _now()
                self._last_error = None
                self._cv.notify_all()
            return replacement
        finally:
            self._reloading = False

    def _rpc_synthesize(self, worker: WorkerHandle, payload: dict[str, Any]) -> dict[str, Any]:
        cmd = {"cmd": "synthesize", "request": payload}
        try:
            return worker.rpc(cmd, timeout=self.synth_timeout_s)
        except WorkerChannelDirty:
            with self._lock:
                current = self._worker if self._worker is not None else worker
                worker = self._reload_dirty_worker(current)
            return worker.rpc(cmd, timeout=self.synth_timeout_s)

    def synthesize(self, request: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            remaining = self._live_call_remaining_unlocked()
            if remaining > 0:
                raise LiveCallActive(remaining)
            worker = self._claim_ready_worker()
        try:
            payload = dict(request)
            if not payload.get("output_path"):
                dest = Path(tempfile.mkdtemp(prefix="tts-lab-e2-")) / "take.wav"
                payload["output_path"] = str(dest)
            reply = self._rpc_synthesize(worker, payload)
            if not reply.get("ok"):
                raise RuntimeError(str(reply.get("message") or "synthesize failed"))
            return dict(reply.get("result") or {})
        finally:
            with self._lock:
                self._synth_inflight = max(0, self._synth_inflight - 1)

    def synthesize_stream(
        self,
        request: dict[str, Any],
        should_cancel: Callable[[], bool] | None = None,
    ) -> Any:
        """Yield s16le PCM as the worker emits chunks. Occupies until the last chunk."""
        with self._lock:
            worker = self._claim_ready_worker()

        def _chunks():
            current = worker
            try:
                payload = dict(request)
                if not payload.get("output_path"):
                    dest = Path(tempfile.mkdtemp(prefix="tts-lab-e2-")) / "take.wav"
                    payload["output_path"] = str(dest)
                cmd = {"cmd": "synthesize", "request": payload}
                observer = self._stream_observer
                try:
                    stream = current.iter_synthesize(
                        cmd, timeout=self.synth_timeout_s, should_cancel=should_cancel
                    )
                    if observer is not None:
                        stream = observer.wrap(dict(payload), stream)
                    yield from stream
                    return
                except WorkerChannelDirty:
                    with self._lock:
                        held = self._worker if self._worker is not None else current
                        current = self._reload_dirty_worker(held)
                    stream = current.iter_synthesize(
                        cmd, timeout=self.synth_timeout_s, should_cancel=should_cancel
                    )
                    if observer is not None:
                        stream = observer.wrap(dict(payload), stream)
                    yield from stream
            finally:
                with self._lock:
                    self._synth_inflight = max(0, self._synth_inflight - 1)

        return _chunks()
