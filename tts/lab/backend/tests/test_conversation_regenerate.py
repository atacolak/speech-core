#!/usr/bin/env python3
"""Regenerate conversation turn variations after the live call. not on the voicecat path."""

from __future__ import annotations

import json
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
from tts.lab.backend.runtime.worker import CountingWorkerFactory, ScriptedWorkerHandle
from tts.lab.backend.services.conversations import begin_session, insert_turn
from tts.wav import write_wav

GIB = 1024 ** 3
FRAMES = [b"\x01\x00" * 480, b"\x02\x00" * 480, b"\x03\x00" * 480]


def _wav(path: Path) -> Path:
    sr = 24000
    write_wav(path, sr, 0.1 * np.sin(2 * np.pi * 440 * np.arange(sr // 5) / sr))
    return path


class ConversationRegenerateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.factory = CountingWorkerFactory(
            handle_factory=lambda: ScriptedWorkerHandle(frames=list(FRAMES))
        )
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
        self.store = self.client.app.state.lab.store

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

    def _captured_assistant_turn(self) -> dict:
        created = self._create_voice()
        self.client.post("/api/talker/voice", json={"voice_id": created["id"]})
        self._wait_ready()
        held = self.client.post("/api/runtime/live-call", json={"ttl_s": 45})
        self.assertEqual(held.status_code, 200, held.text)
        conversation_id = held.json()["conversation_id"]
        pcm = self.client.post(
            "/internal/leftover/v1/audio/speech",
            json={
                "input": "hello there",
                "voice": "ryan",
                "response_format": "pcm",
                "stream": True,
            },
        )
        self.assertEqual(pcm.status_code, 200, pcm.text)
        row = self.store.execute(
            "SELECT * FROM conversation_turns WHERE conversation_id = ? AND role = 'assistant'",
            (conversation_id,),
        ).fetchone()
        self.assertIsNotNone(row)
        return dict(row)

    def _turn_count(self) -> int:
        return int(self.store.execute("SELECT COUNT(*) AS n FROM conversation_turns").fetchone()["n"])

    def _run_count(self) -> int:
        return int(self.store.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"])

    def _clear_live_call(self) -> None:
        cleared = self.client.delete("/api/runtime/live-call")
        self.assertEqual(cleared.status_code, 200, cleared.text)
        if self.runtime.live_call_remaining_s() > 0:
            self.runtime.refresh_live_call_lease(ttl_s=0)

    def _regenerate(self, turn: dict, body: dict | None = None):
        return self.client.post(
            f"/api/conversations/{turn['conversation_id']}/turns/{turn['id']}/regenerate",
            json={} if body is None else body,
        )

    def test_regenerate_is_409_live_call_active_while_leased(self) -> None:
        turn = self._captured_assistant_turn()
        before = self._turn_count()
        response = self._regenerate(turn)
        self.assertEqual(response.status_code, 409, response.text)
        detail = response.json()["detail"]
        self.assertEqual(detail["code"], "live_call_active")
        self.assertGreater(detail["remaining_s"], 0)
        self.assertEqual(self._turn_count(), before)

    def test_regenerate_after_clear_adds_unchosen_variation_with_turn_knobs(self) -> None:
        turn = self._captured_assistant_turn()
        self._clear_live_call()
        response = self._regenerate(turn, {"generation": {"seed": 123}})
        self.assertEqual(response.status_code, 200, response.text)
        rows = self.store.execute(
            """
            SELECT * FROM conversation_turns
            WHERE conversation_id = ? AND msg_seq = ?
            ORDER BY variation_seq
            """,
            (turn["conversation_id"], turn["msg_seq"]),
        ).fetchall()
        self.assertEqual(len(rows), 2)
        new = dict(rows[1])
        self.assertEqual(new["msg_seq"], turn["msg_seq"])
        self.assertEqual(new["variation_seq"], int(turn["variation_seq"]) + 1)
        self.assertEqual(new["chosen"], 0)
        self.assertEqual(new["text"], turn["text"])
        self.assertEqual(json.loads(new["generation_json"])["seed"], 123)
        body = response.json()
        self.assertEqual(body["id"], new["id"])
        self.assertEqual(body["variation_seq"], int(turn["variation_seq"]) + 1)
        self.assertFalse(body["chosen"])
        self.assertEqual(body["generation"]["seed"], 123)

    def test_regenerate_creates_no_generate_run(self) -> None:
        turn = self._captured_assistant_turn()
        self._clear_live_call()
        before = self._run_count()
        response = self._regenerate(turn, {"generation": {"seed": 123}})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self._run_count(), before)

    def test_regenerate_unknown_turn_404(self) -> None:
        conversation_id = begin_session(self.store, None)
        missing_conversation = self.client.post(
            "/api/conversations/cv_missing/turns/ct_missing/regenerate",
            json={},
        )
        self.assertEqual(missing_conversation.status_code, 404, missing_conversation.text)
        missing_turn = self.client.post(
            f"/api/conversations/{conversation_id}/turns/ct_missing/regenerate",
            json={},
        )
        self.assertEqual(missing_turn.status_code, 404, missing_turn.text)
        user = insert_turn(self.store, conversation_id, role="user", text="hello")
        user_resp = self.client.post(
            f"/api/conversations/{conversation_id}/turns/{user['id']}/regenerate",
            json={},
        )
        self.assertEqual(user_resp.status_code, 422, user_resp.text)
        audioless = insert_turn(
            self.store,
            conversation_id,
            role="assistant",
            text="no audio",
            voice_id="vp_missing",
        )
        audio_resp = self.client.post(
            f"/api/conversations/{conversation_id}/turns/{audioless['id']}/regenerate",
            json={},
        )
        self.assertEqual(audio_resp.status_code, 422, audio_resp.text)


if __name__ == "__main__":
    unittest.main()
