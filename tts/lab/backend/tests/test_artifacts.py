#!/usr/bin/env python3
"""Content-addressed lab artifacts. not on the voicecat path."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO))

from tts.lab.backend.models import Interval
from tts.lab.backend.store.artifacts import ArtifactStore
from tts.wav import write_wav


def _tone(path: Path, *, freq: float = 440.0, seconds: float = 0.2) -> Path:
    sr = 24000
    t = np.arange(int(sr * seconds), dtype=np.float32) / sr
    write_wav(path, sr, 0.1 * np.sin(2 * np.pi * freq * t))
    return path


class ArtifactIdentity(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = ArtifactStore(self.root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_same_bytes_share_object_identity_and_do_not_duplicate(self) -> None:
        a = _tone(self.root / "a.wav")
        b = Path(self.root / "b.wav")
        b.write_bytes(a.read_bytes())
        first = self.store.import_audio(a)
        second = self.store.import_audio(b)
        self.assertEqual(first.id, second.id)
        self.assertEqual(first.sha256, second.sha256)
        objects = list((self.root / "objects").glob("*"))
        self.assertEqual(len(objects), 1)
        self.assertTrue(objects[0].is_file())
        self.assertEqual(objects[0].stat().st_size, a.stat().st_size)


class StreamfmCacheKey(unittest.TestCase):
    def test_key_changes_when_intervals_or_config_change(self) -> None:
        from tts.lab.backend.store.cache import streamfm_cache_key

        source = "abc" * 20
        keep = [Interval(start_s=0.0, end_s=10.0)]
        base = streamfm_cache_key(
            source_sha256=source,
            keep_intervals=keep,
            processor_config={"task": "se-predgen", "solver": "lrk4", "checkpoint": "ckpt-a"},
        )
        changed_interval = streamfm_cache_key(
            source_sha256=source,
            keep_intervals=[Interval(start_s=0.0, end_s=9.0)],
            processor_config={"task": "se-predgen", "solver": "lrk4", "checkpoint": "ckpt-a"},
        )
        changed_config = streamfm_cache_key(
            source_sha256=source,
            keep_intervals=keep,
            processor_config={"task": "se-predgen", "solver": "rk4", "checkpoint": "ckpt-a"},
        )
        self.assertNotEqual(base, changed_interval)
        self.assertNotEqual(base, changed_config)
        self.assertEqual(
            base,
            streamfm_cache_key(
                source_sha256=source,
                keep_intervals=keep,
                processor_config={"checkpoint": "ckpt-a", "solver": "lrk4", "task": "se-predgen"},
            ),
        )


class PinAndCacheCleanup(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = ArtifactStore(self.root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_pinned_survives_and_unreferenced_cache_is_removed(self) -> None:
        durable = self.store.import_audio(_tone(self.root / "voice.wav", freq=220.0))
        cached = self.store.import_audio(_tone(self.root / "cache.wav", freq=880.0))
        self.store.pin(durable.id, reason="voice:ata")
        self.store.remember_cache(
            cache_key="streamfm-demo",
            artifact_id=cached.id,
            processor="streamfm",
            config={"task": "se-predgen"},
        )
        removed = self.store.cleanup_cache()
        self.assertIn(cached.id, removed)
        self.assertTrue(self.store.path_for(durable.id).is_file())
        self.assertFalse(self.store.path_for(cached.id).exists())
        with self.assertRaises(KeyError):
            self.store.get(cached.id)


if __name__ == "__main__":
    unittest.main()
