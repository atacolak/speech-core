#!/usr/bin/env python3
"""Streamed GENERATE takes. CPU only; never touches CUDA."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from tts.lab.backend.routes.synthesis import SynthesisBody, resolve_synthesis_request
from tts.lab.backend.runtime.leftover import NoopLeftover
from tts.lab.backend.runtime.manager import E2RuntimeManager
from tts.lab.backend.runtime.vram import FixedVramProbe
from tts.lab.backend.runtime.worker import CountingWorkerFactory, FakeWorkerHandle
from tts.lab.backend.services.progressive import GenerateStreams, iter_generate_pcm
from tts.wav import write_wav

GIB = 1024 ** 3
SAMPLE_RATE = 24000


class GenerateStreamsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.factory = CountingWorkerFactory(
            handle_factory=lambda: FakeWorkerHandle(stream_delay_s=0.15)
        )
        self.runtime = self._runtime()
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
                data={"name": "stream", "transcript": "fixture"},
                files={"audio": ("ref.wav", handle, "audio/wav")},
            ).json()
        self.streams: GenerateStreams = self.state.generate_streams

    def tearDown(self) -> None:
        self.client.close()
        self.runtime.unload()
        self.tmp.cleanup()

    def _runtime(self, factory: CountingWorkerFactory | None = None) -> E2RuntimeManager:
        return E2RuntimeManager(
            vram=FixedVramProbe(free=12 * GIB),
            leftover=NoopLeftover(),
            worker_factory=factory if factory is not None else self.factory,
            required_vram_bytes=8 * GIB,
            vram_margin_bytes=0,
        )

    def _resolved(self, text: str):
        body = SynthesisBody(
            text=text,
            steer="calm",
            voice_profile_id=self.voice["id"],
        )
        return body, resolve_synthesis_request(self.state, body)

    def _stream(self, segments: list[str]):
        body, resolved = self._resolved(" ".join(segments))
        stream = self.streams.begin(voice_profile_id=self.voice["id"])
        chunks = iter_generate_pcm(self.state, stream, body, resolved, segments)
        return stream, chunks

    def _stream_body(self, text: str) -> dict:
        return {"text": text, "steer": "calm", "voice_profile_id": self.voice["id"]}

    def _synth_calls(self) -> list:
        if not self.factory.handles:
            return []
        return [
            call
            for call in self.factory.handles[0].rpc_calls
            if call.get("cmd") == "synthesize"
        ]

    def _runs(self) -> list:
        return self.state.store.execute("SELECT * FROM runs").fetchall()

    def _snapshot(self) -> dict:
        row = self.state.store.execute("SELECT request_json FROM runs").fetchone()
        self.assertIsNotNone(row)
        return json.loads(row["request_json"])

    def _latest_take_id(self) -> str | None:
        items = self.client.get("/api/voices").json()["items"]
        return next(item["latest_take_id"] for item in items if item["id"] == self.voice["id"])

    def test_every_segment_keeps_enrolled_reference(self) -> None:
        stream, chunks = self._stream(["One segment.", "Second segment."])
        pcm = b"".join(chunks)
        self.assertGreater(len(pcm), 0)
        calls = self._synth_calls()
        self.assertEqual(len(calls), 2)
        requests = [call["request"] for call in calls]
        self.assertEqual(len({item["reference_audio"] for item in requests}), 1)
        self.assertEqual(len({item["reference_text"] for item in requests}), 1)
        self.assertEqual(requests[0]["reference_text"], "fixture")
        self.assertNotIn("prior_audio", requests[1])
        self.assertNotIn("previous_audio", requests[1])
        self.assertIsNotNone(stream.run_id)

    def test_stop_mid_document_records_produced_prefix(self) -> None:
        stream, chunks = self._stream(["One.", "Two.", "Three."])
        first = next(chunks)
        self.assertGreater(len(first), 0)
        self.streams.stop(stream.id)
        self.assertEqual(b"".join(chunks), b"")
        rows = self.state.store.execute("SELECT * FROM runs").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], stream.run_id)
        self.assertEqual(len(self._synth_calls()), 1)
        self.assertGreater(float(rows[0]["duration_s"]), 0)

    def test_completed_run_records_exact_produced_prefix(self) -> None:
        stream, chunks = self._stream(["One.", "Two."])
        self.assertGreater(len(b"".join(chunks)), 0)
        self.assertEqual(
            self._snapshot(),
            {
                **self._snapshot(),
                "produced_text": "One. Two.",
                "segments_planned": 2,
                "segments_completed": 2,
                "stopped": False,
            },
        )

    def test_stop_records_only_completed_segment_text(self) -> None:
        stream, chunks = self._stream(["One.", "Two.", "Three."])
        while len(self._synth_calls()) < 2:
            next(chunks)
        self.streams.stop(stream.id)
        b"".join(chunks)
        snapshot = self._snapshot()
        self.assertEqual(snapshot["produced_text"], "One.")
        self.assertEqual(snapshot["segments_completed"], 1)
        self.assertEqual(snapshot["segments_planned"], 3)
        self.assertTrue(snapshot["stopped"])
        self.assertNotIn("Two.", snapshot["produced_text"])
        self.assertNotIn("Three.", snapshot["produced_text"])

    def test_stop_before_audio_keeps_previous_latest_take(self) -> None:
        settled, settled_chunks = self._stream(["One.", "Two."])
        self.assertGreater(len(b"".join(settled_chunks)), 0)
        self.assertIsNotNone(settled.run_id)
        self.assertEqual(self._latest_take_id(), settled.run_id)

        stream, chunks = self._stream(["Three.", "Four."])
        self.streams.stop(stream.id)
        self.assertEqual(b"".join(chunks), b"")
        self.assertIsNone(stream.run_id)
        rows = self._runs()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], settled.run_id)
        self.assertEqual(self._latest_take_id(), settled.run_id)
        self.assertEqual(len(self._synth_calls()), 2)

    def test_http_streams_pcm_and_records_one_take(self) -> None:
        response = self.client.post(
            "/api/generate/stream", json=self._stream_body("One short line.")
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["content-type"], "application/octet-stream")
        self.assertEqual(response.headers["x-sample-rate"], str(SAMPLE_RATE))
        self.assertTrue(response.headers["x-generate-id"].startswith("gen_"))
        pcm = response.content
        self.assertGreater(len(pcm), 0)
        self.assertEqual(len(pcm) % 2, 0)

        rows = self._runs()
        self.assertEqual(len(rows), 1)
        run = rows[0]
        self.assertEqual(self._latest_take_id(), run["id"])
        self.assertAlmostEqual(
            float(run["duration_s"]), len(pcm) / 2 / SAMPLE_RATE, places=6
        )
        audio = self.client.get(f"/api/artifacts/{run['output_artifact_id']}/audio")
        self.assertEqual(audio.status_code, 200, audio.text)
        self.assertEqual(audio.content[:4], b"RIFF")

    def test_stop_route_and_removed_routes(self) -> None:
        stream = self.streams.begin(voice_profile_id=self.voice["id"])
        try:
            stopped = self.client.post(f"/api/generate/{stream.id}/stop")
        finally:
            self.streams.end(stream.id)
        self.assertEqual(stopped.status_code, 200, stopped.text)
        self.assertEqual(stopped.json(), {"id": stream.id, "stopped": True})

        unknown = self.client.post("/api/generate/gen_missing/stop")
        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(unknown.json()["detail"]["code"], "stream_not_found")

        removed = [
            ("get", "/api/generate/gen_missing"),
            ("post", "/api/generate/gen_missing/cancel"),
            ("post", "/api/generate/gen_missing/cursor"),
            ("get", "/api/generate/gen_missing/segments/0/audio"),
        ]
        for method, path in removed:
            response = getattr(self.client, method)(path)
            self.assertEqual(response.status_code, 404, f"{method} {path}")

    def test_empty_text_live_call_and_active_conflicts(self) -> None:
        from fastapi.testclient import TestClient

        from tts.lab.backend.app import create_app

        empty = self.client.post("/api/generate/stream", json=self._stream_body("   \n  "))
        self.assertEqual(empty.status_code, 422)
        self.assertEqual(empty.json()["detail"]["code"], "empty_text")

        self.runtime.refresh_live_call_lease(ttl_s=1.0)
        live = self.client.post("/api/generate/stream", json=self._stream_body("One line."))
        self.assertEqual(live.status_code, 409)
        self.assertEqual(live.json()["detail"]["code"], "live_call_active")
        self.runtime.refresh_live_call_lease(ttl_s=0)

        unloaded = self._runtime()
        with TestClient(create_app(root=Path(self.tmp.name), e2_runtime=unloaded)) as client:
            cold = client.post("/api/generate/stream", json=self._stream_body("One line."))
            self.assertEqual(cold.status_code, 409)
            self.assertEqual(cold.json()["detail"]["code"], "runtime_unloaded")

        busy_runtime = self._runtime(
            CountingWorkerFactory(handle_factory=lambda: FakeWorkerHandle(hang_load=True))
        )
        busy_runtime.begin_load()
        with TestClient(create_app(root=Path(self.tmp.name), e2_runtime=busy_runtime)) as client:
            busy = client.post("/api/generate/stream", json=self._stream_body("One line."))
            self.assertEqual(busy.status_code, 409)
            self.assertEqual(busy.json()["detail"]["code"], "runtime_busy")

        stream = self.streams.begin(voice_profile_id=self.voice["id"])
        try:
            active = self.client.post(
                "/api/generate/stream", json=self._stream_body("One line.")
            )
        finally:
            self.streams.end(stream.id)
        self.assertEqual(active.status_code, 409)
        self.assertEqual(active.json()["detail"]["code"], "generation_active")


if __name__ == "__main__":
    unittest.main()
