"""Session run-dir layout and harness event log for sts-peek audio track.

Publishes a stable contract for Track U (UI) and Track C (cut coordinator):

  run_dir/
    params.env                 # shell-sourceable knobs
    session.json               # ids + paths + mode
    assistant_intended.txt     # assistant TTS text
    user_text.txt              # user-role TTS text (empty in --human)
    events.jsonl               # harness-local session events
    control/
      barge.now               # UI/operator touches this to barge
    pids/
      assistant_play.pid
      user_play.pid
      record_client.pid
    speech_out/
      assistant_events.jsonl  # optional speech-out client stderr/events
      user_events.jsonl
    record/                    # optional record-only synthesis capture
    cancel/
      assistant_cancel.json   # cancel timestamp + reason
    mic/                       # human-mode stub notes / adapter launch info

Stream IDs (do not invent protocol changes):
  user mic:          laptop.live_mic  (or SPEECH_CORE_STREAM_ID)
  assistant self-ASR: assistant.self_asr
"""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, TextIO

from . import (
    ASSISTANT_STREAM_ID,
    DEFAULT_CORE_WS,
    DEFAULT_OUT_WS,
    USER_STREAM_ID,
)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _monotonic_ns() -> int:
    return time.monotonic_ns()


def default_run_base() -> Path:
    env = os.environ.get("STS_PEEK_RUN_BASE") or os.environ.get("SPEECH_CORE_RUN_DIR")
    if env:
        return Path(env)
    return Path.home() / ".local" / "state" / "speech-core" / "sts-peek"


def new_session_id(prefix: str = "sts-peek") -> str:
    return f"{prefix}-{int(time.time())}-{uuid.uuid4().hex[:8]}"


@dataclass
class SessionPaths:
    run_dir: Path
    params_env: Path
    session_json: Path
    assistant_intended: Path
    user_text: Path
    events_jsonl: Path
    control_dir: Path
    barge_now: Path
    pids_dir: Path
    assistant_play_pid: Path
    user_play_pid: Path
    record_client_pid: Path
    speech_out_dir: Path
    assistant_events: Path
    user_events: Path
    record_dir: Path
    cancel_dir: Path
    assistant_cancel: Path
    mic_dir: Path
    readme: Path

    @classmethod
    def under(cls, run_dir: Path) -> "SessionPaths":
        run_dir = run_dir.resolve()
        control = run_dir / "control"
        pids = run_dir / "pids"
        speech_out = run_dir / "speech_out"
        cancel = run_dir / "cancel"
        return cls(
            run_dir=run_dir,
            params_env=run_dir / "params.env",
            session_json=run_dir / "session.json",
            assistant_intended=run_dir / "assistant_intended.txt",
            user_text=run_dir / "user_text.txt",
            events_jsonl=run_dir / "events.jsonl",
            control_dir=control,
            barge_now=control / "barge.now",
            pids_dir=pids,
            assistant_play_pid=pids / "assistant_play.pid",
            user_play_pid=pids / "user_play.pid",
            record_client_pid=pids / "record_client.pid",
            speech_out_dir=speech_out,
            assistant_events=speech_out / "assistant_events.jsonl",
            user_events=speech_out / "user_events.jsonl",
            record_dir=run_dir / "record",
            cancel_dir=cancel,
            assistant_cancel=cancel / "assistant_cancel.json",
            mic_dir=run_dir / "mic",
            readme=run_dir / "README-run.txt",
        )


@dataclass
class SessionConfig:
    intended_text: str
    user_text: str = "wait stop"
    human_mode: bool = False
    mock_audio: bool = False
    record_synthesis: bool = False
    barge_after_ms: int | None = None
    core_ws: str = field(default_factory=lambda: os.environ.get("SPEECH_CORE_WS_URL", DEFAULT_CORE_WS))
    out_ws: str = field(default_factory=lambda: os.environ.get("SPEECH_OUT_WS_URL", DEFAULT_OUT_WS))
    user_stream_id: str = field(
        default_factory=lambda: os.environ.get("SPEECH_CORE_STREAM_ID", USER_STREAM_ID)
    )
    assistant_stream_id: str = ASSISTANT_STREAM_ID
    assistant_voice: str = field(
        default_factory=lambda: os.environ.get("STS_PEEK_ASSISTANT_VOICE")
        or os.environ.get("SPEECH_OUT_VOICE", "M1")
    )
    user_voice: str = field(
        default_factory=lambda: os.environ.get("STS_PEEK_USER_VOICE")
        or os.environ.get("SPEECH_OUT_USER_VOICE", "F1")
    )
    lang: str = field(default_factory=lambda: os.environ.get("SPEECH_OUT_LANG", "en"))
    steps: int = field(default_factory=lambda: int(os.environ.get("SPEECH_OUT_STEPS", "5")))
    speed: float = field(default_factory=lambda: float(os.environ.get("SPEECH_OUT_SPEED", "1.30")))
    play_command: str = field(
        default_factory=lambda: os.environ.get("SPEECH_OUT_PLAY_COMMAND", "pw-play")
    )
    chunk_min_chars: int = field(
        default_factory=lambda: int(os.environ.get("SPEECH_OUT_CHUNK_MIN_CHARS", "8"))
    )
    chunk_max_chars: int = field(
        default_factory=lambda: int(os.environ.get("SPEECH_OUT_CHUNK_MAX_CHARS", "160"))
    )
    stream_session_id: str | None = None
    label: str = "live_peek"


class Session:
    """Owns run_dir creation, params, and events.jsonl harness log."""

    def __init__(
        self,
        config: SessionConfig,
        *,
        run_dir: Path | None = None,
        run_base: Path | None = None,
    ) -> None:
        self.config = config
        self.stream_session_id = config.stream_session_id or new_session_id()
        if run_dir is None:
            base = run_base or default_run_base()
            run_dir = base / self.stream_session_id
        self.paths = SessionPaths.under(Path(run_dir))
        self._events_fh: TextIO | None = None
        self._started_ns = _monotonic_ns()
        self._created = False

    def create(self) -> SessionPaths:
        """Create directory tree and seed artifacts. Idempotent for same run_dir."""
        p = self.paths
        for d in (
            p.run_dir,
            p.control_dir,
            p.pids_dir,
            p.speech_out_dir,
            p.record_dir,
            p.cancel_dir,
            p.mic_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)

        cfg = self.config
        p.assistant_intended.write_text(
            cfg.intended_text.rstrip() + "\n", encoding="utf-8"
        )
        p.user_text.write_text(
            ("" if cfg.human_mode else cfg.user_text.rstrip()) + "\n",
            encoding="utf-8",
        )

        params = self._params_env_body()
        p.params_env.write_text(params, encoding="utf-8")

        meta = {
            "label": cfg.label,
            "created_at": _now_iso(),
            "stream_session_id": self.stream_session_id,
            "user_stream_id": cfg.user_stream_id,
            "assistant_stream_id": cfg.assistant_stream_id,
            "core_ws": cfg.core_ws,
            "out_ws": cfg.out_ws,
            "human_mode": cfg.human_mode,
            "mock_audio": cfg.mock_audio,
            "record_synthesis": cfg.record_synthesis,
            "barge_after_ms": cfg.barge_after_ms,
            "assistant_voice": cfg.assistant_voice,
            "user_voice": cfg.user_voice,
            "lang": cfg.lang,
            "steps": cfg.steps,
            "speed": cfg.speed,
            "play_command": cfg.play_command,
            "paths": {
                "run_dir": str(p.run_dir),
                "events_jsonl": str(p.events_jsonl),
                "barge_now": str(p.barge_now),
                "assistant_intended": str(p.assistant_intended),
                "user_text": str(p.user_text),
                "params_env": str(p.params_env),
                "assistant_play_pid": str(p.assistant_play_pid),
                "user_play_pid": str(p.user_play_pid),
                "assistant_cancel": str(p.assistant_cancel),
                "record_dir": str(p.record_dir),
                "mic_dir": str(p.mic_dir),
            },
            "barge_contract": {
                "trigger": "touch or write run_dir/control/barge.now",
                "cli": "--barge-after-ms N for headless schedule",
                "watchers": "audio loop polls barge.now; UI may touch on keypress",
            },
            "voice_note": (
                "Assistant uses assistant_voice; user-role TTS uses user_voice. "
                "If Supertonic only has one working voice, both may sound the same "
                "(document single-voice limitation)."
            ),
        }
        p.session_json.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        p.readme.write_text(self._readme_body(meta), encoding="utf-8")

        # Fresh events log for a new session dir
        if not p.events_jsonl.exists():
            p.events_jsonl.write_text("", encoding="utf-8")

        self._created = True
        self.emit(
            "session_created",
            stream_session_id=self.stream_session_id,
            run_dir=str(p.run_dir),
            human_mode=cfg.human_mode,
            mock_audio=cfg.mock_audio,
        )
        return p

    def _params_env_body(self) -> str:
        cfg = self.config
        lines = [
            f"# sts-peek session params — {self.stream_session_id}",
            f"STS_PEEK_STREAM_SESSION_ID={self.stream_session_id}",
            f"STS_PEEK_RUN_DIR={self.paths.run_dir}",
            f"SPEECH_CORE_WS_URL={cfg.core_ws}",
            f"SPEECH_OUT_WS_URL={cfg.out_ws}",
            f"SPEECH_CORE_STREAM_ID={cfg.user_stream_id}",
            f"SPEECH_CORE_STREAM_SESSION_ID={self.stream_session_id}",
            f"STS_PEEK_ASSISTANT_STREAM_ID={cfg.assistant_stream_id}",
            f"STS_PEEK_ASSISTANT_VOICE={cfg.assistant_voice}",
            f"STS_PEEK_USER_VOICE={cfg.user_voice}",
            f"SPEECH_OUT_VOICE={cfg.assistant_voice}",
            f"SPEECH_OUT_LANG={cfg.lang}",
            f"SPEECH_OUT_STEPS={cfg.steps}",
            f"SPEECH_OUT_SPEED={cfg.speed}",
            f"SPEECH_OUT_PLAY_COMMAND={cfg.play_command}",
            f"SPEECH_OUT_CHUNK_MIN_CHARS={cfg.chunk_min_chars}",
            f"SPEECH_OUT_CHUNK_MAX_CHARS={cfg.chunk_max_chars}",
            f"STS_PEEK_HUMAN_MODE={'1' if cfg.human_mode else '0'}",
            f"STS_PEEK_MOCK_AUDIO={'1' if cfg.mock_audio else '0'}",
            f"STS_PEEK_RECORD_SYNTHESIS={'1' if cfg.record_synthesis else '0'}",
        ]
        if cfg.barge_after_ms is not None:
            lines.append(f"STS_PEEK_BARGE_AFTER_MS={cfg.barge_after_ms}")
        return "\n".join(lines) + "\n"

    def _readme_body(self, meta: dict[str, Any]) -> str:
        p = self.paths
        return f"""sts-peek audio session
======================

stream_session_id: {self.stream_session_id}
run_dir: {p.run_dir}
label: {meta.get('label')}
human_mode: {meta.get('human_mode')}
mock_audio: {meta.get('mock_audio')}

Barge trigger
-------------
UI or operator:
  touch {p.barge_now}
Headless schedule:
  --barge-after-ms N (also written into params.env)

Streams
-------
user:      {meta.get('user_stream_id')}
assistant: {meta.get('assistant_stream_id')}  (self-ASR / record feed)

Events
------
Harness log: {p.events_jsonl}
Cancel info: {p.assistant_cancel}

See docs/sts-peek-audio.md for full contract.
"""

    def emit(self, event: str, **fields: Any) -> dict[str, Any]:
        """Append one harness-local event to events.jsonl."""
        if not self._created:
            # Allow emit after create; auto-create dirs if needed
            self.paths.run_dir.mkdir(parents=True, exist_ok=True)
        rec: dict[str, Any] = {
            "event": event,
            "diagnostic_mono_ns": _monotonic_ns() - self._started_ns,
            "diagnostic_clock_origin": "harness_local_monotonic",
            "stream_session_id": self.stream_session_id,
            "wall_time": _now_iso(),
            "label": self.config.label,
        }
        rec.update(fields)
        line = json.dumps(rec, ensure_ascii=False) + "\n"
        with self.paths.events_jsonl.open("a", encoding="utf-8") as fh:
            fh.write(line)
        return rec

    def write_cancel(
        self,
        *,
        reason: str,
        play_pid: int | None = None,
        trigger: str = "barge",
    ) -> dict[str, Any]:
        payload = {
            "reason": reason,
            "trigger": trigger,
            "play_pid": play_pid,
            "cancelled_at": _now_iso(),
            "diagnostic_mono_ns": _monotonic_ns() - self._started_ns,
            "stream_session_id": self.stream_session_id,
            "label": self.config.label,
        }
        self.paths.assistant_cancel.write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )
        self.emit(
            "assistant_playback_cancelled",
            reason=reason,
            trigger=trigger,
            play_pid=play_pid,
        )
        return payload

    def write_pid(self, path: Path, pid: int | None) -> None:
        if pid is None:
            if path.exists():
                path.unlink()
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{pid}\n", encoding="utf-8")

    def read_pid(self, path: Path) -> int | None:
        if not path.is_file():
            return None
        raw = path.read_text(encoding="utf-8").strip()
        if not raw.isdigit():
            return None
        return int(raw)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stream_session_id": self.stream_session_id,
            "run_dir": str(self.paths.run_dir),
            "config": asdict(self.config),
        }
