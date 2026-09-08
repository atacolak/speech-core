"""FastAPI dependencies. not on the voicecat path."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import Request

from tts.lab.backend.store.artifacts import ArtifactStore
from tts.paths import lab_root


@dataclass
class LabState:
    store: ArtifactStore
    engine: Any | None = None
    leftover_parked: bool = True


def create_state(
    root: Path | str | None = None,
    *,
    engine: Any | None = None,
    leftover_parked: bool = True,
) -> LabState:
    return LabState(
        store=ArtifactStore(Path(root) if root is not None else lab_root()),
        engine=engine,
        leftover_parked=leftover_parked,
    )


def get_state(request: Request) -> LabState:
    return request.app.state.lab
