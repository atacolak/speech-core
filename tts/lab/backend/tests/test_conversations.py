#!/usr/bin/env python3
"""Conversation sessions, turns, and unsaved ring. not on the voicecat path."""

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
from tts.lab.backend.services.conversations import (
    begin_session,
    clamp_ring_limit,
    end_session,
    insert_turn,
    open_conversation_id,
    prune_unsaved_conversations,
)
from tts.lab.backend.store.artifacts import ArtifactStore
from tts.lab.backend.store.session import (
    read_conversation_ring_limit,
    write_conversation_ring_limit,
)
from tts.wav import write_wav

GIB = 1024 ** 3


def _tone(path: Path, *, freq: float = 440.0, seconds: float = 0.2) -> Path:
    sr = 24000
    t = np.arange(int(sr * seconds), dtype=np.float32) / sr
    write_wav(path, sr, 0.1 * np.sin(2 * np.pi * freq * t))
    return path


class ConversationsServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = ArtifactStore(self.root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_hold_mints_then_reuses_open_conversation(self) -> None:
        first = begin_session(self.store, None)
        self.assertTrue(first.startswith("cv_"))
        self.assertEqual(begin_session(self.store, None), first)
        self.assertEqual(open_conversation_id(self.store), first)
        again = begin_session(self.store, None, reuse_open=False)
        self.assertNotEqual(again, first)
        self.assertTrue(again.startswith("cv_"))
        self.assertEqual(open_conversation_id(self.store), again)
        ended = self.store.execute(
            "SELECT ended_at FROM conversations WHERE id = ?", (first,)
        ).fetchone()
        self.assertIsNotNone(ended["ended_at"])

    def test_new_session_id_ends_open_and_opens_new(self) -> None:
        first = begin_session(self.store, "desk-1")
        self.assertEqual(first, "desk-1")
        self.assertEqual(begin_session(self.store, "desk-1"), first)
        second = begin_session(self.store, "desk-2")
        self.assertEqual(second, "desk-2")
        row = self.store.execute(
            "SELECT ended_at FROM conversations WHERE id = ?", (first,)
        ).fetchone()
        self.assertIsNotNone(row["ended_at"])
        self.assertEqual(open_conversation_id(self.store), "desk-2")
        end_session(self.store)
        reopened = begin_session(self.store, "desk-1")
        self.assertTrue(reopened.startswith("cv_"))
        self.assertNotEqual(reopened, "desk-1")
        self.assertEqual(open_conversation_id(self.store), reopened)

    def test_end_session_prunes_unsaved_ring_and_keeps_saved(self) -> None:
        write_conversation_ring_limit(self.root, 50)
        saved_id = begin_session(self.store, None)
        audio = self.store.import_audio(_tone(self.root / "saved.wav"))
        turn = insert_turn(
            self.store,
            saved_id,
            role="assistant",
            text="kept",
            audio_artifact_id=audio.id,
        )
        pin_reason = f"turn:{turn['id']}"
        self.store.pin(audio.id, reason=pin_reason)
        self.store.execute(
            "UPDATE conversations SET saved = 1 WHERE id = ?", (saved_id,)
        )
        self.store.commit()
        self.assertEqual(end_session(self.store), saved_id)

        unsaved: list[str] = []
        for index in range(5):
            cid = begin_session(self.store, None)
            insert_turn(
                self.store,
                cid,
                role="assistant",
                text=f"unsaved-{index}",
            )
            self.assertEqual(end_session(self.store), cid)
            unsaved.append(cid)

        remaining_before = self.store.execute(
            "SELECT id FROM conversations WHERE saved = 0 ORDER BY created_at ASC, rowid ASC"
        ).fetchall()
        self.assertEqual([row["id"] for row in remaining_before], unsaved)

        pruned = prune_unsaved_conversations(self.store, 3)
        self.assertEqual(pruned, 2)
        remaining = self.store.execute(
            "SELECT id FROM conversations WHERE saved = 0 ORDER BY created_at ASC, rowid ASC"
        ).fetchall()
        self.assertEqual([row["id"] for row in remaining], unsaved[-3:])
        gone = unsaved[:2]
        for cid in gone:
            self.assertIsNone(
                self.store.execute(
                    "SELECT 1 FROM conversations WHERE id = ?", (cid,)
                ).fetchone()
            )
            self.assertIsNone(
                self.store.execute(
                    "SELECT 1 FROM conversation_turns WHERE conversation_id = ?",
                    (cid,),
                ).fetchone()
            )

        self.assertIsNotNone(
            self.store.execute(
                "SELECT 1 FROM conversations WHERE id = ? AND saved = 1",
                (saved_id,),
            ).fetchone()
        )
        self.assertIsNotNone(
            self.store.execute(
                "SELECT 1 FROM conversation_turns WHERE conversation_id = ?",
                (saved_id,),
            ).fetchone()
        )
        self.assertIsNotNone(
            self.store.execute(
                "SELECT 1 FROM pins WHERE artifact_id = ? AND reason = ?",
                (audio.id, pin_reason),
            ).fetchone()
        )
        self.assertTrue(self.store.path_for(audio.id).is_file())

    def test_ring_limit_clamps_1_to_50_default_3(self) -> None:
        self.assertEqual(clamp_ring_limit(None), 3)
        self.assertEqual(clamp_ring_limit("junk"), 3)
        self.assertEqual(clamp_ring_limit(0), 1)
        self.assertEqual(clamp_ring_limit(51), 50)
        self.assertEqual(clamp_ring_limit(3), 3)
        self.assertIsNone(read_conversation_ring_limit(self.root))
        write_conversation_ring_limit(self.root, 7)
        self.assertEqual(read_conversation_ring_limit(self.root), 7)

    def test_insert_turn_allocates_msg_seq_and_single_chosen(self) -> None:
        cid = begin_session(self.store, None)
        first = insert_turn(self.store, cid, role="assistant", text="one")
        self.assertEqual(first["msg_seq"], 0)
        self.assertEqual(first["variation_seq"], 0)
        self.assertEqual(first["chosen"], 1)
        second = insert_turn(self.store, cid, role="assistant", text="two")
        self.assertEqual(second["msg_seq"], 1)
        self.assertEqual(second["variation_seq"], 0)
        third = insert_turn(
            self.store,
            cid,
            role="assistant",
            text="one-b",
            msg_seq=0,
            variation_seq=1,
            chosen=1,
        )
        self.assertEqual(third["msg_seq"], 0)
        self.assertEqual(third["variation_seq"], 1)
        self.assertEqual(third["chosen"], 1)
        sibling = self.store.execute(
            "SELECT chosen FROM conversation_turns WHERE id = ?",
            (first["id"],),
        ).fetchone()
        self.assertEqual(sibling["chosen"], 0)
        chosen_count = self.store.execute(
            """
            SELECT COUNT(*) AS n FROM conversation_turns
            WHERE conversation_id = ? AND msg_seq = 0 AND chosen = 1
            """,
            (cid,),
        ).fetchone()
        self.assertEqual(chosen_count["n"], 1)


class ConversationSessionHttpTest(unittest.TestCase):
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

    def test_hold_mints_and_status_carries_conversation_id(self) -> None:
        held = self.client.post("/api/runtime/live-call", json={})
        self.assertEqual(held.status_code, 200, held.text)
        conversation_id = held.json()["conversation_id"]
        self.assertTrue(conversation_id.startswith("cv_"))
        self.assertEqual(
            self.client.get("/api/runtime").json()["conversation_id"], conversation_id
        )

    def test_heartbeat_with_session_id_reuses_open(self) -> None:
        first = self.client.post("/api/runtime/live-call", json={"session_id": "desk-9"})
        self.assertEqual(first.status_code, 200, first.text)
        conversation_id = first.json()["conversation_id"]
        again = self.client.post("/api/runtime/live-call", json={"session_id": "desk-9"})
        self.assertEqual(again.status_code, 200, again.text)
        self.assertEqual(again.json()["conversation_id"], conversation_id)
        third = self.client.post("/api/runtime/live-call", json={"session_id": "desk-10"})
        self.assertEqual(third.status_code, 200, third.text)
        self.assertNotEqual(third.json()["conversation_id"], conversation_id)
        row = self.client.app.state.lab.store.execute(
            "SELECT ended_at FROM conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
        self.assertIsNotNone(row["ended_at"])

    def test_hold_after_lease_drop_starts_new_conversation(self) -> None:
        first = self.client.post("/api/runtime/live-call", json={}).json()["conversation_id"]
        self.client.app.state.lab.runtime.clear_session_lease()
        second = self.client.post("/api/runtime/live-call", json={}).json()["conversation_id"]
        self.assertNotEqual(first, second)
        row = self.client.app.state.lab.store.execute(
            "SELECT ended_at FROM conversations WHERE id = ?", (first,)
        ).fetchone()
        self.assertIsNotNone(row["ended_at"])

    def test_clear_ends_session_and_prunes_ring(self) -> None:
        held = self.client.post("/api/runtime/live-call", json={})
        self.assertEqual(held.status_code, 200, held.text)
        conversation_id = held.json()["conversation_id"]
        cleared = self.client.delete("/api/runtime/live-call")
        self.assertEqual(cleared.status_code, 200, cleared.text)
        row = self.client.app.state.lab.store.execute(
            "SELECT ended_at FROM conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
        self.assertIsNotNone(row["ended_at"])
        self.assertIsNone(self.client.get("/api/runtime").json()["conversation_id"])


if __name__ == "__main__":
    unittest.main()
