"""Audio I/O for sts-peek: speech-out speak/cancel, barge watch, optional record.

Reuses patterns from scripts/speech-out-live-session.sh (play PID + kill_tree)
and scripts/barge-in-dual-asr/record_client.py (record-only capture) without
modifying those files.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from .session import Session


# ── Process / binary helpers ─────────────────────────────────────────────


def repo_root_from_here() -> Path:
    return Path(__file__).resolve().parents[2]


def bin_search_dirs(repo_root: Path | None = None) -> list[Path]:
    root = repo_root or repo_root_from_here()
    return [
        root / "target" / "debug",
        root / "target" / "release",
        Path.home() / ".local" / "bin",
        Path(sys.prefix) / "bin",
    ]


def which_bin(name: str, extra_dirs: list[Path] | None = None) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    for d in extra_dirs or bin_search_dirs():
        cand = d / name
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return None


def find_speech_out(repo_root: Path | None = None) -> str | None:
    return which_bin("speech-out", bin_search_dirs(repo_root))


def find_mic_adapter(repo_root: Path | None = None) -> str | None:
    return which_bin("speech-core-mic-adapter", bin_search_dirs(repo_root))


def host_port_from_ws(url: str) -> tuple[str, int] | None:
    try:
        parsed = urlparse(url)
        host = parsed.hostname or "127.0.0.1"
        if parsed.port is not None:
            port = parsed.port
        elif parsed.scheme in ("wss", "https"):
            port = 443
        else:
            port = 80
        return host, port
    except Exception:
        return None


def probe_tcp(url: str, timeout: float = 0.4) -> tuple[bool, str]:
    hp = host_port_from_ws(url)
    if hp is None:
        return False, f"unparseable url: {url}"
    host, port = hp
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, f"{host}:{port} open"
    except OSError as exc:
        return False, f"{host}:{port} closed ({exc})"


def collect_descendants(root_pid: int, max_nodes: int = 256) -> list[int]:
    """DFS collect of PID tree (root included). Children ordered before parent."""
    seen: set[int] = set()
    stack = [root_pid]
    all_pids: list[int] = []
    while stack and len(all_pids) < max_nodes:
        p = stack.pop()
        if p in seen:
            continue
        seen.add(p)
        all_pids.append(p)
        try:
            out = subprocess.check_output(
                ["pgrep", "-P", str(p)], text=True, stderr=subprocess.DEVNULL
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            out = ""
        for line in out.split():
            if line.isdigit():
                child = int(line)
                if child not in seen:
                    stack.append(child)
    # leaf-first
    all_pids.reverse()
    return all_pids


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    # zombie check
    try:
        status = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
    except OSError:
        return False
    for line in status.splitlines():
        if line.startswith("State:"):
            state = line.split()[1] if len(line.split()) > 1 else ""
            if state in ("Z", "z"):
                return False
            break
    return True


def pid_starttime(pid: int) -> str:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
        # field 22 is starttime (1-indexed → index 21)
        return fields[21] if len(fields) > 21 else "0"
    except OSError:
        return "0"


def kill_tree(pid: int, timeout_secs: float = 3.0) -> None:
    """SIGTERM entire process tree, then SIGKILL survivors (live-session pattern)."""
    if not pid_alive(pid):
        return
    pids = collect_descendants(pid)
    starttimes = {p: pid_starttime(p) for p in pids}
    for p in pids:
        try:
            os.kill(p, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.monotonic() + timeout_secs
    while time.monotonic() < deadline:
        if not any(pid_alive(p) for p in pids):
            return
        time.sleep(0.1)
    for p in pids:
        if not pid_alive(p):
            continue
        cur = pid_starttime(p)
        old = starttimes.get(p, "0")
        if old not in ("0", "") and cur not in ("0", "") and old != cur:
            continue  # PID reused
        try:
            os.kill(p, signal.SIGKILL)
        except OSError:
            pass


# ── Speak / cancel ───────────────────────────────────────────────────────


@dataclass
class PlayHandle:
    role: str  # "assistant" | "user"
    pid: int | None
    proc: subprocess.Popen[Any] | None
    mock: bool
    text: str
    voice: str
    events_path: Path
    pid_path: Path
    started_at: float
    cancelled: bool = False
    returncode: int | None = None
    cancel_reason: str | None = None


class AudioLayer:
    """Speak assistant / user TTS via speech-out play; cancel on barge."""

    def __init__(
        self,
        session: Session,
        *,
        repo_root: Path | None = None,
        speech_out_bin: str | None = None,
    ) -> None:
        self.session = session
        self.repo_root = repo_root or repo_root_from_here()
        self.speech_out_bin = speech_out_bin or find_speech_out(self.repo_root)
        self._assistant: PlayHandle | None = None
        self._user: PlayHandle | None = None
        self._record_proc: subprocess.Popen[Any] | None = None
        self._barge_seen = False
        self._lock = threading.Lock()

    # ── readiness ────────────────────────────────────────────────────

    def probe(self) -> dict[str, Any]:
        cfg = self.session.config
        out_ok, out_detail = probe_tcp(cfg.out_ws)
        core_ok, core_detail = probe_tcp(cfg.core_ws)
        bin_ok = self.speech_out_bin is not None
        ready = bool(bin_ok and out_ok) or cfg.mock_audio
        return {
            "speech_out_bin": self.speech_out_bin,
            "speech_out_bin_ok": bin_ok,
            "out_ws": cfg.out_ws,
            "out_reachable": out_ok,
            "out_detail": out_detail,
            "core_ws": cfg.core_ws,
            "core_reachable": core_ok,
            "core_detail": core_detail,
            "mock_audio": cfg.mock_audio,
            "ready": ready,
            "mic_adapter": find_mic_adapter(self.repo_root),
        }

    def require_ready(self) -> dict[str, Any]:
        probes = self.probe()
        # Always persist probes so dry-fail runs leave attachable artifacts.
        try:
            (self.session.paths.run_dir / "probes.json").write_text(
                json.dumps(probes, indent=2) + "\n", encoding="utf-8"
            )
        except OSError:
            pass
        self.session.emit("probes", **probes)
        if probes["ready"]:
            return probes
        reasons = []
        if not probes["speech_out_bin_ok"]:
            reasons.append(
                "speech-out binary not found "
                "(cargo build -p speech-out or install client tools)"
            )
        if not probes["out_reachable"] and not self.session.config.mock_audio:
            reasons.append(
                f"speech-out daemon not reachable at {probes['out_ws']} "
                f"({probes['out_detail']})"
            )
        msg = "sts-peek audio not ready: " + "; ".join(reasons)
        msg += (
            ". Start speech-out daemon, or re-run with --mock-audio for offline smoke."
        )
        raise RuntimeError(msg)

    # ── speak ────────────────────────────────────────────────────────

    def speak(
        self,
        *,
        role: str,
        text: str,
        voice: str,
        events_path: Path,
        pid_path: Path,
        play_command: str | None = None,
    ) -> PlayHandle:
        cfg = self.session.config
        play_command = play_command or cfg.play_command
        events_path.parent.mkdir(parents=True, exist_ok=True)
        events_path.write_text("", encoding="utf-8")

        self.session.emit(
            f"{role}_speak_started",
            text=text,
            voice=voice,
            mock=cfg.mock_audio,
            play_command=play_command,
        )

        if cfg.mock_audio:
            return self._speak_mock(
                role=role,
                text=text,
                voice=voice,
                events_path=events_path,
                pid_path=pid_path,
            )

        if not self.speech_out_bin:
            raise RuntimeError("speech-out binary not found")

        # Prefer play-command=true when pw-play missing so synthesis still runs
        effective_play = play_command
        if play_command and play_command not in ("true", "cat", "/bin/true"):
            if shutil.which(play_command.split()[0]) is None:
                self.session.emit(
                    "play_command_fallback",
                    requested=play_command,
                    fallback="true",
                    note="play binary missing; synthesis-only",
                )
                effective_play = "true"

        cmd = [
            self.speech_out_bin,
            "play",
            "--url",
            cfg.out_ws,
            "--voice",
            voice,
            "--lang",
            cfg.lang,
            "--steps",
            str(cfg.steps),
            "--speed",
            str(cfg.speed),
            "--play-command",
            effective_play,
            "--chunk-min-chars",
            str(cfg.chunk_min_chars),
            "--chunk-max-chars",
            str(cfg.chunk_max_chars),
            text,
        ]
        err_fh = events_path.open("a", encoding="utf-8")
        proc = subprocess.Popen(
            cmd,
            stdout=err_fh,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,  # own process group for clean kill
        )
        handle = PlayHandle(
            role=role,
            pid=proc.pid,
            proc=proc,
            mock=False,
            text=text,
            voice=voice,
            events_path=events_path,
            pid_path=pid_path,
            started_at=time.monotonic(),
        )
        self.session.write_pid(pid_path, proc.pid)
        self.session.emit(
            f"{role}_play_pid",
            pid=proc.pid,
            cmd=cmd[:-1] + ["<text>"],  # redact full text from cmd log
        )
        if role == "assistant":
            self._assistant = handle
        else:
            self._user = handle
        return handle

    def _speak_mock(
        self,
        *,
        role: str,
        text: str,
        voice: str,
        events_path: Path,
        pid_path: Path,
    ) -> PlayHandle:
        """Offline smoke: write synthetic events and a short sleep child."""
        words = max(1, len(text.split()))
        # ~80ms/word mock duration, min 200ms
        duration_ms = max(200, words * 80)
        # Child script: emit mock speech-out-like events then sleep until cancelled.
        script = """
import json, time, sys
duration = float(sys.argv[1])
voice = sys.argv[2]
text = sys.argv[3]
t0 = time.monotonic()
print(json.dumps({
    "event": "speech_out_request_received",
    "backend": "mock",
    "voice": voice,
    "text": text,
}), flush=True)
print(json.dumps({"event": "speech_out_synthesis_started", "backend": "mock"}), flush=True)
print(json.dumps({
    "event": "speech_out_playback_started",
    "playback_seq": 1,
    "play_command": "mock",
}), flush=True)
end = t0 + duration
while time.monotonic() < end:
    time.sleep(0.05)
print(json.dumps({
    "event": "speech_out_playback_utterance_completed",
    "backend": "mock",
}), flush=True)
print(json.dumps({"event": "speech_out_completed", "backend": "mock"}), flush=True)
"""
        err_fh = events_path.open("a", encoding="utf-8")
        proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                script,
                str(duration_ms / 1000.0),
                voice,
                text,
            ],
            stdout=err_fh,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        handle = PlayHandle(
            role=role,
            pid=proc.pid,
            proc=proc,
            mock=True,
            text=text,
            voice=voice,
            events_path=events_path,
            pid_path=pid_path,
            started_at=time.monotonic(),
        )
        self.session.write_pid(pid_path, proc.pid)
        self.session.emit(f"{role}_play_pid", pid=proc.pid, mock=True, duration_ms=duration_ms)
        if role == "assistant":
            self._assistant = handle
        else:
            self._user = handle
        return handle

    def speak_assistant(self) -> PlayHandle:
        cfg = self.session.config
        p = self.session.paths
        return self.speak(
            role="assistant",
            text=cfg.intended_text,
            voice=cfg.assistant_voice,
            events_path=p.assistant_events,
            pid_path=p.assistant_play_pid,
        )

    def speak_user(self) -> PlayHandle:
        cfg = self.session.config
        p = self.session.paths
        return self.speak(
            role="user",
            text=cfg.user_text,
            voice=cfg.user_voice,
            events_path=p.user_events,
            pid_path=p.user_play_pid,
        )

    # ── cancel ───────────────────────────────────────────────────────

    def cancel(self, handle: PlayHandle | None, *, reason: str, trigger: str) -> bool:
        if handle is None:
            return False
        with self._lock:
            if handle.cancelled:
                return True
            pid = handle.pid
            if handle.proc is not None and handle.proc.poll() is not None:
                handle.returncode = handle.proc.returncode
                self.session.write_pid(handle.pid_path, None)
                return False
            if pid is None or not pid_alive(pid):
                self.session.write_pid(handle.pid_path, None)
                return False
            handle.cancelled = True
            handle.cancel_reason = reason
            self.session.emit(
                f"{handle.role}_cancel_requested",
                reason=reason,
                trigger=trigger,
                pid=pid,
            )
            kill_tree(pid)
            if handle.proc is not None:
                try:
                    handle.proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    pass
                handle.returncode = handle.proc.returncode
            self.session.write_pid(handle.pid_path, None)
            if handle.role == "assistant":
                self.session.write_cancel(
                    reason=reason, play_pid=pid, trigger=trigger
                )
            else:
                self.session.emit(
                    f"{handle.role}_playback_cancelled",
                    reason=reason,
                    trigger=trigger,
                    play_pid=pid,
                )
            return True

    def cancel_assistant(self, *, reason: str = "barge", trigger: str = "barge") -> bool:
        return self.cancel(self._assistant, reason=reason, trigger=trigger)

    def wait_handle(self, handle: PlayHandle | None, timeout: float | None = None) -> int | None:
        if handle is None or handle.proc is None:
            return None
        try:
            rc = handle.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None
        handle.returncode = rc
        if self.session.read_pid(handle.pid_path) == handle.pid:
            self.session.write_pid(handle.pid_path, None)
        self.session.emit(
            f"{handle.role}_speak_finished",
            returncode=rc,
            cancelled=handle.cancelled,
            cancel_reason=handle.cancel_reason,
        )
        return rc

    def is_active(self, handle: PlayHandle | None) -> bool:
        if handle is None or handle.pid is None:
            return False
        if handle.proc is not None and handle.proc.poll() is not None:
            return False
        return pid_alive(handle.pid)

    # ── barge watch ──────────────────────────────────────────────────

    def barge_flag_present(self) -> bool:
        return self.session.paths.barge_now.exists()

    def clear_barge_flag(self) -> None:
        p = self.session.paths.barge_now
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass

    def signal_barge(self, note: str = "cli") -> None:
        """Write control/barge.now (same contract UI will use)."""
        path = self.session.paths.barge_now
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{note}\n{time.time()}\n", encoding="utf-8")
        self.session.emit("barge_flag_written", path=str(path), note=note)

    def wait_for_barge(
        self,
        *,
        barge_after_ms: int | None = None,
        poll_ms: int = 25,
        max_wait_ms: int | None = None,
        on_tick: Callable[[], None] | None = None,
    ) -> str | None:
        """Poll control/barge.now and optional scheduled barge.

        Returns trigger string when barge fires, else None on timeout /
        assistant natural end.
        """
        start = time.monotonic()
        scheduled_at = None
        if barge_after_ms is not None:
            scheduled_at = start + (barge_after_ms / 1000.0)

        self.session.emit(
            "barge_watch_started",
            barge_after_ms=barge_after_ms,
            barge_path=str(self.session.paths.barge_now),
        )

        while True:
            if on_tick:
                on_tick()

            if self.barge_flag_present():
                self._barge_seen = True
                self.session.emit("barge_flag_observed", path=str(self.session.paths.barge_now))
                return "barge.now"

            if scheduled_at is not None and time.monotonic() >= scheduled_at:
                self.signal_barge(note=f"scheduled:{barge_after_ms}ms")
                self._barge_seen = True
                return "scheduled"

            if not self.is_active(self._assistant):
                return None

            if max_wait_ms is not None:
                if (time.monotonic() - start) * 1000 >= max_wait_ms:
                    return None

            time.sleep(max(0.005, poll_ms / 1000.0))

    # ── optional record-only capture ─────────────────────────────────

    def start_record_client(self) -> dict[str, Any]:
        """Subprocess-launch barge-in-dual-asr record_client (no file edits)."""
        cfg = self.session.config
        if not cfg.record_synthesis:
            return {"started": False, "reason": "record_synthesis disabled"}
        if cfg.mock_audio:
            # Write a stub summary so layout is present offline
            rec_dir = self.session.paths.record_dir
            rec_dir.mkdir(parents=True, exist_ok=True)
            summary = {
                "url": cfg.out_ws,
                "chunks": 0,
                "bytes": 0,
                "events": 0,
                "terminal": "mock_skip",
                "chunk_paths": [],
                "mock": True,
            }
            (rec_dir / "record_summary.json").write_text(
                json.dumps(summary, indent=2) + "\n", encoding="utf-8"
            )
            self.session.emit("record_client_mock_stub", path=str(rec_dir))
            return {"started": False, "reason": "mock_audio", "summary": summary}

        record_script = (
            self.repo_root / "scripts" / "barge-in-dual-asr" / "record_client.py"
        )
        if not record_script.is_file():
            return {"started": False, "reason": f"missing {record_script}"}

        rec_dir = self.session.paths.record_dir
        rec_dir.mkdir(parents=True, exist_ok=True)
        log_path = rec_dir / "record_client.log"
        log_fh = log_path.open("w", encoding="utf-8")
        cmd = [
            sys.executable,
            str(record_script),
            "--url",
            cfg.out_ws,
            "--out-dir",
            str(rec_dir),
            "--max-seconds",
            "120",
        ]
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
        except OSError as exc:
            return {"started": False, "reason": str(exc)}

        self._record_proc = proc
        self.session.write_pid(self.session.paths.record_client_pid, proc.pid)
        self.session.emit("record_client_started", pid=proc.pid, out_dir=str(rec_dir))
        return {"started": True, "pid": proc.pid, "out_dir": str(rec_dir)}

    def stop_record_client(self) -> None:
        proc = self._record_proc
        if proc is None:
            return
        if proc.poll() is None and proc.pid:
            kill_tree(proc.pid)
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                pass
        self.session.write_pid(self.session.paths.record_client_pid, None)
        self.session.emit(
            "record_client_stopped",
            returncode=proc.returncode,
        )
        self._record_proc = None

    # ── human mode stub ──────────────────────────────────────────────

    def prepare_human_mode(self) -> dict[str, Any]:
        """Document mic path; optionally write a launch helper script."""
        cfg = self.session.config
        mic_dir = self.session.paths.mic_dir
        mic_dir.mkdir(parents=True, exist_ok=True)
        mic_bin = find_mic_adapter(self.repo_root)
        info = {
            "human_mode": True,
            "note": (
                "After barge, do NOT play user-role TTS. Leave mic path open "
                "for the operator. Track U / live-session can drive speech-core "
                "mic-adapter; this audio layer only prepares the stub."
            ),
            "mic_adapter_bin": mic_bin,
            "core_ws": cfg.core_ws,
            "user_stream_id": cfg.user_stream_id,
            "stream_session_id": self.session.stream_session_id,
        }
        (mic_dir / "human_mode.json").write_text(
            json.dumps(info, indent=2) + "\n", encoding="utf-8"
        )
        launch = mic_dir / "launch-mic-adapter.sh"
        if mic_bin:
            body = f"""#!/usr/bin/env bash
# Auto-generated by sts-peek audio human-mode stub. Operator may run this
# after barge to open the real mic path (Nemotron A / user stream).
set -euo pipefail
exec {mic_bin} \\
  --url {cfg.core_ws!s} \\
  --stream-id {cfg.user_stream_id!s} \\
  --stream-session-id {self.session.stream_session_id!s} \\
  --adapter-id sts-peek.human.mic \\
  "$@"
"""
            launch.write_text(body, encoding="utf-8")
            launch.chmod(0o755)
            info["launch_script"] = str(launch)
        else:
            (mic_dir / "README.txt").write_text(
                "speech-core-mic-adapter not found. Build with:\n"
                "  cargo build -p speech-core-mic-adapter\n"
                "Then re-run with --human, or start mic manually.\n",
                encoding="utf-8",
            )
        self.session.emit("human_mode_prepared", **{k: v for k, v in info.items() if k != "note"})
        return info

    # ── full audio session sequence ──────────────────────────────────

    def run_sequence(self) -> dict[str, Any]:
        """Headless audio sequence: speak assistant → barge → user TTS or human.

        Returns a result dict suitable for CLI exit code + summary.
        """
        cfg = self.session.config
        result: dict[str, Any] = {
            "ok": False,
            "stream_session_id": self.session.stream_session_id,
            "run_dir": str(self.session.paths.run_dir),
            "mock_audio": cfg.mock_audio,
            "human_mode": cfg.human_mode,
            "barge_trigger": None,
            "assistant_cancelled": False,
            "user_spoken": False,
        }

        probes = self.require_ready()

        if cfg.record_synthesis:
            rec = self.start_record_client()
            result["record"] = rec
            # brief settle so record client can connect before speak
            if rec.get("started"):
                time.sleep(0.15)

        assistant = self.speak_assistant()
        result["assistant_pid"] = assistant.pid

        trigger = self.wait_for_barge(barge_after_ms=cfg.barge_after_ms)
        result["barge_trigger"] = trigger

        if trigger is not None:
            cancelled = self.cancel_assistant(reason="barge", trigger=trigger)
            result["assistant_cancelled"] = cancelled
            # small gap so cancel settles before user TTS
            time.sleep(0.05)
        else:
            # natural completion
            self.wait_handle(assistant, timeout=1.0)
            result["assistant_cancelled"] = False
            self.session.emit("assistant_completed_without_barge")

        if cfg.human_mode:
            human = self.prepare_human_mode()
            result["human"] = human
            result["user_spoken"] = False
            self.session.emit(
                "user_path_human",
                note="mic left for operator; no user-role TTS",
            )
        else:
            user = self.speak_user()
            result["user_pid"] = user.pid
            # Wait for user TTS to finish (or timeout long utterances)
            words = max(1, len(cfg.user_text.split()))
            timeout = max(15.0, words * 0.5 + 5.0)
            if cfg.mock_audio:
                timeout = max(2.0, words * 0.12 + 0.5)
            rc = self.wait_handle(user, timeout=timeout)
            if rc is None and self.is_active(user):
                self.cancel(user, reason="user_timeout", trigger="timeout")
            result["user_spoken"] = True
            result["user_returncode"] = user.returncode

        self.stop_record_client()

        # Ensure assistant handle reaped
        if self.is_active(assistant):
            self.cancel_assistant(reason="session_end", trigger="session_end")
        else:
            self.wait_handle(assistant, timeout=0.5)

        result["ok"] = True
        self.session.emit("audio_sequence_complete", **{
            k: v for k, v in result.items() if k != "human"
        })
        (self.session.paths.run_dir / "audio_result.json").write_text(
            json.dumps(result, indent=2, default=str) + "\n", encoding="utf-8"
        )
        return result

    def cleanup(self) -> None:
        self.cancel_assistant(reason="cleanup", trigger="cleanup")
        if self._user is not None:
            self.cancel(self._user, reason="cleanup", trigger="cleanup")
        self.stop_record_client()
