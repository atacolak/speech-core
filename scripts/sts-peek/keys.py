#!/usr/bin/env python3
"""Keyboard input for sts-peek UI.

Mirrors speech-out-live-session / assistant_self_asr_tui conventions:
prefer /dev/tty, fall back to stdin when it is a TTY, else disabled.

Key map (Track U):
  b / space  barge
  u          user-stop (optional)
  h          human-mode toggle signal
  q / Ctrl-C quit
"""
from __future__ import annotations

import os
import select
import sys
import termios
import tty
from dataclasses import dataclass
from enum import Enum
from typing import Any, TextIO


class KeyAction(str, Enum):
    BARGE = "barge"
    USER_STOP = "user_stop"
    HUMAN_TOGGLE = "human_toggle"
    QUIT = "quit"
    NONE = "none"


@dataclass(frozen=True)
class KeyEvent:
    action: KeyAction
    raw: str


def resolve_tty() -> TextIO | None:
    """Prefer /dev/tty like speech-out-live-session keyboard_loop."""
    try:
        if os.path.exists("/dev/tty") and os.access("/dev/tty", os.R_OK):
            fd = open("/dev/tty", "r")  # noqa: SIM115 — kept open for session
            try:
                termios.tcgetattr(fd.fileno())
                return fd
            except termios.error:
                fd.close()
    except OSError:
        pass
    if sys.stdin.isatty():
        return sys.stdin
    return None


def map_key(ch: str) -> KeyAction:
    if ch in ("b", "B", " "):
        return KeyAction.BARGE
    if ch in ("u", "U"):
        return KeyAction.USER_STOP
    if ch in ("h", "H"):
        return KeyAction.HUMAN_TOGGLE
    if ch in ("q", "Q", "\x03"):
        return KeyAction.QUIT
    return KeyAction.NONE


class RawTTY:
    """cbreak-mode key reader with poll_key(timeout)."""

    def __init__(self, stream: TextIO) -> None:
        self.stream = stream
        self.fd = stream.fileno()
        self._saved: list[Any] | None = None

    def __enter__(self) -> "RawTTY":
        try:
            self._saved = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
        except termios.error:
            self._saved = None
        return self

    def __exit__(self, *exc: object) -> None:
        if self._saved is not None:
            try:
                termios.tcsetattr(self.fd, termios.TCSADRAIN, self._saved)
            except termios.error:
                pass

    def poll_key(self, timeout_s: float = 0.0) -> KeyEvent | None:
        try:
            ready, _, _ = select.select([self.fd], [], [], timeout_s)
        except (ValueError, OSError):
            return None
        if not ready:
            return None
        try:
            ch_b = os.read(self.fd, 1)
        except OSError:
            return None
        if not ch_b:
            return None
        ch = ch_b.decode("utf-8", errors="ignore")
        return KeyEvent(action=map_key(ch), raw=ch)


class ScriptedKeys:
    """Deterministic key source for offline tests.

    Script tokens (comma-separated):
      b / space / u / h / q   → key actions
      sleep:N                 → wait N seconds (float)
      wait:N                  → alias for sleep:N
    """

    def __init__(self, script: str) -> None:
        self._tokens = [t.strip() for t in script.split(",") if t.strip()]
        self._i = 0
        self._sleep_left = 0.0

    def poll_key(self, timeout_s: float = 0.0) -> KeyEvent | None:
        import time

        if self._sleep_left > 0:
            step = min(timeout_s, self._sleep_left)
            time.sleep(step)
            self._sleep_left -= step
            return None

        if self._i >= len(self._tokens):
            if timeout_s > 0:
                time.sleep(timeout_s)
            return None

        tok = self._tokens[self._i]
        self._i += 1
        low = tok.lower()
        if low.startswith("sleep:") or low.startswith("wait:"):
            try:
                self._sleep_left = float(tok.split(":", 1)[1])
            except ValueError:
                self._sleep_left = 0.0
            return self.poll_key(timeout_s)

        # Accept literal "space"
        if low == "space":
            ch = " "
        else:
            ch = tok[:1]
        return KeyEvent(action=map_key(ch), raw=ch)

    @property
    def exhausted(self) -> bool:
        return self._i >= len(self._tokens) and self._sleep_left <= 0


def keys_help_line() -> str:
    return "keys: [b/space]=barge  [u]=user-stop  [h]=human-mode  [q]=quit"
