"""Runtime readiness. not on the voicecat path."""

from __future__ import annotations

from fastapi import APIRouter, Request

from tts.paths import BREEZE_IMPLEMENTATION, BREEZE_PIN_COMMIT, SELECTED_RUNTIME, qual_root

router = APIRouter()


@router.get("/api/runtime")
def runtime(request: Request) -> dict[str, object]:
    state = request.app.state.lab
    ready = state.engine is not None
    return {
        "selected": SELECTED_RUNTIME,
        "implementation": BREEZE_IMPLEMENTATION,
        "pin_commit": BREEZE_PIN_COMMIT,
        "qual_root": str(qual_root()),
        "status": "ready" if ready else "loading",
        "leftover_parked": bool(state.leftover_parked),
        "not_a_pin_swap": True,
        "voicecat_path": False,
    }
