#!/usr/bin/env python3
"""sts-peek live observability UI (Track U).

Composes speech-core-watch for real energy/VAD/smart-turn glyphs and overlays
harness status from a Track L run_dir. Does not reimplement VAD bars.

Offline --mock mode fakes meter lines + key handling so tests pass without
daemons.

Contract with Track L (run_dir layout, best-effort poll):
  run_dir/
    intended.txt                 assistant intended text (optional)
    control/
      barge.now                  touch to request barge/cancel (UI writes)
      human.mode                 "1" or "0" human-mode flag (UI writes)
      user_stop.now              touch for user-stop signal (UI writes)
      state.txt                  optional harness phase string
    cut/
      metrics.json               primary_cut_source, production cut fields
      production_cut_text        cut text (optional plain file)
      commit.json                optional
    watch.jsonl                  optional daemon event log (jsonl attach)
    ui-events.jsonl              optional harness events

On barge key: touch run_dir/control/barge.now so the audio track cancels.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

# Local package imports (scripts/sts-peek/)
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from keys import (  # noqa: E402
    KeyAction,
    KeyEvent,
    RawTTY,
    ScriptedKeys,
    keys_help_line,
    resolve_tty,
)

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

# Glyphs aligned with speech-core-watch debug TUI (compose, don't invent new ones).
_GLYPH_SPEECH = "◖"
_GLYPH_PAUSE = "◗"
_GLYPH_CLOSE = "◆"
_GLYPH_WAIT = "·"
_GLYPH_FALLBACK = "◇"
_GLYPH_SEM = ("①", "②", "③", "④")
_GLYPH_BARGE = "✂"


@dataclass
class UiConfig:
    run_dir: Path | None = None
    mock: bool = False
    mock_seconds: float = 2.5
    tick_ms: float = 80.0
    # Live attach
    watch_bin: str | None = None
    watch_mode: str = "debug"
    stream_id: str | None = None
    stream_session_id: str | None = None
    ws_url: str | None = None
    replay_events: Path | None = None
    watch_jsonl: Path | None = None  # poll/tail run_dir/watch.jsonl or explicit
    # Key script for offline tests: "b,sleep:0.3,q"
    key_script: str | None = None
    # If set, auto-quit after N seconds (tests / non-interactive)
    max_seconds: float | None = None
    # When True, do not clear screen (easier for log capture)
    no_clear: bool = False
    # Quiet: write final summary only
    quiet: bool = False


@dataclass
class HarnessOverlay:
    """State polled from run_dir files (Track L / Track C)."""

    intended: str = ""
    phase: str = "idle"
    barge_touched: bool = False
    barge_ms: float | None = None
    human_mode: bool = False
    user_stop_touched: bool = False
    primary_cut_source: str | None = None
    production_cut: str | None = None
    drain_status: str | None = None
    metrics_raw: dict[str, Any] = field(default_factory=dict)
    control_state: str = ""
    last_error: str = ""


@dataclass
class MockMeters:
    """Fake energy/VAD/smart-turn lines for offline mode."""

    t0: float = field(default_factory=time.monotonic)
    speech: bool = True
    rms: float = 0.12
    energy: float = 0.35
    vad_prob: float = 0.62
    smart_turn_p: float | None = None
    smart_glyph: str = _GLYPH_WAIT
    frame: int = 0

    def tick(self, *, barged: bool, human_mode: bool) -> None:
        self.frame += 1
        elapsed = time.monotonic() - self.t0
        # Gentle oscillation so tests can assert presence of meter lines.
        phase = (elapsed * 2.4) % 1.0
        if barged:
            self.speech = phase < 0.55 or human_mode
            self.rms = 0.08 + 0.35 * phase
            self.energy = 0.20 + 0.50 * phase
            self.vad_prob = 0.40 + 0.45 * phase
            # After barge, surface a smart-turn probe glyph chain.
            step = int(elapsed * 3) % 5
            if step < 4:
                self.smart_glyph = _GLYPH_SEM[min(step, 3)]
                self.smart_turn_p = 0.35 + 0.12 * step
            else:
                self.smart_glyph = _GLYPH_CLOSE
                self.smart_turn_p = 0.91
        else:
            self.speech = True
            self.rms = 0.18 + 0.10 * phase
            self.energy = 0.40 + 0.20 * phase
            self.vad_prob = 0.70 + 0.15 * phase
            self.smart_glyph = _GLYPH_SPEECH if self.speech else _GLYPH_PAUSE
            self.smart_turn_p = None


def control_dir(run_dir: Path) -> Path:
    return run_dir / "control"


def cut_dir(run_dir: Path) -> Path:
    return run_dir / "cut"


def ensure_run_dir_layout(run_dir: Path) -> None:
    control_dir(run_dir).mkdir(parents=True, exist_ok=True)
    cut_dir(run_dir).mkdir(parents=True, exist_ok=True)


def touch_control(run_dir: Path, name: str, content: str | None = None) -> Path:
    ensure_run_dir_layout(run_dir)
    path = control_dir(run_dir) / name
    if content is None:
        # Atomic-ish touch: write then replace mtime.
        path.write_text(f"{time.time():.6f}\n", encoding="utf-8")
    else:
        path.write_text(content, encoding="utf-8")
    return path


def read_text(path: Path, default: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return default


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def poll_harness(run_dir: Path | None, overlay: HarnessOverlay) -> None:
    if run_dir is None:
        return
    # intended
    for cand in (
        run_dir / "intended.txt",
        run_dir / "assistant_intended.txt",
        run_dir / "control" / "intended.txt",
    ):
        if cand.is_file():
            overlay.intended = read_text(cand)
            break

    state = read_text(control_dir(run_dir) / "state.txt")
    if state:
        overlay.control_state = state
        overlay.phase = state

    barge = control_dir(run_dir) / "barge.now"
    if barge.is_file():
        overlay.barge_touched = True
        if overlay.barge_ms is None:
            # Best-effort: file mtime relative not available; leave caller-set.
            pass

    human = read_text(control_dir(run_dir) / "human.mode")
    if human in ("1", "true", "yes", "on"):
        overlay.human_mode = True
    elif human in ("0", "false", "no", "off"):
        overlay.human_mode = False

    if (control_dir(run_dir) / "user_stop.now").is_file():
        overlay.user_stop_touched = True

    # Cut / metrics (Track C may write these)
    metrics = None
    for cand in (
        cut_dir(run_dir) / "metrics.json",
        run_dir / "metrics.json",
    ):
        metrics = read_json(cand)
        if metrics:
            break
    if metrics:
        overlay.metrics_raw = metrics
        src = metrics.get("primary_cut_source")
        if isinstance(src, str):
            overlay.primary_cut_source = src
        cut_text = metrics.get("production_cut_text") or metrics.get("cut_text")
        if isinstance(cut_text, str):
            overlay.production_cut = cut_text
        drain = metrics.get("drain_status") or metrics.get("drain_complete")
        if drain is not None:
            overlay.drain_status = str(drain)

    for cand in (
        cut_dir(run_dir) / "production_cut_text",
        run_dir / "production_cut_text",
    ):
        if cand.is_file():
            txt = read_text(cand)
            if txt:
                overlay.production_cut = txt
            break


def _bar(value: float, width: int = 24, fill: str = "█", empty: str = "░") -> str:
    v = max(0.0, min(1.0, value))
    n = int(round(v * width))
    return fill * n + empty * (width - n)


def render_frame(
    *,
    cfg: UiConfig,
    overlay: HarnessOverlay,
    meters: MockMeters | None,
    watch_tail: str,
    elapsed_ms: float,
    keys_note: str,
    live_watch_running: bool,
) -> str:
    clear = "" if cfg.no_clear else _CLEAR
    mode = "mock" if cfg.mock else "live"
    lines: list[str] = [
        f"{clear}{_HIDE}{_BOLD}sts-peek observability{_RESET}  "
        f"{_DIM}track=U  mode={mode}  t={elapsed_ms:7.0f} ms{_RESET}",
        f"{_DIM}compose: speech-core-watch signals + run_dir harness overlay{_RESET}",
        "",
    ]

    # --- live meters (mock or reduced from watch) ---
    lines.append(f"  {_MAGENTA}{_BOLD}signals{_RESET}  (energy / RMS / VAD / smart-turn)")
    if meters is not None:
        speech_g = _GLYPH_SPEECH if meters.speech else _GLYPH_PAUSE
        st = (
            f"{meters.smart_glyph} p={meters.smart_turn_p:.2f}"
            if meters.smart_turn_p is not None
            else f"{meters.smart_glyph}"
        )
        lines += [
            f"    speech {speech_g}   rms={meters.rms:0.3f}  energy={meters.energy:0.3f}  "
            f"vad={meters.vad_prob:0.2f}",
            f"    energy [{_bar(meters.energy)}]  vad [{_bar(meters.vad_prob)}]",
            f"    smart-turn {st}   glyphs {_GLYPH_SPEECH}{_GLYPH_PAUSE}"
            f"{''.join(_GLYPH_SEM)}{_GLYPH_CLOSE}{_GLYPH_FALLBACK}",
        ]
    elif watch_tail:
        # Show last non-empty watch lines (speech-core-watch already renders glyphs).
        tail_lines = [ln for ln in watch_tail.splitlines() if ln.strip()][-12:]
        lines.append(f"    {_DIM}(from speech-core-watch --mode {cfg.watch_mode}){_RESET}")
        for ln in tail_lines:
            lines.append(f"    {ln}")
    else:
        status = "running" if live_watch_running else "not attached"
        lines.append(
            f"    {_YELLOW}waiting for speech-core-watch frames ({status})…{_RESET}"
        )

    lines.append("")

    # --- barge / control ---
    if overlay.barge_ms is not None:
        barge_line = (
            f"  {_RED}{_BOLD}{_GLYPH_BARGE} barge{_RESET} @ {overlay.barge_ms:.0f} ms"
        )
    elif overlay.barge_touched:
        barge_line = f"  {_RED}{_BOLD}{_GLYPH_BARGE} barge{_RESET} (control/barge.now present)"
    else:
        barge_line = f"  barge: {_DIM}(waiting — press b/space){_RESET}"
    lines.append(barge_line)

    human = "on" if overlay.human_mode else "off"
    lines.append(
        f"  human-mode: {_CYAN}{human}{_RESET}   phase: {_CYAN}{overlay.phase}{_RESET}"
        + (f"   state={overlay.control_state!r}" if overlay.control_state else "")
    )
    if overlay.user_stop_touched:
        lines.append(f"  user-stop: {_YELLOW}signaled{_RESET}")

    # --- intended / cut overlay ---
    lines.append("")
    if overlay.intended:
        preview = overlay.intended if len(overlay.intended) <= 80 else overlay.intended[:77] + "…"
        lines.append(f"  {_BOLD}intended{_RESET}: {preview}")
    else:
        lines.append(f"  {_DIM}intended: (no run_dir intended.txt yet){_RESET}")

    if overlay.primary_cut_source or overlay.production_cut or overlay.drain_status:
        src = overlay.primary_cut_source or "?"
        src_col = _GREEN if src == "drain" else _YELLOW
        lines.append(
            f"  {_BOLD}cut{_RESET}  source={src_col}{src}{_RESET}"
            + (f"  drain={overlay.drain_status}" if overlay.drain_status is not None else "")
        )
        if overlay.production_cut is not None:
            lines.append(f"    cut text: {overlay.production_cut!r}")
    else:
        lines.append(f"  {_DIM}cut: (no cut/metrics.json yet — Track C){_RESET}")

    lines.append("")
    run_s = str(cfg.run_dir) if cfg.run_dir else "(none)"
    lines.append(f"  {_DIM}run_dir: {run_s}{_RESET}")
    lines.append(f"  {_DIM}{keys_help_line()}{_RESET}")
    lines.append(f"  {_DIM}{keys_note}{_RESET}")
    return "\n".join(lines) + "\n"


def find_watch_bin(explicit: str | None) -> str | None:
    if explicit:
        p = Path(explicit)
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
        which = shutil.which(explicit)
        return which
    which = shutil.which("speech-core-watch")
    if which:
        return which
    # Workspace debug binary (compose path used by live-session scripts).
    repo = _HERE.parent.parent
    cand = repo / "target" / "debug" / "speech-core-watch"
    if cand.is_file() and os.access(cand, os.X_OK):
        return str(cand)
    return None


class WatchComposer:
    """Launch or attach speech-core-watch; capture recent stdout for overlay."""

    def __init__(self, cfg: UiConfig) -> None:
        self.cfg = cfg
        self.proc: subprocess.Popen[str] | None = None
        self._buf: list[str] = []
        self._max_lines = 40
        self.error: str = ""

    def start(self) -> bool:
        binary = find_watch_bin(self.cfg.watch_bin)
        if binary is None:
            self.error = "speech-core-watch not found on PATH or target/debug"
            return False

        args = [binary, "--mode", self.cfg.watch_mode]
        if self.cfg.stream_id:
            args += ["--stream-id", self.cfg.stream_id]
        if self.cfg.stream_session_id:
            args += ["--stream-session-id", self.cfg.stream_session_id]
        if self.cfg.ws_url:
            args += ["--url", self.cfg.ws_url]
        if self.cfg.replay_events is not None:
            args += ["--replay-events", str(self.cfg.replay_events)]
        elif self.cfg.watch_jsonl is not None and self.cfg.watch_jsonl.is_file():
            # Prefer replaying the session jsonl if present (offline attach).
            args += ["--replay-events", str(self.cfg.watch_jsonl)]

        # When neither replay nor live ws is desired beyond defaults, still launch
        # live subscribe — Track L run may already be feeding the daemon.
        try:
            self.proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            self.error = f"failed to launch watch: {exc}"
            return False
        return True

    def poll_output(self) -> str:
        """Non-blocking-ish drain of watch stdout into ring buffer."""
        if self.proc is None or self.proc.stdout is None:
            return self.tail()
        # Use select if available
        try:
            import select as sel

            while True:
                ready, _, _ = sel.select([self.proc.stdout], [], [], 0)
                if not ready:
                    break
                line = self.proc.stdout.readline()
                if not line:
                    break
                self._buf.append(line.rstrip("\n"))
                if len(self._buf) > self._max_lines:
                    self._buf = self._buf[-self._max_lines :]
        except (ValueError, OSError):
            pass
        return self.tail()

    def tail(self) -> str:
        return "\n".join(self._buf)

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> None:
        if self.proc is None:
            return
        if self.proc.poll() is None:
            try:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            except OSError:
                pass
        self.proc = None


def _append_ui_event(run_dir: Path | None, event: dict[str, Any]) -> None:
    if run_dir is None:
        return
    path = run_dir / "ui-events.jsonl"
    try:
        ensure_run_dir_layout(run_dir)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError:
        pass


def run_ui(cfg: UiConfig) -> int:
    """Main UI loop. Returns process exit code."""
    if cfg.run_dir is not None:
        ensure_run_dir_layout(cfg.run_dir)

    overlay = HarnessOverlay(phase="mock" if cfg.mock else "live")
    meters = MockMeters() if cfg.mock else None
    watch: WatchComposer | None = None
    watch_tail = ""

    if not cfg.mock:
        # Prefer explicit watch.jsonl under run_dir when present.
        if cfg.watch_jsonl is None and cfg.run_dir is not None:
            cand = cfg.run_dir / "watch.jsonl"
            if cand.is_file():
                cfg.watch_jsonl = cand
        watch = WatchComposer(cfg)
        if not watch.start():
            # Fall back to mock meters with a warning banner rather than hard fail,
            # unless user forbade mock — still report error in overlay.
            overlay.last_error = watch.error
            meters = MockMeters()
            overlay.phase = "degraded_mock"

    # Key source
    scripted: ScriptedKeys | None = None
    raw: RawTTY | None = None
    tty_stream: TextIO | None = None
    if cfg.key_script:
        scripted = ScriptedKeys(cfg.key_script)
        keys_note = f"scripted keys: {cfg.key_script}"
    else:
        tty_stream = resolve_tty()
        if tty_stream is not None:
            raw = RawTTY(tty_stream)
            keys_note = "interactive /dev/tty"
        else:
            keys_note = "no tty — keys disabled (use --key-script or --mock with script)"

    t0 = time.monotonic()
    quit_reason = "max_seconds" if cfg.max_seconds is not None else "loop"
    exit_code = 0

    def elapsed_ms() -> float:
        return (time.monotonic() - t0) * 1000.0

    def handle_action(action: KeyAction) -> bool:
        """Apply key action. Returns True if should quit."""
        nonlocal quit_reason
        now = elapsed_ms()
        if action == KeyAction.QUIT:
            quit_reason = "quit_key"
            return True
        if action == KeyAction.BARGE:
            if overlay.barge_ms is None:
                overlay.barge_ms = now
            overlay.barge_touched = True
            overlay.phase = "barged"
            if cfg.run_dir is not None:
                touch_control(cfg.run_dir, "barge.now")
            _append_ui_event(
                cfg.run_dir,
                {
                    "event": "sts_peek_barge",
                    "t_ms": now,
                    "source": "key",
                    "control": "control/barge.now",
                },
            )
            return False
        if action == KeyAction.USER_STOP:
            overlay.user_stop_touched = True
            if overlay.barge_ms is None:
                overlay.barge_ms = now
                overlay.barge_touched = True
                if cfg.run_dir is not None:
                    touch_control(cfg.run_dir, "barge.now")
            if cfg.run_dir is not None:
                touch_control(cfg.run_dir, "user_stop.now")
            overlay.phase = "user_stop"
            _append_ui_event(
                cfg.run_dir,
                {"event": "sts_peek_user_stop", "t_ms": now, "source": "key"},
            )
            return False
        if action == KeyAction.HUMAN_TOGGLE:
            overlay.human_mode = not overlay.human_mode
            if cfg.run_dir is not None:
                touch_control(
                    cfg.run_dir,
                    "human.mode",
                    "1\n" if overlay.human_mode else "0\n",
                )
            _append_ui_event(
                cfg.run_dir,
                {
                    "event": "sts_peek_human_mode",
                    "t_ms": now,
                    "human_mode": overlay.human_mode,
                },
            )
            return False
        return False

    # Allow SIGINT to quit cleanly
    interrupted = {"v": False}

    def _on_sigint(_sig: int, _frame: Any) -> None:
        interrupted["v"] = True

    old_handler = signal.signal(signal.SIGINT, _on_sigint)

    try:
        if raw is not None:
            raw.__enter__()
        if not cfg.quiet:
            sys.stdout.write(_HIDE)
            sys.stdout.flush()

        tick = cfg.tick_ms / 1000.0
        while True:
            if interrupted["v"]:
                quit_reason = "sigint"
                break

            now = elapsed_ms()
            if cfg.max_seconds is not None and now >= cfg.max_seconds * 1000.0:
                quit_reason = "max_seconds"
                break
            if cfg.mock and now >= cfg.mock_seconds * 1000.0 and scripted is None:
                # Default mock lifetime when no key script / max_seconds.
                if cfg.max_seconds is None and raw is None:
                    quit_reason = "mock_complete"
                    break

            poll_harness(cfg.run_dir, overlay)
            if meters is not None:
                meters.tick(
                    barged=overlay.barge_ms is not None or overlay.barge_touched,
                    human_mode=overlay.human_mode,
                )
            if watch is not None:
                watch_tail = watch.poll_output()
                if watch.error and not overlay.last_error:
                    overlay.last_error = watch.error

            frame = render_frame(
                cfg=cfg,
                overlay=overlay,
                meters=meters,
                watch_tail=watch_tail,
                elapsed_ms=elapsed_ms(),
                keys_note=keys_note
                + (
                    f"  | watch_err={overlay.last_error}"
                    if overlay.last_error
                    else ""
                ),
                live_watch_running=bool(watch and watch.running()),
            )
            if not cfg.quiet:
                sys.stdout.write(frame)
                sys.stdout.flush()

            # Keys
            ev: KeyEvent | None = None
            if scripted is not None:
                ev = scripted.poll_key(tick)
                # If script exhausted and mock, allow clean exit after one more frame.
                if scripted.exhausted and cfg.mock and cfg.max_seconds is None:
                    # Give one paint after last key, then exit unless max set.
                    # Fall through; next loop will still check — use short grace.
                    if overlay.barge_touched or True:
                        # Exit on next iteration after small drain of remaining sleep.
                        if scripted.exhausted:
                            # Immediate exit after processing last action below.
                            pass
            elif raw is not None:
                ev = raw.poll_key(tick)
            else:
                time.sleep(tick)

            if ev is not None and handle_action(ev.action):
                break

            if scripted is not None and scripted.exhausted and cfg.max_seconds is None:
                # Script done → quit (offline tests).
                quit_reason = "script_exhausted"
                break

    finally:
        signal.signal(signal.SIGINT, old_handler)
        if raw is not None:
            raw.__exit__(None, None, None)
        if watch is not None:
            watch.stop()
        if tty_stream is not None and tty_stream is not sys.stdin:
            try:
                tty_stream.close()
            except OSError:
                pass
        if not cfg.quiet:
            sys.stdout.write(_SHOW + _RESET + "\n")
            sys.stdout.flush()

    # Summary line (always, for tests)
    summary = {
        "ok": True,
        "mode": "mock" if cfg.mock else "live",
        "quit_reason": quit_reason,
        "barge": bool(overlay.barge_touched or overlay.barge_ms is not None),
        "human_mode": overlay.human_mode,
        "user_stop": overlay.user_stop_touched,
        "primary_cut_source": overlay.primary_cut_source,
        "run_dir": str(cfg.run_dir) if cfg.run_dir else None,
        "elapsed_ms": round(elapsed_ms(), 1),
        "watch_error": overlay.last_error or None,
    }
    print(f"sts-peek-ui: done  {json.dumps(summary, sort_keys=True)}")
    if cfg.run_dir is not None:
        barge_path = control_dir(cfg.run_dir) / "barge.now"
        if summary["barge"] and barge_path.is_file():
            print(f"sts-peek-ui: barge control → {barge_path}")
    return exit_code


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sts-peek-ui",
        description="Track U live observability UI for sts-peek (compose speech-core-watch).",
    )
    p.add_argument(
        "--run-dir",
        type=Path,
        default=None,
        help="Track L session directory (control/, cut/, intended.txt, watch.jsonl).",
    )
    p.add_argument(
        "--mock",
        action="store_true",
        help="Offline fake meters + key handling (no daemons).",
    )
    p.add_argument(
        "--mock-seconds",
        type=float,
        default=2.5,
        help="Default mock lifetime when no key script / tty (default 2.5).",
    )
    p.add_argument(
        "--max-seconds",
        type=float,
        default=None,
        help="Hard cap on UI lifetime (seconds).",
    )
    p.add_argument(
        "--tick-ms",
        type=float,
        default=80.0,
        help="UI refresh / key poll interval.",
    )
    p.add_argument(
        "--key-script",
        type=str,
        default=None,
        help='Scripted keys for tests, e.g. "sleep:0.2,b,sleep:0.3,h,q".',
    )
    p.add_argument(
        "--watch-bin",
        type=str,
        default=None,
        help="Path to speech-core-watch binary (default: PATH or target/debug).",
    )
    p.add_argument(
        "--watch-mode",
        type=str,
        default="debug",
        choices=["debug", "tui", "transcript", "jsonl"],
        help="speech-core-watch --mode (default debug).",
    )
    p.add_argument(
        "--stream-id",
        type=str,
        default=os.environ.get("SPEECH_CORE_STREAM_ID"),
        help="Filter watch by stream id (env SPEECH_CORE_STREAM_ID).",
    )
    p.add_argument(
        "--stream-session-id",
        type=str,
        default=os.environ.get("SPEECH_CORE_STREAM_SESSION_ID"),
        help="Filter watch by stream session id.",
    )
    p.add_argument(
        "--ws-url",
        type=str,
        default=os.environ.get("SPEECH_CORE_WS_URL"),
        help="Daemon websocket URL for live watch subscribe.",
    )
    p.add_argument(
        "--replay-events",
        type=Path,
        default=None,
        help="Replay a jsonl event file via speech-core-watch --replay-events.",
    )
    p.add_argument(
        "--watch-jsonl",
        type=Path,
        default=None,
        help="Session watch.jsonl to replay/attach (defaults to run_dir/watch.jsonl).",
    )
    p.add_argument(
        "--no-clear",
        action="store_true",
        help="Do not clear screen between frames (log-friendly).",
    )
    p.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress frame paint; print summary only.",
    )
    p.add_argument(
        "--self-check",
        action="store_true",
        help="Run offline self-check and exit.",
    )
    return p


def self_check() -> None:
    """Offline unit check: mock meters + scripted barge writes control file."""
    import tempfile

    with tempfile.TemporaryDirectory(prefix="sts-peek-ui-self-") as td:
        run_dir = Path(td)
        ensure_run_dir_layout(run_dir)
        (run_dir / "intended.txt").write_text(
            "one two three four five", encoding="utf-8"
        )
        # Seed a cut metric as Track C would.
        (cut_dir(run_dir) / "metrics.json").write_text(
            json.dumps(
                {
                    "primary_cut_source": "drain",
                    "production_cut_text": "one two three",
                    "drain_status": "complete",
                    "label": "eval_only",
                }
            ),
            encoding="utf-8",
        )
        cfg = UiConfig(
            run_dir=run_dir,
            mock=True,
            mock_seconds=5.0,
            tick_ms=30.0,
            key_script="sleep:0.05,b,sleep:0.05,h,sleep:0.05,u,sleep:0.05,q",
            no_clear=True,
            quiet=True,
        )
        code = run_ui(cfg)
        assert code == 0, code
        barge = control_dir(run_dir) / "barge.now"
        assert barge.is_file(), "barge.now not written"
        human = read_text(control_dir(run_dir) / "human.mode")
        assert human in ("1", "0")
        assert (control_dir(run_dir) / "user_stop.now").is_file()
        # Overlay read of cut
        ov = HarnessOverlay()
        poll_harness(run_dir, ov)
        assert ov.primary_cut_source == "drain"
        assert ov.production_cut == "one two three"
        assert "one two" in ov.intended
        # map_key smoke
        assert map_key_smoke()
    print("sts-peek-ui self-check: PASS")


def map_key_smoke() -> bool:
    from keys import map_key

    assert map_key("b") == KeyAction.BARGE
    assert map_key(" ") == KeyAction.BARGE
    assert map_key("u") == KeyAction.USER_STOP
    assert map_key("h") == KeyAction.HUMAN_TOGGLE
    assert map_key("q") == KeyAction.QUIT
    return True


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.self_check:
        self_check()
        return 0

    # Default to mock when no run-dir and no live attach hints (safe offline).
    mock = bool(args.mock)
    if (
        not mock
        and args.run_dir is None
        and args.replay_events is None
        and args.watch_jsonl is None
        and args.ws_url is None
    ):
        # Live mode without targets still attempts watch with default ws URL.
        pass

    cfg = UiConfig(
        run_dir=args.run_dir,
        mock=mock,
        mock_seconds=args.mock_seconds,
        tick_ms=args.tick_ms,
        watch_bin=args.watch_bin,
        watch_mode=args.watch_mode,
        stream_id=args.stream_id,
        stream_session_id=args.stream_session_id,
        ws_url=args.ws_url,
        replay_events=args.replay_events,
        watch_jsonl=args.watch_jsonl,
        key_script=args.key_script,
        max_seconds=args.max_seconds,
        no_clear=args.no_clear,
        quiet=args.quiet,
    )
    return run_ui(cfg)


if __name__ == "__main__":
    raise SystemExit(main())
