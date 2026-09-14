"""GPU lease for lab processors. One occupant beside E2, never with it."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from tts.lab.backend.runtime.manager import E2RuntimeManager
from tts.lab.backend.runtime.types import LiveCallActive, RuntimeBusy, RuntimeStatus

BUSY_STATES = ("loading", "unloading")


def _raise_if_busy(status: RuntimeStatus) -> None:
    if status.state in BUSY_STATES:
        raise RuntimeBusy(status.state)


class ProcessorLease:
    """Serializes offline processors (resemble, vibevoice, auk) onto the single GPU.

    E2 is unloaded with ``restore_leftover=False`` and stays unloaded: parked
    leftover never bounces back onto the GPU the processor needs. The operator
    reloads Breeze explicitly.

    ``take``/``release`` are the same thing without a ``with`` block, for a
    processor whose residency spans HTTP requests (AuK load … unload).
    """

    def __init__(self, manager: E2RuntimeManager) -> None:
        self._manager = manager

    @property
    def manager(self) -> E2RuntimeManager:
        """The single-GPU runtime this lease serializes against."""
        return self._manager

    def take(self, name: str) -> None:
        manager = self._manager
        remaining = manager.live_call_remaining_s()
        if remaining > 0:
            raise LiveCallActive(remaining)
        status = manager.status()
        _raise_if_busy(status)
        if status.state == "ready":
            _raise_if_busy(manager.unload(restore_leftover=False))
        manager.set_processor(name)

    def release(self) -> None:
        self._manager.set_processor(None)

    @contextmanager
    def acquire(self, name: str) -> Iterator[None]:
        self.take(name)
        try:
            yield
        finally:
            self.release()
