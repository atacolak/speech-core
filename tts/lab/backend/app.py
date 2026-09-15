"""FastAPI lab application. not on the voicecat path."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from tts.lab.backend.dependencies import create_state
from tts.lab.backend.runtime.auk import AukRuntimeManager
from tts.lab.backend.runtime.manager import E2RuntimeManager
from tts.lab.backend.routes import (
    auk,
    leftover,
    plan,
    progressive,
    runtime,
    runs,
    sources,
    synthesis,
    voices,
)
from tts.lab.backend.web import mount_web


def create_app(
    *,
    root: Path | str | None = None,
    engine: Any | None = None,
    leftover_parked: bool = False,
    web_dist: Path | str | None = None,
    e2_runtime: E2RuntimeManager | None = None,
    auk_runtime: AukRuntimeManager | None = None,
    seed_samples: bool = False,
) -> FastAPI:
    app = FastAPI(title="speech-core tts lab", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.lab = create_state(
        root,
        engine=engine,
        leftover_parked=leftover_parked,
        runtime=e2_runtime,
        auk=auk_runtime,
    )
    app.include_router(plan.router)
    app.include_router(runtime.router)
    app.include_router(voices.router)
    app.include_router(sources.router)
    app.include_router(auk.router)
    app.include_router(leftover.router)
    app.include_router(synthesis.router)
    app.include_router(progressive.router)
    app.include_router(runs.router)
    if seed_samples:
        from tts.lab.backend.services.samples import ensure_sample_voices

        ensure_sample_voices(app.state.lab.store)
    mount_web(app, web_dist)
    return app


app = create_app()
