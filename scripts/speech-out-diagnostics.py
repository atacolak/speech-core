#!/usr/bin/env python3
"""Output-only speech-out observability harness.

This tool deliberately skips mic/VAD/ASR/turn-taking. It either:

* feeds scripted text directly to `speech-out play` and observes the emitted
  speech-out websocket/playback events, or
* generates/replays deterministic JSONL events for TUI/reducer tests.

All displayed timings use this process' monotonic clock (`diagnostic_mono_ns`),
so daemon/client clock-domain differences do not make latency math ambiguous.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

NS_PER_MS = 1_000_000

FIXTURES = {
    "short": "heard you.",
    "chunked": (
        "First diagnostic sentence for speech output. "
        "Second sentence should become another text chunk. "
        "Third sentence makes progress and playback ordering visible."
    ),
    "barge": (
        "This longer diagnostic utterance is intended to be cancelled after "
        "first audio so cancel latency and terminal state are visible."
    ),
}


def monotonic_ns() -> int:
    return time.monotonic_ns()


def fmt_ms(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.1f}ms"


def event_time_ns(event: dict[str, Any]) -> int | None:
    for field_name in (
        "diagnostic_mono_ns",
        "client_mono_ns",
        "daemon_mono_ns",
        "mono_ns",
    ):
        value = event.get(field_name)
        if isinstance(value, int):
            return value
    return None


def event_name(event: dict[str, Any]) -> str:
    value = event.get("event") or event.get("type") or ""
    return str(value)


@dataclass
class ChunkState:
    text: str = ""
    started_ns: int | None = None
    first_audio_ns: int | None = None
    completed_ns: int | None = None
    playback_started_ns: int | None = None
    playback_ready_ns: int | None = None
    playback_completed_ns: int | None = None
    audio_chunks: int = 0
    bytes: int = 0


@dataclass
class DiagnosticState:
    utterance_id: str | None = None
    text: str = ""
    chunks: list[ChunkState] = field(default_factory=list)
    first_seen_ns: int | None = None
    request_queued_ns: int | None = None
    request_received_ns: int | None = None
    synthesis_started_ns: int | None = None
    first_audio_ns: int | None = None
    cancel_requested_ns: int | None = None
    cancel_acknowledged_ns: int | None = None
    terminal_ns: int | None = None
    terminal_outcome: str | None = None
    failure_message: str | None = None
    total_bytes: int = 0
    last_event: str = ""

    def observe(self, event: dict[str, Any]) -> str | None:
        name = event_name(event)
        if not name:
            return None
        now_ns = event_time_ns(event)
        if now_ns is None:
            return None
        if self.first_seen_ns is None:
            self.first_seen_ns = now_ns
        self.last_event = name
        utterance_id = event.get("utterance_id")
        if isinstance(utterance_id, str) and utterance_id:
            self.utterance_id = utterance_id

        if name == "speech_out_request_queued":
            self.request_queued_ns = now_ns
            self.text = str(event.get("text") or self.text)
            return f"QUEUED request text={self.text!r}"

        if name == "speech_out_request_received":
            self.request_received_ns = now_ns
            self.text = str(event.get("text") or self.text)
            count = event.get("num_chunks")
            return f"REQUEST received chunks={count if count is not None else '?'}"

        if name == "speech_out_text_chunks":
            raw_chunks = event.get("chunks")
            if isinstance(raw_chunks, list):
                self.chunks = [ChunkState(text=str(chunk)) for chunk in raw_chunks]
                total_chars = sum(len(chunk.text) for chunk in self.chunks)
                return f"CHUNKED {len(self.chunks)} text chunks chars={total_chars}"
            return "CHUNKED text chunks"

        if name == "speech_out_synthesis_started":
            self.synthesis_started_ns = now_ns
            return "SYNTH synthesis started"

        if name == "speech_out_text_chunk_started":
            index = int(event.get("text_chunk_index") or 0)
            chunk = self.ensure_chunk(index)
            chunk.started_ns = now_ns
            chunk.text = str(event.get("text") or chunk.text)
            count = event.get("text_chunk_count") or len(self.chunks) or "?"
            chars = event.get("chars") or len(chunk.text)
            return f"TEXT {index + 1}/{count} started chars={chars}"

        if name == "speech_out_audio_chunk":
            index = int(event.get("text_chunk_index") or 0)
            chunk = self.ensure_chunk(index)
            chunk.audio_chunks += 1
            byte_count = int(event.get("bytes") or 0)
            chunk.bytes += byte_count
            self.total_bytes = int(event.get("total_bytes") or self.total_bytes + byte_count)
            if chunk.first_audio_ns is None:
                chunk.first_audio_ns = now_ns
            if self.first_audio_ns is None:
                self.first_audio_ns = now_ns
                return f"FIRST_AUDIO chunk={index + 1} bytes={byte_count} first_audio={fmt_ms(self.first_audio_latency_ms())}"
            return f"AUDIO chunk={index + 1} seq={event.get('seq', '?')} bytes={byte_count} total={self.total_bytes}"

        if name == "speech_out_text_chunk_completed":
            index = int(event.get("text_chunk_index") or 0)
            chunk = self.ensure_chunk(index)
            chunk.completed_ns = now_ns
            chunk.bytes = int(event.get("bytes") or chunk.bytes)
            count = event.get("text_chunk_count") or len(self.chunks) or "?"
            duration = elapsed_ms(chunk.started_ns, chunk.completed_ns)
            return f"TEXT {index + 1}/{count} completed bytes={chunk.bytes} duration={fmt_ms(duration)}"

        if name == "speech_out_playback_started":
            index = int(event.get("playback_seq") or 0)
            chunk = self.ensure_chunk(index)
            chunk.playback_started_ns = now_ns
            return f"PLAYBACK {index + 1} started bytes={event.get('bytes', '?')}"

        if name in ("speech_out_playback_ready", "speech_out_playback_gate_released"):
            index = int(event.get("text_chunk_index") or event.get("playback_seq") or 0)
            chunk = self.ensure_chunk(index)
            chunk.playback_ready_ns = now_ns
            return f"PLAYBACK {index + 1} ready"

        if name == "speech_out_playback_completed":
            index = int(event.get("playback_seq") or 0)
            chunk = self.ensure_chunk(index)
            chunk.playback_completed_ns = now_ns
            duration = event.get("playback_duration_ms")
            if not isinstance(duration, (int, float)):
                duration = elapsed_ms(chunk.playback_started_ns, chunk.playback_completed_ns)
            return f"PLAYBACK {index + 1} ended duration={fmt_ms(float(duration) if duration is not None else None)}"

        if name == "speech_out_playback_failed":
            index = int(event.get("playback_seq") or 0)
            self.failure_message = str(event.get("message") or "playback failed")
            return f"PLAYBACK {index + 1} failed message={self.failure_message!r}"

        if name == "speech_out_cancel_requested":
            self.cancel_requested_ns = now_ns
            reason = event.get("reason") or "diagnostic_cancel"
            return f"CANCEL requested reason={reason}"

        if name in ("speech_out_cancel_acknowledged", "speech_out_cancel_ack"):
            self.cancel_acknowledged_ns = now_ns
            self.terminal_outcome = "cancelled"
            self.terminal_ns = now_ns
            return f"CANCEL acknowledged latency={fmt_ms(self.cancel_latency_ms())}"

        if name == "speech_out_completed":
            self.terminal_outcome = "completed"
            self.terminal_ns = now_ns
            self.total_bytes = int(event.get("bytes") or self.total_bytes)
            return f"SYNTH COMPLETED bytes={self.total_bytes} synth_e2e={fmt_ms(self.e2e_ms())} first_audio={fmt_ms(self.first_audio_latency_ms())}"

        if name == "speech_out_failed":
            self.terminal_outcome = "failed"
            self.terminal_ns = now_ns
            self.failure_message = str(event.get("message") or "unknown error")
            return f"FAILED message={self.failure_message!r} e2e={fmt_ms(self.e2e_ms())}"

        if name == "speech_out_diagnostic_terminal":
            outcome = str(event.get("outcome") or self.terminal_outcome or "unknown")
            self.terminal_outcome = outcome
            self.terminal_ns = now_ns
            event.setdefault("e2e_ms", self.e2e_ms())
            event.setdefault("first_audio_ms", self.first_audio_latency_ms())
            event.setdefault("cancel_latency_ms", self.cancel_latency_ms())
            return self.terminal_line(prefix="TERMINAL")

        return None

    def ensure_chunk(self, index: int) -> ChunkState:
        while len(self.chunks) <= index:
            self.chunks.append(ChunkState())
        return self.chunks[index]

    def e2e_ms(self) -> float | None:
        return elapsed_ms(self.request_queued_ns or self.first_seen_ns, self.terminal_ns)

    def first_audio_latency_ms(self) -> float | None:
        return elapsed_ms(self.request_queued_ns or self.request_received_ns, self.first_audio_ns)

    def cancel_latency_ms(self) -> float | None:
        return elapsed_ms(self.cancel_requested_ns, self.cancel_acknowledged_ns)

    def terminal_line(self, prefix: str = "summary") -> str:
        outcome = (self.terminal_outcome or "unknown").upper()
        pieces = [f"{prefix} {outcome}", f"e2e={fmt_ms(self.e2e_ms())}", f"first_audio={fmt_ms(self.first_audio_latency_ms())}"]
        if self.cancel_requested_ns is not None:
            pieces.append(f"cancel_latency={fmt_ms(self.cancel_latency_ms())}")
        if self.failure_message:
            pieces.append(f"message={self.failure_message!r}")
        if self.total_bytes:
            pieces.append(f"bytes={self.total_bytes}")
        return " ".join(pieces)


def elapsed_ms(start_ns: int | None, end_ns: int | None) -> float | None:
    if start_ns is None or end_ns is None:
        return None
    return max(0, end_ns - start_ns) / NS_PER_MS


class EventPrinter:
    def __init__(self, *, jsonl_out: Path | None = None, quiet: bool = False, append: bool = False) -> None:
        self.state = DiagnosticState()
        self.quiet = quiet
        mode = "a" if append else "w"
        self._jsonl_handle = jsonl_out.open(mode, encoding="utf-8") if jsonl_out else None

    def close(self) -> None:
        if self._jsonl_handle:
            self._jsonl_handle.close()
            self._jsonl_handle = None

    def emit(self, event: dict[str, Any]) -> None:
        event.setdefault("diagnostic_mono_ns", monotonic_ns())
        line = self.state.observe(event)
        if self._jsonl_handle:
            self._jsonl_handle.write(json.dumps(event, sort_keys=True) + "\n")
            self._jsonl_handle.flush()
        if line and not self.quiet:
            base = self.state.first_seen_ns or event_time_ns(event) or 0
            event_ns = event_time_ns(event) or base
            print(f"+{(event_ns - base) / NS_PER_MS:8.1f}ms {line}")

    def summary(self) -> str:
        return self.state.terminal_line()


class DeterministicClock:
    def __init__(self) -> None:
        self.now_ns = 1_000_000_000

    def advance(self, ms: int | float) -> int:
        self.now_ns += int(ms * NS_PER_MS)
        return self.now_ns


def chunk_text(text: str, min_chars: int, max_chars: int) -> list[str]:
    text = " ".join(text.split())
    if not text:
        return []
    max_chars = max(max_chars, min_chars + 1)
    chunks: list[str] = []
    remaining = text
    while len(remaining) > max_chars:
        window = remaining[:max_chars]
        split_at = -1
        for mark in (". ", "! ", "? "):
            candidate = window.rfind(mark)
            if candidate + 1 >= min_chars:
                split_at = max(split_at, candidate + 1)
        if split_at < min_chars:
            split_at = window.rfind(" ")
        if split_at < min_chars:
            split_at = max_chars
        chunks.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks


def fixture_texts(args: argparse.Namespace) -> list[tuple[str, str]]:
    selected: list[tuple[str, str]] = []
    for name in args.fixture or []:
        if name not in FIXTURES:
            raise SystemExit(f"unknown fixture {name!r}; choose one of: {', '.join(sorted(FIXTURES))}")
        selected.append((name, FIXTURES[name]))
    for i, text in enumerate(args.text or [], start=1):
        selected.append((f"text-{i}", text))
    if not selected:
        selected.append(("short", FIXTURES["short"]))
    return selected


def deterministic_events(
    *,
    text: str,
    fixture: str,
    utterance_id: str,
    chunk_min_chars: int,
    chunk_max_chars: int,
    cancel_after_ms: int | None,
) -> Iterable[dict[str, Any]]:
    clock = DeterministicClock()
    chunks = chunk_text(text, chunk_min_chars, chunk_max_chars) or [text]

    def ev(event: str, advance_ms: int | float, **fields: Any) -> dict[str, Any]:
        return {
            "event": event,
            "utterance_id": utterance_id,
            "fixture": fixture,
            "diagnostic_mono_ns": clock.advance(advance_ms),
            **fields,
        }

    yield ev("speech_out_request_queued", 0, text=text, state="queued")
    yield ev("speech_out_request_received", 4, text=text, num_chunks=len(chunks), chunk_sizes=[len(c) for c in chunks])
    yield ev("speech_out_text_chunks", 1, num_chunks=len(chunks), chunks=chunks)
    yield ev("speech_out_synthesis_started", 15, backend="mock-replay", streaming_mode="mock_text_chunked")

    cancel_deadline = None if cancel_after_ms is None else 1_000_000_000 + cancel_after_ms * NS_PER_MS
    total_bytes = 0
    for index, chunk in enumerate(chunks):
        if cancel_deadline is not None and clock.now_ns >= cancel_deadline:
            yield ev("speech_out_cancel_requested", 0, reason="mock_cancel_after_ms")
            yield ev("speech_out_cancel_acknowledged", 12, reason="mock_cancel_after_ms")
            yield ev("speech_out_diagnostic_terminal", 1, outcome="cancelled")
            return
        yield ev(
            "speech_out_text_chunk_started",
            10,
            text_chunk_index=index,
            text_chunk_count=len(chunks),
            text=chunk,
            chars=len(chunk),
        )
        bytes_this_chunk = 2048 + index * 256
        total_bytes += bytes_this_chunk
        yield ev(
            "speech_out_audio_chunk",
            55 if index == 0 else 30,
            seq=index,
            text_chunk_index=index,
            text_chunk_count=len(chunks),
            text_chunk_seq=0,
            bytes=bytes_this_chunk,
            total_bytes=total_bytes,
            format="wav",
        )
        if cancel_deadline is not None and clock.now_ns >= cancel_deadline:
            yield ev("speech_out_cancel_requested", 0, reason="mock_cancel_after_ms")
            yield ev("speech_out_cancel_acknowledged", 12, reason="mock_cancel_after_ms")
            yield ev("speech_out_diagnostic_terminal", 1, outcome="cancelled")
            return
        yield ev(
            "speech_out_text_chunk_completed",
            20,
            text_chunk_index=index,
            text_chunk_count=len(chunks),
            bytes=bytes_this_chunk,
            total_bytes=total_bytes,
        )
        yield ev("speech_out_playback_started", 2, playback_seq=index, bytes=bytes_this_chunk, play_command="mock")
        yield ev("speech_out_playback_completed", 35, playback_seq=index, bytes=bytes_this_chunk, playback_duration_ms=35.0)
        yield ev("speech_out_playback_ready", 1, playback_seq=index, text_chunk_index=index)
    yield ev("speech_out_completed", 3, bytes=total_bytes, chunk_count=len(chunks), total_synthesis_duration_ms=123.0)
    yield ev("speech_out_diagnostic_terminal", 1, outcome="completed")


def command_mock(args: argparse.Namespace) -> int:
    printer = EventPrinter(jsonl_out=args.jsonl_out, quiet=args.quiet)
    try:
        for fixture, text in fixture_texts(args):
            printer.state = DiagnosticState()
            utterance_id = args.utterance_id or f"mock-{fixture}"
            for event in deterministic_events(
                text=text,
                fixture=fixture,
                utterance_id=utterance_id,
                chunk_min_chars=args.chunk_min_chars,
                chunk_max_chars=args.chunk_max_chars,
                cancel_after_ms=args.cancel_after_ms,
            ):
                printer.emit(event)
            if not args.quiet:
                print(printer.summary())
    finally:
        printer.close()
    return 0


def command_replay(args: argparse.Namespace) -> int:
    printer = EventPrinter(quiet=args.quiet)
    try:
        with args.events.open("r", encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError as exc:
                    print(f"warning: skip invalid jsonl line: {exc}", file=sys.stderr)
                    continue
                printer.emit(event)
        if not args.quiet:
            print(printer.summary())
    finally:
        printer.close()
    return 0


def resolve_speech_out(binary: str | None) -> str:
    if binary:
        return binary
    env_binary = os.environ.get("SPEECH_OUT_BIN")
    if env_binary:
        return env_binary
    repo_candidate = Path(__file__).resolve().parents[1] / "target" / "debug" / "speech-out"
    if repo_candidate.exists():
        return str(repo_candidate)
    return "speech-out"


def reader_thread(stream: Any, out: "queue.Queue[tuple[str, str]]") -> None:
    for line in stream:
        out.put(("stderr", line.rstrip("\n")))
    out.put(("stderr_eof", ""))


def terminate_process_group(process: subprocess.Popen[str]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError:
        process.terminate()


def kill_process_group(process: subprocess.Popen[str]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except PermissionError:
        process.kill()


def command_run(args: argparse.Namespace) -> int:
    speech_out = resolve_speech_out(args.speech_out)
    overall_status = 0
    for fixture_index, (fixture, text) in enumerate(fixture_texts(args)):
        utterance_id = args.utterance_id or str(uuid.uuid4())
        printer = EventPrinter(jsonl_out=args.jsonl_out, quiet=args.quiet, append=fixture_index > 0)
        cancel_requested = False
        cancel_deadline_ns = None if args.cancel_after_ms is None else monotonic_ns() + args.cancel_after_ms * NS_PER_MS
        try:
            printer.emit(
                {
                    "event": "speech_out_request_queued",
                    "utterance_id": utterance_id,
                    "fixture": fixture,
                    "text": text,
                    "state": "queued",
                }
            )
            cmd = [
                speech_out,
                "play",
                "--url",
                args.url,
                "--utterance-id",
                utterance_id,
                "--voice",
                args.voice,
                "--lang",
                args.lang,
                "--steps",
                str(args.steps),
                "--speed",
                str(args.speed),
                "--play-command",
                args.play_command,
                "--chunk-min-chars",
                str(args.chunk_min_chars),
                "--chunk-max-chars",
                str(args.chunk_max_chars),
            ]
            for play_arg in args.play_arg or []:
                cmd.extend(["--play-arg", play_arg])
            if args.reference:
                cmd.extend(["--reference", args.reference])
            if args.style:
                cmd.extend(["--style", args.style])
            cmd.append(text)
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            assert process.stderr is not None
            events: "queue.Queue[tuple[str, str]]" = queue.Queue()
            thread = threading.Thread(target=reader_thread, args=(process.stderr, events), daemon=True)
            thread.start()
            stderr_done = False
            sigkill_deadline_ns: int | None = None
            while True:
                try:
                    source, line = events.get(timeout=0.02)
                except queue.Empty:
                    source, line = "", ""
                if source == "stderr_eof":
                    stderr_done = True
                elif source == "stderr" and line:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        if not args.quiet:
                            print(f"[speech-out] {line}", file=sys.stderr)
                    else:
                        event.setdefault("diagnostic_mono_ns", monotonic_ns())
                        printer.emit(event)
                        if event_name(event) == "speech_out_playback_completed":
                            playback_seq = int(event.get("playback_seq") or 0)
                            printer.emit(
                                {
                                    "event": "speech_out_playback_ready",
                                    "utterance_id": utterance_id,
                                    "playback_seq": playback_seq,
                                    "text_chunk_index": playback_seq,
                                }
                            )
                now_ns = monotonic_ns()
                if (
                    cancel_deadline_ns is not None
                    and not cancel_requested
                    and process.poll() is None
                    and now_ns >= cancel_deadline_ns
                ):
                    cancel_requested = True
                    printer.emit(
                        {
                            "event": "speech_out_cancel_requested",
                            "utterance_id": utterance_id,
                            "reason": "diagnostic_cancel_after_ms",
                        }
                    )
                    terminate_process_group(process)
                    sigkill_deadline_ns = now_ns + args.cancel_grace_ms * NS_PER_MS
                if sigkill_deadline_ns is not None and process.poll() is None and monotonic_ns() >= sigkill_deadline_ns:
                    kill_process_group(process)
                    sigkill_deadline_ns = None
                if process.poll() is not None and stderr_done and events.empty():
                    break
            exit_code = process.wait()
            thread.join(timeout=0.2)
            if cancel_requested:
                printer.emit(
                    {
                        "event": "speech_out_cancel_acknowledged",
                        "utterance_id": utterance_id,
                        "reason": "process_exited",
                        "exit_code": exit_code,
                    }
                )
                printer.emit(
                    {
                        "event": "speech_out_diagnostic_terminal",
                        "utterance_id": utterance_id,
                        "outcome": "cancelled",
                        "exit_code": exit_code,
                    }
                )
            elif exit_code != 0:
                overall_status = exit_code or 1
                if printer.state.terminal_outcome != "failed":
                    printer.emit(
                        {
                            "event": "speech_out_failed",
                            "utterance_id": utterance_id,
                            "message": f"speech-out play exited with {exit_code}",
                        }
                    )
                printer.emit(
                    {
                        "event": "speech_out_diagnostic_terminal",
                        "utterance_id": utterance_id,
                        "outcome": "failed",
                        "exit_code": exit_code,
                    }
                )
            else:
                # `speech_out_completed` is the daemon-side synthesis terminal event.
                # Emit an explicit diagnostic terminal after `speech-out play` exits so
                # e2e timing includes local playback completion/failure reporting too.
                outcome = "failed" if printer.state.failure_message else "completed"
                printer.emit(
                    {
                        "event": "speech_out_diagnostic_terminal",
                        "utterance_id": utterance_id,
                        "outcome": outcome,
                        "exit_code": exit_code,
                    }
                )
            if not args.quiet:
                print(printer.summary())
        finally:
            printer.close()
    return overall_status


def add_fixture_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--fixture", action="append", choices=sorted(FIXTURES), help="built-in text fixture; repeatable")
    parser.add_argument("--text", action="append", help="literal text fixture; repeatable")
    parser.add_argument("--utterance-id", help="fixed utterance id; defaults to UUID for live run and fixture name for mock")
    parser.add_argument("--chunk-min-chars", type=int, default=8)
    parser.add_argument("--chunk-max-chars", type=int, default=80)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="speech-out output-only diagnostics and deterministic replay")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="feed text fixtures directly to speech-out play and display observed output timings")
    add_fixture_args(run)
    run.add_argument("--speech-out", help="path to speech-out binary (default: SPEECH_OUT_BIN, target/debug/speech-out, then PATH)")
    run.add_argument("--url", default=os.environ.get("SPEECH_OUT_WS_URL", "ws://127.0.0.1:8788/ws/speech-out"))
    run.add_argument("--voice", default=os.environ.get("SPEECH_OUT_VOICE", "M1"))
    run.add_argument("--lang", default=os.environ.get("SPEECH_OUT_LANG", "en"))
    run.add_argument("--steps", type=int, default=int(os.environ.get("SPEECH_OUT_STEPS", "5")))
    run.add_argument("--speed", type=float, default=float(os.environ.get("SPEECH_OUT_SPEED", "1.30")))
    run.add_argument("--reference", default=os.environ.get("SPEECH_OUT_REFERENCE"))
    run.add_argument("--style", default=os.environ.get("SPEECH_OUT_STYLE"))
    run.add_argument("--play-command", default=os.environ.get("SPEECH_OUT_DIAG_PLAY_COMMAND", "true"), help="playback command; defaults to true for no-audio output diagnostics")
    run.add_argument("--play-arg", action="append", help="extra playback arg; repeatable")
    run.add_argument("--cancel-after-ms", type=int, help="request supervisor cancel after this many monotonic ms")
    run.add_argument("--cancel-grace-ms", type=int, default=1000)
    run.add_argument("--jsonl-out", type=Path, help="write combined diagnostic/speech-out events")
    run.add_argument("--quiet", action="store_true", help="only write --jsonl-out")
    run.set_defaults(func=command_run)

    mock = sub.add_parser("mock", help="emit deterministic speech-out events without daemon/audio backend")
    add_fixture_args(mock)
    mock.add_argument("--cancel-after-ms", type=int, help="deterministically cancel once this mock time has elapsed")
    mock.add_argument("--jsonl-out", type=Path, help="write deterministic events for TUI replay")
    mock.add_argument("--quiet", action="store_true", help="only write --jsonl-out")
    mock.set_defaults(func=command_mock)

    replay = sub.add_parser("replay", help="display timings from a diagnostic JSONL event log")
    replay.add_argument("events", type=Path)
    replay.add_argument("--quiet", action="store_true")
    replay.set_defaults(func=command_replay)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
