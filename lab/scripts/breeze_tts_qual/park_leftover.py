"""Park parked-qwen rollback unit for exclusive VRAM benches.

ata-speech-tts.service is qwentts.cpp rollback, not the live synthesizer.
Live GPU occupant is breeze-tts-2 E2. restore() starts qwen — do not
call it while E2 is loaded. Mouth daemon must not Wants= this unit.
"""

from __future__ import annotations

import subprocess

UNIT = "ata-speech-tts.service"


def _systemctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["systemctl", "--user", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def park() -> None:
    result = _systemctl("is-active", UNIT)
    if result.stdout.strip() == "active":
        _systemctl("stop", UNIT)


def restore() -> None:
    _systemctl("start", UNIT)
