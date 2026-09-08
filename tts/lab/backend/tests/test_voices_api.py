#!/usr/bin/env python3
"""Voice HTTP API. not on the voicecat path."""

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

from tts.lab.backend.app import create_app
from tts.wav import is_riff_wav, write_wav


def _wav(path: Path) -> Path:
    sr = 24000
    write_wav(path, sr, 0.1 * np.sin(2 * np.pi * 440 * np.arange(sr // 5) / sr))
    return path


class VoicesApi(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.client = TestClient(create_app(root=self.root, leftover_parked=True))

    def tearDown(self) -> None:
        self.client.close()
        self.tmp.cleanup()

    def _create(self, name: str = "ata") -> dict:
        wav = _wav(self.root / f"{name}.wav")
        with wav.open("rb") as handle:
            response = self.client.post(
                "/api/voices",
                data={"name": name, "transcript": "hello from ata", "tags": '["lab"]'},
                files={"audio": ("ata.wav", handle, "audio/wav")},
            )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_runtime_endpoint(self) -> None:
        response = self.client.get("/api/runtime")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["selected"], "E2")
        self.assertTrue(body["leftover_parked"])
        self.assertTrue(body["not_a_pin_swap"])
        self.assertFalse(body["voicecat_path"])

    def test_create_retrieve_update_delete(self) -> None:
        created = self._create()
        voice_id = created["id"]
        self.assertEqual(created["name"], "ata")
        self.assertEqual(created["source_transcript"], "hello from ata")
        got = self.client.get(f"/api/voices/{voice_id}")
        self.assertEqual(got.status_code, 200)
        self.assertEqual(got.json()["id"], voice_id)
        listed = self.client.get("/api/voices")
        self.assertEqual(len(listed.json()["items"]), 1)
        patched = self.client.patch(f"/api/voices/{voice_id}", json={"name": "ata australian"})
        self.assertEqual(patched.status_code, 200)
        self.assertEqual(patched.json()["name"], "ata australian")
        deleted = self.client.delete(f"/api/voices/{voice_id}")
        self.assertEqual(deleted.status_code, 200)
        missing = self.client.get(f"/api/voices/{voice_id}")
        self.assertEqual(missing.status_code, 404)


if __name__ == "__main__":
    unittest.main()
