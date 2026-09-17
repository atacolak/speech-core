#!/usr/bin/env python3
"""A Stopped take's flush window and saved wav. CPU only; never touches CUDA."""

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
from tts.lab.backend.runtime.worker import CountingWorkerFactory, ScriptedWorkerHandle
from tts.lab.backend.services.progressive import iter_generate_pcm
from tts.wav import read_wav, write_wav

GIB = 1024 ** 3
SAMPLE_RATE = 24000
FRAME = 120  # 5 ms at 24 kHz: one comparison frame


def shaped(levels: list[int]) -> np.ndarray:
    """One constant-amplitude comparison frame per level."""
    return np.concatenate([np.full(FRAME, level, dtype="<i2") for level in levels])


SPEECH = shaped([6000] * 20)  # 100 ms of steady audio
WINDOW = shaped([5000, 5500, 6000, 7000, 5500, 4000, 2000, 1000, 3000, 5000])
FLAT = shaped([4000] * 10)  # 50 ms at one level
QUIETEST = 7  # index of the quietest frame in WINDOW


class StopTrimStreamTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.factory = CountingWorkerFactory(handle_factory=ScriptedWorkerHandle)
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
        write_wav(ref, SAMPLE_RATE, np.zeros(2400, dtype=np.float32))
        with ref.open("rb") as handle:
            self.voice = self.client.post(
                "/api/voices",
                data={"name": "trim", "transcript": "fixture"},
                files={"audio": ("ref.wav", handle, "audio/wav")},
            ).json()
        self.handle = self.factory.handles[0]
        self.delivered = 0
        self.stream = None

    def tearDown(self) -> None:
        self.client.close()
        self.runtime.unload()
        self.tmp.cleanup()

    def _script(self, frames: list[np.ndarray], *, stop_after: int | None = None) -> None:
        """Load the worker script; optionally press Stop as frame N is delivered."""
        self.handle.frames = [frame.tobytes() for frame in frames]
        self.delivered = 0

        def on_frame(_index: int) -> None:
            self.delivered += 1
            if stop_after is not None and self.delivered == stop_after:
                self.state.generate_streams.stop(self.stream.id)

        self.handle.on_frame = on_frame

    def _run(self, segments: list[str]):
        body = SynthesisBody(
            text=" ".join(segments), steer="calm", voice_profile_id=self.voice["id"]
        )
        resolved = resolve_synthesis_request(self.state, body)
        self.stream = self.state.generate_streams.begin(
            voice_profile_id=self.voice["id"]
        )
        chunks = iter_generate_pcm(self.state, self.stream, body, resolved, segments)
        return self.stream, chunks

    def _rows(self) -> list:
        return self.state.store.execute("SELECT * FROM runs").fetchall()

    def _row(self) -> dict:
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        return rows[0]

    def _saved(self) -> np.ndarray:
        artifact = self.state.store.get(str(self._row()["output_artifact_id"]))
        rate, samples = read_wav(artifact.path)
        self.assertEqual(rate, SAMPLE_RATE)
        return samples

    def _snapshot(self) -> dict:
        return json.loads(self._row()["request_json"])

    def test_stop_latches_the_take_buffer_length(self) -> None:
        self._script([SPEECH, SPEECH, WINDOW, SPEECH], stop_after=3)
        stream, chunks = self._run(["One."])
        heard = b"".join(chunks)
        self.assertEqual(stream.stop_sample, 2 * SPEECH.size)
        self.assertEqual(len(heard) // 2, 2 * SPEECH.size + WINDOW.size)

    def test_todays_cancel_usually_leaves_an_empty_window(self) -> None:
        self._script([SPEECH, SPEECH, WINDOW, SPEECH])
        stream, chunks = self._run(["One."])
        heard = b"".join(next(chunks) for _ in range(2))
        self.state.generate_streams.stop(stream.id)
        self.assertEqual(b"".join(chunks), b"")
        self.assertEqual(stream.stop_sample, 2 * SPEECH.size)
        self.assertEqual(self._saved().size, len(heard) // 2)

    def test_a_second_stop_does_not_move_the_window(self) -> None:
        streams = self.state.generate_streams
        stream = streams.begin(voice_profile_id=self.voice["id"])
        try:
            pcm = bytearray()
            stream.record(pcm, SPEECH.tobytes())
            streams.stop(stream.id)
            stream.record(pcm, WINDOW.tobytes())
            streams.stop(stream.id)
        finally:
            streams.end(stream.id)
        self.assertEqual(stream.stop_sample, SPEECH.size)
        self.assertEqual(stream.recorded_samples, SPEECH.size + WINDOW.size)

    def test_stop_before_any_audio_records_no_take(self) -> None:
        self._script([SPEECH, WINDOW, SPEECH])
        stream, chunks = self._run(["One."])
        self.state.generate_streams.stop(stream.id)
        self.assertEqual(b"".join(chunks), b"")
        self.assertEqual(stream.stop_sample, 0)
        self.assertEqual(self._rows(), [])

    def test_stopped_take_ends_at_the_quietest_flush_sample(self) -> None:
        self._script([SPEECH, SPEECH, WINDOW, SPEECH], stop_after=3)
        stream, chunks = self._run(["One."])
        heard = np.frombuffer(b"".join(chunks), dtype="<i2")
        expected = 2 * SPEECH.size + QUIETEST * FRAME + 1
        saved = self._saved()
        self.assertEqual(saved.size, expected)
        self.assertEqual(int(saved[-1]), 1000)
        self.assertTrue(np.array_equal(saved, heard[:expected]))
        self.assertGreater(saved.size, stream.stop_sample)
        self.assertAlmostEqual(
            float(self._row()["duration_s"]), expected / SAMPLE_RATE, places=6
        )

    def test_energy_flat_window_keeps_the_last_sample(self) -> None:
        self._script([SPEECH, SPEECH, FLAT, SPEECH], stop_after=3)
        stream, chunks = self._run(["One."])
        heard = b"".join(chunks)
        self.assertEqual(stream.stop_sample, 2 * SPEECH.size)
        self.assertEqual(self._saved().size, len(heard) // 2)

    def test_completed_take_is_never_trimmed(self) -> None:
        self._script([SPEECH, WINDOW, SPEECH])
        stream, chunks = self._run(["One."])
        heard = b"".join(chunks)
        self.assertIsNone(stream.stop_sample)
        self.assertEqual(self._saved().size, len(heard) // 2)
        self.assertFalse(self._snapshot()["stopped"])

    def test_produced_text_is_unchanged_by_the_trim(self) -> None:
        self._script([SPEECH, WINDOW, SPEECH], stop_after=5)
        stream, chunks = self._run(["One.", "Two."])
        b"".join(chunks)
        self.assertEqual(stream.stop_sample, 3 * SPEECH.size + WINDOW.size)
        self.assertEqual(
            self._saved().size, 3 * SPEECH.size + WINDOW.size + QUIETEST * FRAME + 1
        )
        snapshot = self._snapshot()
        self.assertEqual(snapshot["produced_text"], "One.")
        self.assertEqual(snapshot["segments_completed"], 1)
        self.assertEqual(snapshot["segments_planned"], 2)
        self.assertTrue(snapshot["stopped"])
        self.assertNotIn("Two.", snapshot["produced_text"])


if __name__ == "__main__":
    unittest.main()
