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

    def _job_status(self, job_id: str) -> dict:
        response = self.client.get(f"/api/generate/{job_id}")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _start_long_job(self) -> str:
        """A document with more segments than the lookahead window, so it keeps running."""
        started = self.client.post(
            "/api/generate",
            json={
                "text": "The quick brown fox jumps over the lazy dog. " * 40,
                "steer": "calm",
                "voice_profile_id": self.voice["id"],
            },
        )
        self.assertEqual(started.status_code, 200, started.text)
        return started.json()["id"]

    def test_http_first_segment_is_playable_before_document_complete(self) -> None:
        started = self.client.post(
            "/api/generate",
            json={
                "text": "First sentence. " * 40 + "Final sentence.",
                "steer": "calm",
                "voice_profile_id": self.voice["id"],
            },
        )
        self.assertEqual(started.status_code, 200, started.text)
        job_id = started.json()["id"]
        wait_for(
            lambda: self.client.get(f"/api/generate/{job_id}").json()["segments"][0]["state"]
            == "generated"
        )
        status = self.client.get(f"/api/generate/{job_id}").json()
        self.assertEqual(status["state"], "running")
        states = {segment["state"] for segment in status["segments"]}
        self.assertIn("generated", states)
        self.assertTrue(states & {"generating", "queued", "pending"})
        audio = self.client.get(f"/api/generate/{job_id}/segments/0/audio")
        self.assertEqual(audio.status_code, 200, audio.text)
        self.assertEqual(audio.headers["content-type"], "audio/wav")
        self.assertEqual(audio.content[:4], b"RIFF")

    def test_http_cursor_cancel_and_errors(self) -> None:
        job = self._start_long_job()
        missing = self.client.get("/api/generate/job_missing")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["detail"]["code"], "job_not_found")
        early = self.client.get(f"/api/generate/{job}/segments/5/audio")
        self.assertEqual(early.status_code, 409)
        self.assertEqual(early.json()["detail"]["code"], "segment_not_ready")
        bad = self.client.post(f"/api/generate/{job}/cursor", json={"index": 99})
        self.assertEqual(bad.status_code, 422)
        self.assertEqual(bad.json()["detail"]["code"], "invalid_cursor")
        jumped = self.client.post(f"/api/generate/{job}/cursor", json={"index": 5})
        self.assertEqual(jumped.status_code, 422)
        self.assertEqual(jumped.json()["detail"]["code"], "invalid_cursor")
        wait_for(lambda: self._job_status(job)["segments"][0]["state"] == "generated")
        cursor = self.client.post(f"/api/generate/{job}/cursor", json={"index": 0})
        self.assertEqual(cursor.status_code, 200)
        self.assertEqual(cursor.json()["cursor"], 0)
        wait_for(lambda: self._job_status(job)["segments"][2]["state"] != "pending")
        cancelled = self.client.post(f"/api/generate/{job}/cancel")
        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(cancelled.json()["segments"][0]["state"], "generated")
        wait_for(lambda: self._job_status(job)["state"] == "cancelled")
        again = self.client.post(f"/api/generate/{job}/cancel")
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json()["state"], "cancelled")
        scheduled = len(self._synth_calls())
        time.sleep(0.3)
        self.assertEqual(len(self._synth_calls()), scheduled)
        self.assertEqual(self._job_status(job)["segments"][0]["state"], "generated")
        audio = self.client.get(f"/api/generate/{job}/segments/0/audio")
        self.assertEqual(audio.status_code, 200, audio.text)
        self.assertEqual(audio.content[:4], b"RIFF")

    def test_http_unknown_segment_index_is_a_404(self) -> None:
        job = self._start_long_job()
        beyond = self.client.get(f"/api/generate/{job}/segments/999/audio")
        self.assertEqual(beyond.status_code, 404)
        self.assertEqual(beyond.json()["detail"]["code"], "segment_not_found")

    def test_http_empty_text_is_rejected(self) -> None:
        response = self.client.post(
            "/api/generate",
            json={"text": "   \n  ", "steer": "calm", "voice_profile_id": self.voice["id"]},
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"]["code"], "empty_text")

    def test_http_live_call_and_running_job_conflict(self) -> None:
        self.runtime.refresh_live_call_lease(ttl_s=1.0)
        blocked = self.client.post(
            "/api/generate",
            json={
                "text": "First sentence. " * 40,
                "steer": "calm",
                "voice_profile_id": self.voice["id"],
            },
        )
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(blocked.json()["detail"]["code"], "live_call_active")
        self.runtime.refresh_live_call_lease(ttl_s=0)
        running = self._start_long_job()
        second = self.client.post(
            "/api/generate",
            json={
                "text": "Another document entirely.",
                "steer": "calm",
                "voice_profile_id": self.voice["id"],
            },
        )
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.json()["detail"]["code"], "generation_active")
        self.assertEqual(self._job_status(running)["state"], "running")

    def test_http_complete_job_exposes_one_run(self) -> None:
        started = self.client.post(
            "/api/generate",
            json={
                "text": "One short line.",
                "steer": "calm",
                "voice_profile_id": self.voice["id"],
            },
        )
        self.assertEqual(started.status_code, 200, started.text)
        job_id = started.json()["id"]
        wait_for(lambda: self._job_status(job_id)["state"] == "complete")
        status = self._job_status(job_id)
        self.assertIsNotNone(status["run_id"])
        runs = self.client.get(
            "/api/runs", params={"voice_id": self.voice["id"]}
        ).json()["items"]
        self.assertEqual([run["id"] for run in runs], [status["run_id"]])
        self.assertEqual(runs[0]["output_artifact_id"], status["output_artifact_id"])
        audio = self.client.get(f"/api/artifacts/{status['output_artifact_id']}/audio")
        self.assertEqual(audio.status_code, 200, audio.text)
        self.assertEqual(audio.content[:4], b"RIFF")

    def _synth_calls(self) -> list:
        if not self.factory.handles:
            return []
        return [call for call in self.factory.handles[0].rpc_calls if call.get("cmd") == "synthesize"]


if __name__ == "__main__":
    unittest.main()
