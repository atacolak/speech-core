"""Serve the React lab shell from FastAPI. not on the voicecat path."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

DEFAULT_WEB_DIST = Path(__file__).resolve().parents[1] / "web" / "dist"

SHELL_PATHS = ("/", "/desk", "/generate", "/conversations", "/lab")


def mount_web(app: FastAPI, dist: Path | str | None = None) -> None:
    root = Path(dist) if dist is not None else DEFAULT_WEB_DIST
    if not (root / "index.html").is_file():
        return
    assets = root / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="web-assets")

    def index() -> FileResponse:
        return FileResponse(
            root / "index.html",
            headers={"Cache-Control": "no-store, max-age=0"},
        )

    for path in SHELL_PATHS:
        app.add_api_route(path, index, methods=["GET"], include_in_schema=False)
        if path != "/":
            app.add_api_route(f"{path}/", index, methods=["GET"], include_in_schema=False)
