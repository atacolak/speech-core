#!/usr/bin/env python3
"""Conversations HTTP + JOIN-tori user-turn ingest. not on the voicecat path."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO))

from tts.lab.backend.app import create_app
from tts.lab.backend.runtime.leftover import NoopLeftover
from tts.lab.backend.runtime.manager import E2RuntimeManager
from tts.lab.backend.runtime.vram import FixedVramProbe
from tts.lab.backend.runtime.worker import CountingWorkerFactory
from tts.lab.backend.services.conversations import begin_session, insert_turn
from tts.wav import as_pcm16, read_wav

GIB = 1024 ** 3


class _ConversationsHttpCase(unittest.TestCase):
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
        self.store = self.client.app.state.lab.store

    def tearDown(self) -> None:
        self.client.close()
        self.tmp.cleanup()


class ConversationsApiTest(_ConversationsHttpCase):
    def test_list_latest_first_with_summary_fields(self) -> None:
        older = begin_session(self.store, "older")
        insert_turn(self.store, older, role="user", text="first")
        newer = begin_session(self.store, "newer")
        insert_turn(self.store, newer, role="user", text="hello")
        insert_turn(self.store, newer, role="assistant", text="hi")
        self.store.execute("UPDATE conversations SET saved = 1 WHERE id = ?", (older,))
        self.store.commit()

        response = self.client.get("/api/conversations")
        self.assertEqual(response.status_code, 200, response.text)
        items = response.json()["items"]
        self.assertEqual([item["id"] for item in items], [newer, older])
        for item in items:
            self.assertIn("started_at", item)
            self.assertIn("ended_at", item)
            self.assertIsInstance(item["saved"], bool)
            self.assertIsInstance(item["turn_count"], int)
        by_id = {item["id"]: item for item in items}
        self.assertTrue(by_id[older]["saved"])
        self.assertFalse(by_id[newer]["saved"])
        self.assertEqual(by_id[older]["turn_count"], 1)
        self.assertEqual(by_id[newer]["turn_count"], 2)
        self.assertIsNotNone(by_id[older]["ended_at"])
        self.assertIsNone(by_id[newer]["ended_at"])
        self.assertEqual(by_id[older]["title"], "first")
        self.assertEqual(by_id[newer]["title"], "hello")

    def test_list_title_falls_back_to_first_user_text(self) -> None:
        conversation_id = begin_session(self.store, None)
        insert_turn(
            self.store,
            conversation_id,
            role="user",
            text="  Say one sentence to me.  ",
        )
        insert_turn(self.store, conversation_id, role="assistant", text="ok")
        response = self.client.get("/api/conversations")
        self.assertEqual(response.status_code, 200, response.text)
        by_id = {item["id"]: item for item in response.json()["items"]}
        self.assertEqual(by_id[conversation_id]["title"], "Say one sentence to me.")


    def test_detail_returns_variations_with_parsed_json(self) -> None:
        conversation_id = begin_session(self.store, None)
        generation_a = {"seed": 1, "cfg": 1.5}
        generation_b = {"seed": 2, "cfg": 2.0}
        alignment = {
            "status": "ready",
            "words": [{"text": "hello", "start_s": 0.1, "end_s": 0.4}],
        }
        first = insert_turn(
            self.store,
            conversation_id,
            role="assistant",
            text="hello",
            generation=generation_a,
            variation_seq=0,
            chosen=0,
        )
        second = insert_turn(
            self.store,
            conversation_id,
            role="assistant",
            text="hello",
            generation=generation_b,
            msg_seq=first["msg_seq"],
            variation_seq=1,
            chosen=1,
        )
        self.store.execute(
            "UPDATE conversation_turns SET alignment_json = ? WHERE id = ?",
            (json.dumps(alignment), second["id"]),
        )
        self.store.commit()

        response = self.client.get(f"/api/conversations/{conversation_id}")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["id"], conversation_id)
        self.assertEqual(len(body["turns"]), 2)
        turns = {turn["variation_seq"]: turn for turn in body["turns"]}
        self.assertIs(turns[0]["chosen"], False)
        self.assertIs(turns[1]["chosen"], True)
        self.assertEqual(turns[0]["generation"], generation_a)
        self.assertEqual(turns[1]["generation"], generation_b)
        self.assertIsNone(turns[0]["alignment"])
        self.assertEqual(turns[1]["alignment"], alignment)

    def test_detail_unknown_conversation_404(self) -> None:
        response = self.client.get("/api/conversations/cv_missing")
        self.assertEqual(response.status_code, 404, response.text)

    def test_put_ring_limit_clamps_and_persists(self) -> None:
        clamped = self.client.put("/api/conversations/config", json={"ring_limit": 51})
        self.assertEqual(clamped.status_code, 200, clamped.text)
        self.assertEqual(clamped.json()["ring_limit"], 50)
        session = json.loads((self.store.root / "session.json").read_text(encoding="utf-8"))
        self.assertEqual(session["conversation_ring_limit"], 50)

        valid = self.client.put("/api/conversations/config", json={"ring_limit": 7})
        self.assertEqual(valid.status_code, 200, valid.text)
        self.assertEqual(valid.json()["ring_limit"], 7)
        session = json.loads((self.store.root / "session.json").read_text(encoding="utf-8"))
        self.assertEqual(session["conversation_ring_limit"], 7)


class UserTurnIngestTest(_ConversationsHttpCase):
    def test_user_turn_stores_transcript_verbatim_and_audio(self) -> None:
        conversation_id = begin_session(self.store, None)
        pcm = b"\x05\x00" * 2400
        transcript = "  um [laughs] hello?  "
        response = self.client.post(
            f"/api/conversations/{conversation_id}/user-turn",
            data={"transcript": transcript, "sample_rate": "24000"},
            files={"audio": ("u.pcm", pcm, "application/octet-stream")},
        )
        self.assertEqual(response.status_code, 200, response.text)
        row = self.store.execute(
            "SELECT * FROM conversation_turns WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        self.assertEqual(row["role"], "user")
        self.assertEqual(row["text"], transcript)
        artifact = self.store.get(row["audio_artifact_id"])
        self.assertEqual(as_pcm16(read_wav(artifact.path)[1]).tobytes(), pcm)

    def test_transcript_only_user_turn_is_valid(self) -> None:
        conversation_id = begin_session(self.store, None)
        response = self.client.post(
            f"/api/conversations/{conversation_id}/user-turn",
            data={"transcript": "hello there"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertIsNone(body["audio_artifact_id"])
        row = self.store.execute(
            "SELECT text, audio_artifact_id FROM conversation_turns WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        self.assertEqual(row["text"], "hello there")
        self.assertIsNone(row["audio_artifact_id"])

    def test_unknown_conversation_404_and_empty_transcript_422(self) -> None:
        missing = self.client.post(
            "/api/conversations/cv_nope/user-turn",
            data={"transcript": "hello"},
        )
        self.assertEqual(missing.status_code, 404, missing.text)
        conversation_id = begin_session(self.store, None)
        empty = self.client.post(
            f"/api/conversations/{conversation_id}/user-turn",
            data={"transcript": "  "},
        )
        self.assertEqual(empty.status_code, 422, empty.text)

    def test_audio_without_sample_rate_422(self) -> None:
        conversation_id = begin_session(self.store, None)
        response = self.client.post(
            f"/api/conversations/{conversation_id}/user-turn",
            data={"transcript": "hello"},
            files={"audio": ("u.pcm", b"\x05\x00" * 10, "application/octet-stream")},
        )
        self.assertEqual(response.status_code, 422, response.text)


if __name__ == "__main__":
    unittest.main()
