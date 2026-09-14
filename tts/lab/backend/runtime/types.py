"""E2 runtime types. not on the voicecat path."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from tts.paths import DISPLAY_NAME, ENGINE_ID, SELECTED_RUNTIME

RuntimeState = Literal[
    "unloaded",
    "loading",
    "ready",
    "unloading",
    "insufficient_vram",
    "error",
]


@dataclass
class RuntimeErrorInfo:
    code: str
    message: str = ""
    required_bytes: int | None = None
    free_bytes: int | None = None
    shortfall_bytes: int | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.required_bytes is not None:
            payload["required_bytes"] = self.required_bytes
        if self.free_bytes is not None:
            payload["free_bytes"] = self.free_bytes
        if self.shortfall_bytes is not None:
            payload["shortfall_bytes"] = self.shortfall_bytes
        return payload


class RuntimeUnloaded(Exception):
    def __init__(self, state: str = "unloaded") -> None:
        super().__init__("E2 is unloaded")
        self.state = state
        self.code = "runtime_unloaded"


class RuntimeBusy(Exception):
    def __init__(self, state: str) -> None:
        super().__init__(f"E2 is {state}")
        self.state = state
        self.code = "runtime_busy"


class LiveCallActive(Exception):
    """Desk leftover hop holds the single engine; lab generate must wait."""

    def __init__(self, remaining_s: float) -> None:
        super().__init__("live call owns the engine")
        self.code = "live_call_active"
        self.state = "ready"
        self.remaining_s = float(remaining_s)


class InsufficientVram(Exception):
    def __init__(self, *, required_bytes: int, free_bytes: int) -> None:
        shortfall = max(0, required_bytes - free_bytes)
        super().__init__(
            f"insufficient VRAM: {free_bytes} free, {required_bytes} required"
        )
        self.code = "insufficient_vram"
        self.required_bytes = required_bytes
        self.free_bytes = free_bytes
        self.shortfall_bytes = shortfall

    def to_info(self) -> RuntimeErrorInfo:
        return RuntimeErrorInfo(
            code=self.code,
            message=str(self),
            required_bytes=self.required_bytes,
            free_bytes=self.free_bytes,
            shortfall_bytes=self.shortfall_bytes,
        )


@dataclass
class RuntimeStatus:
    state: RuntimeState
    gpu_index: int = 0
    worker_pid: int | None = None
    required_vram_bytes: int = 0
    vram_margin_bytes: int = 0
    free_vram_bytes: int | None = None
    used_vram_bytes: int | None = None
    last_error: RuntimeErrorInfo | None = None
    loaded_at: datetime | None = None
    model_revision: str | None = None
    leftover_parked: bool = False
    selected: str = SELECTED_RUNTIME
    display_name: str = DISPLAY_NAME
    engine: str = ENGINE_ID
    implementation: str = "breeze-tts-2-e2"
    not_a_pin_swap: bool = True
    voicecat_path: bool = True
    load_phase: str | None = None
    load_started_at: datetime | None = None
    load_elapsed_s: float | None = None
    live_call_active: bool = False
    live_call_remaining_s: float = 0.0
    live_call_holder: str | None = None
    processor: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "gpu_index": self.gpu_index,
            "worker_pid": self.worker_pid,
            "required_vram_bytes": self.required_vram_bytes,
            "vram_margin_bytes": self.vram_margin_bytes,
            "free_vram_bytes": self.free_vram_bytes,
            "used_vram_bytes": self.used_vram_bytes,
            "last_error": None if self.last_error is None else self.last_error.to_dict(),
            "loaded_at": None if self.loaded_at is None else self.loaded_at.isoformat(),
            "model_revision": self.model_revision,
            "leftover_parked": self.leftover_parked,
            "selected": self.selected,
            "display_name": self.display_name,
            "engine": self.engine,
            "implementation": self.implementation,
            "not_a_pin_swap": self.not_a_pin_swap,
            "voicecat_path": self.voicecat_path,
            "status": self.state,
            "load_phase": self.load_phase,
            "load_started_at": None if self.load_started_at is None else self.load_started_at.isoformat(),
            "load_elapsed_s": self.load_elapsed_s,
            "live_call_active": self.live_call_active,
            "live_call_remaining_s": self.live_call_remaining_s,
            "live_call_holder": self.live_call_holder,
            "processor": self.processor,
        }
