#!/usr/bin/env python3
"""Worker JSON-line RPC skips CUDA banners. not on the voicecat path."""

from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO))

from tts.lab.backend.runtime.worker import (
    ScriptedWorkerHandle,
    StreamCancelled,
    decode_rpc_line,
    read_rpc_reply,
)


class FakeStdout:
    def __init__(self, lines: list[str]) -> None:
        self.lines = list(lines)

    def readline(self) -> str:
        if not self.lines:
            return ""
        return self.lines.pop(0)


class WorkerRpc(unittest.TestCase):
    def test_decode_skips_non_objects(self) -> None:
        self.assertIsNone(decode_rpc_line("OK"))
        self.assertIsNone(decode_rpc_line("[W] cuda"))
        self.assertIsNone(decode_rpc_line("true"))
        self.assertEqual(decode_rpc_line('{"ok": true, "pid": 3}'), {"ok": True, "pid": 3})

    def test_read_skips_banner_then_json(self) -> None:
        stdout = FakeStdout(
            [
                "OK\n",
                "[W0909 cuda_check.cc:12] noise\n",
                '{"ok": true, "pid": 7}\n',
            ]
        )
        self.assertEqual(read_rpc_reply(stdout, timeout=1.0)["pid"], 7)

    def test_closed_stdout_mentions_junk(self) -> None:
        stdout = FakeStdout(["OK\n"])
        with self.assertRaisesRegex(RuntimeError, "OK"):
            read_rpc_reply(stdout, timeout=1.0)

    def test_read_skips_pcm_chunk_then_reply(self) -> None:
        stdout = FakeStdout(
            [
                '{"event": "pcm_chunk", "pcm_b64": "AAAA"}\n',
                '{"event": "pcm_chunk", "pcm_b64": "AQAB"}\n',
                '{"ok": true, "result": {"wav_path": "take.wav"}}\n',
            ]
        )
        seen: list[str] = []
        reply = read_rpc_reply(
            stdout, timeout=1.0, on_progress=lambda payload: seen.append(str(payload["event"]))
        )
        self.assertTrue(reply["ok"])
        self.assertEqual(seen, ["pcm_chunk", "pcm_chunk"])

    def test_read_skips_load_progress_then_reply(self) -> None:
        stdout = FakeStdout(
            [
                '{"event": "load_progress", "phase": "loading weights"}\n',
                '{"event": "load_progress", "phase": "warmup CUDA graphs"}\n',
                '{"ok": true, "state": "ready", "pid": 9}\n',
            ]
        )
        seen: list[str] = []
        reply = read_rpc_reply(
            stdout, timeout=1.0, on_progress=lambda payload: seen.append(str(payload["phase"]))
        )
        self.assertEqual(reply["pid"], 9)
        self.assertEqual(seen, ["loading weights", "warmup CUDA graphs"])

    def test_read_skips_foreign_id_then_own(self) -> None:
        stdout = FakeStdout(
            [
                '{"id": "old", "ok": true, "result": {"wav_path": "stale.wav", "wall_s": 384.2}}\n',
                '{"id": "new", "ok": true, "result": {"wav_path": "own.wav", "wall_s": 1.1}}\n',
            ]
        )
        reply = read_rpc_reply(stdout, timeout=1.0, request_id="new")
        self.assertEqual(reply["result"]["wav_path"], "own.wav")
        self.assertEqual(reply["id"], "new")

    def test_abandon_residue_does_not_steal_next_result(self) -> None:
        stdout = FakeStdout(
            [
                '{"id": "a", "event": "pcm_chunk", "pcm_b64": "AAAA"}\n',
                '{"id": "a", "ok": true, "result": {"duration_s": 10.24, "wall_s": 384.2}}\n',
                '{"id": "b", "ok": true, "result": {"duration_s": 0.5, "wall_s": 0.6}}\n',
            ]
        )
        reply = read_rpc_reply(stdout, timeout=1.0, request_id="b")
        self.assertEqual(reply["result"]["duration_s"], 0.5)
        self.assertEqual(reply["id"], "b")


class ScriptedWorkerHandleTest(unittest.TestCase):
    """The double mirrors the subprocess reader: cancel is checked before a
    frame is delivered, so a cancel that lands during delivery is too late."""

    def _handle(self, frames: list[bytes], on_frame=None) -> ScriptedWorkerHandle:
        handle = ScriptedWorkerHandle(frames=frames, on_frame=on_frame)
        handle.start()
        return handle

    def _payload(self) -> dict:
        dest = Path(tempfile.mkdtemp(prefix="scripted-")) / "take.wav"
        return {"cmd": "synthesize", "id": "s1", "request": {"output_path": str(dest)}}

    def test_delivers_every_scripted_frame_in_order(self) -> None:
        handle = self._handle([b"\x01\x00", b"\x02\x00"])
        frames = list(handle.iter_synthesize(self._payload(), 1.0))
        self.assertEqual(frames, [b"\x01\x00", b"\x02\x00"])
        self.assertEqual(handle.delivered, 2)

    def test_cancel_before_a_frame_drops_it(self) -> None:
        cancelled = threading.Event()
        handle = self._handle([b"\x01\x00", b"\x02\x00"])
        stream = handle.iter_synthesize(
            self._payload(), 1.0, should_cancel=cancelled.is_set
        )
        self.assertEqual(next(stream), b"\x01\x00")
        cancelled.set()
        with self.assertRaises(StreamCancelled):
            next(stream)
        self.assertEqual(handle.delivered, 1)

    def test_cancel_during_delivery_still_delivers_that_frame(self) -> None:
        cancelled = threading.Event()
        handle = self._handle(
            [b"\x01\x00", b"\x02\x00", b"\x03\x00"],
            on_frame=lambda index: cancelled.set() if index == 1 else None,
        )
        stream = handle.iter_synthesize(
            self._payload(), 1.0, should_cancel=cancelled.is_set
        )
        self.assertEqual([next(stream), next(stream)], [b"\x01\x00", b"\x02\x00"])
        with self.assertRaises(StreamCancelled):
            next(stream)
        self.assertEqual(handle.delivered, 2)


if __name__ == "__main__":
    unittest.main()

