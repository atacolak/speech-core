#!/usr/bin/env python3
"""Synthesis HTTP API. not on the voicecat path."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "lab" / "scripts"))

from breeze_tts_qual.configs import EngineConfig
from breeze_tts_qual.engine import BreezeEngine, FakeBackend

from tts.lab.backend.app import create_app
from tts.wav import is_riff_wav, write_wav


def _wav(path: Path) -> Path:
    sr = 24000
    write_wav(path, sr, 0.1 * np.sin(2 * np.pi * 220 * np.arange(sr // 4) / sr))
    return path


class SynthesisApi(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        engine = BreezeEngine(
            EngineConfig(name="E2", precision="bf16"),
            ckpt_dir=".",
            backend=FakeBackend(),
        )
        self.client = TestClient(
            create_app(root=self.root, engine=engine, leftover_parked=True)
        )

    def tearDown(self) -> None:
        self.client.close()
        self.tmp.cleanup()

    def test_synthesize_stored_voice_returns_riff_run(self) -> None:
        wav = _wav(self.root / "ref.wav")
        with wav.open("rb") as handle:
            voice = self.client.post(
                "/api/voices",
                data={"name": "ata", "transcript": "fixture"},
                files={"audio": ("ref.wav", handle, "audio/wav")},
            ).json()
        response = self.client.post(
            "/api/synthesize",
            json={
                "text": "i found the issue. the worker is holding the old session open.",
                "steer": "calm and matter-of-fact.",
                "voice_profile_id": voice["id"],
                "generation": {"guidance": {"mode": "single", "cfg": 4.0}, "seed": 42},
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertGreater(body["duration_s"], 0)
        self.assertTrue(is_riff_wav(body["wav_path"]))
        header = Path(body["wav_path"]).read_bytes()[:12]
        self.assertEqual(header[:4], b"RIFF")
        self.assertEqual(header[8:12], b"WAVE")
        audio = self.client.get(f"/api/artifacts/{body['output_artifact_id']}/audio")
        self.assertEqual(audio.status_code, 200)
        self.assertEqual(audio.content[:4], b"RIFF")

    def test_synthesis_run_records_the_generation_snapshot(self) -> None:
        wav = _wav(self.root / "record-ref.wav")
        with wav.open("rb") as handle:
            voice = self.client.post(
                "/api/voices",
                data={"name": "record", "transcript": "fixture"},
                files={"audio": ("ref.wav", handle, "audio/wav")},
            ).json()
        sent = {"guidance": {"mode": "single", "cfg": 3.0}, "seed": 7}
        response = self.client.post(
            "/api/synthesize",
            json={
                "text": "the same one-shot contract remains",
                "steer": "calm",
                "voice_profile_id": voice["id"],
                "generation": sent,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        run = self.client.get(f"/api/runs/{response.json()['id']}").json()
        self.assertEqual(run["request_snapshot"]["generation"], sent)
        refreshed = self.client.get("/api/voices").json()["items"]
        stored = next(item for item in refreshed if item["id"] == voice["id"])
        self.assertEqual(stored["generation"], sent)


if __name__ == "__main__":
    unittest.main()
