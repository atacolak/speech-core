#!/usr/bin/env python3
"""stream.fm cache-key cases live beside artifact identity. not on the voicecat path."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO))

from tts.lab.backend.models import Interval
from tts.lab.backend.store.cache import canonical_processor_config, streamfm_cache_key


class StreamfmCacheKeyModule(unittest.TestCase):
    def test_canonical_config_is_stable(self) -> None:
        left = canonical_processor_config(
            {"solver": "lrk4", "task": "se-predgen", "checkpoint": "x"}
        )
        right = canonical_processor_config(
            {"checkpoint": "x", "task": "se-predgen", "solver": "lrk4"}
        )
        self.assertEqual(left, right)
        self.assertEqual(left["processor"], "streamfm")
        self.assertIn("preprocess_version", left)

    def test_keep_interval_order_does_not_fork_key(self) -> None:
        source = "deadbeef"
        a = streamfm_cache_key(
            source_sha256=source,
            keep_intervals=[
                Interval(start_s=7.0, end_s=11.0),
                Interval(start_s=0.0, end_s=4.0),
            ],
            processor_config={"task": "se-predgen", "solver": "lrk4", "checkpoint": "c"},
        )
        b = streamfm_cache_key(
            source_sha256=source,
            keep_intervals=[
                Interval(start_s=0.0, end_s=4.0),
                Interval(start_s=7.0, end_s=11.0),
            ],
            processor_config={"task": "se-predgen", "solver": "lrk4", "checkpoint": "c"},
        )
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
