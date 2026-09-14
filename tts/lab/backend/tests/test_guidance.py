#!/usr/bin/env python3
"""Discriminated single/dual guidance. not on the voicecat path."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

from pydantic import ValidationError

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO))

from tts.lab.backend.models import DualGuidance, GenerationConfig, SingleGuidance, parse_guidance


class GuidanceModels(unittest.TestCase):
    def test_single_and_dual_are_discriminated(self) -> None:
        single = parse_guidance({"mode": "single", "cfg": 4.0})
        dual = parse_guidance({"mode": "dual", "reference": 1.0, "instruction": 2.0})
        self.assertIsInstance(single, SingleGuidance)
        self.assertEqual(single.mode, "single")
        self.assertEqual(single.cfg, 4.0)
        self.assertIsInstance(dual, DualGuidance)
        self.assertEqual(dual.mode, "dual")
        self.assertEqual(dual.reference, 1.0)
        self.assertEqual(dual.instruction, 2.0)

        config = GenerationConfig(guidance=single, seed=42)
        self.assertEqual(config.guidance.mode, "single")
        dual_config = GenerationConfig(guidance=dual, seed=7)
        self.assertEqual(dual_config.guidance.mode, "dual")

    def test_mixed_cfg_state_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            parse_guidance({"mode": "single", "cfg": 4.0, "reference": 1.0})
        with self.assertRaises(ValidationError):
            parse_guidance({"mode": "single", "cfg": 4.0, "instruction": 2.0})
        with self.assertRaises(ValidationError):
            parse_guidance(
                {"mode": "dual", "cfg": 4.0, "reference": 1.0, "instruction": 2.0}
            )
        with self.assertRaises(ValidationError):
            parse_guidance({"mode": "dual", "reference": 1.0})
        with self.assertRaises(ValidationError):
            parse_guidance({"mode": "single"})
        with self.assertRaises(ValidationError):
            parse_guidance({"mode": "dual", "reference": 1.0, "instruction": None})


if __name__ == "__main__":
    unittest.main()
