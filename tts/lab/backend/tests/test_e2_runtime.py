#!/usr/bin/env python3
"""E2 runtime lifecycle. not on the voicecat path. No real GPU required."""

from __future__ import annotations

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
from tts.lab.backend.runtime.types import LiveCallActive, RuntimeBusy, RuntimeUnloaded
from tts.lab.backend.runtime.vram import FixedVramProbe
from tts.lab.backend.runtime.worker import CountingWorkerFactory, FakeWorkerHandle

GIB = 1024 ** 3


def _manager(
    *,
    free: int = 12 * GIB,
    required: int = 8 * GIB,
    margin: int = 0,
    handle_factory=None,
    leftover: NoopLeftover | None = None,
) -> tuple[E2RuntimeManager, CountingWorkerFactory, NoopLeftover]:
    leftover = leftover or NoopLeftover()
    factory = CountingWorkerFactory(handle_factory=handle_factory or FakeWorkerHandle)
    manager = E2RuntimeManager(
        vram=FixedVramProbe(free=free, used=1 * GIB),
        leftover=leftover,
        worker_factory=factory,
        required_vram_bytes=required,
        vram_margin_bytes=margin,
        load_timeout_s=2.0,
        unload_timeout_s=1.0,
        synth_timeout_s=2.0,
    )
    return manager, factory, leftover


class ManagerLifecycle(unittest.TestCase):
    def test_starts_unloaded_with_no_worker(self) -> None:
        manager, factory, leftover = _manager()
        status = manager.status()
        self.assertEqual(status.state, "unloaded")
        self.assertIsNone(status.worker_pid)
        self.assertEqual(factory.spawn_count, 0)
        self.assertFalse(leftover.is_parked())

    def test_load_unloaded_loading_ready_and_idempotent(self) -> None:
        manager, factory, leftover = _manager()
        first = manager.load()
        self.assertEqual(first.state, "ready")
        self.assertIsNotNone(first.worker_pid)
        self.assertEqual(factory.spawn_count, 1)
        self.assertTrue(leftover.is_parked())
        second = manager.load()
        self.assertEqual(second.state, "ready")
        self.assertEqual(second.worker_pid, first.worker_pid)
        self.assertEqual(factory.spawn_count, 1)

    def test_unload_ready_unloading_unloaded_and_idempotent(self) -> None:
        manager, factory, leftover = _manager()
        loaded = manager.load()
        pid = loaded.worker_pid
        self.assertIsNotNone(pid)
        unloaded = manager.unload()
        self.assertEqual(unloaded.state, "unloaded")
        self.assertIsNone(unloaded.worker_pid)
        self.assertFalse(factory.handles[0].is_alive())
        self.assertFalse(leftover.is_parked())
        again = manager.unload()
        self.assertEqual(again.state, "unloaded")
        self.assertIsNone(again.worker_pid)

    def test_synthesize_while_unloaded_raises_typed_error(self) -> None:
        manager, _, _ = _manager()
        with self.assertRaises(RuntimeUnloaded) as ctx:
            manager.synthesize({"text": "hi", "steer": "calm", "reference_audio": "x.wav"})
        self.assertEqual(ctx.exception.code, "runtime_unloaded")

    def test_insufficient_vram_does_not_spawn_worker(self) -> None:
        manager, factory, leftover = _manager(free=6 * GIB, required=8 * GIB, margin=0)
        status = manager.load()
        self.assertEqual(status.state, "insufficient_vram")
        self.assertIsNotNone(status.last_error)
        self.assertEqual(status.last_error.code, "insufficient_vram")
        self.assertEqual(status.last_error.required_bytes, 8 * GIB)
        self.assertEqual(status.last_error.free_bytes, 6 * GIB)
        self.assertEqual(status.last_error.shortfall_bytes, 2 * GIB)
        self.assertEqual(factory.spawn_count, 0)
        self.assertFalse(leftover.is_parked())

    def test_failed_worker_startup_leaves_manager_in_error(self) -> None:
        manager, factory, leftover = _manager(
            handle_factory=lambda: FakeWorkerHandle(die_after_start=True)
        )
        status = manager.load()
        self.assertEqual(status.state, "error")
        self.assertEqual(factory.spawn_count, 1)
        self.assertFalse(factory.handles[0].is_alive())
        self.assertFalse(leftover.is_parked())

    def test_failed_load_cleans_worker(self) -> None:
        manager, factory, leftover = _manager(
            handle_factory=lambda: FakeWorkerHandle(
                load_reply={"ok": False, "code": "error", "message": "boom"}
            )
        )
        status = manager.load()
        self.assertEqual(status.state, "error")
        self.assertEqual(factory.spawn_count, 1)
        handle = factory.handles[0]
        self.assertTrue(handle.terminated or handle.killed)
        self.assertFalse(handle.is_alive())
        self.assertFalse(leftover.is_parked())

    def test_cuda_oom_on_load_is_insufficient_vram_and_cleans_worker(self) -> None:
        manager, factory, leftover = _manager(
            handle_factory=lambda: FakeWorkerHandle(
                load_reply={"ok": False, "code": "cuda_oom", "message": "CUDA OOM"}
            )
        )
        status = manager.load()
        self.assertEqual(status.state, "insufficient_vram")
        self.assertEqual(factory.spawn_count, 1)
        self.assertFalse(factory.handles[0].is_alive())
        self.assertFalse(leftover.is_parked())

    def test_worker_crash_is_reflected_in_status(self) -> None:
        manager, factory, leftover = _manager()
        manager.load()
        factory.handles[0]._alive = False
        status = manager.status()
        self.assertEqual(status.state, "error")
        self.assertEqual(status.last_error.code, "worker_crash")
        self.assertIsNone(status.worker_pid)
        self.assertFalse(leftover.is_parked())

    def test_unload_kills_stuck_worker(self) -> None:
        manager, factory, _ = _manager(
            handle_factory=lambda: FakeWorkerHandle(ignore_shutdown=True)
        )
        manager.load()
        status = manager.unload()
        self.assertEqual(status.state, "unloaded")
        self.assertTrue(factory.handles[0].killed)
        self.assertFalse(factory.handles[0].is_alive())
        self.assertIsNone(status.worker_pid)

    def test_post_unload_pid_is_gone(self) -> None:
        manager, factory, _ = _manager()
        loaded = manager.load()
        self.assertTrue(factory.handles[0].is_alive())
        manager.unload()
        self.assertFalse(factory.handles[0].is_alive())
        self.assertIsNone(manager.status().worker_pid)
        self.assertNotEqual(loaded.worker_pid, manager.status().worker_pid)

    def test_synthesize_busy_during_load(self) -> None:
        manager, _, _ = _manager(
            handle_factory=lambda: FakeWorkerHandle(hang_load=True)
        )
        begin = manager.begin_load()
        self.assertEqual(begin.state, "loading")
        with self.assertRaises(RuntimeBusy):
            manager.synthesize({"text": "hi", "steer": "x", "reference_audio": "x.wav"})
        manager.wait_until_not("loading", timeout=2.0)

    def test_synthesize_stream_yields_before_tail(self) -> None:
        import time

        manager, factory, _ = _manager(
            handle_factory=lambda: FakeWorkerHandle(stream_delay_s=0.4)
        )
        manager.load()
        t0 = time.monotonic()
        it = manager.synthesize_stream(
            {"text": "hi", "steer": "x", "reference_audio": "x.wav"}
        )
        first = next(it)
        first_s = time.monotonic() - t0
        self.assertGreater(len(first), 0)
        self.assertLess(first_s, 0.15)
        rest = b"".join(it)
        wall_s = time.monotonic() - t0
        self.assertGreaterEqual(wall_s, 0.35)
        self.assertGreater(len(first) + len(rest), 100)
        self.assertEqual(factory.handles[0].rpc_calls[-1]["cmd"], "synthesize")

    def test_abandon_stream_then_next_synthesize_is_own_result(self) -> None:
        manager, factory, _ = _manager(
            handle_factory=lambda: FakeWorkerHandle(stream_delay_s=0.3)
        )
        manager.load()
        it = manager.synthesize_stream(
            {"text": "abandoned", "steer": "x", "reference_audio": "x.wav"}
        )
        first = next(it)
        self.assertGreater(len(first), 0)
        it.close()
        self.assertTrue(factory.handles[0].cancel_requested)
        result = manager.synthesize(
            {"text": "own", "steer": "x", "reference_audio": "x.wav"}
        )
        self.assertEqual(result["duration_s"], 0.05)
        self.assertEqual(factory.handles[0].rpc_calls[-1]["cmd"], "synthesize")
        self.assertEqual(factory.handles[0].rpc_calls[-1]["request"]["text"], "own")

    def test_live_call_lease_blocks_lab_synthesize(self) -> None:
        manager, _, _ = _manager()
        manager.load()
        manager.refresh_live_call_lease(ttl_s=30)
        self.assertTrue(manager.live_call_active())
        with self.assertRaises(LiveCallActive) as ctx:
            manager.synthesize({"text": "hi", "steer": "x", "reference_audio": "x.wav"})
        self.assertEqual(ctx.exception.code, "live_call_active")
        manager.refresh_live_call_lease(ttl_s=0)
        result = manager.synthesize({"text": "hi", "steer": "x", "reference_audio": "x.wav"})
        self.assertEqual(result["duration_s"], 0.05)

    def test_session_lease_holds_after_hop_ttl_expires(self) -> None:
        manager, _, _ = _manager()
        manager.load()
        manager.hold_session_lease(ttl_s=30)
        manager.refresh_live_call_lease(ttl_s=0)
        self.assertEqual(manager.status().live_call_holder, "session")
        with self.assertRaises(LiveCallActive) as ctx:
            manager.synthesize({"text": "hi", "steer": "x", "reference_audio": "x.wav"})
        self.assertEqual(ctx.exception.code, "live_call_active")
        manager.clear_session_lease()
        self.assertFalse(manager.live_call_active())
        result = manager.synthesize({"text": "hi", "steer": "x", "reference_audio": "x.wav"})
        self.assertEqual(result["duration_s"], 0.05)

    def test_clear_session_lease_keeps_hop_safety(self) -> None:
        manager, _, _ = _manager()
        manager.load()
        manager.hold_session_lease(ttl_s=30)
        manager.refresh_live_call_lease(ttl_s=30)
        manager.clear_session_lease()
        self.assertEqual(manager.status().live_call_holder, "hop")
        with self.assertRaises(LiveCallActive):
            manager.synthesize({"text": "hi", "steer": "x", "reference_audio": "x.wav"})
        manager.refresh_live_call_lease(ttl_s=0)
        result = manager.synthesize({"text": "hi", "steer": "x", "reference_audio": "x.wav"})
        self.assertEqual(result["duration_s"], 0.05)

    def test_loading_status_exposes_phase_and_elapsed(self) -> None:
        manager, _, _ = _manager(
            handle_factory=lambda: FakeWorkerHandle(hang_load=True)
        )
        begin = manager.begin_load()
        self.assertEqual(begin.state, "loading")
        self.assertTrue(begin.load_phase)
        self.assertIsNotNone(begin.load_started_at)
        body = begin.to_dict()
        self.assertIn(body["load_phase"], {"parking leftover", "checking VRAM", "starting worker", "loading weights"})
        self.assertIsNotNone(body["load_started_at"])
        self.assertGreaterEqual(body["load_elapsed_s"] or 0.0, 0.0)


class RuntimeHttp(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        leftover = NoopLeftover()
        factory = CountingWorkerFactory()
        self.factory = factory
        self.leftover = leftover
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

    def test_lab_starts_unloaded(self) -> None:
        body = self.client.get("/api/runtime/e2").json()
        self.assertEqual(body["state"], "unloaded")
        self.assertIsNone(body["worker_pid"])
        self.assertEqual(self.factory.spawn_count, 0)
        compat = self.client.get("/api/runtime").json()
        self.assertEqual(compat["status"], "unloaded")
        self.assertEqual(compat["state"], "unloaded")
        self.assertEqual(compat["display_name"], "Breeze TTS2")
        self.assertEqual(compat["engine"], "breeze-tts2")
        self.assertTrue(compat["voicecat_path"])
        breeze = self.client.get("/api/runtime/breeze").json()
        self.assertEqual(breeze["state"], "unloaded")
        self.assertEqual(breeze["display_name"], "Breeze TTS2")
        self.assertEqual(breeze["engine"], "breeze-tts2")

    def test_http_load_unload_and_synthesize_unloaded(self) -> None:
        loaded = self.client.post("/api/runtime/e2/load")
        self.assertEqual(loaded.status_code, 200, loaded.text)
        self.assertIn(loaded.json()["state"], {"loading", "ready"})
        for _ in range(50):
            body = self.client.get("/api/runtime/e2").json()
            if body["state"] == "ready":
                break
        else:
            self.fail(body)
        self.assertEqual(self.factory.spawn_count, 1)
        again = self.client.post("/api/runtime/e2/load")
        self.assertEqual(again.json()["state"], "ready")
        synth = self.client.post(
            "/api/synthesize",
            json={"text": "hello", "steer": "calm", "voice_profile_id": "missing"},
        )
        # voice missing is 404 only if runtime is ready; we didn't create a voice

        self.assertIn(synth.status_code, {404, 409})
        unloaded = self.client.post("/api/runtime/e2/unload")
        self.assertEqual(unloaded.status_code, 200)
        for _ in range(50):
            body = self.client.get("/api/runtime/e2").json()
            if body["state"] == "unloaded":
                break
        else:
            self.fail(body)
        self.assertIsNone(body["worker_pid"])
        self.assertTrue(self.leftover.is_parked())
        self.assertEqual(self.leftover.restore_calls, 0)
        refused = self.client.post(
            "/api/synthesize",
            json={"text": "hello", "steer": "calm", "voice_profile_id": "vp_x"},
        )
        self.assertEqual(refused.status_code, 409)
        self.assertEqual(refused.json()["detail"]["code"], "runtime_unloaded")

    def test_http_insufficient_vram_does_not_spawn(self) -> None:
        leftover = NoopLeftover()
        factory = CountingWorkerFactory()
        runtime = E2RuntimeManager(
            vram=FixedVramProbe(free=6 * GIB),
            leftover=leftover,
            worker_factory=factory,
            required_vram_bytes=8 * GIB,
            vram_margin_bytes=0,
        )
        with TestClient(create_app(root=Path(self.tmp.name), e2_runtime=runtime)) as client:
            response = client.post("/api/runtime/e2/load")
            self.assertEqual(response.status_code, 200)
            for _ in range(50):
                body = client.get("/api/runtime/e2").json()
                if body["state"] != "loading":
                    break
            self.assertEqual(body["state"], "insufficient_vram")
            self.assertEqual(body["last_error"]["code"], "insufficient_vram")
            self.assertEqual(factory.spawn_count, 0)
            self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
