"""FastAPI lab application. not on the voicecat path."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI

from tts.lab.backend.dependencies import create_state
from tts.lab.backend.routes import runtime, runs, synthesis, voices


def create_app(
    *,
    root: Path | str | None = None,
    engine: Any | None = None,
    leftover_parked: bool = True,
) -> FastAPI:
    app = FastAPI(title="speech-core tts lab", version="0.1.0")
    app.state.lab = create_state(root, engine=engine, leftover_parked=leftover_parked)
    app.include_router(runtime.router)
    app.include_router(voices.router)
    app.include_router(synthesis.router)
    app.include_router(runs.router)
    return app


app = create_app()
