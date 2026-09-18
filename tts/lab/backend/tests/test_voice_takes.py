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
        self.client = self._client_for_root(self.root)

    def _client_for_root(self, root: Path) -> TestClient:
        engine = BreezeEngine(
            EngineConfig(name="E2", precision="bf16"),
            ckpt_dir=".",
            backend=FakeBackend(),
        )
        return TestClient(create_app(root=root, engine=engine, leftover_parked=True))

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

    def test_run_name_persists(self) -> None:
        voice = self._voice()
        run = self._synth(voice["id"])
        before = self.client.get(f"/api/runs/{run['id']}")
        self.assertEqual(before.status_code, 200, before.text)
        self.assertIsNone(before.json().get("name"))
        response = self.client.patch(f"/api/runs/{run['id']}", json={"name": "morning take"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json().get("name"), "morning take")
        got = self.client.get(f"/api/runs/{run['id']}")
        self.assertEqual(got.status_code, 200, got.text)
        self.assertEqual(got.json().get("name"), "morning take")
        listed = self.client.get(f"/api/runs?voice_id={voice['id']}").json()["items"]
        item = next(row for row in listed if row["id"] == run["id"])
        self.assertEqual(item.get("name"), "morning take")

    def test_blank_run_name_is_rejected(self) -> None:
        voice = self._voice()
        run = self._synth(voice["id"])
        response = self.client.patch(f"/api/runs/{run['id']}", json={"name": "   "})
        self.assertEqual(response.status_code, 422, response.text)
        got = self.client.get(f"/api/runs/{run['id']}")
        self.assertEqual(got.status_code, 200, got.text)
        self.assertIsNone(got.json().get("name"))

    def test_naming_a_take_sets_keep(self) -> None:
        voice = self._voice()
        run = self._synth(voice["id"])
        self.assertNotEqual(run.get("rating"), "keep")
        response = self.client.patch(f"/api/runs/{run['id']}", json={"name": "saved take"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["rating"], "keep")
        got = self.client.get(f"/api/runs/{run['id']}")
        self.assertEqual(got.json()["rating"], "keep")
        self.assertEqual(got.json()["name"], "saved take")

    def test_naming_a_take_enrolls_voice_take(self) -> None:
        voice = self._voice()
        run = self._synth(voice["id"])
        titled = self.client.patch(f"/api/runs/{run['id']}", json={"name": "  morning take  "})
        self.assertEqual(titled.status_code, 200, titled.text)
        profile = self.client.get(f"/api/voices/{voice['id']}")
        self.assertEqual(profile.status_code, 200, profile.text)
        artifact_id = f"vt_{run['id']}"
        takes = [item for item in profile.json()["artifacts"] if item["id"] == artifact_id]
        self.assertEqual(len(takes), 1)
        enrolled = takes[0]
        self.assertEqual(enrolled["role"], "reference")
        self.assertEqual(enrolled["kind"], "take")
        self.assertEqual(enrolled["name"], "morning take")
        self.assertEqual(enrolled["audio_artifact_id"], run["output_artifact_id"])
        self.assertEqual(enrolled["instruction"], "one process that does not suck.")
        self.assertEqual(enrolled["settings"], {"run_id": run["id"]})
        self.assertFalse(enrolled["stale"])

        renamed = self.client.patch(f"/api/runs/{run['id']}", json={"name": "evening take"})
        self.assertEqual(renamed.status_code, 200, renamed.text)
        after = self.client.get(f"/api/voices/{voice['id']}").json()
        takes = [item for item in after["artifacts"] if item["kind"] == "take"]
        self.assertEqual(len(takes), 1)
        self.assertEqual(takes[0]["id"], artifact_id)
        self.assertEqual(takes[0]["name"], "evening take")

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

    def test_synthesize_sets_latest_take_and_lists_it(self) -> None:
        voice = self._voice("ata")
        run = self._synth(voice["id"])
        listed = self.client.get("/api/voices").json()["items"]
        item = next(row for row in listed if row["id"] == voice["id"])
        self.assertEqual(item["latest_take_id"], run["id"])

    def test_latest_take_is_per_voice_and_survives_reopen(self) -> None:
        first = self._voice("ata")
        second = self._voice("bex")
        first_run = self._synth(first["id"])
        second_run = self._synth(second["id"])
        self.client.close()
        self.client = self._client_for_root(self.root)
        items = {item["id"]: item for item in self.client.get("/api/voices").json()["items"]}
        self.assertEqual(items[first["id"]]["latest_take_id"], first_run["id"])
        self.assertEqual(items[second["id"]]["latest_take_id"], second_run["id"])

    def test_deleted_latest_take_serializes_as_null(self) -> None:
        voice = self._voice("ata")
        run = self._synth(voice["id"])
        self.assertEqual(self.client.delete(f"/api/runs/{run['id']}").status_code, 200)
        listed = self.client.get("/api/voices").json()["items"]
        item = next(row for row in listed if row["id"] == voice["id"])
        self.assertIsNone(item["latest_take_id"])


if __name__ == "__main__":
    unittest.main()
