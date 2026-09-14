#!/usr/bin/env python3
"""Resemble denoise-only reference variant. No GPU, no weights: denoise is faked."""

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
from tts.lab.backend.runtime.worker import CountingWorkerFactory
from tts.wav import read_wav, write_wav

GIB = 1024 ** 3
DENOISE_HOOK = "tts.lab.backend.services.resemble.denoise_hook"


def _wav(path: Path) -> Path:
    sr = 24000
    write_wav(path, sr, 0.1 * np.sin(2 * np.pi * 440 * np.arange(sr // 5) / sr))
    return path


def _fake_denoise(dwav, sr, device):
    """Half-amplitude 'denoise'. Different bytes from the keep wav it was handed."""
    return np.asarray(dwav, dtype=np.float32) * 0.5, sr


class ResembleVariant(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.client = TestClient(create_app(root=self.root, leftover_parked=True))

    def tearDown(self) -> None:
        self.client.close()
        self.tmp.cleanup()

    def _create_voice(self, name: str = "ata") -> dict:
        wav = _wav(self.root / f"{name}.wav")
        with wav.open("rb") as handle:
            response = self.client.post(
                "/api/voices",
                data={"name": name, "transcript": "hello from ata", "tags": '["lab"]'},
                files={"audio": ("ata.wav", handle, "audio/wav")},
            )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_streamfm_routes_are_gone(self) -> None:
        created = self._create_voice()
        streamfm = self.client.post(f"/api/voices/{created['id']}/streamfm")
        self.assertEqual(streamfm.status_code, 404, streamfm.text)
        status = self.client.get("/api/streamfm/status")
        self.assertEqual(status.status_code, 404, status.text)

    def test_denoise_writes_resemble_variant_and_keeps_original(self) -> None:
        created = self._create_voice()
        original = next(item for item in created["variants"] if item["kind"] == "original")
        store = self.client.app.state.lab.store
        original_sha = store.get(original["audio_artifact_id"]).sha256
        with patch(DENOISE_HOOK, _fake_denoise):
            response = self.client.post(f"/api/voices/{created['id']}/reference/denoise")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["id"], created["id"])
        variants = {item["kind"]: item for item in body["variants"]}
        self.assertEqual(set(variants), {"original", "resemble"})
        self.assertEqual(body["original_artifact_id"], created["original_artifact_id"])
        self.assertEqual(body["active_variant"]["kind"], "original")
        self.assertEqual(variants["original"]["audio_artifact_id"], original["audio_artifact_id"])
        self.assertEqual(store.get(original["audio_artifact_id"]).sha256, original_sha)
        clean = variants["resemble"]
        self.assertNotEqual(clean["audio_artifact_id"], original["audio_artifact_id"])
        self.assertTrue(clean["processor_cache_key"])
        clean_sha = store.get(clean["audio_artifact_id"]).sha256
        self.assertNotEqual(clean_sha, original_sha)
        clean_sr, clean_samples = read_wav(store.get(clean["audio_artifact_id"]).path)
        _, original_samples = read_wav(store.get(original["audio_artifact_id"]).path)
        self.assertLess(float(np.max(np.abs(clean_samples))), float(np.max(np.abs(original_samples))))

    def test_activate_original_or_resemble(self) -> None:
        created = self._create_voice()
        with patch(DENOISE_HOOK, _fake_denoise):
            self.client.post(f"/api/voices/{created['id']}/reference/denoise")
        clean = self.client.post(
            f"/api/voices/{created['id']}/reference/activate", json={"kind": "resemble"}
        )
        self.assertEqual(clean.status_code, 200, clean.text)
        self.assertEqual(clean.json()["active_variant"]["kind"], "resemble")
        original = self.client.post(
            f"/api/voices/{created['id']}/reference/activate", json={"kind": "original"}
        )
        self.assertEqual(original.status_code, 200, original.text)
        self.assertEqual(original.json()["active_variant"]["kind"], "original")

    def test_keep_change_stales_the_resemble_variant(self) -> None:
        created = self._create_voice()
        with patch(DENOISE_HOOK, _fake_denoise):
            self.client.post(f"/api/voices/{created['id']}/reference/denoise")
        before = self.client.get(f"/api/voices/{created['id']}").json()
        self.assertFalse({item["kind"]: item for item in before["variants"]}["resemble"]["stale"])
        cropped = self.client.post(
            f"/api/voices/{created['id']}/reference/keep-only",
            json={"start_s": 0.0, "end_s": 0.08},
        )
        self.assertEqual(cropped.status_code, 200, cropped.text)
        after = {item["kind"]: item for item in cropped.json()["variants"]}
        self.assertTrue(after["resemble"]["stale"])
        self.assertFalse(after["original"]["stale"])
        with patch(DENOISE_HOOK, _fake_denoise):
            refreshed = self.client.post(f"/api/voices/{created['id']}/reference/denoise")
        self.assertEqual(refreshed.status_code, 200, refreshed.text)
        again = {item["kind"]: item for item in refreshed.json()["variants"]}
        self.assertFalse(again["resemble"]["stale"])
        self.assertEqual(again["resemble"]["id"], after["resemble"]["id"])


class ResembleWithRuntime(unittest.TestCase):
    """Denoise owns the single GPU behind ProcessorLease; synthesis clones its artifact."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.factory = CountingWorkerFactory()
        self.runtime = E2RuntimeManager(
            vram=FixedVramProbe(free=12 * GIB),
            leftover=NoopLeftover(),
            worker_factory=self.factory,
            required_vram_bytes=8 * GIB,
            vram_margin_bytes=0,
        )
        self.client = TestClient(
            create_app(root=self.root, leftover_parked=False, e2_runtime=self.runtime)
        )

    def tearDown(self) -> None:
        self.client.close()
        self.tmp.cleanup()

    def _create_voice(self, name: str = "ata") -> dict:
        wav = _wav(self.root / f"{name}.wav")
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

    def _activate_resemble(self, voice_id: str) -> dict:
        with patch(DENOISE_HOOK, _fake_denoise):
            response = self.client.post(f"/api/voices/{voice_id}/reference/denoise")
        self.assertEqual(response.status_code, 200, response.text)
        activated = self.client.post(
            f"/api/voices/{voice_id}/reference/activate", json={"kind": "resemble"}
        )
        self.assertEqual(activated.status_code, 200, activated.text)
        variants = {item["kind"]: item for item in activated.json()["variants"]}
        return variants["resemble"]

    def _last_reference_audio(self) -> str:
        calls = [call for call in self.factory.handles[0].rpc_calls if call.get("cmd") == "synthesize"]
        return str(calls[-1]["request"]["reference_audio"])

    def _hop(self) -> None:
        response = self.client.post(
            "/internal/leftover/v1/audio/speech",
            json={"input": "hello there", "voice": "ryan", "response_format": "pcm", "stream": True},
        )
        self.assertEqual(response.status_code, 200, response.text)

    def _crop(self, voice_id: str) -> None:
        cropped = self.client.post(
            f"/api/voices/{voice_id}/reference/keep-only",
            json={"start_s": 0.0, "end_s": 0.08},
        )
        self.assertEqual(cropped.status_code, 200, cropped.text)

    def test_denoise_releases_the_processor_slot(self) -> None:
        created = self._create_voice()
        with patch(DENOISE_HOOK, _fake_denoise):
            response = self.client.post(f"/api/voices/{created['id']}/reference/denoise")
        self.assertEqual(response.status_code, 200, response.text)
        status = self.client.get("/api/runtime").json()
        self.assertIsNone(status["processor"])
        self.assertEqual(status["state"], "unloaded")
        self.assertEqual(self.factory.spawn_count, 0)

    def test_denoise_is_blocked_during_a_live_call(self) -> None:
        created = self._create_voice()
        held = self.client.post("/api/runtime/live-call", json={"ttl_s": 45})
        self.assertEqual(held.status_code, 200, held.text)
        self.assertTrue(held.json()["live_call_active"])
        with patch(DENOISE_HOOK, _fake_denoise):
            denied = self.client.post(f"/api/voices/{created['id']}/reference/denoise")
        self.assertEqual(denied.status_code, 409, denied.text)
        self.assertEqual(denied.json()["detail"]["code"], "processor_blocked_live_call")
        after = self.client.get(f"/api/voices/{created['id']}").json()
        self.assertEqual([item["kind"] for item in after["variants"]], ["original"])
        self.assertEqual(after["original_artifact_id"], created["original_artifact_id"])
        self.assertEqual(self.factory.spawn_count, 0)
        status = self.client.get("/api/runtime").json()
        self.assertTrue(status["live_call_active"])
        self.assertIsNone(status["processor"])

    def test_talker_clones_the_resemble_artifact_until_the_keep_moves(self) -> None:
        created = self._create_voice()
        clean = self._activate_resemble(created["id"])
        self.client.post("/api/talker/voice", json={"voice_id": created["id"]})
        self._wait_ready()
        store = self.client.app.state.lab.store
        clean_path = str(store.get(clean["audio_artifact_id"]).path)
        self._hop()
        self.assertEqual(self._last_reference_audio(), clean_path)
        self._crop(created["id"])
        self._hop()
        fallback = self._last_reference_audio()
        self.assertNotEqual(fallback, clean_path)
        sr, samples = read_wav(Path(fallback))
        self.assertAlmostEqual(len(samples) / sr, 0.08, places=2)

    def test_talker_crops_the_current_keep_when_no_cleanup_is_current(self) -> None:
        created = self._create_voice()
        self.client.post("/api/talker/voice", json={"voice_id": created["id"]})
        self._wait_ready()
        self._hop()
        whole = self._last_reference_audio()
        self._crop(created["id"])
        self._hop()
        cropped = self._last_reference_audio()
        self.assertNotEqual(cropped, whole)
        sr, samples = read_wav(Path(cropped))
        self.assertAlmostEqual(len(samples) / sr, 0.08, places=2)

    def test_synthesis_clones_resemble_only_while_the_keep_matches(self) -> None:
        created = self._create_voice()
        clean = self._activate_resemble(created["id"])
        self._wait_ready()
        store = self.client.app.state.lab.store
        clean_path = str(store.get(clean["audio_artifact_id"]).path)
        first = self.client.post(
            "/api/synthesize",
            json={"text": "hello", "steer": "calm", "voice_profile_id": created["id"]},
        )
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(self._last_reference_audio(), clean_path)
        self._crop(created["id"])
        second = self.client.post(
            "/api/synthesize",
            json={"text": "hello", "steer": "calm", "voice_profile_id": created["id"]},
        )
        self.assertEqual(second.status_code, 200, second.text)
        fallback = self._last_reference_audio()
        self.assertNotEqual(fallback, clean_path)
        sr, samples = read_wav(Path(fallback))
        self.assertAlmostEqual(len(samples) / sr, 0.08, places=2)


if __name__ == "__main__":
    unittest.main()
