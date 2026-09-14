#!/usr/bin/env python3
"""Voice HTTP API. not on the voicecat path."""

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
        self.assertTrue(body["voicecat_path"])
        self.assertEqual(body["engine"], "breeze-tts2")

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

    def test_import_mp3_decodes_to_working_wav(self) -> None:
        import subprocess
        import shutil

        if shutil.which("ffmpeg") is None:
            self.skipTest("ffmpeg missing")
        wav = _wav(self.root / "src.wav")
        mp3 = self.root / "src.mp3"
        proc = subprocess.run(
            ["ffmpeg", "-y", "-i", str(wav), "-q:a", "4", str(mp3)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with mp3.open("rb") as handle:
            response = self.client.post(
                "/api/voices",
                data={"name": "ata-mp3", "transcript": "hello"},
                files={"audio": ("ata.mp3", handle, "audio/mpeg")},
            )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["original_format"], "mp3")
        self.assertGreater(body["duration_s"], 0)
        self.assertTrue(body["variants"])
        audio = self.client.get(f"/api/artifacts/{body['source_audio_artifact_id']}/audio")
        self.assertEqual(audio.status_code, 200)
        self.assertEqual(audio.content[:4], b"RIFF")

    def test_seed_sample_voice_is_first_and_idempotent(self) -> None:
        import shutil

        from tts.lab.backend.services.samples import SAMPLE_FIXTURE, SAMPLE_NAME, SAMPLE_VOICE_ID

        if shutil.which("ffmpeg") is None:
            self.skipTest("ffmpeg missing")
        if not SAMPLE_FIXTURE.is_file():
            self.skipTest("sample fixture missing")
        self._create("ata")
        with patch("tts.lab.backend.services.voices.transcribe_alignment", return_value={"text": "", "words": []}):
            seeded = TestClient(create_app(root=self.root, leftover_parked=True, seed_samples=True))
        try:
            items = seeded.get("/api/voices").json()["items"]
            self.assertEqual(items[0]["id"], SAMPLE_VOICE_ID)
            self.assertEqual(items[0]["name"], SAMPLE_NAME)
            self.assertIn("sample", items[0]["tags"])
            self.assertEqual({item["name"] for item in items}, {SAMPLE_NAME, "ata"})
            audio = seeded.get(f"/api/voices/{SAMPLE_VOICE_ID}/reference/audio")
            self.assertEqual(audio.status_code, 200)
            self.assertEqual(audio.content[:4], b"RIFF")
        finally:
            seeded.close()
        with patch("tts.lab.backend.services.voices.transcribe_alignment", return_value={"text": "", "words": []}):
            again = TestClient(create_app(root=self.root, leftover_parked=True, seed_samples=True))
        try:
            items = again.get("/api/voices").json()["items"]
            self.assertEqual(sum(1 for item in items if item["id"] == SAMPLE_VOICE_ID), 1)
        finally:
            again.close()


    def test_import_auto_transcribes_blank_transcript(self) -> None:
        wav = _wav(self.root / "blank.wav")
        with patch(
            "tts.lab.backend.services.voices.transcribe_alignment",
            return_value={"text": "hello from parakeet", "words": []},
        ) as mocked:
            with wav.open("rb") as handle:
                response = self.client.post(
                    "/api/voices",
                    data={"name": "blank", "transcript": ""},
                    files={"audio": ("blank.wav", handle, "audio/wav")},
                )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["source_transcript"], "hello from parakeet")
        self.assertEqual(body["effective_transcript"], "hello from parakeet")
        mocked.assert_called_once()

    def test_supplied_transcript_skips_parakeet(self) -> None:
        wav = _wav(self.root / "named.wav")
        with patch(
            "tts.lab.backend.services.voices.transcribe_alignment",
            return_value={"text": "should not be used", "words": []},
        ) as mocked:
            with wav.open("rb") as handle:
                response = self.client.post(
                    "/api/voices",
                    data={"name": "named", "transcript": "operator text"},
                    files={"audio": ("named.wav", handle, "audio/wav")},
                )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["effective_transcript"], "operator text")
        mocked.assert_not_called()

    def test_multiple_excludes_persist_across_reopen(self) -> None:
        created = self._create("clip")
        voice_id = created["id"]
        duration = float(created["duration_s"])
        first = self.client.post(
            f"/api/voices/{voice_id}/reference/exclude",
            json={"start_s": 0.05, "end_s": 0.08},
        )
        self.assertEqual(first.status_code, 200, first.text)
        second = self.client.post(
            f"/api/voices/{voice_id}/reference/exclude",
            json={
                "intervals": [
                    {"start_s": 0.12, "end_s": 0.15},
                ]
            },
        )
        self.assertEqual(second.status_code, 200, second.text)
        keep = second.json()["keep_intervals"]
        self.assertGreaterEqual(len(keep), 2)
        self.client.close()
        reopened = TestClient(create_app(root=self.root, leftover_parked=True))
        try:
            got = reopened.get(f"/api/voices/{voice_id}")
            self.assertEqual(got.status_code, 200, got.text)
            self.assertEqual(got.json()["keep_intervals"], keep)
            self.assertLess(keep[0]["end_s"], duration)
        finally:
            reopened.close()
        self.client = TestClient(create_app(root=self.root, leftover_parked=True))

    def test_keep_only_crops_duration_and_cached_transcript(self) -> None:
        created = self._create("clip")
        voice_id = created["id"]
        words = [
            {"text": "hello", "start_s": 0.00, "end_s": 0.05},
            {"text": "from", "start_s": 0.05, "end_s": 0.10},
            {"text": "the", "start_s": 0.10, "end_s": 0.15},
            {"text": "bunker", "start_s": 0.15, "end_s": 0.20},
        ]
        self.root.joinpath("ignored").mkdir(exist_ok=True)
        store = self.client.app.state.lab.store
        store.execute(
            "UPDATE voices SET source_words_json=?, source_transcript=?, effective_transcript=?, transcript_locked=0 WHERE id=?",
            (
                __import__("json").dumps(words),
                "hello from the bunker",
                "hello from the bunker",
                voice_id,
            ),
        )
        store.commit()
        cropped = self.client.post(
            f"/api/voices/{voice_id}/reference/keep-only",
            json={"start_s": 0.10, "end_s": 0.20},
        )
        self.assertEqual(cropped.status_code, 200, cropped.text)
        body = cropped.json()
        self.assertEqual(body["effective_transcript"], "the bunker")
        self.assertEqual(body["source_transcript"], "hello from the bunker")
        self.assertAlmostEqual(body["effective_duration_s"], 0.10, places=3)
        self.assertGreater(body["source_duration_s"], body["effective_duration_s"])
        audio = self.client.get(f"/api/voices/{voice_id}/reference/audio")
        self.assertEqual(audio.status_code, 200)
        self.assertEqual(audio.content[:4], b"RIFF")

    def test_manual_transcript_survives_keep_only(self) -> None:
        created = self._create("clip")
        voice_id = created["id"]
        edited = self.client.patch(
            f"/api/voices/{voice_id}",
            json={"effective_transcript": "operator typed this"},
        )
        self.assertEqual(edited.status_code, 200, edited.text)
        self.assertTrue(edited.json()["transcript_locked"])
        cropped = self.client.post(
            f"/api/voices/{voice_id}/reference/keep-only",
            json={"start_s": 0.02, "end_s": 0.18},
        )
        self.assertEqual(cropped.status_code, 200, cropped.text)
        self.assertEqual(cropped.json()["effective_transcript"], "operator typed this")


if __name__ == "__main__":
    unittest.main()
