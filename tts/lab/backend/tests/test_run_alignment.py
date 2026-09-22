#!/usr/bin/env python3
"""Cached CPU Parakeet word alignment on settled runs. CPU only; never touches CUDA."""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import numpy as np
from fastapi.testclient import TestClient

from tts.lab.backend.runtime.leftover import NoopLeftover
from tts.lab.backend.runtime.manager import E2RuntimeManager
from tts.lab.backend.runtime.vram import FixedVramProbe
from tts.lab.backend.runtime.worker import CountingWorkerFactory, FakeWorkerHandle
from tts.lab.backend.services.run_alignment import align_run
from tts.wav import write_wav

GIB = 1024 ** 3
SAMPLE_RATE = 24000
POLL_S = 10.0

ALIGN_TARGET = "tts.lab.backend.services.run_alignment.transcribe_alignment"
THREAD_TARGET = "tts.lab.backend.services.run_alignment.threading"

WORDS = [
    {"text": "hello", "start_s": 0.10, "end_s": 0.42},
    {"text": "world", "start_s": 0.48, "end_s": 0.91},
]
HEARD = {"text": "hello world", "words": WORDS}


class FakeAlignment:
    """The CPU aligner stand-in: records calls, optionally held open by a gate."""

    def __init__(self, result: dict[str, Any] | None = None, *, held: bool = False) -> None:
        self.result = HEARD if result is None else result
        self.entered = threading.Event()
        self.release = threading.Event()
        self.paths: list[str] = []
        self._lock = threading.Lock()
        if not held:
            self.release.set()

    @property
    def calls(self) -> int:
        with self._lock:
            return len(self.paths)

    def __call__(self, path: Path | str) -> dict[str, Any]:
        with self._lock:
            self.paths.append(str(path))
        self.entered.set()
        self.release.wait(timeout=POLL_S)
        return self.result


class BrokenThreadStart:
    """threading.Thread stand-in whose start() fails like an exhausted process."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def start(self) -> None:
        raise RuntimeError("can't start new thread")


class RunAlignmentTest(unittest.TestCase):
    def setUp(self) -> None:
        from tts.lab.backend.app import create_app

        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.factory = CountingWorkerFactory(
            handle_factory=lambda: FakeWorkerHandle(stream_delay_s=0.05)
        )
        self.runtime = E2RuntimeManager(
            vram=FixedVramProbe(free=12 * GIB),
            leftover=NoopLeftover(),
            worker_factory=self.factory,
            required_vram_bytes=8 * GIB,
            vram_margin_bytes=0,
        )
        self.runtime.load()
        self.client = TestClient(create_app(root=self.root, e2_runtime=self.runtime))
        self.state = self.client.app.state.lab
        ref = self.root / "ref.wav"
        write_wav(ref, SAMPLE_RATE, np.zeros(2400, dtype=np.float32))
        with ref.open("rb") as handle:
            self.voice = self.client.post(
                "/api/voices",
                data={"name": "align", "transcript": "fixture"},
                files={"audio": ("ref.wav", handle, "audio/wav")},
            ).json()

    def tearDown(self) -> None:
        self.client.close()
        self.runtime.unload()
        self.tmp.cleanup()

    def _body(self, text: str) -> dict:
        return {"text": text, "steer": "calm", "voice_profile_id": self.voice["id"]}

    def _synth_calls(self) -> list:
        if not self.factory.handles:
            return []
        return [call for call in self.factory.handles[0].rpc_calls if call.get("cmd") == "synthesize"]

    def _only_run_id(self) -> str:
        rows = self.state.store.execute("SELECT id FROM runs ORDER BY rowid").fetchall()
        self.assertEqual(len(rows), 1)
        return str(rows[0]["id"])

    def _output_path(self, run_id: str) -> Path:
        row = self.state.store.execute(
            "SELECT output_artifact_id FROM runs WHERE id = ?", (run_id,)
        ).fetchone()
        self.assertIsNotNone(row)
        return self.state.store.get(str(row["output_artifact_id"])).path

    def _artifact_id(self, run_id: str) -> str:
        row = self.state.store.execute(
            "SELECT output_artifact_id FROM runs WHERE id = ?", (run_id,)
        ).fetchone()
        self.assertIsNotNone(row)
        return str(row["output_artifact_id"])

    def _seed_pending_run(self, run_id: str = "run_pending_seed") -> tuple[str, str]:
        """A durable pending row, as a restart or a stale read would find it."""
        settled = self.root / f"{run_id}.wav"
        write_wav(settled, SAMPLE_RATE, np.zeros(2400, dtype=np.float32))
        artifact = self.state.store.import_audio(settled)
        self.state.store.execute(
            """
            INSERT INTO runs (
                id, voice_id, request_json, output_artifact_id, effective_reference_json,
                latency_ms, first_audio_ms, duration_s, rating, tags_json, created_at,
                alignment_json
            ) VALUES (?, NULL, ?, ?, ?, ?, NULL, ?, NULL, ?, ?, ?)
            """,
            (
                run_id,
                json.dumps({"text": "hello world"}),
                artifact.id,
                json.dumps({}),
                120.0,
                0.2,
                json.dumps([]),
                "2026-09-16T00:00:00Z",
                json.dumps({"status": "pending"}),
            ),
        )
        self.state.store.commit()
        return run_id, artifact.id

    def _alignment(self, run_id: str, client: TestClient | None = None) -> dict:
        response = (client or self.client).get(f"/api/runs/{run_id}")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["alignment"]

    def _settled(self, run_id: str, client: TestClient | None = None) -> dict:
        """Poll the real run route until the cache leaves pending."""
        deadline = time.monotonic() + POLL_S
        while True:
            response = (client or self.client).get(f"/api/runs/{run_id}")
            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()
            alignment = body.get("alignment") or {}
            if alignment.get("status") != "pending":
                return body
            if time.monotonic() >= deadline:
                self.fail(f"alignment stayed pending: {response.text}")
            time.sleep(0.02)

    def test_one_shot_run_returns_pending_then_cached_cpu_words(self) -> None:
        aligner = FakeAlignment(held=True)
        with patch(ALIGN_TARGET, new=aligner):
            try:
                response = self.client.post(
                    "/api/synthesize", json=self._body("One short line.")
                )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertGreater(len(response.content), 0)
                self.assertTrue(aligner.entered.wait(timeout=POLL_S), "alignment never started")
                self.assertFalse(aligner.release.is_set())
                run_id = self._only_run_id()
                self.assertEqual(self._alignment(run_id), {"status": "pending"})
                self.assertEqual(
                    self.client.get("/api/runs").json()["items"][0]["alignment"],
                    {"status": "pending"},
                )
            finally:
                aligner.release.set()
            body = self._settled(run_id)
        self.assertEqual(
            body["alignment"], {"status": "ready", "text": "hello world", "words": WORDS}
        )
        # Reads of the pending row never started a second alignment.
        self.assertEqual(aligner.calls, 1)
        self.assertEqual(aligner.paths, [str(self._output_path(run_id))])
    def test_streamed_generate_does_not_create_or_schedule_alignment(self) -> None:
        aligner = FakeAlignment(held=True)
        with patch(ALIGN_TARGET, new=aligner):
            response = self.client.post(
                "/api/generate/stream", json=self._body("One short line.")
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertGreater(len(response.content), 0)
            run_id = self._only_run_id()
            row = self.state.store.execute(
                "SELECT alignment_json FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
        self.assertIsNone(row["alignment_json"])
        self.assertEqual(aligner.calls, 0)

    def test_pending_alignment_resumes_after_restart(self) -> None:
        from tts.lab.backend.app import create_app

        settled = self.root / "settled.wav"
        write_wav(
            settled, SAMPLE_RATE, 0.1 * np.sin(2 * np.pi * 220 * np.arange(4800) / SAMPLE_RATE)
        )
        artifact = self.state.store.import_audio(settled)
        run_id = "run_pending_seed"
        self.state.store.execute(
            """
            INSERT INTO runs (
                id, voice_id, request_json, output_artifact_id, effective_reference_json,
                latency_ms, first_audio_ms, duration_s, rating, tags_json, created_at,
                alignment_json
            ) VALUES (?, NULL, ?, ?, ?, ?, NULL, ?, NULL, ?, ?, ?)
            """,
            (
                run_id,
                json.dumps({"text": "hello world"}),
                artifact.id,
                json.dumps({}),
                120.0,
                0.2,
                json.dumps([]),
                "2026-09-16T00:00:00Z",
                json.dumps({"status": "pending"}),
            ),
        )
        self.state.store.commit()
        self.client.close()

        aligner = FakeAlignment(held=True)
        with patch(ALIGN_TARGET, new=aligner):
            with TestClient(create_app(root=self.root, e2_runtime=self.runtime)) as restarted:
                listed = restarted.get("/api/runs")
                self.assertEqual(listed.status_code, 200, listed.text)
                self.assertEqual(
                    listed.json()["items"][0]["alignment"], {"status": "pending"}
                )
                self.assertTrue(
                    aligner.entered.wait(timeout=POLL_S), "restart never resumed alignment"
                )
                try:
                    self.assertEqual(
                        self._alignment(run_id, restarted), {"status": "pending"}
                    )
                finally:
                    aligner.release.set()
                body = self._settled(run_id, restarted)
        self.assertEqual(
            body["alignment"], {"status": "ready", "text": "hello world", "words": WORDS}
        )
        self.assertEqual(aligner.paths, [str(artifact.path)])
        self.assertEqual(aligner.calls, 1)

    def test_unavailable_alignment_is_cached_without_gpu(self) -> None:
        aligner = FakeAlignment(result={"text": "", "words": []})
        with patch(ALIGN_TARGET, new=aligner):
            response = self.client.post(
                "/api/synthesize", json=self._body("One short line.")
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertGreater(len(response.content), 0)
            run_id = self._only_run_id()
            body = self._settled(run_id)
            self.assertEqual(
                body["alignment"], {"status": "unavailable", "text": "", "words": []}
            )
            synth_calls = len(self._synth_calls())
            self.assertGreaterEqual(synth_calls, 1)
            for _ in range(3):
                self.client.get(f"/api/runs/{run_id}")
                self.client.get("/api/runs")
            # An unavailable cache is durable and never reaches the worker runtime.
            self.assertEqual(len(self._synth_calls()), synth_calls)
        self.assertEqual(aligner.calls, 1)
        self.assertEqual(aligner.paths, [str(self._output_path(run_id))])

    def test_late_duplicate_pending_snapshot_cannot_downgrade_ready(self) -> None:
        """A duplicate pass from a stale pending snapshot cannot overwrite a settled cache."""
        aligner = FakeAlignment()
        with patch(ALIGN_TARGET, new=aligner):
            response = self.client.post(
                "/api/synthesize", json=self._body("One short line.")
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertGreater(len(response.content), 0)
            run_id = self._only_run_id()
            body = self._settled(run_id)
        self.assertEqual(
            body["alignment"], {"status": "ready", "text": "hello world", "words": WORDS}
        )
        # A reader holding the pre-settlement snapshot schedules one more pass.
        late = FakeAlignment(result={"text": "", "words": []})
        with patch(ALIGN_TARGET, new=late):
            align_run(self.state.store, run_id, self._artifact_id(run_id))
        self.assertEqual(late.calls, 1)
        self.assertEqual(
            self._alignment(run_id),
            {"status": "ready", "text": "hello world", "words": WORDS},
        )

    def test_failed_thread_start_leaves_run_reschedulable(self) -> None:
        """A start() that blows up must not strand the run id as unschedulable."""
        run_id, _artifact_id = self._seed_pending_run()
        leaked: Exception | None = None
        with patch(THREAD_TARGET, new=SimpleNamespace(Thread=BrokenThreadStart)):
            try:
                listed = self.client.get("/api/runs")
            except Exception as exc:  # 388db09 lets the start failure escape the route
                leaked = exc
            self.assertIsNone(leaked, "a failed thread start must not escape the run route")
            self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(listed.json()["items"][0]["alignment"], {"status": "pending"})
        # Still pending, so the next read has to schedule it again.
        aligner = FakeAlignment(held=True)
        with patch(ALIGN_TARGET, new=aligner):
            fetched = self.client.get(f"/api/runs/{run_id}")
            self.assertEqual(fetched.status_code, 200, fetched.text)
            self.assertTrue(aligner.entered.wait(timeout=POLL_S), "run was never rescheduled")
            try:
                self.assertEqual(fetched.json()["alignment"], {"status": "pending"})
            finally:
                aligner.release.set()
            body = self._settled(run_id)
        self.assertEqual(
            body["alignment"], {"status": "ready", "text": "hello world", "words": WORDS}
        )
        self.assertEqual(aligner.calls, 1)


class TurnAlignmentTest(unittest.TestCase):
    def setUp(self) -> None:
        from tts.lab.backend.app import create_app
        from tts.lab.backend.runtime.worker import ScriptedWorkerHandle

        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.factory = CountingWorkerFactory(
            handle_factory=lambda: ScriptedWorkerHandle(
                frames=[b"\x01\x00" * 480, b"\x02\x00" * 480, b"\x03\x00" * 480]
            )
        )
        self.runtime = E2RuntimeManager(
            vram=FixedVramProbe(free=12 * GIB),
            leftover=NoopLeftover(),
            worker_factory=self.factory,
            required_vram_bytes=8 * GIB,
            vram_margin_bytes=0,
        )
        self.runtime.load()
        self.client = TestClient(create_app(root=self.root, leftover_parked=False, e2_runtime=self.runtime))
        self.state = self.client.app.state.lab
        self.store = self.state.store
        ref = self.root / "ref.wav"
        write_wav(ref, SAMPLE_RATE, np.zeros(2400, dtype=np.float32))
        with ref.open("rb") as handle:
            self.voice = self.client.post(
                "/api/voices",
                data={"name": "align", "transcript": "fixture"},
                files={"audio": ("ref.wav", handle, "audio/wav")},
            ).json()

    def tearDown(self) -> None:
        self.client.close()
        self.runtime.unload()
        self.tmp.cleanup()

    def _capture_assistant_turn(self) -> dict:
        self.client.post("/api/talker/voice", json={"voice_id": self.voice["id"]})
        held = self.client.post("/api/runtime/live-call", json={"ttl_s": 45})
        self.assertEqual(held.status_code, 200, held.text)
        conversation_id = held.json()["conversation_id"]
        pcm = self.client.post(
            "/internal/leftover/v1/audio/speech",
            json={
                "input": "hello world",
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

    def _turn_alignment(self, conversation_id: str, client: TestClient | None = None) -> dict:
        response = (client or self.client).get(f"/api/conversations/{conversation_id}")
        self.assertEqual(response.status_code, 200, response.text)
        turns = response.json()["turns"]
        self.assertEqual(len(turns), 1)
        return turns[0]["alignment"]

    def _settled_turn(self, conversation_id: str, client: TestClient | None = None) -> dict:
        deadline = time.monotonic() + POLL_S
        while True:
            response = (client or self.client).get(f"/api/conversations/{conversation_id}")
            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()
            alignment = (body["turns"][0].get("alignment") or {})
            if alignment.get("status") != "pending":
                return body
            if time.monotonic() >= deadline:
                self.fail(f"turn alignment stayed pending: {response.text}")
            time.sleep(0.02)

    def test_turn_alignment_pending_then_ready_cached(self) -> None:
        aligner = FakeAlignment(held=True)
        with patch(ALIGN_TARGET, new=aligner):
            try:
                turn = self._capture_assistant_turn()
                self.assertTrue(aligner.entered.wait(timeout=POLL_S), "alignment never started")
                self.assertFalse(aligner.release.is_set())
                raw = self.store.execute(
                    "SELECT alignment_json FROM conversation_turns WHERE id = ?",
                    (turn["id"],),
                ).fetchone()
                self.assertEqual(json.loads(raw["alignment_json"]), {"status": "pending"})
                self.assertEqual(
                    self._turn_alignment(turn["conversation_id"]), {"status": "pending"}
                )
            finally:
                aligner.release.set()
            body = self._settled_turn(turn["conversation_id"])
        self.assertEqual(
            body["turns"][0]["alignment"],
            {"status": "ready", "text": "hello world", "words": WORDS},
        )
        self.assertEqual(aligner.calls, 1)

    def test_pending_turn_alignment_resumes_on_detail_read(self) -> None:
        from tts.lab.backend.app import create_app

        settled = self.root / "turn-pending.wav"
        write_wav(settled, SAMPLE_RATE, np.zeros(2400, dtype=np.float32))
        artifact = self.store.import_audio(settled)
        conversation_id = "cv_pending_seed"
        turn_id = "ct_pending_seed"
        self.store.execute(
            "INSERT INTO conversations (id, started_at, ended_at, saved, created_at)"
            " VALUES (?, ?, NULL, 0, ?)",
            (conversation_id, "2026-09-16T00:00:00Z", "2026-09-16T00:00:00Z"),
        )
        self.store.execute(
            """
            INSERT INTO conversation_turns (
                id, conversation_id, msg_seq, variation_seq, role, text,
                audio_artifact_id, voice_id, steer, generation_json, alignment_json,
                chosen, started_at, ended_at, created_at
            ) VALUES (?, ?, 0, 0, 'assistant', 'hello world', ?, ?, NULL, NULL, ?, 1, NULL, NULL, ?)
            """,
            (
                turn_id,
                conversation_id,
                artifact.id,
                self.voice["id"],
                json.dumps({"status": "pending"}),
                "2026-09-16T00:00:00Z",
            ),
        )
        self.store.commit()
        self.client.close()

        aligner = FakeAlignment(held=True)
        with patch(ALIGN_TARGET, new=aligner):
            with TestClient(create_app(root=self.root, leftover_parked=False, e2_runtime=self.runtime)) as restarted:
                listed = restarted.get(f"/api/conversations/{conversation_id}")
                self.assertEqual(listed.status_code, 200, listed.text)
                self.assertEqual(listed.json()["turns"][0]["alignment"], {"status": "pending"})
                self.assertTrue(
                    aligner.entered.wait(timeout=POLL_S), "detail never resumed alignment"
                )
                try:
                    self.assertEqual(
                        listed.json()["turns"][0]["alignment"], {"status": "pending"}
                    )
                finally:
                    aligner.release.set()
                body = self._settled_turn(conversation_id, restarted)
        self.assertEqual(
            body["turns"][0]["alignment"],
            {"status": "ready", "text": "hello world", "words": WORDS},
        )
        self.assertEqual(aligner.paths, [str(artifact.path)])
        self.assertEqual(aligner.calls, 1)



if __name__ == "__main__":
    unittest.main()
