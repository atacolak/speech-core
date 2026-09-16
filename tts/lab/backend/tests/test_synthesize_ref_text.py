#!/usr/bin/env python3
"""Clone synthesize requires ref_text. not on the voicecat path."""

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
from tts.wav import write_wav


def _wav(path: Path) -> Path:
    sr = 24000
    write_wav(path, sr, 0.1 * np.sin(2 * np.pi * 440 * np.arange(sr // 5) / sr))
    return path


class DummyEngine:
    def synthesize(self, **kwargs):
        raise AssertionError("engine should not run without ref_text")


class SynthesizeRefText(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.client = TestClient(
            create_app(root=self.root, leftover_parked=True, engine=DummyEngine())
        )

    def tearDown(self) -> None:
        self.client.close()
        self.tmp.cleanup()

    def _create(self, *, transcript: str, import_text: str | None = None) -> dict:
        wav = _wav(self.root / "voice.wav")
        with patch(
            "tts.lab.backend.services.voices.transcribe_alignment",
            return_value={"text": "" if import_text is None else import_text, "words": []},
        ):
            with wav.open("rb") as handle:
                response = self.client.post(
                    "/api/voices",
                    data={"name": "clone", "transcript": transcript},
                    files={"audio": ("voice.wav", handle, "audio/wav")},
                )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_empty_transcript_is_422_when_transcribe_unavailable(self) -> None:
        voice = self._create(transcript="")
        with patch(
            "breeze_tts_qual.transcribe.transcribe_audio",
            side_effect=FileNotFoundError("transcribe-cli missing"),
        ):
            response = self.client.post(
                "/api/synthesize",
                json={
                    "text": "hello",
                    "steer": "calm",
                    "voice_profile_id": voice["id"],
                },
            )
        self.assertEqual(response.status_code, 422, response.text)
        detail = response.json()["detail"]
        self.assertEqual(detail["code"], "missing_ref_text")

    def test_auto_transcribe_fills_ref_text(self) -> None:
        calls: list[str] = []

        class RecordingEngine:
            def synthesize(self, **kwargs):
                calls.append(str(kwargs.get("reference_text") or ""))
                dest = Path(tempfile.mkdtemp(prefix="tts-lab-test-")) / "take.wav"
                write_wav(dest, 24000, np.zeros(2400, dtype=np.float32))
                from types import SimpleNamespace

                return [
                    SimpleNamespace(
                        pcm=np.zeros(2400, dtype=np.int16).tobytes(),
                        sample_rate=24000,
                        n_samples=2400,
                        t_rel_s=0.0,
                        timing={"first_pcm": 0.01},
                    )
                ]

        self.client.close()
        self.client = TestClient(
            create_app(root=self.root, leftover_parked=True, engine=RecordingEngine())
        )
        voice = self._create(transcript="", import_text="hello from the reference")
        self.assertEqual(voice["effective_transcript"], "hello from the reference")
        response = self.client.post(
            "/api/synthesize",
            json={
                "text": "hello",
                "steer": "calm",
                "voice_profile_id": voice["id"],
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(calls, ["hello from the reference"])
        stored = self.client.get(f"/api/voices/{voice['id']}").json()
        self.assertEqual(stored["effective_transcript"], "hello from the reference")


if __name__ == "__main__":
    unittest.main()
