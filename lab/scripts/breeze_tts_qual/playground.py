#!/usr/bin/env python3
"""not on the voicecat path.

Compatibility shim. The TTS laboratory lives in tts/playground.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from breeze_tts_qual import park_leftover  # noqa: E402
from tts.playground.app import (  # noqa: E402
    DEFAULT_PRESET,
    PlaygroundSession,
    _default_engine_factory,
    build_interface,
    launch_kwargs,
    main,
    parse_args,
    transcribe_audio,
)

__all__ = [
    "DEFAULT_PRESET",
    "PlaygroundSession",
    "_default_engine_factory",
    "build_interface",
    "launch_kwargs",
    "main",
    "parse_args",
    "park_leftover",
    "transcribe_audio",
]


if __name__ == "__main__":
    raise SystemExit(main())
