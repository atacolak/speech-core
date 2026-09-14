"""Lab+desk shared session. not on the voicecat path.

Persists the active voice profile so /lab and /desk show the same
selection. Does not load GPU weights. Does not touch the leftover mouth.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SESSION_NAME = "session.json"


def _path(root: Path | str) -> Path:
    return Path(root) / SESSION_NAME


def read_session(root: Path | str) -> dict[str, Any]:
    path = _path(root)
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def read_active_voice_id(root: Path | str) -> str | None:
    value = read_session(root).get("active_voice_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def write_active_voice_id(root: Path | str, voice_id: str | None) -> str | None:
    path = _path(root)
    payload = read_session(root)
    cleaned = voice_id.strip() if isinstance(voice_id, str) and voice_id.strip() else None
    if cleaned is None:
        payload.pop("active_voice_id", None)
    else:
        payload["active_voice_id"] = cleaned
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return cleaned
