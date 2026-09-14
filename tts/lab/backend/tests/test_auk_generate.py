#!/usr/bin/env python3
"""POST /api/voices/{id}/auk: render, import, record one candidate.

The sampler itself is not exercised here — that needs the AuK venv and the pin. These
tests pin the route contract around it: a render becomes a `kind=auk` candidate while
the source keeps its bytes, and a missing engine is still a 501 that writes nothing.
"""

from __future__ import annotations

import hashlib
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from tts.lab.backend.routes.auk import EngineUnavailable
from tts.lab.backend.runtime.auk import AukRuntimeManager
from tts.lab.backend.tests.test_auk_artifacts import INSTRUCTION, SETTINGS, _AukCase
from tts.wav import read_wav, write_wav


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _render_stub(calls: list[dict]):
    """Stand-in for the worker: half-amplitude 'enhance', written where the route asked."""

    def generate(_self, **kwargs):
        calls.append(kwargs)
        out = Path(kwargs["output_path"])
        sr, samples = read_wav(kwargs["source_wav"])
        write_wav(out, sr, (np.asarray(samples, dtype=np.float32) * 0.5).astype(np.float32))
        return {"output_path": str(out), "wall_s": 0.0}

    return generate


def _unavailable_stub(_self, **kwargs):
    raise EngineUnavailable("the AuK engine is not built for this pin")


class AukGenerateRoute(_AukCase):
    def test_render_records_a_candidate_and_leaves_the_source_alone(self) -> None:
        created = self._create_voice()
        source = self.store.get(created["source_audio_artifact_id"])
        source_sha = _sha(source.path)
        calls: list[dict] = []

        with patch.object(AukRuntimeManager, "generate", _render_stub(calls)):
            response = self.client.post(
                f"/api/voices/{created['id']}/auk",
                json={"auk_task": "enhance", "instruction": INSTRUCTION, "seed": 7, "settings": SETTINGS},
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["task"], "enhance")
        self.assertEqual(calls[0]["seed"], 7)
        self.assertEqual(_sha(source.path), source_sha)

        voice = self._voice(created["id"])
        self.assertEqual({item["kind"] for item in voice["variants"]}, {"original", "auk"})
        candidate = self._variant(voice, "auk")
        self.assertEqual(candidate["auk_task"], "enhance")
        self.assertEqual(candidate["instruction"], INSTRUCTION)
        self.assertEqual(candidate["model_variant"], "auk-base")
        self.assertEqual(candidate["encoder_precision"], "w4a8")
        self.assertFalse(candidate["stale"])
        rendered = self.store.get(candidate["audio_artifact_id"])
        self.assertNotEqual(rendered.sha256, source.sha256)

    def test_missing_engine_is_501_and_writes_no_candidate(self) -> None:
        created = self._create_voice()

        with patch.object(AukRuntimeManager, "generate", _unavailable_stub):
            response = self.client.post(
                f"/api/voices/{created['id']}/auk",
                json={"auk_task": "enhance", "instruction": INSTRUCTION},
            )

        self.assertEqual(response.status_code, 501, response.text)
        self.assertEqual(response.json()["detail"]["code"], "engine_unavailable")
        voice = self._voice(created["id"])
        self.assertEqual([item["kind"] for item in voice["variants"]], ["original"])


if __name__ == "__main__":
    unittest.main()
