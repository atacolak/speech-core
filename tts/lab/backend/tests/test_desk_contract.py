#!/usr/bin/env python3
"""Desk-facing runtime/talker/leftover hop. Fail closed; never qwen :18091."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO))

from tts.lab.backend.app import create_app
from tts.lab.backend.runtime.leftover import NoopLeftover
from tts.lab.backend.runtime.manager import E2RuntimeManager
from tts.lab.backend.runtime.vram import FixedVramProbe
from tts.lab.backend.runtime.worker import CountingWorkerFactory
from tts.wav import write_wav

GIB = 1024 ** 3


def _wav(path: Path) -> Path:
    sr = 24000
    write_wav(path, sr, 0.1 * np.sin(2 * np.pi * 440 * np.arange(sr // 5) / sr))
    return path


class DeskContract(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        leftover = NoopLeftover()
        factory = CountingWorkerFactory()
        self.factory = factory
        self.runtime = E2RuntimeManager(
            vram=FixedVramProbe(free=12 * GIB),
            leftover=leftover,
            worker_factory=factory,
            required_vram_bytes=8 * GIB,
            vram_margin_bytes=0,
        )
        self.client = TestClient(
            create_app(root=Path(self.tmp.name), leftover_parked=False, e2_runtime=self.runtime)
        )

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

    def test_runtime_load_alias_and_engine(self) -> None:
        body = self.client.get("/api/runtime").json()
        self.assertEqual(body["engine"], "breeze-tts2")
        self.assertEqual(body["state"], "unloaded")
        self.assertTrue(body["voicecat_path"])
        self.assertIn("leftover_parked", body)
        loaded = self.client.post("/api/runtime/load")
        self.assertEqual(loaded.status_code, 200, loaded.text)
        self.assertEqual(loaded.json()["engine"], "breeze-tts2")
        self.assertTrue(loaded.json()["voicecat_path"])

    def test_talker_and_desk_voices(self) -> None:
        created = self._create_voice()
        missing = self.client.post("/api/talker/voice", json={"voice_id": "vp_missing"})
        self.assertEqual(missing.status_code, 404)
        set_voice = self.client.post("/api/talker/voice", json={"voice_id": created["id"]})
        self.assertEqual(set_voice.status_code, 200, set_voice.text)
        self.assertEqual(set_voice.json()["engine"], "breeze-tts2")
        self.assertEqual(set_voice.json()["voice_id"], created["id"])
        talker = self.client.get("/api/talker").json()
        self.assertEqual(talker["voice_id"], created["id"])
        desk = self.client.get("/api/voices", params={"for": "desk"})
        self.assertEqual(desk.status_code, 200, desk.text)
        item = desk.json()["items"][0]
        self.assertEqual(set(item), {"id", "name", "transcript"})
        self.assertEqual(item["id"], created["id"])
        self.assertEqual(item["transcript"], "hello from ata")

    def test_leftover_hop_fail_closed_then_pcm(self) -> None:
        created = self._create_voice()
        self.client.post("/api/talker/voice", json={"voice_id": created["id"]})
        refused = self.client.post(
            "/internal/leftover/v1/audio/speech",
            json={"input": "hello there", "voice": "default", "response_format": "pcm", "stream": True},
        )
        self.assertEqual(refused.status_code, 503, refused.text)
        detail = refused.json()["detail"]
        self.assertIn(detail["code"], {"engine_not_ready", "engine_loading"})
        self.assertIn(refused.headers.get("x-speech-engine-state"), {"unloaded", "loading", "error"})
        self.assertEqual(refused.headers.get("retry-after"), "5")
        self.assertNotIn("18091", refused.text)
        self.assertIn(self.client.get("/api/runtime").json()["state"], {"loading", "ready"})
        self._wait_ready()
        pcm = self.client.post(
            "/internal/leftover/v1/audio/speech",
            json={"input": "hello there", "voice": "ryan", "response_format": "pcm", "stream": True},
        )
        self.assertEqual(pcm.status_code, 200, pcm.text)
        self.assertEqual(pcm.headers["content-type"], "application/octet-stream")
        self.assertGreater(len(pcm.content), 100)
        self.assertEqual(len(pcm.content) % 2, 0)
        synth_calls = [
            call
            for handle in self.factory.handles
            for call in handle.rpc_calls
            if call.get("cmd") == "synthesize"
        ]
        self.assertEqual(len(synth_calls), 1)
        self.assertEqual(synth_calls[0]["request"]["voice_profile_id"], created["id"])

    def test_leftover_hop_streams_first_bytes_before_synth_finishes(self) -> None:
        import time

        from tts.lab.backend.runtime.worker import FakeWorkerHandle

        leftover = NoopLeftover()
        factory = CountingWorkerFactory(
            handle_factory=lambda: FakeWorkerHandle(stream_delay_s=0.4)
        )
        runtime = E2RuntimeManager(
            vram=FixedVramProbe(free=12 * GIB),
            leftover=leftover,
            worker_factory=factory,
            required_vram_bytes=8 * GIB,
            vram_margin_bytes=0,
        )
        client = TestClient(
            create_app(root=Path(self.tmp.name), leftover_parked=False, e2_runtime=runtime)
        )
        try:
            created = self._create_voice("stream")
            client.post("/api/talker/voice", json={"voice_id": created["id"]})
            loaded = client.post("/api/runtime/load")
            self.assertEqual(loaded.status_code, 200, loaded.text)
            for _ in range(50):
                if client.get("/api/runtime").json()["state"] == "ready":
                    break
            else:
                self.fail("runtime not ready")
            t0 = time.monotonic()
            first_s = None
            first_len = 0
            total = 0
            with client.stream(
                "POST",
                "/internal/leftover/v1/audio/speech",
                json={"input": "hello there", "voice": "ryan", "response_format": "pcm", "stream": True},
            ) as response:
                self.assertEqual(response.status_code, 200)
                for chunk in response.iter_bytes():
                    if first_s is None:
                        first_s = time.monotonic() - t0
                        first_len = len(chunk)
                    total += len(chunk)
            self.assertGreater(first_len, 0)
            self.assertIsNotNone(first_s)
            self.assertGreater(total, 100)
            # TestClient buffers the ASGI stream; first-byte-before-tail is
            # proven by ManagerLifecycle.test_synthesize_stream_yields_before_tail
            # and the live hop probe (chunked, first 4k << e2e).
            self.assertGreaterEqual(time.monotonic() - t0, 0.35)
        finally:
            client.close()

    def test_leftover_hop_needs_talker_voice(self) -> None:
        self._wait_ready()
        refused = self.client.post(
            "/internal/leftover/v1/audio/speech",
            json={"input": "hello", "voice": "default"},
        )
        self.assertEqual(refused.status_code, 409, refused.text)
        self.assertEqual(refused.json()["detail"]["code"], "talker_voice_missing")

    def test_leftover_hop_abandon_then_next_is_own(self) -> None:
        created = self._create_voice()
        self.client.post("/api/talker/voice", json={"voice_id": created["id"]})
        self._wait_ready()
        handle = self.factory.handles[0]
        chunks = iter(
            self.runtime.synthesize_stream(
                {
                    "text": "abandoned hop",
                    "steer": "",
                    "voice_profile_id": created["id"],
                    "reference_audio": str(Path(self.tmp.name) / "ata.wav"),
                    "reference_text": "hello from ata",
                }
            )
        )
        first = next(chunks)
        self.assertGreater(len(first), 0)
        chunks.close()
        self.assertTrue(handle.cancel_requested)
        pcm = self.client.post(
            "/internal/leftover/v1/audio/speech",
            json={"input": "hello there", "voice": "ryan", "response_format": "pcm", "stream": True},
        )
        self.assertEqual(pcm.status_code, 200, pcm.text)
        self.assertGreater(len(pcm.content), 100)
        last = [call for call in handle.rpc_calls if call.get("cmd") == "synthesize"][-1]
        self.assertEqual(last["request"]["text"], "hello there")

    def test_leftover_hop_lease_blocks_lab_synthesize(self) -> None:
        created = self._create_voice()
        self.client.post("/api/talker/voice", json={"voice_id": created["id"]})
        self._wait_ready()
        pcm = self.client.post(
            "/internal/leftover/v1/audio/speech",
            json={"input": "hello there", "voice": "ryan", "response_format": "pcm", "stream": True},
        )
        self.assertEqual(pcm.status_code, 200, pcm.text)
        status = self.client.get("/api/runtime").json()
        self.assertTrue(status["live_call_active"])
        refused = self.client.post(
            "/api/synthesize",
            json={"text": "hello", "steer": "calm", "voice_profile_id": created["id"]},
        )
        self.assertEqual(refused.status_code, 409, refused.text)
        self.assertEqual(refused.json()["detail"]["code"], "live_call_active")
        hop_while_leased = self.client.post(
            "/internal/leftover/v1/audio/speech",
            json={"input": "still the mouth", "voice": "ryan", "response_format": "pcm", "stream": True},
        )
        self.assertEqual(hop_while_leased.status_code, 200, hop_while_leased.text)
        self.runtime.refresh_live_call_lease(ttl_s=0)
        allowed = self.client.post(
            "/api/synthesize",
            json={"text": "hello", "steer": "calm", "voice_profile_id": created["id"]},
        )
        self.assertEqual(allowed.status_code, 200, allowed.text)

    def test_session_live_call_endpoint_hold_and_clear(self) -> None:
        created = self._create_voice()
        self.client.post("/api/talker/voice", json={"voice_id": created["id"]})
        self._wait_ready()
        held = self.client.post("/api/runtime/live-call", json={"ttl_s": 45})
        self.assertEqual(held.status_code, 200, held.text)
        body = held.json()
        self.assertTrue(body["live_call_active"])
        self.assertEqual(body["live_call_holder"], "session")
        self.assertGreater(body["live_call_remaining_s"], 0)
        refused = self.client.post(
            "/api/synthesize",
            json={"text": "hello", "steer": "calm", "voice_profile_id": created["id"]},
        )
        self.assertEqual(refused.status_code, 409, refused.text)
        self.assertEqual(refused.json()["detail"]["code"], "live_call_active")
        cleared = self.client.delete("/api/runtime/live-call")
        self.assertEqual(cleared.status_code, 200, cleared.text)
        self.assertFalse(cleared.json()["live_call_active"])
        allowed = self.client.post(
            "/api/synthesize",
            json={"text": "hello", "steer": "calm", "voice_profile_id": created["id"]},
        )
        self.assertEqual(allowed.status_code, 200, allowed.text)


if __name__ == "__main__":
    unittest.main()

