#!/usr/bin/env python3
"""CPU tests for lab/scripts/breeze_tts_qual. not on the voicecat path."""

from __future__ import annotations

import sys
import struct
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent / "breeze_tts_qual"
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT))


class Tombstone(unittest.TestCase):
    def test_runtime_files_carry_lab_tombstone(self) -> None:
        py_files = sorted(ROOT.glob("*.py"))
        self.assertTrue(py_files, "expected breeze_tts_qual python files")
        for path in py_files:
            text = path.read_text(encoding="utf-8")
            self.assertIn(
                "not on the voicecat path.",
                text,
                f"{path} missing lab tombstone",
            )


class ConfigTable(unittest.TestCase):
    def test_brief_configs_exist(self) -> None:
        from breeze_tts_qual.configs import CONFIGS, EngineConfig

        names = {c.name for c in CONFIGS}
        self.assertGreaterEqual(
            names,
            {
                "A",
                "B_depth",
                "B_codec",
                "B_backbone_decode",
                "B_backbone_prefill",
                "C0",
                "C1",
                "C2",
                "C3",
                "C4",
                "D",
            },
        )
        by = {c.name: c for c in CONFIGS}
        self.assertEqual(by["A"].precision, "bf16")
        self.assertFalse(by["A"].fast_depth_decoder)
        self.assertFalse(by["A"].fast_codec)
        self.assertFalse(by["A"].fast_backbone_decode)
        self.assertFalse(by["A"].fast_backbone_prefill)
        self.assertTrue(by["B_depth"].fast_depth_decoder)
        self.assertFalse(by["B_depth"].fast_codec)
        self.assertEqual(by["C0"].precision, "hybrid_int8")
        self.assertFalse(by["C0"].fast_depth_decoder)
        self.assertTrue(by["C1"].fast_depth_decoder)
        self.assertTrue(by["C2"].fast_codec)
        self.assertTrue(by["C3"].fast_backbone_decode)
        self.assertTrue(by["C4"].fast_backbone_prefill)
        self.assertEqual(by["D"].precision, "full_int8")
        for cfg in CONFIGS:
            self.assertIsInstance(cfg, EngineConfig)
            self.assertFalse(
                getattr(cfg, "fast_text_encoder", False),
                f"{cfg.name} must not enable text-encoder graphs",
            )


class Metrics(unittest.TestCase):
    def test_first_nonsilent_skips_subthreshold(self) -> None:
        from breeze_tts_qual.metrics import SILENCE_ABS_S16, first_nonsilent_s

        self.assertEqual(SILENCE_ABS_S16, 512)
        silent = struct.pack("<" + "h" * 8, *([0] * 7 + [511]))
        t, idx = first_nonsilent_s(
            [(0.010, silent)],
            sample_rate=24000,
        )
        self.assertIsNone(t)
        self.assertIsNone(idx)

    def test_first_nonsilent_uses_chunk_clock_plus_offset(self) -> None:
        from breeze_tts_qual.metrics import first_nonsilent_s

        # 4 silent samples, then 600, at 24 kHz → 4/24000 s after chunk start
        pcm = struct.pack("<hhhhh", 0, 0, 0, 0, 600)
        t, idx = first_nonsilent_s([(0.100, pcm)], sample_rate=24000)
        self.assertEqual(idx, 4)
        self.assertAlmostEqual(t, 0.100 + 4 / 24000.0, places=9)

    def test_percentiles_rtf_jitter(self) -> None:
        from breeze_tts_qual.metrics import inter_chunk_gaps, percentile, rtf

        xs = [float(i) for i in range(1, 31)]
        self.assertEqual(len(xs), 30)
        self.assertAlmostEqual(percentile(xs, 0.50), 15.5, places=5)
        self.assertAlmostEqual(percentile(xs, 0.95), 28.55, places=5)
        self.assertAlmostEqual(percentile(xs, 0.99), 29.71, places=5)
        self.assertAlmostEqual(rtf(wall_s=0.5, audio_s=1.0), 0.5)
        gaps = inter_chunk_gaps(
            [
                {"t_rel_s": 0.10, "duration_s": 0.04},
                {"t_rel_s": 0.20, "duration_s": 0.04},
                {"t_rel_s": 0.40, "duration_s": 0.04},
            ]
        )
        # gap = t[i] - (t[i-1] + duration[i-1])
        self.assertAlmostEqual(gaps[0], 0.06, places=9)
        self.assertAlmostEqual(gaps[1], 0.16, places=9)

if __name__ == "__main__":
    unittest.main()
