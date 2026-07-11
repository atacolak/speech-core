#!/usr/bin/env python3
"""Minimum viable interactive peek TUI for assistant self-ASR eval (eval_only).

Inspired by speech-out-live-session keyboard_loop (/dev/tty) + debug timeline.

What you see
------------
  - Assistant intended stream progressing (mock clock, optional --play TTS)
  - Dual transcripts (assistant / user-role)
  - Barge-in fire moment, drain window, production cut (drain vs fallback)
  - Truncated assistant commit line

Keys (on /dev/tty when available)
---------------------------------
  b / space  barge-in now (operator initiates; same semantic as first alnum token)
  u          finish user-role speech / transcript_committed early
  f          force fallback cut at finalize
  q / Ctrl-C quit

Barge-in semantics (contract / live-session)
-------------------------------------------
  Production barge-in fires on Nemotron-transcribed user audio (first
  alphanumeric token), not on VAD dirt/echo. This TUI mimics that path:
  operator keypress ≈ first barge token; optional auto-delay still available.
  Acoustic echo is a separate later suite — not required for this eval claim.

Optional --play
---------------
  When speech-out is up, may shell out to `speech-out say` / SPEECH_OUT_PLAY_CMD
  for assistant and user-role TTS. If play fails, TUI continues in mock-only.

Offline default needs no daemons.
"""
from __future__ import annotations

import os
import select
import shutil
import subprocess
import sys
import termios
import time
import tty
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from assistant_self_asr_cut import (
    DEFAULT_PAD_WORDS,
    mock_alignment_clock,
    mock_drain_during_user_speech,
    normalize_whitespace,
    score_three_way,
    tokenize_words,
)
from assistant_self_asr_tts_eval import (
    DEFAULT_USER_BARGE_TEXT,
    DualClockConfig,
    DualClockResult,
    run_dual_clock_tts_eval,
)

# ANSI helpers (no curses dependency for MVP)
_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_CYAN = "\033[36m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_MAGENTA = "\033[35m"
_RED = "\033[31m"
_CLEAR = "\033[2J\033[H"
_HIDE = "\033[?25l"
_SHOW = "\033[?25h"


@dataclass
class TuiConfig:
    intended_text: str
    user_barge_text: str = DEFAULT_USER_BARGE_TEXT
    pad_words: int = DEFAULT_PAD_WORDS
    assistant_words_per_second: float = 3.0
    drain_words_per_second: float = 12.0
    playback_lag_words: int = 1
    auto_barge_ms: float | None = None  # None = wait for keypress only
    user_speech_ms: float = 1500.0
    tick_ms: float = 50.0
    play: bool = False
    play_command: str | None = None  # default: speech-out say --backend mock
    force_fallback: bool = False
    utterance_id: str = "eval-tui-1"
    out_dir: Path | None = None


def _resolve_tty() -> TextIO | None:
    """Prefer /dev/tty like speech-out-live-session keyboard_loop."""
    try:
        if os.path.exists("/dev/tty") and os.access("/dev/tty", os.R_OK):
            fd = open("/dev/tty", "r")  # noqa: SIM115 — kept open for session
            # Probe stty-capable
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


class _RawTTY:
    def __init__(self, stream: TextIO) -> None:
        self.stream = stream
        self.fd = stream.fileno()
        self._saved: list[Any] | None = None

    def __enter__(self) -> "_RawTTY":
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

    def poll_key(self, timeout_s: float = 0.0) -> str | None:
        try:
            r, _, _ = select.select([self.fd], [], [], timeout_s)
        except (ValueError, OSError):
            return None
        if not r:
            return None
        try:
            ch = os.read(self.fd, 1)
        except OSError:
            return None
        if not ch:
            return None
        return ch.decode("utf-8", errors="ignore")


def _play_tts(text: str, *, role: str, play_command: str | None) -> subprocess.Popen[Any] | None:
    """Best-effort non-blocking TTS. Returns Popen or None."""
    cmd_str = play_command or os.environ.get("SPEECH_OUT_PLAY_CMD")
    if cmd_str:
        # Shell form allows full speech-out play lines.
        try:
            return subprocess.Popen(
                cmd_str,
                shell=True,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
            )
        except OSError:
            return None

    # Default: speech-out say mock (JSON only, no audio device) — proves wiring.
    binary = shutil.which("speech-out")
    if binary is None:
        # Try cargo workspace binary path (optional).
        return None
    try:
        return subprocess.Popen(
            [binary, "say", "--backend", "mock", text],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return None


def _kill(proc: subprocess.Popen[Any] | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        try:
            proc.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            proc.kill()
    except OSError:
        pass


def _prefix_words(text: str, n: int) -> str:
    words = tokenize_words(text)
    if n <= 0:
        return ""
    return " ".join(words[: min(n, len(words))])


def _render(
    *,
    phase: str,
    elapsed_ms: float,
    intended: str,
    user_barge: str,
    assistant_pos: int,
    playback_pos: int,
    user_pos: int,
    drain_pos: int,
    drain_complete: bool | None,
    barge_ms: float | None,
    cut_source: str | None,
    production_cut: str | None,
    intended_at_playback: str | None,
    force_fallback: bool,
    play: bool,
    keys_help: str,
    note: str,
) -> str:
    lines = [
        f"{_CLEAR}{_HIDE}{_BOLD}assistant-self-asr interactive peek{_RESET}  "
        f"{_DIM}label=eval_only  mode=tui{_RESET}",
        f"{_DIM}barge-in = Nemotron-transcribed first alnum token path "
        f"(keypress mimics token; not VAD/echo){_RESET}",
        "",
        f"  phase: {_CYAN}{_BOLD}{phase}{_RESET}   t={elapsed_ms:7.0f} ms   "
        f"play={'on' if play else 'mock-only'}"
        + (f"   {_YELLOW}force_fallback{_RESET}" if force_fallback else ""),
        "",
        f"  {_MAGENTA}{_BOLD}assistant{_RESET} (intended / Nemotron-B stream)",
        f"    emit@  [{assistant_pos:3d}] {_prefix_words(intended, assistant_pos)}",
        f"    play@  [{playback_pos:3d}] {_prefix_words(intended, playback_pos)}",
        f"    drain@ [{drain_pos:3d}] {_prefix_words(intended, drain_pos)}"
        + (
            f"  {_GREEN}complete{_RESET}"
            if drain_complete is True
            else (
                f"  {_YELLOW}incomplete{_RESET}"
                if drain_complete is False
                else ""
            )
        ),
        "",
        f"  {_CYAN}{_BOLD}user-role{_RESET} (TTS barge / Nemotron-A shaped)",
        f"    text: {user_barge!r}",
        f"    asr@  [{user_pos:3d}] {_prefix_words(user_barge, user_pos)}",
        "",
        f"  barge-in @ {barge_ms:.0f} ms" if barge_ms is not None else "  barge-in: (waiting — press b/space)",
        "",
    ]
    if production_cut is not None:
        src_col = _GREEN if cut_source == "drain" else _YELLOW
        lines += [
            f"  {_BOLD}production cut{_RESET}  source={src_col}{cut_source}{_RESET}",
            f"    cut:   {production_cut!r}",
            f"    heard: {intended_at_playback!r}",
            f"    commit: assistant_turn_truncated_eval_only (immutable)",
            "",
        ]
    lines += [
        f"  {_DIM}{keys_help}{_RESET}",
        f"  {_DIM}{note}{_RESET}",
    ]
    return "\n".join(lines) + "\n"


def run_interactive_tui(cfg: TuiConfig) -> DualClockResult | None:
    """Run MVP interactive peek. Returns DualClockResult after finalize, or None if quit early."""
    intended = normalize_whitespace(cfg.intended_text)
    user_barge = normalize_whitespace(cfg.user_barge_text)
    if not intended:
        print("error: intended text empty", file=sys.stderr)
        return None

    tty_stream = _resolve_tty()
    no_tty = tty_stream is None
    if no_tty and cfg.auto_barge_ms is None:
        # Non-interactive environments: auto-barge so the mode still produces artifacts.
        auto_barge_ms = 1200.0
        note_auto = "no tty — auto barge @ 1200 ms (set --auto-barge-ms to override)"
    else:
        auto_barge_ms = cfg.auto_barge_ms
        note_auto = "press b/space to barge-in (or wait for --auto-barge-ms)"

    keys_help = "keys: [b/space]=barge  [u]=user-stop  [f]=force-fallback  [q]=quit"
    if no_tty:
        keys_help = "keys: disabled (no /dev/tty) — auto timeline"

    phase = "assistant_speaking"
    t0 = time.monotonic()
    barge_ms: float | None = None
    user_stop_ms: float | None = None
    force_fallback = cfg.force_fallback
    assistant_play: subprocess.Popen[Any] | None = None
    user_play: subprocess.Popen[Any] | None = None
    result: DualClockResult | None = None
    quit_early = False

    if cfg.play:
        assistant_play = _play_tts(
            intended, role="assistant", play_command=cfg.play_command
        )

    def elapsed() -> float:
        return (time.monotonic() - t0) * 1000.0

    def positions(now_ms: float) -> tuple[int, int, int, int, bool | None]:
        if barge_ms is None:
            emit = mock_alignment_clock(
                intended,
                words_per_second=cfg.assistant_words_per_second,
                elapsed_ms=now_ms,
            )
            play = max(0, emit - max(0, cfg.playback_lag_words))
            return emit, play, 0, 0, None
        # Frozen emit at barge; drain during user speech
        emit = mock_alignment_clock(
            intended,
            words_per_second=cfg.assistant_words_per_second,
            elapsed_ms=barge_ms,
        )
        play = max(0, emit - max(0, cfg.playback_lag_words))
        user_elapsed = now_ms - barge_ms
        user_wps = max(
            1.0, len(tokenize_words(user_barge)) / max(0.001, cfg.user_speech_ms / 1000.0)
        )
        user_pos = mock_alignment_clock(
            user_barge, words_per_second=user_wps, elapsed_ms=user_elapsed
        )
        drained, dpos, complete = mock_drain_during_user_speech(
            intended,
            emitted_pos_words_at_pause=emit,
            drain_words_per_second=cfg.drain_words_per_second,
            user_speech_ms=user_elapsed,
        )
        return emit, play, user_pos, dpos, complete

    def do_barge(now_ms: float) -> None:
        nonlocal phase, barge_ms, assistant_play, user_play
        if barge_ms is not None:
            return
        barge_ms = now_ms
        phase = "user_speaking_drain"
        _kill(assistant_play)
        assistant_play = None
        if cfg.play:
            user_play = _play_tts(
                user_barge, role="user", play_command=cfg.play_command
            )

    def do_finalize(now_ms: float) -> DualClockResult:
        nonlocal phase, user_stop_ms, result, user_play
        assert barge_ms is not None
        user_stop_ms = now_ms
        phase = "finalized"
        _kill(user_play)
        user_play = None
        speech_ms = max(0.0, user_stop_ms - barge_ms)
        dcfg = DualClockConfig(
            intended_text=intended,
            user_barge_text=user_barge,
            pad_words=cfg.pad_words,
            assistant_words_per_second=cfg.assistant_words_per_second,
            playback_lag_words=cfg.playback_lag_words,
            barge_in_delay_ms=barge_ms,
            user_speech_ms=speech_ms,
            drain_words_per_second=cfg.drain_words_per_second,
            force_fallback=force_fallback,
            utterance_id=cfg.utterance_id,
        )
        result = run_dual_clock_tts_eval(dcfg)
        # Annotate interactive provenance
        result.metrics["mode"] = "tui"
        result.metrics["interactive"] = {
            "barge_trigger": "keypress_or_auto_delay",
            "barge_semantics": "nemotron_first_alnum_token_path",
            "not_vad_echo": True,
            "play_attempted": cfg.play,
            "auto_barge_ms": auto_barge_ms,
            "note_auto": note_auto,
        }
        if cfg.out_dir is not None:
            result.write_artifacts(cfg.out_dir)
        return result

    raw_ctx = _RawTTY(tty_stream) if tty_stream is not None else None
    try:
        if raw_ctx is not None:
            raw_ctx.__enter__()
        sys.stdout.write(_HIDE)
        sys.stdout.flush()

        while True:
            now = elapsed()
            # Auto barge
            if (
                barge_ms is None
                and auto_barge_ms is not None
                and now >= auto_barge_ms
            ):
                do_barge(now)

            # Auto user-stop after user_speech_ms
            if (
                barge_ms is not None
                and user_stop_ms is None
                and now >= barge_ms + cfg.user_speech_ms
            ):
                do_finalize(now)

            emit, play, user_pos, drain_pos, drain_complete = positions(elapsed())
            cut_source = result.primary_cut_source if result else None
            production_cut = result.production_cut_text if result else None
            iap = result.intended_at_playback if result else None

            frame = _render(
                phase=phase,
                elapsed_ms=elapsed(),
                intended=intended,
                user_barge=user_barge,
                assistant_pos=emit,
                playback_pos=play,
                user_pos=user_pos,
                drain_pos=drain_pos,
                drain_complete=drain_complete if barge_ms is not None else None,
                barge_ms=barge_ms,
                cut_source=cut_source,
                production_cut=production_cut,
                intended_at_playback=iap,
                force_fallback=force_fallback,
                play=cfg.play,
                keys_help=keys_help,
                note=note_auto
                if barge_ms is None
                else (
                    f"primary_cut_source={cut_source}  artifacts={cfg.out_dir}"
                    if result
                    else "draining Nemotron-B during user speech window…"
                ),
            )
            sys.stdout.write(frame)
            sys.stdout.flush()

            if result is not None:
                # Hold final frame briefly so operator can read cut.
                if raw_ctx is not None:
                    end_wait = time.monotonic() + 2.5
                    while time.monotonic() < end_wait:
                        k = raw_ctx.poll_key(0.1)
                        if k in ("q", "Q", "\x03"):
                            break
                break

            timeout = cfg.tick_ms / 1000.0
            key = raw_ctx.poll_key(timeout) if raw_ctx is not None else None
            if raw_ctx is None:
                time.sleep(timeout)

            if key is None:
                continue
            if key in ("q", "Q", "\x03"):
                quit_early = True
                break
            if key in ("b", "B", " "):
                do_barge(elapsed())
            elif key in ("u", "U"):
                if barge_ms is None:
                    do_barge(elapsed())
                if user_stop_ms is None and barge_ms is not None:
                    do_finalize(elapsed())
            elif key in ("f", "F"):
                force_fallback = not force_fallback
    finally:
        _kill(assistant_play)
        _kill(user_play)
        if raw_ctx is not None:
            raw_ctx.__exit__(None, None, None)
        sys.stdout.write(_SHOW + _RESET + "\n")
        sys.stdout.flush()
        if tty_stream is not None and tty_stream is not sys.stdin:
            try:
                tty_stream.close()
            except OSError:
                pass

    if quit_early and result is None:
        print("tui: quit before finalize (no metrics written)")
        return None

    if result is not None:
        print(
            f"tui: done  primary_cut_source={result.primary_cut_source}  "
            f"cut={result.production_cut_text!r}"
        )
        if cfg.out_dir is not None:
            print(f"tui: artifacts → {cfg.out_dir}")
    return result


def run_self_check() -> None:
    """Non-interactive auto-timeline sanity check."""
    cfg = TuiConfig(
        intended_text="one two three four five six seven eight nine ten",
        user_barge_text="wait stop",
        pad_words=2,
        assistant_words_per_second=3.0,
        drain_words_per_second=20.0,
        playback_lag_words=1,
        auto_barge_ms=1000.0,
        user_speech_ms=800.0,
        tick_ms=20.0,
        play=False,
        out_dir=None,
    )
    # Force no-tty path by not requiring display — call dual clock directly
    # for pure unit check; interactive loop is covered by harness --mode tui
    # with auto barge under redirected stdin.
    dcfg = DualClockConfig(
        intended_text=cfg.intended_text,
        user_barge_text=cfg.user_barge_text,
        pad_words=cfg.pad_words,
        assistant_words_per_second=cfg.assistant_words_per_second,
        barge_in_delay_ms=1000.0,
        user_speech_ms=800.0,
        drain_words_per_second=20.0,
        playback_lag_words=1,
    )
    r = run_dual_clock_tts_eval(dcfg)
    assert r.primary_cut_source == "drain"
    scored = score_three_way(
        cfg.intended_text,
        playback_pos_words=r.playback_pos_at_pause,
        nemotron_pos_words=r.drained_pos_words,
        asr_recovered_at_stop=r.drained_text,
        pad_words=2,
        drain_complete=True,
        drained_asr=r.drained_text,
    )
    assert scored.prefix_valid is True


if __name__ == "__main__":
    run_self_check()
    print("assistant_self_asr_tui self-check: PASS")
