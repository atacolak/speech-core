#!/usr/bin/env python3
"""CPU (+ optional GPU) tests for E2 synthesis service. not on the voicecat path."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "lab" / "scripts"))

FIXTURE_TEXT = "i found the issue. the worker is holding the old session open."
FIXTURE_STEER = "calm and matter-of-fact."


def _pin_root() -> Path:
    from tts.paths import qual_root

    return qual_root()


class SynthesisServiceCpu(unittest.TestCase):
    def test_fake_backend_writes_riff_wav_with_duration(self) -> None:
        from breeze_tts_qual.configs import EngineConfig
        from breeze_tts_qual.engine import BreezeEngine, FakeBackend

        from tts.lab.backend.services.breeze import SynthesisRequest, synthesize_e2

        engine = BreezeEngine(
            EngineConfig(name="E2", precision="bf16"),
            ckpt_dir=".",
            backend=FakeBackend(),
        )
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "take.wav"
            result = synthesize_e2(
                SynthesisRequest(
                    text=FIXTURE_TEXT,
                    steer=FIXTURE_STEER,
                    reference_audio=Path(tmp) / "ref.wav",
                    reference_text="ref",
                    output_path=dest,
                ),
                engine=engine,
            )
            self.assertTrue(dest.is_file())
            header = dest.read_bytes()[:12]
            self.assertEqual(header[:4], b"RIFF")
            self.assertEqual(header[8:12], b"WAVE")
            self.assertGreater(result.duration_s, 0)
            self.assertEqual(result.sample_rate, 24000)
            self.assertGreater(result.samples.size, 0)
            self.assertEqual(result.request_snapshot["text"], FIXTURE_TEXT)
            self.assertEqual(result.request_snapshot["steer"], FIXTURE_STEER)
            self.assertEqual(result.runtime["selected"], "E2")

    def test_mixed_dual_guidance_is_rejected(self) -> None:
        from breeze_tts_qual.configs import EngineConfig
        from breeze_tts_qual.engine import BreezeEngine, FakeBackend

        from tts.generation import GenerationSettings
        from tts.lab.backend.services.breeze import SynthesisRequest, synthesize_e2

        engine = BreezeEngine(
            EngineConfig(name="E2", precision="bf16"),
            ckpt_dir=".",
            backend=FakeBackend(),
        )
        with self.assertRaises(ValueError):
            synthesize_e2(
                SynthesisRequest(
                    text=FIXTURE_TEXT,
                    steer=FIXTURE_STEER,
                    reference_audio="ref.wav",
                    reference_text="ref",
                    generation=GenerationSettings(cfg_scale_ref=1.0, cfg_scale_ins=None),
                ),
                engine=engine,
            )

    def test_service_does_not_load_engine_when_one_is_passed(self) -> None:
        from breeze_tts_qual.configs import EngineConfig
        from breeze_tts_qual.engine import BreezeEngine, FakeBackend, PcmChunk

        from tts.lab.backend.services.breeze import SynthesisRequest, synthesize_e2

        calls = {"synthesize": 0, "close": 0}

        class Probe(FakeBackend):
            def synthesize(self, **kwargs):
                calls["synthesize"] += 1
                yield from super().synthesize(**kwargs)

            def close(self) -> None:
                calls["close"] += 1

        engine = BreezeEngine(
            EngineConfig(name="E2", precision="bf16"),
            ckpt_dir=".",
            backend=Probe(),
        )
        with tempfile.TemporaryDirectory() as tmp:
            synthesize_e2(
                SynthesisRequest(
                    text=FIXTURE_TEXT,
                    steer=FIXTURE_STEER,
                    reference_audio=Path(tmp) / "ref.wav",
                    reference_text="ref",
                    output_path=Path(tmp) / "take.wav",
                ),
                engine=engine,
            )
        self.assertEqual(calls["synthesize"], 1)
        self.assertEqual(calls["close"], 0)


@unittest.skipUnless(os.environ.get("TTS_LAB_E2_SMOKE") == "1", "GPU E2 smoke")
class SynthesisServiceGpu(unittest.TestCase):
    def test_known_good_e2_fixture_writes_riff_wav(self) -> None:
        from tts.lab.backend.services.breeze import SynthesisRequest, synthesize_e2
        from tts.wav import is_riff_wav, read_wav

        pin = _pin_root()
        ref = pin / "fixtures" / "ref.wav"
        transcript = (pin / "fixtures" / "ref.txt").read_text(encoding="utf-8").strip()
        self.assertTrue(ref.is_file(), f"missing pin fixture {ref}")
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "e2.wav"
            result = synthesize_e2(
                SynthesisRequest(
                    text=FIXTURE_TEXT,
                    steer=FIXTURE_STEER,
                    reference_audio=ref,
                    reference_text=transcript,
                    output_path=dest,
                )
            )
            self.assertTrue(is_riff_wav(dest))
            header = dest.read_bytes()[:12]
            self.assertEqual(header[:4], b"RIFF")
            self.assertEqual(header[8:12], b"WAVE")
            sr, samples = read_wav(dest)
            self.assertGreater(sr, 0)
            self.assertGreater(samples.size, 0)
            self.assertGreater(result.duration_s, 0)
            self.assertIsNone(result.runtime.get("dual_cfg_path"))


if __name__ == "__main__":
    unittest.main()
