"""Streamed GENERATE takes. not on the voicecat path.

One in-memory registry, one live stream. Segments are synthesized one at a time
through the resident E2 stream and yielded immediately as raw s16le PCM;
whatever was produced is recorded once, on completion or Stop, through the same
`record_synthesis_run` path `/api/synthesize` uses.
"""

from __future__ import annotations

import tempfile
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np

from tts.lab.backend.routes.synthesis import record_synthesis_run
from tts.lab.backend.runtime.worker import StreamCancelled
from tts.lab.backend.services.breeze import GenerationCancelled
from tts.packets import new_id
from tts.wav import duration_s, write_wav

SAMPLE_RATE = 24000
WAIT_S = 0.25


@dataclass
class GenerateStream:
    id: str
    voice_profile_id: str
    stop_event: threading.Event = field(default_factory=threading.Event, repr=False)
    run_id: str | None = None


class GenerationActive(RuntimeError):
    """Another generate stream still owns the engine."""


class GenerateStreams:
    """Registry of streamed takes. In-memory; no sqlite tables."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._streams: dict[str, GenerateStream] = {}
        self._active_id: str | None = None

    def begin(self, *, voice_profile_id: str) -> GenerateStream:
        """Claim the engine for one stream; GenerationActive while one runs."""
        with self._lock:
            if self._active_id is not None:
                raise GenerationActive(self._active_id)
            stream = GenerateStream(new_id("gen"), voice_profile_id)
            self._streams[stream.id] = stream
            self._active_id = stream.id
            return stream

    def get(self, stream_id: str) -> GenerateStream:
        """Return the registered stream; KeyError for an unknown id."""
        with self._lock:
            stream = self._streams.get(stream_id)
        if stream is None:
            raise KeyError(stream_id)
        return stream

    def stop(self, stream_id: str) -> GenerateStream:
        """End unborn work. Produced audio is still recorded as one take."""
        stream = self.get(stream_id)
        stream.stop_event.set()
        return stream

    def end(self, stream_id: str) -> None:
        """Release the engine so the next stream can begin."""
        with self._lock:
            if self._active_id == stream_id:
                self._active_id = None


def _payload(body: Any, resolved: Any, segment: str) -> dict[str, Any]:
    """The resident-worker request shape, with the enrolled reference every time."""
    return {
        "text": segment,
        "steer": body.steer,
        "synthesis_text": segment,
        "voice_profile_id": body.voice_profile_id,
        "reference_audio": str(resolved.reference_path),
        "reference_text": resolved.reference_text,
        "generation": resolved.settings.to_dict(),
    }


def iter_generate_pcm(
    state: Any,
    stream: GenerateStream,
    body: Any,
    resolved: Any,
    segments: list[str],
) -> Iterator[bytes]:
    """Yield the whole document as s16le PCM; record the produced audio as one take.

    Every segment sends the same enrolled reference, never accumulated PCM.
    Stop ends unborn segments and still records what was produced.
    """
    started = time.monotonic()
    first_audio_s: float | None = None
    pcm = bytearray()
    try:
        for segment in segments:
            if stream.stop_event.is_set():
                return
            while state.runtime.live_call_remaining_s() > 0:
                time.sleep(WAIT_S)
                if stream.stop_event.is_set():
                    return
            try:
                for chunk in state.runtime.synthesize_stream(
                    _payload(body, resolved, segment),
                    should_cancel=stream.stop_event.is_set,
                ):
                    if not chunk:
                        continue
                    if first_audio_s is None:
                        first_audio_s = time.monotonic() - started
                    pcm.extend(chunk)
                    yield chunk
            except (StreamCancelled, GenerationCancelled):
                return
    finally:
        try:
            _record_take(state, stream, body, resolved, pcm, started, first_audio_s)
        finally:
            state.generate_streams.end(stream.id)


def _record_take(
    state: Any,
    stream: GenerateStream,
    body: Any,
    resolved: Any,
    pcm: bytearray,
    started: float,
    first_audio_s: float | None,
) -> None:
    """One ordinary take for the produced audio. Nothing produced, nothing recorded."""
    raw = bytes(pcm)
    if len(raw) % 2:
        raw = raw[:-1]
    if not raw:
        return
    samples = np.frombuffer(raw, dtype="<i2")
    dest = Path(tempfile.mkdtemp(prefix=f"tts-lab-gen-{stream.id}-")) / "take.wav"
    write_wav(dest, SAMPLE_RATE, samples)
    result = SimpleNamespace(
        wav_path=dest,
        duration_s=duration_s(SAMPLE_RATE, samples),
        wall_s=time.monotonic() - started,
        first_audio_s=first_audio_s,
    )
    payload = record_synthesis_run(state, body, resolved, result)
    stream.run_id = str(payload["id"])
