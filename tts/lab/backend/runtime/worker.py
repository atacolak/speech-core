"""Dedicated E2 worker process. CUDA lives here, not in FastAPI.

JSON-line protocol on stdin/stdout. stderr is logs. Process exit
destroys the CUDA context.

Every command carries `id`. The worker echoes that id on every event
and reply. Readers drop foreign/stale ids. Abandoned streams send
SIGUSR1 so synthesize_e2 stops, then drain to the matching terminal
reply (bounded); a failed drain marks the channel dirty so the parent
respawns before the next request.
"""

from __future__ import annotations

import base64
import json
import os
import signal
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from subprocess import PIPE, Popen
from typing import Any, Protocol

_cancel = threading.Event()


class WorkerChannelDirty(RuntimeError):
    """Stdout still holds a previous command's events; respawn before the next RPC."""


class StreamCancelled(RuntimeError):
    """A cooperative cancel predicate aborted an in-flight pcm_chunk stream."""


def _on_usr1(_signum: int, _frame: Any) -> None:
    _cancel.set()


def install_cancel_handler() -> None:
    try:
        signal.signal(signal.SIGUSR1, _on_usr1)
    except (ValueError, OSError):
        pass


def ensure_request_id(payload: dict[str, Any]) -> str:
    rid = payload.get("id")
    if rid is None or rid == "":
        rid = uuid.uuid4().hex
        payload["id"] = rid
    return str(rid)


def write_reply(payload: dict[str, Any], request_id: str | None = None) -> None:
    if request_id is not None:
        payload = dict(payload)
        payload["id"] = request_id
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def decode_rpc_line(line: str) -> dict[str, Any] | None:
    text = line.strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(payload, dict):
        return payload
    return None


def _event_matches_request(decoded: dict[str, Any], request_id: str | None) -> bool:
    if request_id is None:
        return True
    got = decoded.get("id")
    return got is not None and str(got) == str(request_id)


def read_rpc_events(
    stdout: Any,
    timeout: float,
    on_progress: Any | None = None,
    request_id: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield JSON-object events until a final reply (`ok` present)."""
    deadline = time.monotonic() + timeout
    junk: list[str] = []
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            preview = " | ".join(junk[-3:]) or "no stdout"
            raise TimeoutError(f"worker rpc timed out after {timeout}s ({preview})")
        line = ""

        def _read() -> None:
            nonlocal line
            line = stdout.readline()

        reader = threading.Thread(target=_read, name="e2-rpc-read", daemon=True)
        reader.start()
        reader.join(remaining)
        if reader.is_alive():
            preview = " | ".join(junk[-3:]) or "no stdout"
            raise TimeoutError(f"worker rpc timed out after {timeout}s ({preview})")
        if not line:
            preview = junk[-1] if junk else "eof"
            raise RuntimeError(f"worker closed stdout ({preview})")
        decoded = decode_rpc_line(line)
        if decoded is None:
            junk.append(line.strip()[:200])
            continue
        if not _event_matches_request(decoded, request_id):
            continue
        if decoded.get("event") == "load_progress":
            if on_progress is not None:
                on_progress(decoded)
            continue
        yield decoded
        if "ok" in decoded:
            return


def read_rpc_reply(
    stdout: Any,
    timeout: float,
    on_progress: Any | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Read one JSON-object reply, skipping banners and stream events."""
    for decoded in read_rpc_events(
        stdout, timeout, on_progress=on_progress, request_id=request_id
    ):
        if decoded.get("event") == "pcm_chunk":
            if on_progress is not None:
                on_progress(decoded)
            continue
        return decoded
    raise RuntimeError("worker closed stdout (no reply)")


def drain_rpc_until_reply(
    stdout: Any,
    timeout: float,
    request_id: str | None = None,
) -> dict[str, Any] | None:
    """Consume events until the matching terminal reply. Used after abandon."""
    try:
        for decoded in read_rpc_events(stdout, timeout, request_id=request_id):
            if decoded.get("event") == "pcm_chunk":
                continue
            return decoded
    except (TimeoutError, RuntimeError):
        return None
    return None


def _cuda_oom(exc: BaseException) -> bool:
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    return "outofmemory" in name or "out of memory" in text or "cuda oom" in text


def worker_main() -> int:
    install_cancel_handler()
    engine: Any = None
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as exc:
            write_reply({"ok": False, "code": "error", "message": f"bad json: {exc}"})
            continue
        cmd = str(msg.get("cmd") or "")
        raw_id = msg.get("id")
        request_id = str(raw_id) if raw_id not in (None, "") else None

        def reply(payload: dict[str, Any]) -> None:
            write_reply(payload, request_id)

        if cmd == "ping":
            reply({"ok": True, "pid": os.getpid()})
            continue
        if cmd == "load":
            if engine is not None:
                reply({"ok": True, "state": "ready", "pid": os.getpid()})
                continue
            try:
                from tts.breeze.runtime import load_selected_engine

                def _progress(phase: str) -> None:
                    reply({"event": "load_progress", "phase": phase})

                _progress("loading weights")
                engine = load_selected_engine(on_progress=_progress)
                reply({"ok": True, "state": "ready", "pid": os.getpid()})
            except Exception as exc:  # noqa: BLE001 - worker must stay up to reply
                code = "cuda_oom" if _cuda_oom(exc) else "error"
                engine = None
                reply({"ok": False, "code": code, "message": str(exc)})
            continue
        if cmd == "synthesize":
            if engine is None:
                reply({"ok": False, "code": "runtime_unloaded", "message": "E2 is unloaded"})
                continue
            try:
                from tts.generation import GenerationSettings
                from tts.lab.backend.services.breeze import (
                    GenerationCancelled,
                    SynthesisRequest,
                    synthesize_e2,
                )

                request = msg.get("request") or {}
                generation = request.get("generation") or {}
                settings = GenerationSettings.from_dict(generation)
                _cancel.clear()

                def _emit_pcm(chunk: Any) -> None:
                    if _cancel.is_set():
                        raise GenerationCancelled("synthesize cancelled")
                    pcm = getattr(chunk, "pcm", b"") or b""
                    if not pcm:
                        return
                    reply(
                        {
                            "event": "pcm_chunk",
                            "pcm_b64": base64.b64encode(pcm).decode("ascii"),
                            "sample_rate": int(getattr(chunk, "sample_rate", 24000) or 24000),
                        }
                    )

                result = synthesize_e2(
                    SynthesisRequest(
                        text=str(request.get("text") or ""),
                        steer=str(request.get("steer") or ""),
                        synthesis_text=request.get("synthesis_text"),
                        voice_profile_id=request.get("voice_profile_id"),
                        reference_audio=Path(str(request["reference_audio"])),
                        reference_text=str(request.get("reference_text") or ""),
                        generation=settings,
                        output_path=Path(str(request["output_path"]))
                        if request.get("output_path")
                        else None,
                    ),
                    engine=engine,
                    on_chunk=_emit_pcm,
                    cancel_event=_cancel,
                )
                reply(
                    {
                        "ok": True,
                        "result": {
                            "wav_path": str(result.wav_path),
                            "sample_rate": result.sample_rate,
                            "duration_s": result.duration_s,
                            "wall_s": result.wall_s,
                            "first_audio_s": result.first_audio_s,
                            "runtime": result.runtime,
                        },
                    }
                )
            except GenerationCancelled:
                reply({"ok": False, "code": "cancelled", "message": "synthesize cancelled"})
            except Exception as exc:  # noqa: BLE001
                reply({"ok": False, "code": "error", "message": str(exc)})
            continue
        if cmd == "shutdown":
            closer = getattr(engine, "close", None)
            if closer is not None:
                try:
                    closer()
                except Exception:
                    pass
            engine = None
            reply({"ok": True, "state": "unloaded"})
            return 0
        reply({"ok": False, "code": "error", "message": f"unknown cmd: {cmd}"})
    return 0


class WorkerHandle(Protocol):
    pid: int | None
    dirty: bool

    def start(self) -> None: ...
    def rpc(
        self,
        payload: dict[str, Any],
        timeout: float,
        on_progress: Any | None = None,
    ) -> dict[str, Any]: ...
    def iter_synthesize(
        self,
        payload: dict[str, Any],
        timeout: float,
        on_progress: Any | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> Iterator[bytes]: ...
    def is_alive(self) -> bool: ...
    def terminate(self, timeout: float = 2.0) -> None: ...
    def kill(self) -> None: ...


class SubprocessWorker:
    def __init__(
        self,
        *,
        python: str | None = None,
        env: dict[str, str] | None = None,
        module: str = "tts.lab.backend.runtime.worker",
    ) -> None:
        repo = Path(__file__).resolve().parents[4]
        self.python = python or sys.executable
        self.module = module
        self.env = dict(os.environ)
        if env:
            self.env.update(env)
        path = self.env.get("PYTHONPATH", "")
        parts = [str(repo)] + ([path] if path else [])
        qual = self.env.get("QUAL_ROOT")
        if qual:
            breeze_src = str(Path(qual) / "src" / "breeze-tts")
            if breeze_src not in parts:
                parts.append(breeze_src)
        self.env["PYTHONPATH"] = os.pathsep.join(parts)
        self.env.setdefault("PYTHONUNBUFFERED", "1")
        self._proc: Popen[str] | None = None
        self._lock = threading.Lock()
        self._stderr_lines: list[str] = []
        self.dirty = False

    @property
    def pid(self) -> int | None:
        if self._proc is None:
            return None
        return self._proc.pid

    def start(self) -> None:
        self.dirty = False
        self._proc = Popen(
            [self.python, "-m", self.module],
            stdin=PIPE,
            stdout=PIPE,
            stderr=PIPE,
            text=True,
            env=self.env,
            bufsize=1,
        )
        threading.Thread(target=self._drain_stderr, name="e2-stderr", daemon=True).start()

    def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        for raw in proc.stderr:
            self._stderr_lines.append(raw.rstrip())
            if len(self._stderr_lines) > 80:
                del self._stderr_lines[:-40]

    def _require_proc(self) -> Popen[str]:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.stdout is None:
            raise RuntimeError("worker not started")
        return proc

    def _send_locked(self, payload: dict[str, Any]) -> str:
        if self.dirty:
            raise WorkerChannelDirty("worker channel dirty; respawn required")
        proc = self._require_proc()
        request_id = ensure_request_id(payload)
        proc.stdin.write(json.dumps(payload) + "\n")
        proc.stdin.flush()
        return request_id

    def _signal_cancel(self) -> None:
        proc = self._proc
        if proc is None or proc.poll() is not None or proc.pid is None:
            return
        try:
            os.kill(proc.pid, signal.SIGUSR1)
        except OSError:
            pass

    def _drain_or_dirty(self, stdout: Any, timeout: float, request_id: str) -> None:
        reply = drain_rpc_until_reply(stdout, max(0.5, timeout), request_id=request_id)
        if reply is None:
            self.dirty = True

    def rpc(
        self,
        payload: dict[str, Any],
        timeout: float,
        on_progress: Any | None = None,
    ) -> dict[str, Any]:
        proc = self._require_proc()
        deadline = time.monotonic() + timeout
        with self._lock:
            request_id = self._send_locked(payload)
            try:
                return read_rpc_reply(
                    proc.stdout, timeout, on_progress=on_progress, request_id=request_id
                )
            except WorkerChannelDirty:
                raise
            except Exception as exc:
                remaining = max(0.5, deadline - time.monotonic())
                self._drain_or_dirty(proc.stdout, min(remaining, 30.0), request_id)
                err = self._stderr_lines[-8:]
                if err:
                    raise type(exc)(f"{exc}; stderr: {' | '.join(err)}") from exc
                raise

    def iter_synthesize(
        self,
        payload: dict[str, Any],
        timeout: float,
        on_progress: Any | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> Iterator[bytes]:
        proc = self._require_proc()
        deadline = time.monotonic() + timeout
        with self._lock:
            request_id = self._send_locked(payload)
            reply: dict[str, Any] | None = None
            try:
                for decoded in read_rpc_events(
                    proc.stdout, timeout, on_progress=on_progress, request_id=request_id
                ):
                    if decoded.get("event") == "pcm_chunk":
                        if should_cancel is not None and should_cancel():
                            self._signal_cancel()
                            remaining = max(0.5, min(8.0, deadline - time.monotonic()))
                            self._drain_or_dirty(proc.stdout, remaining, request_id)
                            raise StreamCancelled("synthesize cancelled")
                        pcm = base64.b64decode(decoded.get("pcm_b64") or "")
                        if pcm:
                            yield pcm
                        continue
                    reply = decoded
                    break
            except GeneratorExit:
                self._signal_cancel()
                remaining = max(0.5, min(8.0, deadline - time.monotonic()))
                self._drain_or_dirty(proc.stdout, remaining, request_id)
                raise
            except WorkerChannelDirty:
                raise
            except StreamCancelled:
                raise
            except Exception as exc:
                remaining = max(0.5, deadline - time.monotonic())
                self._drain_or_dirty(proc.stdout, min(remaining, 30.0), request_id)
                err = self._stderr_lines[-8:]
                if err:
                    raise type(exc)(f"{exc}; stderr: {' | '.join(err)}") from exc
                raise
            if reply is None:
                raise RuntimeError("worker closed stdout (no reply)")
            if not reply.get("ok"):
                raise RuntimeError(str(reply.get("message") or "synthesize failed"))

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def terminate(self, timeout: float = 2.0) -> None:
        proc = self._proc
        if proc is None:
            return
        if proc.poll() is None:
            try:
                if proc.stdin is not None:
                    proc.stdin.write(json.dumps({"cmd": "shutdown", "id": uuid.uuid4().hex}) + "\n")
                    proc.stdin.flush()
            except Exception:
                pass
            try:
                proc.wait(timeout=timeout)
            except Exception:
                proc.kill()
                proc.wait(timeout=2)
        self._proc = proc

    def kill(self) -> None:
        proc = self._proc
        if proc is None:
            return
        if proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=2)
            except Exception:
                pass


class CountingWorkerFactory:
    """Test double. Records spawn count; never starts a CUDA process."""

    def __init__(self, handle_factory: Any | None = None) -> None:
        self.spawn_count = 0
        self.handles: list[Any] = []
        self._handle_factory = handle_factory or FakeWorkerHandle

    def spawn(self) -> WorkerHandle:
        self.spawn_count += 1
        handle = self._handle_factory()
        self.handles.append(handle)
        return handle


class FakeWorkerHandle:
    """In-process worker double for unit tests."""

    next_pid = 4242

    def __init__(
        self,
        *,
        load_reply: dict[str, Any] | None = None,
        load_error: Exception | None = None,
        hang_load: bool = False,
        die_after_start: bool = False,
        ignore_shutdown: bool = False,
        stream_delay_s: float = 0.0,
    ) -> None:
        self.pid: int | None = None
        self.started = False
        self.killed = False
        self.terminated = False
        self.dirty = False
        self.rpc_calls: list[dict[str, Any]] = []
        self.load_reply = load_reply or {"ok": True, "state": "ready", "pid": None}
        self.load_error = load_error
        self.hang_load = hang_load
        self.die_after_start = die_after_start
        self.ignore_shutdown = ignore_shutdown
        self.stream_delay_s = float(stream_delay_s)
        self.cancel_requested = False
        self._alive = False

    def start(self) -> None:
        self.started = True
        type(self).next_pid += 1
        self.pid = type(self).next_pid
        self._alive = not self.die_after_start
        reply = dict(self.load_reply)
        if reply.get("pid") is None:
            reply["pid"] = self.pid
        self.load_reply = reply

    def _stamp(self, payload: dict[str, Any], reply: dict[str, Any]) -> dict[str, Any]:
        rid = payload.get("id")
        if rid in (None, ""):
            return reply
        stamped = dict(reply)
        stamped["id"] = rid
        return stamped

    def _synth_reply(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = payload.get("request") or {}
        dest = Path(str(request.get("output_path") or "take.wav"))
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.is_file():
            from tts.wav import write_wav
            import numpy as np

            write_wav(dest, 24000, np.zeros(int(24000 * 0.05), dtype=np.float32))
        return self._stamp(
            payload,
            {
                "ok": True,
                "result": {
                    "wav_path": str(dest),
                    "sample_rate": 24000,
                    "duration_s": 0.05,
                    "wall_s": 0.1,
                    "first_audio_s": 0.01,
                    "runtime": "E2",
                },
            },
        )

    def rpc(
        self,
        payload: dict[str, Any],
        timeout: float,
        on_progress: Any | None = None,
    ) -> dict[str, Any]:
        del timeout, on_progress
        if self.dirty:
            raise WorkerChannelDirty("worker channel dirty; respawn required")
        self.rpc_calls.append(payload)
        cmd = str(payload.get("cmd") or "")
        if self.hang_load and cmd == "load":
            time.sleep(10)
        if self.load_error is not None and cmd == "load":
            raise self.load_error
        if cmd == "load":
            return self._stamp(payload, dict(self.load_reply))
        if cmd == "synthesize":
            return self._synth_reply(payload)
        if cmd == "shutdown":
            if not self.ignore_shutdown:
                self._alive = False
            return self._stamp(payload, {"ok": True, "state": "unloaded"})
        return self._stamp(payload, {"ok": False, "code": "error", "message": f"unknown cmd: {cmd}"})

    def iter_synthesize(
        self,
        payload: dict[str, Any],
        timeout: float,
        on_progress: Any | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> Iterator[bytes]:
        reply = self.rpc(payload, timeout, on_progress=on_progress)
        if not reply.get("ok"):
            raise RuntimeError(str(reply.get("message") or "synthesize failed"))
        from tts.wav import as_pcm16, read_wav

        wav_path = Path(str((reply.get("result") or {})["wav_path"]))
        _sr, samples = read_wav(wav_path)
        pcm = as_pcm16(samples).tobytes()
        head = pcm[:960] or pcm
        tail = pcm[len(head) :]
        self.cancel_requested = False
        try:
            self._check_cancel(should_cancel)
            yield head
            if self.stream_delay_s > 0:
                time.sleep(self.stream_delay_s)
            self._check_cancel(should_cancel)
            if tail:
                yield tail
        except GeneratorExit:
            self.cancel_requested = True
            raise

    def _check_cancel(self, should_cancel: Callable[[], bool] | None) -> None:
        """Mirror the subprocess reader: cancel before delivering the next chunk."""
        if should_cancel is None or not should_cancel():
            return
        self.cancel_requested = True
        raise StreamCancelled("synthesize cancelled")

    def is_alive(self) -> bool:
        return self._alive

    def terminate(self, timeout: float = 2.0) -> None:
        del timeout
        self.terminated = True
        if not self.ignore_shutdown:
            self._alive = False

    def kill(self) -> None:
        self.killed = True
        self._alive = False

class ScriptedWorkerHandle(FakeWorkerHandle):
    """In-process double that streams caller-supplied pcm frames.

    `on_frame(index)` runs after the cancel check and before the frame is
    delivered — the same gap in which a real Stop lands a frame in the take
    buffer after the flush window has been latched.
    """

    def __init__(
        self,
        *,
        frames: list[bytes] | None = None,
        on_frame: Callable[[int], None] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.frames: list[bytes] = list(frames or [])
        self.on_frame = on_frame
        self.delivered = 0

    def iter_synthesize(
        self,
        payload: dict[str, Any],
        timeout: float,
        on_progress: Any | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> Iterator[bytes]:
        reply = self.rpc(payload, timeout, on_progress=on_progress)
        if not reply.get("ok"):
            raise RuntimeError(str(reply.get("message") or "synthesize failed"))
        self.cancel_requested = False
        try:
            for index, frame in enumerate(self.frames):
                self._check_cancel(should_cancel)
                if self.on_frame is not None:
                    self.on_frame(index)
                self.delivered += 1
                yield frame
        except GeneratorExit:
            self.cancel_requested = True
            raise


class FakeAukWorkerHandle:
    """In-process AuK worker double. Never touches CUDA, weights or torch."""

    next_pid = 5252

    def __init__(
        self,
        *,
        load_reply: dict[str, Any] | None = None,
        generate_reply: dict[str, Any] | None = None,
        hang_load: bool = False,
        die_after_start: bool = False,
        ignore_shutdown: bool = False,
    ) -> None:
        self.pid: int | None = None
        self.started = False
        self.killed = False
        self.terminated = False
        self.dirty = False
        self.rpc_calls: list[dict[str, Any]] = []
        self.load_reply = load_reply or {"ok": True}
        self.generate_reply = generate_reply
        self.hang_load = hang_load
        self.die_after_start = die_after_start
        self.ignore_shutdown = ignore_shutdown
        self.loaded = {"encoder": False, "model": False, "vae": False}
        self._alive = False

    def start(self) -> None:
        self.started = True
        type(self).next_pid += 1
        self.pid = type(self).next_pid
        self._alive = not self.die_after_start

    def _stamp(self, payload: dict[str, Any], reply: dict[str, Any]) -> dict[str, Any]:
        rid = payload.get("id")
        if rid in (None, ""):
            return reply
        return {**reply, "id": rid}

    def rpc(
        self,
        payload: dict[str, Any],
        timeout: float,
        on_progress: Any | None = None,
    ) -> dict[str, Any]:
        del timeout, on_progress
        if self.dirty:
            raise WorkerChannelDirty("worker channel dirty; respawn required")
        self.rpc_calls.append(payload)
        cmd = str(payload.get("cmd") or "")
        if self.hang_load and cmd == "load":
            time.sleep(10)
        if cmd == "ping":
            return self._stamp(payload, {"ok": True, "state": "ready", "pid": self.pid})
        if cmd == "load":
            if not self.load_reply.get("ok"):
                return self._stamp(payload, dict(self.load_reply))
            self.loaded = {"encoder": True, "model": True, "vae": True}
            return self._stamp(
                payload,
                {
                    "ok": True,
                    "result": {
                        "precision": payload.get("precision"),
                        "components": dict(self.loaded),
                        "vram_peak_bytes": 4 * 1024 * 1024,
                    },
                },
            )
        if cmd == "offload_component":
            component = str(payload.get("component"))
            self.loaded[component] = False
            return self._stamp(payload, {"ok": True, "result": {"components": dict(self.loaded)}})
        if cmd == "generate":
            request = dict(payload.get("request") or {})
            if self.generate_reply is not None:
                return self._stamp(payload, dict(self.generate_reply))
            dest = Path(str(request.get("output_path") or "take.wav"))
            dest.parent.mkdir(parents=True, exist_ok=True)
            if not dest.is_file():
                import numpy as np

                from tts.wav import write_wav

                write_wav(dest, 24000, np.zeros(1200, dtype=np.float32))
            return self._stamp(
                payload,
                {
                    "ok": True,
                    "result": {
                        "output_path": str(dest),
                        "sample_rate": 24000,
                        "duration_s": 0.05,
                        "wall_s": 0.1,
                        "vram_peak_bytes": 8 * 1024 * 1024,
                    },
                },
            )
        if cmd == "shutdown":
            if not self.ignore_shutdown:
                self._alive = False
            return self._stamp(payload, {"ok": True, "state": "unloaded"})
        return self._stamp(payload, {"ok": False, "code": "error", "message": f"unknown cmd: {cmd}"})

    def is_alive(self) -> bool:
        return self._alive

    def terminate(self, timeout: float = 2.0) -> None:
        del timeout
        self.terminated = True
        if not self.ignore_shutdown:
            self._alive = False

    def kill(self) -> None:
        self.killed = True
        self._alive = False


if __name__ == "__main__":
    raise SystemExit(worker_main())
