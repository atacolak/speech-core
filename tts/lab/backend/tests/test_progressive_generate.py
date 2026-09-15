#!/usr/bin/env python3
"""Progressive GENERATE scheduler. CPU only; never touches CUDA."""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

import numpy as np

from tts.lab.backend.routes.synthesis import SynthesisBody, resolve_synthesis_request
from tts.lab.backend.runtime.leftover import NoopLeftover
from tts.lab.backend.runtime.manager import E2RuntimeManager
from tts.lab.backend.runtime.vram import FixedVramProbe
from tts.lab.backend.runtime.worker import (
    CountingWorkerFactory,
    FakeWorkerHandle,
    StreamCancelled,
)
from tts.lab.backend.services.progressive import ProgressiveJobs
from tts.wav import is_riff_wav, write_wav

GIB = 1024 ** 3


def wait_for(predicate, timeout_s: float = 2.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition did not become true")


class ProgressiveJobsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.factory = CountingWorkerFactory(
            handle_factory=lambda: FakeWorkerHandle(stream_delay_s=0.15)
        )
        self.runtime = E2RuntimeManager(
            vram=FixedVramProbe(free=12 * GIB),
            leftover=NoopLeftover(),
            worker_factory=self.factory,
            required_vram_bytes=8 * GIB,
            vram_margin_bytes=0,
        )
        self.runtime.load()
        from fastapi.testclient import TestClient

        from tts.lab.backend.app import create_app

        self.client = TestClient(
            create_app(root=Path(self.tmp.name), e2_runtime=self.runtime)
        )
        self.state = self.client.app.state.lab
        ref = Path(self.tmp.name) / "ref.wav"
        write_wav(ref, 24000, np.zeros(2400, dtype=np.float32))
        with ref.open("rb") as handle:
            self.voice = self.client.post(
                "/api/voices",
                data={"name": "job", "transcript": "fixture"},
                files={"audio": ("ref.wav", handle, "audio/wav")},
            ).json()
        self.jobs: ProgressiveJobs = self.state.progressive

    def tearDown(self) -> None:
        self.client.close()
        self.runtime.unload()
        self.tmp.cleanup()

    def _resolved(self, text: str):
        body = SynthesisBody(
            text=text,
            steer="calm",
            voice_profile_id=self.voice["id"],
        )
        return body, resolve_synthesis_request(self.state, body)

    def test_lookahead_waits_for_playback_cursor(self) -> None:
        body, resolved = self._resolved("One. Two. Three. Four. Five. Six.")
        job = self.jobs.create(
            self.state,
            body=body,
            resolved=resolved,
            segments=["One.", "Two.", "Three.", "Four.", "Five.", "Six."],
        )
        wait_for(lambda: sum(s.state == "generated" for s in job.segments) == 2)
        time.sleep(0.2)
        self.assertEqual(sum(s.state == "generated" for s in job.segments), 2)
        self.assertEqual(job.segments[2].state, "pending")
        self.jobs.set_cursor(job.id, 0)
        wait_for(lambda: job.segments[2].state == "generated")
        synth = [c for c in self.factory.handles[0].rpc_calls if c.get("cmd") == "synthesize"]
        self.assertEqual(len(synth), 3)

    def test_cancel_preserves_prefix_and_abandons_active_stream(self) -> None:
        body, resolved = self._resolved("One. Two. Three. Four.")
        job = self.jobs.create(
            self.state,
            body=body,
            resolved=resolved,
            segments=["One.", "Two.", "Three.", "Four."],
        )
        wait_for(lambda: job.segments[0].state == "generated")
        wait_for(lambda: job.segments[1].state == "generating")
        self.jobs.cancel(job.id)
        wait_for(lambda: job.state == "cancelled")
        self.assertTrue(self.factory.handles[0].cancel_requested)
        self.assertTrue(is_riff_wav(job.segments[0].wav_path))
        self.assertIsNone(job.run_id)
        rows = self.state.store.execute("SELECT id FROM runs").fetchall()
        self.assertEqual(rows, [])

    def test_live_call_interrupt_returns_segment_to_queue(self) -> None:
        body, resolved = self._resolved("One. Two. Three.")
        job = self.jobs.create(
            self.state,
            body=body,
            resolved=resolved,
            segments=["One.", "Two.", "Three."],
        )
        wait_for(lambda: job.segments[0].state == "generated")
        wait_for(lambda: job.segments[1].state == "generating")
        self.runtime.refresh_live_call_lease(ttl_s=1.0)
        wait_for(lambda: job.blocked_on_live_call)
        self.assertEqual(job.segments[1].state, "queued")
        self.assertTrue(self.factory.handles[0].cancel_requested)
        self.runtime.refresh_live_call_lease(ttl_s=0)
        wait_for(lambda: job.segments[1].state == "generated")

    def test_complete_records_one_composed_take(self) -> None:
        body, resolved = self._resolved("One. Two.")
        job = self.jobs.create(
            self.state,
            body=body,
            resolved=resolved,
            segments=["One.", "Two."],
        )
        wait_for(lambda: job.state == "complete")
        self.assertIsNotNone(job.run_id)
        rows = self.state.store.execute("SELECT * FROM runs").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], job.run_id)
        self.assertGreater(float(rows[0]["duration_s"]), 0)

    def test_runtime_cancel_predicate_abandons_before_next_pcm(self) -> None:
        cancel = threading.Event()
        request = {
            "text": "cancel callback",
            "steer": "",
            "voice_profile_id": self.voice["id"],
            "reference_audio": str(Path(self.tmp.name) / "ref.wav"),
            "reference_text": "fixture",
        }
        chunks = iter(self.runtime.synthesize_stream(request, should_cancel=cancel.is_set))
        self.assertGreater(len(next(chunks)), 0)
        cancel.set()
        with self.assertRaises(StreamCancelled):
            next(chunks)
        self.assertTrue(self.factory.handles[0].cancel_requested)


if __name__ == "__main__":
    unittest.main()
