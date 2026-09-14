"""not on the voicecat path.

Filesystem roots for the lab TTS suite.

Durable pin + saved lab artifacts live under XDG data
(~/.local/share/speech-out), not ~/.cache. lab/cache and lab/tmp are
derived rebuildable files and may be deleted anytime.
"""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TTS_ROOT = Path(__file__).resolve().parent
_DATA_HOME = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
DEFAULT_QUAL_ROOT = _DATA_HOME / "speech-out" / "breeze-tts-2-e2"
DEFAULT_LAB_ROOT = _DATA_HOME / "speech-out" / "tts-lab"
DEFAULT_AUK_PIN_ROOT = _DATA_HOME / "speech-out" / "auk-base"

SELECTED_RUNTIME = "E2"
DISPLAY_NAME = "Breeze TTS2"
ENGINE_ID = "breeze-tts2"
BREEZE_IMPLEMENTATION = "breeze-tts-2-e2"
BREEZE_PIN_COMMIT = "43e2ea1595297c4059477e2e4a300653761c759b"


def qual_root() -> Path:
    return Path(os.environ.get("QUAL_ROOT", str(DEFAULT_QUAL_ROOT))).expanduser()


def lab_root() -> Path:
    return Path(os.environ.get("TTS_LAB_ROOT", str(DEFAULT_LAB_ROOT))).expanduser()


def auk_pin_root() -> Path:
    """AuK weights, venv and assets. Never inside the repo: they are not source."""
    return Path(os.environ.get("AUK_PIN_ROOT", str(DEFAULT_AUK_PIN_ROOT))).expanduser()


def auk_venv_python() -> Path:
    return auk_pin_root() / "venv" / "bin" / "python"


def ensure_lab_dirs(root: Path | None = None) -> Path:
    base = Path(root) if root is not None else lab_root()
    for name in (
        "voices",
        "delivery_profiles",
        "references",
        "generations",
        "lexicon",
        "runs",
        "cache",
        "tmp",
    ):
        (base / name).mkdir(parents=True, exist_ok=True)
    return base
