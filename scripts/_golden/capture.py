"""Capture: direct WebSocket event subscription and event stream persistence.

Implements spec §7: subscribe directly to daemon events, wait for terminal
markers, persist `event-stream.jsonl`. Never tails a global log or sleeps.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from .constants import (
    DEFAULT_TERMINAL_MARKERS,
    EXIT_CAPTURE_INCOMPLETE,
    EXIT_CAPTURE_TIMEOUT,
    EXIT_DAEMON_UNREACHABLE,
    EXIT_EVENT_SCHEMA_INVALID,
    EXIT_INTERNAL_ERROR,
    EXIT_TERMINAL_MARKER_MISSING,
    SAMPLE_RATE,
    TERMINAL_EVENT_TYPES,
    Event,
    EventStream,
)


# ── transport abstraction ────────────────────────────────────────────────────

class EventTransport:
    """Abstract transport for receiving subscribed daemon events.

    Real implementations connect via WebSocket. Mock implementations
    replay from a list for deterministic testing.
    """

    async def connect(self, url: str, stream_session_id: str) -> None:
        raise NotImplementedError

    async def receive_event(self) -> Optional[str]:
        """Return next raw JSON event string, or None when closed."""
        raise NotImplementedError

    async def send_control(self, message: Dict[str, Any]) -> None:
        """Send a JSON control message."""
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError

    @property
    def closed_cleanly(self) -> bool:
        raise NotImplementedError

    @property
    def close_code(self) -> Optional[int]:
        raise NotImplementedError


class MockEventTransport(EventTransport):
    """Replays a pre-defined list of event JSON strings for deterministic tests."""

    def __init__(self, events: List[str], close_cleanly: bool = True,
                 close_code: Optional[int] = None, connect_delay: float = 0.0):
        self._events = events
        self._index = 0
        self._closed_cleanly = close_cleanly
        self._close_code = close_code
        self._connect_delay = connect_delay
        self._connected = False
        self._session_id: Optional[str] = None

    async def connect(self, url: str, stream_session_id: str) -> None:
        await asyncio.sleep(self._connect_delay)
        self._connected = True
        self._session_id = stream_session_id

    async def receive_event(self) -> Optional[str]:
        if not self._connected:
            return None
        if self._index < len(self._events):
            event = self._events[self._index]
            self._index += 1
            return event
        return None  # stream exhausted

    async def send_control(self, message: Dict[str, Any]) -> None:
        pass  # mock no-ops control

    async def close(self) -> None:
        self._connected = False

    @property
    def closed_cleanly(self) -> bool:
        return self._closed_cleanly

    @property
    def close_code(self) -> Optional[int]:
        return self._close_code


class WebsocketEventTransport(EventTransport):
    """Real WebSocket transport using the standard library (no external deps).

    Connects to daemon, sends SubscribeEvents, receives filtered events.
    """

    def __init__(self, timeout: float = 30.0, connect_timeout: float = 10.0):
        self._timeout = timeout
        self._connect_timeout = connect_timeout
        self._ws: Any = None
        self._closed_cleanly = False
        self._close_code: Optional[int] = None

    async def connect(self, url: str, stream_session_id: str) -> None:
        import socket
        import ssl

        from http.client import HTTPConnection, HTTPResponse
        from urllib.parse import urlparse

        parsed = urlparse(url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 8765
        path = parsed.path or "/"

        # Check daemon reachable first
        try:
            sock = socket.create_connection((host, port), timeout=self._connect_timeout)
        except OSError as e:
            raise ConnectionError(
                f"Daemon unreachable at {host}:{port}: {e}"
            ) from e

        # Build WebSocket upgrade manually (stdlib, no dependencies)
        try:
            key = os.urandom(16)
            import base64
            key_b64 = base64.b64encode(key).decode()

            request = (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {host}:{port}\r\n"
                f"Upgrade: websocket\r\n"
                f"Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key_b64}\r\n"
                f"Sec-WebSocket-Version: 13\r\n"
                f"\r\n"
            )
            sock.sendall(request.encode())

            # Read HTTP response
            response_data = b""
            while b"\r\n\r\n" not in response_data:
                chunk = sock.recv(4096)
                if not chunk:
                    raise ConnectionError("Daemon closed connection during handshake")
                response_data += chunk

            header_end = response_data.find(b"\r\n\r\n")
            headers = response_data[:header_end].decode(errors="replace")
            if "101" not in headers.split("\r\n")[0]:
                raise ConnectionError(f"WebSocket handshake failed: {headers.split(chr(10))[0]}")
        except Exception:
            sock.close()
            raise

        self._ws = _StdlibWebSocket(sock)
        self._closed_cleanly = False
        self._close_code = None

    async def receive_event(self) -> Optional[str]:
        if self._ws is None:
            return None
        try:
            frame = await asyncio.wait_for(
                asyncio.to_thread(self._ws.recv_frame),
                timeout=self._timeout,
            )
        except asyncio.TimeoutError:
            return None
        if frame is None:
            self._closed_cleanly = self._ws.close_code is not None
            self._close_code = self._ws.close_code
            return None
        opcode, payload = frame
        if opcode == 0x1:  # text
            return payload.decode("utf-8", errors="replace")
        elif opcode == 0x8:  # close
            self._closed_cleanly = True
            self._close_code = self._ws.close_code
            return None
        elif opcode == 0x9:  # ping
            self._ws.send_frame(0xA, payload)  # pong
            return await self.receive_event()
        return await self.receive_event()

    async def send_control(self, message: Dict[str, Any]) -> None:
        if self._ws is None:
            return
        text = json.dumps(message)
        self._ws.send_frame(0x1, text.encode())

    async def close(self) -> None:
        if self._ws is not None:
            self._ws.send_frame(0x8, b"")
            self._ws.close()
            self._ws = None

    @property
    def closed_cleanly(self) -> bool:
        return self._closed_cleanly

    @property
    def close_code(self) -> Optional[int]:
        return self._close_code


class _StdlibWebSocket:
    """Minimal RFC 6455 WebSocket client (text frames, ping/pong, close)."""

    def __init__(self, sock):
        self._sock = sock
        self.close_code: Optional[int] = None

    def recv_frame(self) -> Optional[Tuple[int, bytes]]:
        import struct
        try:
            header = self._recv_exact(2)
        except OSError:
            return None
        if len(header) < 2:
            return None

        byte0, byte1 = header[0], header[1]
        opcode = byte0 & 0x0F
        masked = (byte1 & 0x80) != 0
        length = byte1 & 0x7F

        if length == 126:
            ext = self._recv_exact(2)
            length = struct.unpack("!H", ext)[0]
        elif length == 127:
            ext = self._recv_exact(8)
            length = struct.unpack("!Q", ext)[0]

        if masked:
            mask_key = self._recv_exact(4)

        payload = self._recv_exact(length)
        if masked:
            payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))

        if opcode == 0x8 and len(payload) >= 2:
            self.close_code = struct.unpack("!H", payload[:2])[0]

        return (opcode, payload)

    def send_frame(self, opcode: int, payload: bytes) -> None:
        import struct
        frame = bytearray()
        frame.append(0x80 | opcode)
        length = len(payload)
        if length < 126:
            frame.append(length)
        elif length < 65536:
            frame.append(126)
            frame.extend(struct.pack("!H", length))
        else:
            frame.append(127)
            frame.extend(struct.pack("!Q", length))
        frame.extend(payload)
        self._sock.sendall(bytes(frame))

    def _recv_exact(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                break
            buf += chunk
        return buf

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


# ── event matching ───────────────────────────────────────────────────────────

def event_matches_filter(
    raw: str,
    stream_session_id: Optional[str] = None,
    event_type: Optional[str] = None,
) -> bool:
    """Check if a raw JSON event matches stream_session_id and optional event type."""
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return False
    if stream_session_id is not None:
        if value.get("stream_session_id") != stream_session_id:
            return False
    if event_type is not None:
        observed = value.get("event") or value.get("type")
        if observed != event_type:
            return False
    return True


def event_type_from_raw(raw: str) -> Optional[str]:
    """Extract event type from a raw JSON event string."""
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return value.get("event") or value.get("type")


# ── terminal marker detection ────────────────────────────────────────────────

def _marker_matches(marker_def: Dict[str, Any], event: Dict[str, Any]) -> bool:
    """Check if a parsed event satisfies a terminal marker definition."""
    event_name = marker_def.get("event")
    if event_name:
        observed = event.get("event") or event.get("type")
        if observed != event_name:
            return False
    where = marker_def.get("where", {})
    for key, expected in where.items():
        if event.get(key) != expected:
            return False
    return True


class TerminalMarkerTracker:
    """Tracks which terminal markers have been observed."""

    def __init__(self, required_markers: List[Dict[str, Any]]):
        self._required = required_markers
        self._observed: Set[int] = set()  # indices into _required

    @property
    def all_observed(self) -> bool:
        return len(self._observed) == len(self._required)

    @property
    def required_count(self) -> int:
        return len(self._required)

    @property
    def observed_count(self) -> int:
        return len(self._observed)

    @property
    def missing_markers(self) -> List[Dict[str, Any]]:
        return [self._required[i] for i in range(len(self._required)) if i not in self._observed]

    def feed(self, event: Dict[str, Any]) -> None:
        for i, marker_def in enumerate(self._required):
            if i not in self._observed and _marker_matches(marker_def, event):
                self._observed.add(i)


# ── main capture function ────────────────────────────────────────────────────

async def capture_events(
    transport: EventTransport,
    url: str,
    stream_session_id: str,
    out_dir: Path,
    *,
    terminal_markers: Optional[List[Dict[str, Any]]] = None,
    timeout_ms: int = 30_000,
    adapter_command: Optional[List[str]] = None,
    adapter_cwd: Optional[Path] = None,
) -> Tuple[int, EventStream, Dict[str, Any]]:
    """Subscribe, replay/capture, wait for terminal markers, persist.

    Returns (exit_code, events, validity_record).

    Args:
        transport: EventTransport implementation (real WS or mock).
        url: Daemon websocket URL.
        stream_session_id: Unique session id for this run.
        out_dir: Output directory for event-stream.jsonl.
        terminal_markers: Required terminal marker definitions.
        timeout_ms: Maximum wait for terminal markers.
        adapter_command: Optional adapter process command to spawn.
        adapter_cwd: Working directory for adapter.
    """
    if terminal_markers is None:
        terminal_markers = DEFAULT_TERMINAL_MARKERS

    out_dir.mkdir(parents=True, exist_ok=True)
    events_path = out_dir / "event-stream.jsonl"
    diagnostics_path = out_dir / "filtered-live-diagnostics.jsonl"

    events: EventStream = []
    diagnostics: EventStream = []
    validity: Dict[str, Any] = {
        "stream_session_id": stream_session_id,
        "capture_start_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "required_terminal_markers": terminal_markers,
        "observed_terminal_markers": [],
        "event_count": 0,
        "turn_closed_count": 0,
        "exit_code": None,
        "reason": None,
        "adapter_exit_code": None,
        "daemon_closed_cleanly": None,
        "daemon_close_code": None,
        "frame_sample_coverage": {"min_source_sample_start": None, "max_source_sample_end": None,
                                  "total_samples": 0, "gaps": []},
        "artifact_hashes": {},
        "valid": False,
    }

    tracker = TerminalMarkerTracker(terminal_markers)
    adapter_proc = None

    # ── spawn adapter if requested ───────────────────────────────────────
    if adapter_command:
        import subprocess
        adapter_proc = subprocess.Popen(
            adapter_command,
            cwd=str(adapter_cwd) if adapter_cwd else None,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

    start_time = time.monotonic()
    timeout_sec = timeout_ms / 1000.0
    adapter_exit_code = None

    try:
        # Connect and subscribe
        await transport.connect(url, stream_session_id)

        subscribe_msg = {
            "type": "subscribe_events",
            "stream_session_id": stream_session_id,
        }
        await transport.send_control(subscribe_msg)

        # ── receive loop ─────────────────────────────────────────────────
        while True:
            elapsed = time.monotonic() - start_time
            remaining = timeout_sec - elapsed
            if remaining <= 0:
                validity["reason"] = f"Capture timeout after {timeout_ms}ms"
                validity["exit_code"] = EXIT_CAPTURE_TIMEOUT
                validity["valid"] = False
                return EXIT_CAPTURE_TIMEOUT, events, validity

            raw = await transport.receive_event()
            if raw is None:
                # Stream ended (WebSocket closed or mock exhausted)
                if not transport.closed_cleanly:
                    validity["reason"] = "Daemon closed connection before terminal markers"
                    validity["exit_code"] = EXIT_CAPTURE_INCOMPLETE
                    validity["daemon_closed_cleanly"] = False
                    validity["daemon_close_code"] = transport.close_code
                    validity["valid"] = False
                    return EXIT_CAPTURE_INCOMPLETE, events, validity

                validity["daemon_closed_cleanly"] = transport.closed_cleanly
                validity["daemon_close_code"] = transport.close_code
                break  # clean close — check markers below

            # Parse event
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                validity["reason"] = f"Malformed JSON in event stream: {raw[:200]}"
                validity["exit_code"] = EXIT_EVENT_SCHEMA_INVALID
                validity["valid"] = False
                return EXIT_EVENT_SCHEMA_INVALID, events, validity

            # Validate session id if present
            evt_session = event.get("stream_session_id")
            if evt_session is not None and evt_session != stream_session_id:
                validity["reason"] = (
                    f"Wrong stream_session_id: expected {stream_session_id}, "
                    f"got {evt_session}"
                )
                validity["exit_code"] = EXIT_EVENT_SCHEMA_INVALID
                validity["valid"] = False
                return EXIT_EVENT_SCHEMA_INVALID, events, validity

            evt_type = event.get("event") or event.get("type")

            # Track sample coverage
            _update_sample_coverage(validity, event)

            # Classify: filtered diagnostics vs main event stream
            if evt_type in ("vad_meter", "turn_hold", "audio_frame_ingested", "vad_frame"):
                diagnostics.append(event)
            else:
                events.append(event)

            # Count terminal events
            if evt_type == "turn_closed":
                validity["turn_closed_count"] += 1

            # Feed terminal marker tracker
            tracker.feed(event)
            if evt_type in TERMINAL_EVENT_TYPES:
                validity["observed_terminal_markers"].append(
                    {"event": evt_type, "observed": True}
                )

            # Check if all markers observed
            if tracker.all_observed:
                validity["observed_terminal_markers"] = [
                    {"event": m["event"], "observed": True} for m in terminal_markers
                ]
                break

        # ── post-loop: verify terminal markers ───────────────────────────
        if not tracker.all_observed:
            missing = tracker.missing_markers
            validity["reason"] = f"Missing terminal markers: {missing}"
            validity["exit_code"] = EXIT_TERMINAL_MARKER_MISSING
            validity["missing_markers"] = missing
            validity["valid"] = False
            return EXIT_TERMINAL_MARKER_MISSING, events, validity

        # ── check adapter exit ───────────────────────────────────────────
        if adapter_proc is not None:
            try:
                adapter_exit_code = adapter_proc.poll()
                if adapter_exit_code is not None and adapter_exit_code != 0:
                    validity["reason"] = f"Adapter exited nonzero: {adapter_exit_code}"
                    validity["exit_code"] = EXIT_INTERNAL_ERROR
                    validity["adapter_exit_code"] = adapter_exit_code
                    validity["valid"] = False
                    return EXIT_INTERNAL_ERROR, events, validity
            except Exception:
                pass

        # ── write event stream and diagnostics ───────────────────────────
        _write_jsonl(events_path, events)
        if diagnostics:
            _write_jsonl(diagnostics_path, diagnostics)

        # ── compute hash ─────────────────────────────────────────────────
        events_hash = _sha256_file(events_path)
        validity["artifact_hashes"]["event_stream_sha256"] = events_hash
        validity["event_count"] = len(events)
        validity["capture_end_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        validity["adapter_exit_code"] = adapter_exit_code
        validity["valid"] = True
        validity["exit_code"] = 0
        validity["reason"] = "All terminal markers observed"

        return 0, events, validity

    finally:
        await transport.close()
        if adapter_proc is not None:
            try:
                adapter_proc.terminate()
                adapter_proc.wait(timeout=5)
            except Exception:
                try:
                    adapter_proc.kill()
                except Exception:
                    pass


# ── helpers ──────────────────────────────────────────────────────────────────

def _update_sample_coverage(validity: Dict[str, Any], event: Dict[str, Any]) -> None:
    """Track audio frame sample coverage from ingested events."""
    cov = validity["frame_sample_coverage"]
    sample_start = event.get("sample_start") or event.get("source_sample_start")
    sample_count = event.get("sample_count")
    if sample_start is not None and sample_count is not None:
        sample_start = int(sample_start)
        sample_count = int(sample_count)
        if cov["min_source_sample_start"] is None or sample_start < cov["min_source_sample_start"]:
            cov["min_source_sample_start"] = sample_start
        sample_end = sample_start + sample_count
        if cov["max_source_sample_end"] is None or sample_end > cov["max_source_sample_end"]:
            cov["max_source_sample_end"] = sample_end
        cov["total_samples"] += sample_count
    # Track gaps from gap events
    gap_event = event.get("audio_gap") or event.get("audio_sample_gap")
    if gap_event:
        cov["gaps"].append({
            "event": event.get("event") or event.get("type"),
            "details": gap_event if isinstance(gap_event, dict) else str(gap_event),
        })


def _write_jsonl(path: Path, events: EventStream) -> None:
    """Write events as JSONL file."""
    with open(path, "w", encoding="utf-8") as f:
        for event in events:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")


def _sha256_file(path: Path) -> str:
    """Compute SHA-256 hash of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
