"""not on the voicecat path.

Stop/start leftover ata-speech-tts.service for exclusive VRAM benches.
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
