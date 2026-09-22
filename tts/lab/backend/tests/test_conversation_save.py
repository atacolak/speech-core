#!/usr/bin/env python3
"""Choose a variation and save conversation turns as keep-takes. not on the voicecat path."""

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
from tts.lab.backend.services.conversations import begin_session, end_session, insert_turn
from tts.wav import write_wav

GIB = 1024 ** 3
FRAMES = [b"\x01\x00" * 480, b"\x02\x00" * 480, b"\x03\x00" * 480]


def _wav(path: Path) -> Path:
    sr = 24000
    write_wav(path, sr, 0.1 * np.sin(2 * np.pi * 440 * np.arange(sr // 5) / sr))
    return path


class ConversationSaveTest(unittest.TestCase):
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

    def test_choose_marks_exactly_one_variation(self) -> None:
        turn = self._captured_assistant_turn()
        sibling = insert_turn(
            self.store,
            turn["conversation_id"],
            role="assistant",
            text=turn["text"],
            msg_seq=turn["msg_seq"],
            variation_seq=int(turn["variation_seq"]) + 1,
            chosen=0,
            audio_artifact_id=turn["audio_artifact_id"],
            voice_id=turn["voice_id"],
        )
        response = self.client.post(
            f"/api/conversations/{turn['conversation_id']}/turns/{sibling['id']}/choose"
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["id"], sibling["id"])
        self.assertTrue(body["chosen"])
        rows = self.store.execute(
            """
            SELECT id, chosen FROM conversation_turns
            WHERE conversation_id = ? AND msg_seq = ?
            ORDER BY variation_seq
            """,
            (turn["conversation_id"], turn["msg_seq"]),
        ).fetchall()
        chosen = [row["id"] for row in rows if row["chosen"]]
        self.assertEqual(chosen, [sibling["id"]])
        self.assertEqual(len(rows), 2)

    def test_save_turn_writes_keep_run_and_advances_latest_take_id(self) -> None:
        turn = self._captured_assistant_turn()
        response = self.client.post(
            f"/api/conversations/{turn['conversation_id']}/turns/{turn['id']}/save"
        )
        self.assertEqual(response.status_code, 200, response.text)
        run = self.store.execute(
            "SELECT * FROM runs WHERE id = ?", (response.json()["run_id"],)
        ).fetchone()
        self.assertEqual(run["rating"], "keep")
        self.assertEqual(run["voice_id"], turn["voice_id"])
        snapshot = json.loads(run["request_json"])
        self.assertEqual(snapshot["conversation_turn_id"], turn["id"])
        self.assertEqual(snapshot["text"], turn["text"])
        voice = self.store.execute(
            "SELECT latest_take_id FROM voices WHERE id = ?", (turn["voice_id"],)
        ).fetchone()
        self.assertEqual(voice["latest_take_id"], run["id"])

    def test_save_turn_rejects_user_or_audioless_turn_422(self) -> None:
        conversation_id = begin_session(self.store, None)
        user = insert_turn(self.store, conversation_id, role="user", text="hello")
        missing_audio = insert_turn(
            self.store,
            conversation_id,
            role="assistant",
            text="no audio",
            voice_id="vp_missing",
        )
        user_resp = self.client.post(
            f"/api/conversations/{conversation_id}/turns/{user['id']}/save"
        )
        self.assertEqual(user_resp.status_code, 422, user_resp.text)
        audio_resp = self.client.post(
            f"/api/conversations/{conversation_id}/turns/{missing_audio['id']}/save"
        )
        self.assertEqual(audio_resp.status_code, 422, audio_resp.text)

    def test_save_conversation_keeps_audio_pinned_and_survives_ring(self) -> None:
        turn = self._captured_assistant_turn()
        conversation_id = turn["conversation_id"]
        user_pcm = b"\x05\x00" * 480
        user_resp = self.client.post(
            f"/api/conversations/{conversation_id}/user-turn",
            data={"transcript": "hello from the desk", "sample_rate": "24000"},
            files={"audio": ("u.pcm", user_pcm, "application/octet-stream")},
        )
        self.assertEqual(user_resp.status_code, 200, user_resp.text)
        user = user_resp.json()
        saved = self.client.post(f"/api/conversations/{conversation_id}/save")
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertTrue(saved.json()["saved"])
        row = self.store.execute(
            "SELECT saved FROM conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
        self.assertEqual(row["saved"], 1)
        pins = {
            pin["reason"]
            for pin in self.store.execute(
                "SELECT reason FROM pins WHERE artifact_id IN (?, ?)",
                (turn["audio_artifact_id"], user["audio_artifact_id"]),
            ).fetchall()
        }
        self.assertIn(f"turn:{turn['id']}", pins)
        self.assertIn(f"turn:{user['id']}", pins)
        end_session(self.store)
        for index in range(5):
            extra = begin_session(self.store, None)
            insert_turn(self.store, extra, role="assistant", text=f"unsaved-{index}")
            end_session(self.store)
        remaining = self.store.execute(
            "SELECT id, saved FROM conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
        self.assertIsNotNone(remaining)
        self.assertEqual(remaining["saved"], 1)
        self.assertIsNotNone(
            self.store.execute(
                "SELECT 1 FROM pins WHERE artifact_id = ? AND reason = ?",
                (turn["audio_artifact_id"], f"turn:{turn['id']}"),
            ).fetchone()
        )
        self.assertIsNotNone(
            self.store.execute(
                "SELECT 1 FROM pins WHERE artifact_id = ? AND reason = ?",
                (user["audio_artifact_id"], f"turn:{user['id']}"),
            ).fetchone()
        )

    def test_keep_never_touches_other_runs(self) -> None:
        turn = self._captured_assistant_turn()
        wav = _wav(self.root / "generate-keep.wav")
        artifact = self.store.import_audio(wav)
        run_id = "run_generate_keep"
        self.store.execute(
            """
            INSERT INTO runs (
                id, voice_id, request_json, output_artifact_id, effective_reference_json,
                latency_ms, first_audio_ms, duration_s, rating, tags_json, created_at,
                alignment_json
            ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                turn["voice_id"],
                json.dumps({"text": "one process that does not suck.", "steer": "dry"}),
                artifact.id,
                json.dumps({}),
                80.0,
                0.2,
                "keep",
                json.dumps([]),
                "2026-09-16T00:00:00Z",
                json.dumps({"status": "ready", "text": "", "words": []}),
            ),
        )
        self.store.pin(artifact.id, reason=f"run:{run_id}")
        self.store.execute(
            "UPDATE voices SET latest_take_id = ? WHERE id = ?",
            (run_id, turn["voice_id"]),
        )
        self.store.commit()
        before = tuple(
            self.store.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        )
        artifacts_before = [
            tuple(row)
            for row in self.store.execute("SELECT * FROM voice_artifacts ORDER BY id").fetchall()
        ]
        saved = self.client.post(f"/api/conversations/{turn['conversation_id']}/save")
        self.assertEqual(saved.status_code, 200, saved.text)
        after = tuple(
            self.store.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        )
        self.assertEqual(after, before)
        artifacts_after = [
            tuple(row)
            for row in self.store.execute("SELECT * FROM voice_artifacts ORDER BY id").fetchall()
        ]
        self.assertEqual(artifacts_after, artifacts_before)
        voice = self.store.execute(
            "SELECT latest_take_id FROM voices WHERE id = ?", (turn["voice_id"],)
        ).fetchone()
        self.assertNotEqual(voice["latest_take_id"], run_id)


if __name__ == "__main__":
    unittest.main()
