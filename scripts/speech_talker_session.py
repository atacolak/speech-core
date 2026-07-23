#!/usr/bin/env python3
"""Speech Talker session — thin edge over speech-core + Talker profile + speech-out.

MVP B loop:
  mic/ASR (speech-core) → transcript_committed → Talker (pi --profile talker)
  → speech-out play → speaker
  barge (first alphanumeric user token while speaking):
    (a) stop playout
    (b) cancel in-flight Talker generation
    (c) truncate last assistant turn to heard prefix (wall-clock approx)

This is the product wrapper, not a second conversational brain.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


def eprint(*args: object) -> None:
    print(*args, file=sys.stderr, flush=True)


def now_ms() -> int:
    return int(time.time() * 1000)


def mono_ns() -> int:
    return time.monotonic_ns()


def is_speech_evidence(token: str) -> bool:
    return bool(re.search(r"[A-Za-z0-9]", token or ""))


def first_sentence(text: str, max_chars: int = 120) -> str:
    t = (text or "").strip()
    if not t:
        return t
    m = re.search(r"[.!?](?:\s|$)", t)
    if m and m.end() <= max_chars:
        return t[: m.end()].strip()
    if len(t) <= max_chars:
        return t
    cut = t[:max_chars]
    sp = cut.rfind(" ")
    return (cut[:sp] if sp > 20 else cut).strip()


def approx_cut_by_played_ms(text: str, played_ms: int, wps: float = 2.6) -> str:
    """Cheap provisional cut: wall-clock words. Same idea as harness approx."""
    words = (text or "").split()
    if not words or played_ms <= 0:
        return ""
    n = max(1, min(len(words), int((played_ms / 1000.0) * wps + 0.5)))
    return " ".join(words[:n])


@dataclass
class TurnState:
    intended: str = ""
    spoken_prefix: str = ""
    hear_start_ms: int = 0
    barge_played_ms: int = 0
    utterance_id: str = ""
    cut_gen: int = 0


@dataclass
class SessionState:
    history: list[dict[str, str]] = field(default_factory=list)
    turn_text: str = ""
    turn_committed_seen: bool = False
    speaking: bool = False
    current: TurnState = field(default_factory=TurnState)
    cut_gen: int = 0
    echo_deadline_ms: int = 0
    last_spoken_full: str = ""


class ProcHandle:
    def __init__(self, name: str, popen: subprocess.Popen[Any]):
        self.name = name
        self.popen = popen

    @property
    def pid(self) -> Optional[int]:
        return self.popen.pid

    def alive(self) -> bool:
        return self.popen.poll() is None

    def kill_tree(self, sig: int = signal.SIGTERM) -> None:
        if not self.alive():
            return
        pid = self.popen.pid
        try:
            # kill process group if we started one
            os.killpg(pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                self.popen.send_signal(sig)
            except ProcessLookupError:
                return
        try:
            self.popen.wait(timeout=1.5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    self.popen.kill()
                except ProcessLookupError:
                    pass
            try:
                self.popen.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                pass


class SpeechTalkerSession:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.state = SessionState()
        self.run_dir = Path(args.run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.events_log = (self.run_dir / "events.jsonl").open("a", encoding="utf-8")
        self.trigger_log = (self.run_dir / "trigger.log").open("a", encoding="utf-8")
        self.history_path = self.run_dir / "talker_history.jsonl"
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._tts: Optional[ProcHandle] = None
        self._talker: Optional[ProcHandle] = None
        self._watch: Optional[ProcHandle] = None
        self._mic: Optional[ProcHandle] = None
        self._dispatch_thread: Optional[threading.Thread] = None
        self._turn_cancelled = threading.Event()
        self.session_id = args.stream_session_id or str(uuid.uuid4())
        # only set when operator passes --pi-session for an existing resume id
        self.pi_session_id = args.pi_session or ""

    def log(self, msg: str) -> None:
        line = f"[{time.strftime('%Y-%m-%dT%H:%M:%S')}] {msg}"
        print(line, flush=True)
        self.trigger_log.write(line + "\n")
        self.trigger_log.flush()

    def emit(self, event: dict[str, Any]) -> None:
        event.setdefault("diagnostic_mono_ns", mono_ns())
        event.setdefault("diagnostic_clock_origin", "talker_session_monotonic")
        event.setdefault("stream_session_id", self.session_id)
        line = json.dumps(event, ensure_ascii=False)
        self.events_log.write(line + "\n")
        self.events_log.flush()
        if self.args.print_events:
            print(line, flush=True)

    def persist_history(self) -> None:
        with self.history_path.open("w", encoding="utf-8") as f:
            for m in self.state.history:
                f.write(json.dumps(m, ensure_ascii=False) + "\n")

    def suppress_echo(self, text: str) -> bool:
        if now_ms() > self.state.echo_deadline_ms:
            return False
        norm = re.sub(r"\W+", " ", (text or "").lower()).strip()
        last = re.sub(r"\W+", " ", (self.state.last_spoken_full or "").lower()).strip()
        if not norm or not last:
            return False
        if norm in last or last in norm:
            return True
        # high overlap of tokens
        a, b = set(norm.split()), set(last.split())
        if not a:
            return False
        return len(a & b) / max(1, len(a)) >= 0.7

    def build_talker_prompt(self, user_text: str) -> str:
        parts = [
            "You are on a live voice channel (Talker front layer).",
            "Reply for speech: first sentence very short; then brief body if needed.",
            "Use ask_reasoner / send_to_reasoner tools only when the routing rules say so.",
            "Do not invent reasoner results from stubs.",
            "",
            "Conversation so far (may include barge-truncated assistant lines):",
        ]
        for m in self.state.history[-12:]:
            role = m.get("role", "?")
            parts.append(f"{role}: {m.get('text', '')}")
        parts.append(f"user: {user_text.strip()}")
        parts.append("")
        parts.append("assistant:")
        return "\n".join(parts)

    def start_mic(self) -> None:
        if self.args.no_mic:
            return
        mic = self.args.mic_adapter
        if not mic or not Path(mic).is_file():
            self.log(f"mic adapter missing ({mic}); run with external mic or --no-mic + --replay-events")
            return
        cmd = [
            mic,
            "--url",
            self.args.core_ws_url,
            "--stream-id",
            self.args.stream_id,
            "--stream-session-id",
            self.session_id,
            "--adapter-id",
            self.args.adapter_id,
            "--sample-rate-hz",
            str(self.args.sample_rate_hz),
            "--channels",
            str(self.args.channels),
            "--format",
            self.args.format,
            "--frame-ms",
            str(self.args.frame_ms),
        ]
        if self.args.record_wav:
            cmd.extend(["--record-wav", self.args.record_wav])
        self.log(f"start mic: {' '.join(cmd)}")
        p = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=(self.run_dir / "mic.stderr").open("a"),
            start_new_session=True,
        )
        self._mic = ProcHandle("mic", p)

    def start_watch(self) -> subprocess.Popen[str]:
        if self.args.replay_events:
            cmd = [
                self.args.watch_bin,
                "--replay-events",
                self.args.replay_events,
                "--mode",
                "jsonl",
            ]
        else:
            cmd = [
                self.args.watch_bin,
                "--url",
                self.args.core_ws_url,
                "--stream-id",
                self.args.stream_id,
                "--stream-session-id",
                self.session_id,
                "--mode",
                "jsonl",
            ]
        self.log(f"start watch: {' '.join(cmd)}")
        p = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=(self.run_dir / "watch.stderr").open("a"),
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        self._watch = ProcHandle("watch", p)
        assert p.stdout is not None
        return p

    def _set_echo_window(self, secs: float = 2.5) -> None:
        self.state.echo_deadline_ms = now_ms() + int(secs * 1000)

    # ── (a) stop playout ──────────────────────────────────────────────
    def stop_playout(self, trigger: str) -> None:
        with self._lock:
            tts = self._tts
            self._tts = None
            self.state.speaking = False
        if tts and tts.alive():
            self.log(f"(a) stop playout trigger={trigger} pid={tts.pid}")
            self.emit(
                {
                    "event": "speech_out_barge_in",
                    "trigger": trigger,
                    "tts_pid": tts.pid or 0,
                }
            )
            tts.kill_tree()
        self._set_echo_window()

    # ── (b) cancel in-flight Talker generation ────────────────────────
    def cancel_generation(self, trigger: str) -> None:
        with self._lock:
            talker = self._talker
            self._talker = None
        if talker and talker.alive():
            self.log(f"(b) cancel generation trigger={trigger} pid={talker.pid}")
            self.emit(
                {
                    "event": "talker_generation_cancelled",
                    "trigger": trigger,
                    "pid": talker.pid or 0,
                }
            )
            talker.kill_tree()

    # ── (c) truncate history to heard prefix ──────────────────────────
    def truncate_history_to_heard(self, trigger: str) -> str:
        with self._lock:
            cur = self.state.current
            intended = cur.intended
            hear_start = cur.hear_start_ms
            played = 0
            if hear_start > 0:
                played = max(0, now_ms() - hear_start)
            # cap later if we know audio length; for MVP wall-clock only
            prefix = approx_cut_by_played_ms(intended, played)
            if not prefix and intended:
                # never leave full text as "heard" on a real barge
                prefix = first_sentence(intended, 40)
            cur.spoken_prefix = prefix
            cur.barge_played_ms = played
            self.state.cut_gen += 1
            cur.cut_gen = self.state.cut_gen

            # rewrite last assistant message if it matches intended
            if self.state.history and self.state.history[-1].get("role") == "assistant":
                if self.state.history[-1].get("text") == intended or not prefix:
                    self.state.history[-1] = {
                        "role": "assistant",
                        "text": prefix or self.state.history[-1].get("text", ""),
                        "truncated": "true",
                        "played_ms": str(played),
                    }
            elif prefix:
                self.state.history.append(
                    {
                        "role": "assistant",
                        "text": prefix,
                        "truncated": "true",
                        "played_ms": str(played),
                    }
                )
            self.state.last_spoken_full = prefix
            self.persist_history()

        self.emit(
            {
                "event": "assistant_turn_truncated",
                "text": prefix,
                "intended_text": intended,
                "cut_text": prefix,
                "spoken_prefix": prefix,
                "primary_cut_source": "approx_wallclock",
                "played_ms": played,
                "confidence": "low",
                "cut_gen": cur.cut_gen,
                "utterance_id": cur.utterance_id,
                "trigger": trigger,
            }
        )
        self.log(f"(c) truncate played_ms={played} prefix={prefix!r}")
        return prefix

    def barge(self, trigger: str) -> None:
        """OpenAI-style interrupt triple."""
        with self._lock:
            if not self.state.speaking and not (self._tts and self._tts.alive()) and not (
                self._talker and self._talker.alive()
            ):
                return
        self._turn_cancelled.set()
        self.stop_playout(trigger)  # (a)
        self.cancel_generation(trigger)  # (b)
        # only truncate history if we already had assistant audio/text in flight
        with self._lock:
            had_intended = bool(self.state.current.intended)
        if had_intended:
            self.truncate_history_to_heard(trigger)  # (c)

    def run_talker(self, user_text: str) -> str:
        prompt = self.build_talker_prompt(user_text)
        # Multi-turn continuity is via history injected into the prompt.
        # pi --session only resumes an *existing* session id and fails otherwise.
        cmd = [
            self.args.pi_bin,
            "--profile",
            self.args.profile,
            "--model",
            self.args.model,
            "--thinking",
            self.args.thinking,
        ]
        if self.args.pi_session:
            cmd.extend(["--session", self.args.pi_session])
        cmd.extend(["-p", prompt])
        self.log(f"talker start profile={self.args.profile} session_opt={self.args.pi_session or '-'}")
        self.emit({"event": "talker_turn_started", "user_text": user_text})
        # capture stdout only; stderr to file
        err_path = self.run_dir / f"talker-{now_ms()}.stderr"
        p = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=err_path.open("w"),
            text=True,
            start_new_session=True,
        )
        with self._lock:
            self._talker = ProcHandle("talker", p)
        assert p.stdout is not None
        chunks: list[str] = []
        try:
            for line in p.stdout:
                if self._stop.is_set():
                    break
                chunks.append(line)
            rc = p.wait(timeout=self.args.talker_timeout_s)
        except subprocess.TimeoutExpired:
            self.log("talker timeout — killing")
            ProcHandle("talker", p).kill_tree()
            rc = -1
        finally:
            with self._lock:
                if self._talker and self._talker.popen is p:
                    self._talker = None
        text = "".join(chunks).strip()
        # strip common tool-json noise lines if any leaked
        cleaned_lines = []
        for ln in text.splitlines():
            s = ln.strip()
            if s.startswith("{") and '"status":"stub_logged"' in s:
                continue
            cleaned_lines.append(ln)
        text = "\n".join(cleaned_lines).strip()
        self.emit(
            {
                "event": "talker_turn_finished",
                "exit_code": rc,
                "assistant_text": text,
                "stderr_path": str(err_path),
            }
        )
        self.log(f"talker done rc={rc} chars={len(text)}")
        return text

    def run_speech_out(self, text: str) -> None:
        if not text.strip():
            return
        # Prefer short first chunk for cheap TTFA: speak full text; daemon chunks.
        # Optionally prepend nothing — Talker prompt already shortens first sentence.
        utt = str(uuid.uuid4())
        with self._lock:
            self.state.current = TurnState(
                intended=text,
                utterance_id=utt,
                hear_start_ms=0,
            )
            self.state.speaking = True
            self.state.last_spoken_full = text
            # append full assistant to history; barge may rewrite
            self.state.history.append({"role": "assistant", "text": text})
            self.persist_history()

        cmd = [
            self.args.speech_out_bin,
            "play",
            "--url",
            self.args.out_ws_url,
            "--utterance-id",
            utt,
            "--voice",
            self.args.voice,
            "--lang",
            self.args.lang,
            "--steps",
            str(self.args.steps),
            "--speed",
            str(self.args.speed),
            "--play-command",
            self.args.play_command,
            "--chunk-min-chars",
            str(self.args.chunk_min_chars),
            "--chunk-max-chars",
            str(self.args.chunk_max_chars),
            text,
        ]
        self.log(f"speech-out play utt={utt} chars={len(text)}")
        self.emit(
            {
                "event": "speech_out_request",
                "utterance_id": utt,
                "text": text,
            }
        )
        err = (self.run_dir / "speech-out.stderr").open("a")
        p = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=err,
            text=True,
            start_new_session=True,
        )
        with self._lock:
            self._tts = ProcHandle("tts", p)
            # provisional hear start = spawn; refined if we parse playback_started
            self.state.current.hear_start_ms = now_ms()

        # parse client stdout/stderr-ish events if printed as json lines on stdout
        assert p.stdout is not None

        def _reader() -> None:
            for line in p.stdout or []:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                et = ev.get("event") or ev.get("type")
                if et == "speech_out_playback_started":
                    with self._lock:
                        self.state.current.hear_start_ms = now_ms()
                    self.emit({"event": "speech_out_playback_started", "utterance_id": utt})
                elif et in ("speech_out_playback_completed", "speech_out_playback_cancelled"):
                    self.emit({"event": et, "utterance_id": utt, **{k: ev.get(k) for k in ("reason",) if k in ev}})

        th = threading.Thread(target=_reader, name="tts-reader", daemon=True)
        th.start()
        rc = p.wait()
        th.join(timeout=0.5)
        with self._lock:
            if self._tts and self._tts.popen is p:
                self._tts = None
            self.state.speaking = False
        self._set_echo_window(1.5)
        self.log(f"speech-out done rc={rc}")

    def dispatch_user_turn(self, user_text: str, source: str) -> None:
        user_text = (user_text or "").strip()
        if not user_text:
            self.log(f"{source}: empty transcript skip")
            return
        if self.suppress_echo(user_text):
            self.log(f"{source}: self-echo skip text={user_text!r}")
            self.emit({"event": "speech_out_skipped", "reason": "self_echo", "text": user_text})
            return

        # New user turn cancels any residual speak/gen
        self.stop_playout("new_response")
        self.cancel_generation("new_response")

        self.state.history.append({"role": "user", "text": user_text})
        self.persist_history()
        self.emit({"event": "talker_dispatch", "source": source, "text": user_text})

        def _job() -> None:
            try:
                self._turn_cancelled.clear()
                reply = self.run_talker(user_text)
                if self._stop.is_set() or self._turn_cancelled.is_set():
                    self.log("skip speak — cancelled or stopping")
                    return
                if not reply:
                    self.log("talker empty reply")
                    return
                self.run_speech_out(reply)
            except Exception as ex:  # noqa: BLE001
                self.log(f"dispatch error: {ex!r}")
                self.emit({"event": "talker_dispatch_error", "error": repr(ex)})

        # serialize dispatches: wait previous
        if self._dispatch_thread and self._dispatch_thread.is_alive():
            self.log("waiting prior dispatch…")
            self._dispatch_thread.join(timeout=self.args.talker_timeout_s + 30)
        self._dispatch_thread = threading.Thread(target=_job, name="dispatch", daemon=True)
        self._dispatch_thread.start()

    def handle_event(self, ev: dict[str, Any]) -> None:
        event = ev.get("event") or ""
        if event == "turn_started":
            self.state.turn_text = ""
            self.state.turn_committed_seen = False
            return

        if event == "transcript_token_committed":
            token = ev.get("text") or ""
            if is_speech_evidence(token):
                self.state.turn_text += token
                if self.state.speaking or (self._tts and self._tts.alive()) or (
                    self._talker and self._talker.alive()
                ):
                    self.barge("transcript_token_committed")
            return

        if event in ("transcript_committed", "turn_transcript_committed"):
            text = (ev.get("text") or "").strip()
            self.state.turn_text = text
            self.state.turn_committed_seen = True
            self.dispatch_user_turn(text, event)
            return

        if event == "turn_closed":
            if not self.state.turn_committed_seen:
                # legacy fallback
                text = (ev.get("text") or self.state.turn_text or "").strip()
                if text:
                    self.dispatch_user_turn(text, "turn_closed_legacy")
            self.state.turn_text = ""
            self.state.turn_committed_seen = False
            return

    def event_loop(self) -> None:
        watch = self.start_watch()
        assert watch.stdout is not None
        self.emit(
            {
                "event": "talker_session_started",
                "core_ws_url": self.args.core_ws_url,
                "out_ws_url": self.args.out_ws_url,
                "profile": self.args.profile,
                "model": self.args.model,
                "pi_session": self.pi_session_id,
                "run_dir": str(self.run_dir),
            }
        )
        try:
            for line in watch.stdout:
                if self._stop.is_set():
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # always archive
                self.events_log.write(line + "\n")
                self.events_log.flush()
                try:
                    self.handle_event(ev)
                except Exception as ex:  # noqa: BLE001
                    self.log(f"handle_event error: {ex!r}")
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        self.log("shutdown")
        self.stop_playout("session_end")
        self.cancel_generation("session_end")
        for h in (self._watch, self._mic):
            if h:
                h.kill_tree()
        self.emit({"event": "talker_session_ended"})
        self.events_log.close()
        self.trigger_log.close()


def resolve_bin(name: str, candidates: list[str]) -> str:
    for c in candidates:
        if c and Path(c).is_file() and os.access(c, os.X_OK):
            return c
    # PATH
    from shutil import which

    w = which(name)
    if w:
        return w
    return candidates[0] if candidates else name


def load_client_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip("'\"")
    return out


def build_arg_parser() -> argparse.ArgumentParser:
    home = Path.home()
    repo = Path(__file__).resolve().parent.parent
    env = load_client_env(Path(os.environ.get("SPEECH_CORE_CONFIG_FILE", home / ".config/speech-core/client.env")))

    p = argparse.ArgumentParser(description="Talker voice session edge (MVP B)")
    p.add_argument("--core-ws-url", default=os.environ.get("SPEECH_CORE_WS_URL", env.get("SPEECH_CORE_WS_URL", "ws://127.0.0.1:8765/ws/audio-ingress")))
    p.add_argument("--out-ws-url", default=os.environ.get("SPEECH_OUT_WS_URL", env.get("SPEECH_OUT_WS_URL", "ws://127.0.0.1:8788/ws/speech-out")))
    p.add_argument("--stream-id", default=os.environ.get("SPEECH_CORE_STREAM_ID", env.get("SPEECH_CORE_STREAM_ID", "laptop.live_mic")))
    p.add_argument("--stream-session-id", default=os.environ.get("SPEECH_CORE_STREAM_SESSION_ID", ""))
    p.add_argument("--adapter-id", default=os.environ.get("SPEECH_CORE_ADAPTER_ID", "laptop.cpal.default"))
    p.add_argument("--sample-rate-hz", type=int, default=int(os.environ.get("SPEECH_CORE_SAMPLE_RATE_HZ", "16000")))
    p.add_argument("--channels", type=int, default=1)
    p.add_argument("--format", default="pcm-s16-le")
    p.add_argument("--frame-ms", type=int, default=20)
    p.add_argument("--profile", default="talker")
    p.add_argument("--model", default=os.environ.get("TALKER_MODEL", "cpa/deepseek-v4-flash"))
    p.add_argument("--thinking", default=os.environ.get("TALKER_THINKING", "low"))
    p.add_argument("--pi-session", default="", help="stable pi session id for multi-turn")
    p.add_argument("--pi-bin", default=os.environ.get("PI_BIN", "/home/sf/workspace/.pi/bin/pi"))
    p.add_argument("--voice", default=os.environ.get("SPEECH_OUT_VOICE", env.get("SPEECH_OUT_VOICE", "M1")))
    p.add_argument("--lang", default=os.environ.get("SPEECH_OUT_LANG", "en"))
    p.add_argument("--steps", type=int, default=int(os.environ.get("SPEECH_OUT_STEPS", env.get("SPEECH_OUT_STEPS", "5"))))
    p.add_argument("--speed", type=float, default=float(os.environ.get("SPEECH_OUT_SPEED", env.get("SPEECH_OUT_SPEED", "1.30"))))
    p.add_argument("--play-command", default=os.environ.get("SPEECH_OUT_PLAY_COMMAND", env.get("SPEECH_OUT_PLAY_COMMAND", "pw-play")))
    p.add_argument("--chunk-min-chars", type=int, default=8)
    p.add_argument("--chunk-max-chars", type=int, default=160)
    p.add_argument("--run-dir", default=os.environ.get("SPEECH_TALKER_RUN_DIR", f"/tmp/speech-talker-{time.strftime('%Y%m%d-%H%M%S')}"))
    p.add_argument("--no-mic", action="store_true", help="do not start mic-adapter (events only)")
    p.add_argument("--replay-events", default="", help="jsonl replay instead of live watch")
    p.add_argument("--print-events", action="store_true")
    p.add_argument("--record-wav", default="")
    p.add_argument("--talker-timeout-s", type=int, default=120)
    p.add_argument("--once-text", default="", help="synthetic: skip mic/watch; one user turn then exit")
    p.add_argument(
        "--watch-bin",
        default=resolve_bin(
            "speech-core-watch",
            [
                os.environ.get("SPEECH_CORE_WATCH_BIN", ""),
                str(home / ".local/libexec/speech-core/speech-core-watch"),
                str(home / ".local/bin/speech-core-watch"),
                str(repo / "target/release/speech-core-watch"),
                str(repo / "target/debug/speech-core-watch"),
            ],
        ),
    )
    p.add_argument(
        "--speech-out-bin",
        default=resolve_bin(
            "speech-out",
            [
                os.environ.get("SPEECH_OUT_BIN", ""),
                str(home / ".local/bin/speech-out"),
                str(repo / "target/release/speech-out"),
                str(repo / "target/debug/speech-out"),
            ],
        ),
    )
    p.add_argument(
        "--mic-adapter",
        default=resolve_bin(
            "speech-core-mic-adapter",
            [
                os.environ.get("SPEECH_CORE_MIC_ADAPTER", ""),
                str(home / ".local/libexec/speech-core/speech-core-mic-adapter"),
                str(home / ".local/bin/speech-core-mic-adapter"),
                str(repo / "target/release/speech-core-mic-adapter"),
                str(repo / "target/debug/speech-core-mic-adapter"),
            ],
        ),
    )
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    sess = SpeechTalkerSession(args)

    def _sig(_signum: int, _frame: Any) -> None:
        eprint("signal — shutting down")
        sess.shutdown()
        sys.exit(130)

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    eprint(f"speech-talker-session run_dir={sess.run_dir}")
    eprint(f"  core={args.core_ws_url}")
    eprint(f"  out={args.out_ws_url}")
    eprint(f"  profile={args.profile} model={args.model}")
    eprint(f"  session={sess.session_id}")

    if args.once_text:
        # synthetic single-turn dogfood without mic
        sess.dispatch_user_turn(args.once_text, "once_text")
        if sess._dispatch_thread:
            sess._dispatch_thread.join(timeout=args.talker_timeout_s + 60)
        sess.shutdown()
        return 0

    if not args.replay_events:
        sess.start_mic()
    sess.event_loop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
