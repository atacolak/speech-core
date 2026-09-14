#!/usr/bin/env python3
"""Per-voice generation persistence and unsaved take cap. not on the voicecat path."""

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


def _wav(path: Path) -> Path:
    sr = 24000
    write_wav(path, sr, 0.1 * np.sin(2 * np.pi * 330 * np.arange(sr // 5) / sr))
    return path


class VoiceTakes(unittest.TestCase):
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

    def _voice(self, name: str = "ata") -> dict:
        wav = _wav(self.root / f"{name}.wav")
        with wav.open("rb") as handle:
            response = self.client.post(
                "/api/voices",
                data={"name": name, "transcript": "hello from ata"},
                files={"audio": ("ata.wav", handle, "audio/wav")},
            )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _synth(self, voice_id: str) -> dict:
        response = self.client.post(
            "/api/synthesize",
            json={
                "text": "one process that does not suck.",
                "steer": "dry",
                "voice_profile_id": voice_id,
                "generation": {
                    "guidance": {"mode": "single", "cfg": 4.0},
                    "seed": 7,
                    "temperature": 0.8,
                },
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_generation_and_take_limit_persist_on_voice(self) -> None:
        voice = self._voice()
        payload = {
            "generation": {
                "guidance": {"mode": "dual", "reference": 1.0, "instruction": 4.0},
                "seed": 11,
                "temperature": 0.7,
            },
            "take_limit": 8,
        }
        patched = self.client.patch(f"/api/voices/{voice['id']}", json=payload)
        self.assertEqual(patched.status_code, 200, patched.text)
        body = patched.json()
        self.assertEqual(body["take_limit"], 8)
        self.assertEqual(body["generation"]["seed"], 11)
        self.assertEqual(body["generation"]["guidance"]["mode"], "dual")
        got = self.client.get(f"/api/voices/{voice['id']}").json()
        self.assertEqual(got["take_limit"], 8)
        self.assertEqual(got["generation"]["guidance"]["instruction"], 4.0)

    def test_empty_name_is_rejected(self) -> None:
        voice = self._voice()
        response = self.client.patch(f"/api/voices/{voice['id']}", json={"name": "   "})
        self.assertEqual(response.status_code, 422, response.text)

    def test_unsaved_takes_are_capped_saved_are_not(self) -> None:
        voice = self._voice()
        voice_id = voice["id"]
        self.assertEqual(self.client.patch(f"/api/voices/{voice_id}", json={"take_limit": 3}).status_code, 200)
        ids = [self._synth(voice_id)["id"] for _ in range(5)]
        listed = self.client.get(f"/api/runs?voice_id={voice_id}").json()["items"]
        self.assertEqual(len(listed), 3)
        kept_ids = {item["id"] for item in listed}
        self.assertTrue(kept_ids.issubset(set(ids)))
        self.assertIn(ids[-1], kept_ids)
        saved_id = ids[-1]
        rated = self.client.patch(f"/api/runs/{saved_id}", json={"rating": "keep"})
        self.assertEqual(rated.status_code, 200, rated.text)
        extra = [self._synth(voice_id)["id"] for _ in range(3)]
        listed = self.client.get(f"/api/runs?voice_id={voice_id}").json()["items"]
        self.assertEqual(len(listed), 4)
        self.assertIn(saved_id, {item["id"] for item in listed})
        unsaved = [item for item in listed if item["rating"] != "keep"]
        self.assertEqual(len(unsaved), 3)
        self.assertTrue({item["id"] for item in unsaved}.issubset(set(extra) | kept_ids))

    def test_lowering_take_limit_prunes_unsaved_only(self) -> None:
        voice = self._voice()
        voice_id = voice["id"]
        first = self._synth(voice_id)["id"]
        self.client.patch(f"/api/runs/{first}", json={"rating": "keep"})
        others = [self._synth(voice_id)["id"] for _ in range(4)]
        listed = self.client.get(f"/api/runs?voice_id={voice_id}").json()["items"]
        self.assertEqual(len(listed), 5)
        patched = self.client.patch(f"/api/voices/{voice_id}", json={"take_limit": 2})
        self.assertEqual(patched.status_code, 200, patched.text)
        listed = self.client.get(f"/api/runs?voice_id={voice_id}").json()["items"]
        self.assertEqual(len(listed), 3)
        ids = {item["id"] for item in listed}
        self.assertIn(first, ids)
        unsaved = [item for item in listed if item["rating"] != "keep"]
        self.assertEqual(len(unsaved), 2)
        self.assertTrue({item["id"] for item in unsaved}.issubset(set(others)))

    def test_delete_voice_removes_its_takes(self) -> None:
        left = self._voice("left")
        right = self._voice("right")
        self._synth(left["id"])
        keep = self._synth(right["id"])
        deleted = self.client.delete(f"/api/voices/{left['id']}")
        self.assertEqual(deleted.status_code, 200, deleted.text)
        runs = self.client.get("/api/runs").json()["items"]
        self.assertEqual([item["id"] for item in runs], [keep["id"]])
        missing = self.client.get(f"/api/voices/{left['id']}")
        self.assertEqual(missing.status_code, 404)


if __name__ == "__main__":
    unittest.main()
