"""FastAPI dependencies. not on the voicecat path."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import Request

from tts.lab.backend.runtime.auk import AukRuntimeManager
from tts.lab.backend.runtime.leftover import NoopLeftover
from tts.lab.backend.runtime.manager import E2RuntimeManager
from tts.lab.backend.runtime.processors import ProcessorLease
from tts.lab.backend.store.artifacts import ArtifactStore
from tts.paths import lab_root


@dataclass
class LabState:
    store: ArtifactStore
    runtime: E2RuntimeManager
    auk: AukRuntimeManager
    engine: Any | None = None
    leftover_parked: bool = False


def create_state(
    root: Path | str | None = None,
    *,
    engine: Any | None = None,
    leftover_parked: bool = False,
    runtime: E2RuntimeManager | None = None,
    auk: AukRuntimeManager | None = None,
) -> LabState:
    runtime = runtime or E2RuntimeManager(leftover=NoopLeftover())
    return LabState(
        store=ArtifactStore(Path(root) if root is not None else lab_root()),
        runtime=runtime,
        auk=auk or AukRuntimeManager(lease=ProcessorLease(runtime)),
        engine=engine,
        leftover_parked=leftover_parked,
    )


def get_state(request: Request) -> LabState:
    return request.app.state.lab
