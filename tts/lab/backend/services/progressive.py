"""Progressive GENERATE jobs. not on the voicecat path.

One in-memory registry, one running job, one scheduler thread. Segments are
synthesized one at a time through the resident E2 stream and written as wavs
in the job's temp dir; the composed take goes through the same
`record_synthesis_run` path `/api/synthesize` uses.
"""

from __future__ import annotations

import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

import numpy as np

from tts.lab.backend.routes.synthesis import record_synthesis_run
from tts.lab.backend.runtime.worker import StreamCancelled
from tts.lab.backend.services.breeze import (
    GenerationCancelled,
    SynthesisRequest,
    synthesize_e2,
)
from tts.packets import new_id
from tts.wav import duration_s, read_wav, write_wav

SAMPLE_RATE = 24000
LOOKAHEAD_SEGMENTS = 2
WAIT_S = 0.25

SegmentState = Literal["pending", "queued", "generating", "generated", "cancelled", "error"]
JobState = Literal["running", "complete", "cancelled", "error"]


def segment_to_dict(job_id: str, segment: Segment) -> dict[str, Any]:
    """Public segment status. Only a generated segment carries a playable URL."""
    return {
        "index": segment.index,
        "text": segment.text,
        "state": segment.state,
        "duration_s": segment.duration_s,
        "audio_url": (
            f"/api/generate/{job_id}/segments/{segment.index}/audio"
            if segment.state == "generated"
            else None
        ),
    }


@dataclass
class Segment:
    index: int
    text: str
    state: SegmentState = "pending"
    wav_path: Path | None = None
    duration_s: float | None = None
    first_audio_s: float | None = None


@dataclass
class ProgressiveJob:
    id: str
    voice_profile_id: str
    segments: list[Segment]
    root: Path
    body: Any
    resolved: Any
    state: JobState = "running"
    cursor: int = -1
    lookahead: int = LOOKAHEAD_SEGMENTS
    blocked_on_live_call: bool = False
    cancel_requested: bool = False
    run_id: str | None = None
    output_artifact_id: str | None = None
    error: str | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    changed: threading.Condition = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.changed = threading.Condition(self.lock)

    def to_dict(self) -> dict[str, Any]:
        with self.lock:
            return {
                "id": self.id,
                "state": self.state,
                "cursor": self.cursor,
                "lookahead": self.lookahead,
                "blocked_on_live_call": self.blocked_on_live_call,
                "run_id": self.run_id,
                "output_artifact_id": self.output_artifact_id,
                "error": self.error,
                "segments": [segment_to_dict(self.id, item) for item in self.segments],
            }


class GenerationActive(RuntimeError):
    """Another progressive job still owns the engine."""


class ProgressiveJobs:
    """Registry plus one scheduler thread per job. In-memory; no sqlite tables."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._jobs: dict[str, ProgressiveJob] = {}

    def create(self, state: Any, *, body: Any, resolved: Any, segments: list[str]) -> ProgressiveJob:
        """Reject a running job, snapshot inputs, register it, and start its thread."""
        with self._lock:
            active = next(
                (job for job in self._jobs.values() if job.state == "running"), None
            )
            if active is not None:
                raise GenerationActive(active.id)
            job_id = new_id("gen")
            job = ProgressiveJob(
                id=job_id,
                voice_profile_id=body.voice_profile_id,
                segments=[
                    Segment(index=index, text=text) for index, text in enumerate(segments)
                ],
                root=Path(tempfile.mkdtemp(prefix=f"tts-lab-gen-{job_id}-")),
                body=body.model_copy(deep=True),
                resolved=resolved,
            )
            self._jobs[job_id] = job
        threading.Thread(
            target=self._run,
            args=(state, job),
            name=f"progressive-{job_id}",
            daemon=True,
        ).start()
        return job

    def get(self, job_id: str) -> ProgressiveJob:
        """Return the registered job; KeyError for an unknown id."""
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise KeyError(job_id)
        return job

    def set_cursor(self, job_id: str, index: int) -> ProgressiveJob:
        """Advance completed playback by one and wake the scheduler."""
        job = self.get(job_id)
        with job.changed:
            if index < job.cursor or index > job.cursor + 1:
                raise ValueError(f"cursor {index} is not in [{job.cursor}, {job.cursor + 1}]")
            job.cursor = index
            job.changed.notify_all()
        return job

    def cancel(self, job_id: str) -> ProgressiveJob:
        """Set the cancellation signal and wake the scheduler."""
        job = self.get(job_id)
        with job.changed:
            job.cancel_requested = True
            job.cancel_event.set()
            job.changed.notify_all()
        return job

    def _run(self, state: Any, job: ProgressiveJob) -> None:
        started = time.monotonic()
        try:
            self._schedule(state, job, started)
        except Exception as exc:  # noqa: BLE001 - a dead thread must not look running
            with job.changed:
                if job.state == "running":
                    job.state = "error"
                    job.error = str(exc)
                job.changed.notify_all()

    def _schedule(self, state: Any, job: ProgressiveJob, started: float) -> None:
        """Drive one segment at a time inside the lookahead window."""
        while True:
            with job.changed:
                if job.cancel_requested or job.state != "running":
                    break
                if state.runtime.live_call_remaining_s() > 0:
                    job.blocked_on_live_call = True
                    job.changed.wait(timeout=WAIT_S)
                    continue
                job.blocked_on_live_call = False
                window = job.cursor + job.lookahead
                for segment in job.segments:
                    if segment.state == "pending" and segment.index <= window:
                        segment.state = "queued"
                next_segment = next(
                    (item for item in job.segments if item.state == "queued"), None
                )
                if next_segment is None:
                    if not all(item.state == "generated" for item in job.segments):
                        job.changed.wait(timeout=WAIT_S)
                        continue
                else:
                    next_segment.state = "generating"
            if next_segment is None:
                self._compose(state, job, started)
                return
            self._synthesize(state, job, next_segment)
        with job.changed:
            if job.state == "running":
                job.state = "cancelled"
            job.changed.notify_all()

    def _synthesize(self, state: Any, job: ProgressiveJob, segment: Segment) -> None:
        try:
            pcm, sample_rate, first_audio_s = _segment_pcm(state, job, segment)
        except (StreamCancelled, GenerationCancelled):
            with job.changed:
                if job.cancel_event.is_set():
                    segment.state = "cancelled"
                else:
                    job.blocked_on_live_call = state.runtime.live_call_remaining_s() > 0
                    segment.state = "queued"
                job.changed.notify_all()
            return
        except Exception as exc:  # noqa: BLE001 - surfaced as job error, never swallowed
            with job.changed:
                segment.state = "error"
                job.state = "error"
                job.error = str(exc)
                job.changed.notify_all()
            return
        samples = np.frombuffer(pcm, dtype="<i2")
        dest = job.root / f"seg_{segment.index:04d}.wav"
        write_wav(dest, sample_rate, samples)
        with job.changed:
            segment.wav_path = dest
            segment.duration_s = duration_s(sample_rate, samples)
            segment.first_audio_s = first_audio_s
            segment.state = "generated"
            job.changed.notify_all()

    def _compose(self, state: Any, job: ProgressiveJob, started: float) -> None:
        """Concatenate every segment into one take through record_synthesis_run."""
        composed = _compose_wav(job)
        sample_rate, samples = read_wav(composed)
        result = SimpleNamespace(
            wav_path=composed,
            duration_s=duration_s(sample_rate, samples),
            wall_s=time.monotonic() - started,
            first_audio_s=job.segments[0].first_audio_s,
        )
        payload = record_synthesis_run(state, job.body, job.resolved, result)
        with job.changed:
            job.run_id = str(payload["id"])
            job.output_artifact_id = str(payload["output_artifact_id"])
            job.state = "complete"
            job.changed.notify_all()


def _compose_wav(job: ProgressiveJob) -> Path:
    frames = [read_wav(segment.wav_path)[1] for segment in job.segments]
    dest = job.root / "composed.wav"
    write_wav(dest, SAMPLE_RATE, np.concatenate(frames))
    return dest


def _segment_pcm(
    state: Any, job: ProgressiveJob, segment: Segment
) -> tuple[bytes, int, float | None]:
    """s16le, sample rate, seconds to first audio. The resident stream, never a new engine."""
    payload = {
        "text": segment.text,
        "steer": job.body.steer,
        "synthesis_text": segment.text,
        "voice_profile_id": job.body.voice_profile_id,
        "reference_audio": str(job.resolved.reference_path),
        "reference_text": job.resolved.reference_text,
        "generation": job.resolved.settings.to_dict(),
    }
    if state.engine is not None:
        result = synthesize_e2(
            SynthesisRequest(
                text=segment.text,
                steer=job.body.steer,
                synthesis_text=segment.text,
                voice_profile_id=job.body.voice_profile_id,
                reference_audio=job.resolved.reference_path,
                reference_text=job.resolved.reference_text,
                generation=job.resolved.settings,
            ),
            engine=state.engine,
            cancel_event=job.cancel_event,
        )
        return result.pcm_s16le, result.sample_rate, result.first_audio_s

    def interrupted() -> bool:
        return job.cancel_event.is_set() or state.runtime.live_call_remaining_s() > 0

    pcm: list[bytes] = []
    started = time.monotonic()
    first_audio_s: float | None = None
    for chunk in state.runtime.synthesize_stream(payload, should_cancel=interrupted):
        if first_audio_s is None:
            first_audio_s = time.monotonic() - started
        pcm.append(chunk)
    return b"".join(pcm), SAMPLE_RATE, first_audio_s
