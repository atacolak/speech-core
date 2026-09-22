#!/usr/bin/env python3
"""Side recorder on synthesize_stream. not on the voicecat path."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO))

from tts.lab.backend.app import create_app
from tts.lab.backend.runtime.leftover import NoopLeftover
from tts.lab.backend.runtime.manager import E2RuntimeManager
from tts.lab.backend.runtime.vram import FixedVramProbe
from tts.lab.backend.runtime.worker import CountingWorkerFactory, ScriptedWorkerHandle
from tts.lab.backend.services.conversations import begin_session, end_session
from tts.wav import as_pcm16, read_wav, write_wav

GIB = 1024 ** 3
FRAMES = [b"\x01\x00" * 480, b"\x02\x00" * 480, b"\x03\x00" * 480]


def _wav(path: Path) -> Path:
    sr = 24000
    write_wav(path, sr, 0.1 * np.sin(2 * np.pi * 440 * np.arange(sr // 5) / sr))
    return path


class ConversationCaptureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.factory = CountingWorkerFactory(
            handle_factory=lambda: ScriptedWorkerHandle(frames=list(FRAMES))
        )
        self.runtime = E2RuntimeManager(
            vram=FixedVramProbe(free=12 * GIB),
            leftover=NoopLeftover(),
            worker_factory=self.factory,
            required_vram_bytes=8 * GIB,
            vram_margin_bytes=0,
        )
        self.client = TestClient(
            create_app(root=Path(self.tmp.name), leftover_parked=False, e2_runtime=self.runtime)
        )
        self.store = self.client.app.state.lab.store

    def tearDown(self) -> None:
        self.client.close()
        self.tmp.cleanup()

    def _create_voice(self, name: str = "ata") -> dict:
        wav = _wav(Path(self.tmp.name) / f"{name}.wav")
        with wav.open("rb") as handle:
            response = self.client.post(
                "/api/voices",
                data={"name": name, "transcript": "hello from ata", "tags": '["lab"]'},
                files={"audio": ("ata.wav", handle, "audio/wav")},
            )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _wait_ready(self) -> dict:
        loaded = self.client.post("/api/runtime/load")
        self.assertEqual(loaded.status_code, 200, loaded.text)
        for _ in range(50):
            body = self.client.get("/api/runtime").json()
            if body["state"] == "ready":
                return body
        self.fail(body)

    def test_hop_turn_lands_as_assistant_turn_with_exact_pcm(self) -> None:
        created = self._create_voice()
        self.client.post("/api/talker/voice", json={"voice_id": created["id"]})
        self._wait_ready()
        held = self.client.post("/api/runtime/live-call", json={"ttl_s": 45})
        self.assertEqual(held.status_code, 200, held.text)
        if self.store.execute(
            "SELECT id FROM conversations WHERE ended_at IS NULL LIMIT 1"
        ).fetchone() is None:
            begin_session(self.store, None)
        pcm = self.client.post(
            "/internal/leftover/v1/audio/speech",
            json={
                "input": "hello there",
                "voice": "ryan",
                "response_format": "pcm",
                "stream": True,
            },
        )
        self.assertEqual(pcm.status_code, 200, pcm.text)
        rows = self.store.execute("SELECT * FROM conversation_turns").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["role"], "assistant")
        self.assertEqual(rows[0]["text"], "hello there")
        artifact = self.store.get(rows[0]["audio_artifact_id"])
        _sr, samples = read_wav(artifact.path)
        self.assertEqual(as_pcm16(samples).tobytes(), b"".join(FRAMES))

    def test_closed_stream_stores_only_pulled_prefix(self) -> None:
        self.runtime.load()
        begin_session(self.store, None)
        stream = self.runtime.synthesize_stream(
            {"text": "hi", "steer": "", "voice_profile_id": None}
        )
        first = next(stream)
        stream.close()
        rows = self.store.execute("SELECT * FROM conversation_turns").fetchall()
        self.assertEqual(len(rows), 1)
        artifact = self.store.get(rows[0]["audio_artifact_id"])
        self.assertEqual(as_pcm16(read_wav(artifact.path)[1]).tobytes(), first)

    def test_no_open_session_records_nothing(self) -> None:
        self.runtime.load()
        chunks = list(
            self.runtime.synthesize_stream(
                {"text": "hi", "steer": "", "voice_profile_id": None}
            )
        )
        self.assertEqual(chunks, list(FRAMES))
        rows = self.store.execute("SELECT * FROM conversation_turns").fetchall()
        self.assertEqual(len(rows), 0)

    def test_stream_after_session_end_is_not_recorded(self) -> None:
        self.runtime.load()
        begin_session(self.store, None)
        end_session(self.store)
        chunks = list(
            self.runtime.synthesize_stream(
                {"text": "hi", "steer": "", "voice_profile_id": None}
            )
        )
        self.assertEqual(chunks, list(FRAMES))
        rows = self.store.execute("SELECT * FROM conversation_turns").fetchall()
        self.assertEqual(len(rows), 0)

    def test_finalize_failure_never_reaches_the_stream(self) -> None:
        self.runtime.load()
        begin_session(self.store, None)
        with patch.object(self.store, "import_audio", side_effect=RuntimeError("boom")):
            chunks = list(
                self.runtime.synthesize_stream(
                    {"text": "hi", "steer": "", "voice_profile_id": None}
                )
            )
        self.assertEqual(chunks, list(FRAMES))
        rows = self.store.execute("SELECT * FROM conversation_turns").fetchall()
        self.assertEqual(len(rows), 0)


if __name__ == "__main__":
    unittest.main()
