#!/usr/bin/env python3
"""Run retrieval API. not on the voicecat path."""

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
from tts.wav import write_wav


class RunsApi(unittest.TestCase):
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

    def _voice(self) -> dict:
        wav = self.root / "ref.wav"
        write_wav(wav, 24000, 0.1 * np.sin(2 * np.pi * 330 * np.arange(6000) / 24000))
        with wav.open("rb") as handle:
            return self.client.post(
                "/api/voices",
                data={"name": "ata", "transcript": "fixture"},
                files={"audio": ("ref.wav", handle, "audio/wav")},
            ).json()

    def test_retrieve_run_restores_request_snapshot(self) -> None:
        voice = self._voice()
        snapshot = {
            "text": "i found the issue. the worker is holding the old session open.",
            "steer": "calm and matter-of-fact.",
            "voice_profile_id": voice["id"],
            "generation": {"guidance": {"mode": "single", "cfg": 4.0}, "seed": 9},
        }
        created = self.client.post("/api/synthesize", json=snapshot)
        self.assertEqual(created.status_code, 200, created.text)
        run_id = created.json()["id"]
        got = self.client.get(f"/api/runs/{run_id}")
        self.assertEqual(got.status_code, 200)
        body = got.json()
        self.assertEqual(body["request_snapshot"]["text"], snapshot["text"])
        self.assertEqual(body["request_snapshot"]["steer"], snapshot["steer"])
        self.assertEqual(body["request_snapshot"]["voice_profile_id"], voice["id"])
        self.assertEqual(body["request_snapshot"]["generation"]["seed"], 9)
        listed = self.client.get("/api/runs")
        self.assertEqual(len(listed.json()["items"]), 1)

    def test_one_shot_snapshot_has_no_produced_prefix_fields(self) -> None:
        voice = self._voice()
        created = self.client.post(
            "/api/synthesize",
            json={
                "text": "One shot line.",
                "steer": "calm",
                "voice_profile_id": voice["id"],
            },
        )
        self.assertEqual(created.status_code, 200, created.text)
        body = self.client.get(f"/api/runs/{created.json()['id']}").json()
        snapshot = body["request_snapshot"]
        for key in ("produced_text", "segments_planned", "segments_completed", "stopped"):
            self.assertNotIn(key, snapshot)
        self.assertEqual(snapshot["text"], "One shot line.")


if __name__ == "__main__":
    unittest.main()
