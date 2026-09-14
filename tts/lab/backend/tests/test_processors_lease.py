#!/usr/bin/env python3
"""Processor GPU lease: take the GPU from E2 without bouncing leftover. No real GPU."""

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
from tts.lab.backend.runtime.processors import ProcessorLease
from tts.lab.backend.runtime.types import LiveCallActive, RuntimeBusy
from tts.lab.backend.runtime.vram import FixedVramProbe
from tts.lab.backend.runtime.worker import CountingWorkerFactory, FakeWorkerHandle

GIB = 1024 ** 3


def _manager(
    *,
    handle_factory=None,
    leftover: NoopLeftover | None = None,
) -> tuple[E2RuntimeManager, CountingWorkerFactory, NoopLeftover]:
    leftover = leftover or NoopLeftover()
    factory = CountingWorkerFactory(handle_factory=handle_factory or FakeWorkerHandle)
    manager = E2RuntimeManager(
        vram=FixedVramProbe(free=12 * GIB, used=1 * GIB),
        leftover=leftover,
        worker_factory=factory,
        required_vram_bytes=8 * GIB,
        vram_margin_bytes=0,
        load_timeout_s=2.0,
        unload_timeout_s=1.0,
        synth_timeout_s=2.0,
    )
    return manager, factory, leftover


class ProcessorLeaseTests(unittest.TestCase):
    def test_lease_unloads_ready_e2_without_restoring_leftover(self) -> None:
        manager, factory, leftover = _manager()
        manager.load()
        self.assertTrue(leftover.is_parked())
        restores = leftover.restore_calls

        lease = ProcessorLease(manager)
        with lease.acquire("resemble"):
            status = manager.status()
            self.assertEqual(status.state, "unloaded")
            self.assertIsNone(status.worker_pid)
            self.assertEqual(status.processor, "resemble")
            self.assertEqual(leftover.restore_calls, restores)
            self.assertTrue(leftover.is_parked())
            self.assertFalse(factory.handles[0].is_alive())

        self.assertIsNone(manager.status().processor)
        self.assertEqual(leftover.restore_calls, restores)
        self.assertTrue(leftover.is_parked())

    def test_lease_blocked_by_live_call_leaves_e2_ready(self) -> None:
        manager, factory, leftover = _manager()
        manager.load()
        manager.refresh_live_call_lease(ttl_s=30)

        lease = ProcessorLease(manager)
        with self.assertRaises(LiveCallActive) as ctx:
            with lease.acquire("vibevoice"):
                pass
        self.assertEqual(ctx.exception.code, "live_call_active")

        status = manager.status()
        self.assertEqual(status.state, "ready")
        self.assertIsNone(status.processor)
        self.assertTrue(factory.handles[0].is_alive())
        self.assertEqual(leftover.restore_calls, 0)

    def test_lease_blocked_while_e2_busy(self) -> None:
        manager, factory, leftover = _manager(
            handle_factory=lambda: FakeWorkerHandle(hang_load=True)
        )
        manager.begin_load()

        lease = ProcessorLease(manager)
        with self.assertRaises(RuntimeBusy) as ctx:
            with lease.acquire("resemble"):
                pass
        self.assertEqual(ctx.exception.code, "runtime_busy")
        self.assertEqual(ctx.exception.state, "loading")
        self.assertIsNone(manager.status().processor)
        self.assertEqual(factory.spawn_count, 1)
        self.assertEqual(leftover.restore_calls, 0)
        manager.wait_until_not("loading", timeout=2.0)

    def test_lease_on_unloaded_e2_spawns_no_worker(self) -> None:
        manager, factory, leftover = _manager()
        lease = ProcessorLease(manager)
        with lease.acquire("resemble"):
            self.assertEqual(manager.status().state, "unloaded")
            self.assertEqual(manager.status().processor, "resemble")
        self.assertIsNone(manager.status().processor)
        self.assertEqual(factory.spawn_count, 0)
        self.assertEqual(leftover.restore_calls, 0)

    def test_occupant_cleared_when_body_raises(self) -> None:
        manager, _, _ = _manager()
        manager.load()
        lease = ProcessorLease(manager)
        with self.assertRaises(ValueError):
            with lease.acquire("resemble"):
                raise ValueError("processor blew up")
        self.assertIsNone(manager.status().processor)

    def test_second_lease_after_first_releases(self) -> None:
        manager, factory, leftover = _manager()
        manager.load()
        lease = ProcessorLease(manager)
        with lease.acquire("resemble"):
            self.assertEqual(manager.status().processor, "resemble")
        with lease.acquire("vibevoice"):
            self.assertEqual(manager.status().processor, "vibevoice")
        self.assertIsNone(manager.status().processor)
        self.assertEqual(factory.spawn_count, 1)


class ProcessorStatusHttp(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.manager, _, _ = _manager()
        self.manager.load()
        self.client = TestClient(
            create_app(
                root=Path(self.tmp.name),
                leftover_parked=False,
                e2_runtime=self.manager,
            )
        )

    def tearDown(self) -> None:
        self.client.close()
        self.tmp.cleanup()

    def test_runtime_status_reports_processor_occupant(self) -> None:
        self.assertIn("processor", self.client.get("/api/runtime").json())
        self.assertIsNone(self.client.get("/api/runtime").json()["processor"])
        with ProcessorLease(self.manager).acquire("resemble"):
            self.assertEqual(self.client.get("/api/runtime").json()["processor"], "resemble")
        self.assertIsNone(self.client.get("/api/runtime").json()["processor"])


if __name__ == "__main__":
    unittest.main()
