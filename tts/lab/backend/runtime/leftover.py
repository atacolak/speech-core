"""Leftover live-mouth park/restore. Injectable; tests use Noop."""

from __future__ import annotations

from typing import Protocol


class LeftoverController(Protocol):
    def park(self) -> None: ...
    def restore(self) -> None: ...
    def is_parked(self) -> bool: ...


class NoopLeftover:
    def __init__(self) -> None:
        self._parked = False
        self.park_calls = 0
        self.restore_calls = 0

    def park(self) -> None:
        self.park_calls += 1
        self._parked = True

    def restore(self) -> None:
        self.restore_calls += 1
        self._parked = False

    def is_parked(self) -> bool:
        return self._parked


class SystemdLeftover:
    def park(self) -> None:
        from breeze_tts_qual.park_leftover import park

        park()

    def restore(self) -> None:
        from breeze_tts_qual.park_leftover import restore

        restore()

    def is_parked(self) -> bool:
        from breeze_tts_qual.park_leftover import UNIT, _systemctl

        return _systemctl("is-active", UNIT).stdout.strip() != "active"
