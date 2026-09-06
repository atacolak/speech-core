#!/usr/bin/env python3
"""CPU tests for lab/scripts/breeze_tts_qual. not on the voicecat path."""

from __future__ import annotations

import sys
import json
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
            if cfg.name.startswith("E"):
                continue
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


class EngineContract(unittest.TestCase):
    def test_synthesize_yields_incremental_pcm(self) -> None:
        from breeze_tts_qual.configs import CONFIGS
        from breeze_tts_qual.engine import BreezeEngine, PcmChunk, FakeBackend
        from breeze_tts_qual.metrics import first_nonsilent_s

        cfg = next(c for c in CONFIGS if c.name == "A")
        engine = BreezeEngine(cfg, ckpt_dir=".", backend=FakeBackend())
        chunks = list(
            engine.synthesize(
                text="yeah.",
                reference_audio="ref.wav",
                reference_text="ref",
            )
        )
        self.assertGreaterEqual(len(chunks), 2)
        self.assertTrue(all(isinstance(c, PcmChunk) for c in chunks))
        self.assertTrue(chunks[-1].is_final)
        self.assertFalse(chunks[0].is_final)
        self.assertGreater(chunks[0].n_samples, 0)
        first_bytes = next(c.t_rel_s for c in chunks if c.n_samples > 0)
        t_ns, _ = first_nonsilent_s(
            [(c.t_rel_s, c.pcm) for c in chunks],
            sample_rate=chunks[0].sample_rate,
        )
        self.assertIsNotNone(t_ns)
        self.assertGreaterEqual(t_ns, first_bytes)
        engine.close()


class ConfigTableE(unittest.TestCase):
    def test_e_ladder_exists(self) -> None:
        from breeze_tts_qual.configs import CONFIGS, EngineConfig

        names = {c.name for c in CONFIGS}
        self.assertGreaterEqual(names, {"E1", "E2", "E3", "E4", "E5"})
        by = {c.name: c for c in CONFIGS}
        for name in ("E1", "E2", "E3", "E4", "E5"):
            self.assertEqual(by[name].precision, "bf16")
            self.assertIsInstance(by[name], EngineConfig)

        self.assertTrue(by["E1"].fast_depth_decoder)
        self.assertFalse(by["E1"].fast_codec)
        self.assertFalse(by["E1"].fast_backbone_decode)
        self.assertFalse(by["E1"].fast_backbone_prefill)
        self.assertFalse(by["E1"].fast_text_encoder)

        self.assertTrue(by["E2"].fast_depth_decoder)
        self.assertTrue(by["E2"].fast_codec)
        self.assertFalse(by["E2"].fast_backbone_decode)
        self.assertFalse(by["E2"].fast_text_encoder)

        self.assertTrue(by["E3"].fast_backbone_decode)
        self.assertFalse(by["E3"].fast_backbone_prefill)

        self.assertTrue(by["E4"].fast_backbone_prefill)
        self.assertFalse(by["E4"].fast_text_encoder)

        self.assertTrue(by["E5"].fast_depth_decoder)
        self.assertTrue(by["E5"].fast_codec)
        self.assertTrue(by["E5"].fast_backbone_decode)
        self.assertTrue(by["E5"].fast_backbone_prefill)
        self.assertTrue(by["E5"].fast_text_encoder)

        for cfg in CONFIGS:
            if cfg.name.startswith("E"):
                continue
            self.assertFalse(
                cfg.fast_text_encoder,
                f"{cfg.name} must not enable text-encoder graphs",
            )


class Protocol(unittest.TestCase):
    def test_stop_adding_graphs(self) -> None:
        from breeze_tts_qual.protocol import (
            should_stop_adding_graphs,
            would_exceed_hard_ceiling,
            UNSAFE_PEAK_GIB,
        )

        self.assertAlmostEqual(UNSAFE_PEAK_GIB, 9.0)
        prev = {"p50_ttfa_s": 0.20, "gap_p95_s": 0.05, "startup_s": 10.0}
        unsafe = dict(prev, p50_ttfa_s=0.18, peak_allocated_gib=9.1)
        stop, reason = should_stop_adding_graphs(prev, unsafe)
        self.assertTrue(stop)
        self.assertEqual(reason, "unsafe_vram")
        no_win = dict(prev, p50_ttfa_s=0.21, peak_allocated_gib=8.0)
        stop, reason = should_stop_adding_graphs(prev, no_win)
        self.assertTrue(stop)
        self.assertEqual(reason, "latency_no_improve")
        jitter = dict(prev, p50_ttfa_s=0.10, gap_p95_s=0.06, peak_allocated_gib=8.0)
        stop, reason = should_stop_adding_graphs(prev, jitter)
        self.assertTrue(stop)
        self.assertEqual(reason, "jitter_worse")
        bad = dict(prev, p50_ttfa_s=0.10, peak_allocated_gib=8.0, correctness_changed=True)
        stop, reason = should_stop_adding_graphs(prev, bad)
        self.assertTrue(stop)
        self.assertEqual(reason, "correctness_changed")
        init = dict(prev, p50_ttfa_s=0.10, peak_allocated_gib=8.0, init_unreasonable=True)
        stop, reason = should_stop_adding_graphs(prev, init)
        self.assertTrue(stop)
        self.assertEqual(reason, "init_unreasonable")
        ok = dict(prev, p50_ttfa_s=0.10, gap_p95_s=0.04, peak_allocated_gib=8.0)
        stop, reason = should_stop_adding_graphs(prev, ok)
        self.assertFalse(stop)
        self.assertIsNone(reason)
        at_ceiling = dict(prev, p50_ttfa_s=0.10, gap_p95_s=0.04, peak_allocated_gib=9.0)
        stop, reason = should_stop_adding_graphs(prev, at_ceiling)
        self.assertFalse(stop)
        self.assertIsNone(reason)
        self.assertTrue(would_exceed_hard_ceiling(9.1))
        self.assertFalse(would_exceed_hard_ceiling(9.0))

    def test_utterance_classes_and_run_counts(self) -> None:
        from breeze_tts_qual.protocol import measured_plan
        from breeze_tts_qual.utterances import DIRECTIONS, LONG, MEDIUM, SHORT, TINY

        self.assertEqual(TINY, ("yeah.", "got it.", "one second."))
        self.assertEqual(
            SHORT,
            ("I found the issue. The worker is holding the old session open.",),
        )
        self.assertGreaterEqual(len(MEDIUM[0].split()), 40)
        self.assertLessEqual(len(MEDIUM[0].split()), 70)
        self.assertGreaterEqual(len(LONG[0].split()), 150)
        self.assertLessEqual(len(LONG[0].split()), 250)
        self.assertEqual(
            list(DIRECTIONS),
            [
                "calm and matter-of-fact",
                "slightly amused",
                "urgent but controlled",
                "quiet / thoughtful",
            ],
        )
        plan = measured_plan()
        self.assertEqual(plan["tiny"], 30)
        self.assertEqual(plan["short"], 30)
        self.assertEqual(plan["medium"], 3)
        self.assertEqual(plan["long"], 3)
        self.assertEqual(plan["warmup"], 3)


class Int8Convrot(unittest.TestCase):
    def test_replace_named_linear(self) -> None:
        import torch
        from torch import nn
        from breeze_tts_qual.int8_convrot import (
            ConvRotInt8Linear,
            QuantLayerInfo,
            replace_quantized_linears,
        )

        class Toy(nn.Module):
            def __init__(self) -> None:
                super().__init__()
                self.backbone = nn.Linear(16, 8, bias=True)
                self.depth = nn.Linear(16, 8, bias=True)

        model = Toy()
        qmap = {
            "backbone": QuantLayerInfo(prefix="backbone", group_size=16, in_features=16, out_features=8, has_bias=True)
        }
        replaced = replace_quantized_linears(model, qmap)
        self.assertEqual(replaced, ["backbone"])
        self.assertIsInstance(model.backbone, ConvRotInt8Linear)
        self.assertIsInstance(model.depth, nn.Linear)

    def test_missing_target_raises(self) -> None:
        import torch
        from torch import nn
        from breeze_tts_qual.int8_convrot import QuantLayerInfo, replace_quantized_linears

        class Toy(nn.Module):
            def __init__(self) -> None:
                super().__init__()
                self.other = nn.Linear(16, 8)

        with self.assertRaises(RuntimeError):
            replace_quantized_linears(
                Toy(),
                {"backbone": QuantLayerInfo(prefix="backbone", group_size=16, in_features=16, out_features=8)},
            )

    def test_no_comfyui_import(self) -> None:
        text = (ROOT / "int8_convrot.py").read_text(encoding="utf-8")
        self.assertNotIn("import comfy\n", text)
        self.assertNotIn("from comfy", text)
        self.assertNotIn("nodes.py", text)


class Report(unittest.TestCase):
    def test_compact_table_and_recommendation(self) -> None:
        from breeze_tts_qual.report import RECOMMENDATIONS, render_report, sanitize_path

        self.assertEqual(
            set(RECOMMENDATIONS),
            {"SHELF", "PROMISING — NEEDS ONE MORE EXPERIMENT", "REJECT"},
        )
        rows = [
            {
                "configuration": "A",
                "peak_vram_gib": 7.7,
                "p50_ttfa_s": 0.30,
                "p95_ttfa_s": 0.40,
                "rtf": 0.8,
                "stream_jitter_p95_s": 0.02,
                "quality_notes": "baseline",
            }
        ]
        md, payload = render_report(
            rows=rows,
            recommendation="REJECT",
            bead="sc-breeze-hybrid-81p",
            qual_root="/home/sf/.cache/speech-out/breeze-tts-qual-sc-breeze-hybrid-81p",
        )
        self.assertIn("configuration", md)
        self.assertIn("peak VRAM", md)
        self.assertIn("p50 TTFA", md)
        self.assertIn("p95 TTFA", md)
        self.assertIn("RTF", md)
        self.assertIn("stream jitter", md)
        self.assertIn("quality notes", md)
        self.assertIn("REJECT", md)
        self.assertEqual(payload["recommendation"], "REJECT")
        self.assertNotIn("/home/sf", md)
        self.assertNotIn("/home/sf", json.dumps(payload))
        self.assertTrue(sanitize_path("/home/sf/.cache/x").startswith("~") or "$HOME" in sanitize_path("/home/sf/.cache/x"))

    def test_invalid_recommendation_rejected(self) -> None:
        from breeze_tts_qual.report import render_report

        with self.assertRaises(ValueError):
            render_report(rows=[], recommendation="MAYBE", bead="sc-breeze-hybrid-81p", qual_root="~")


if __name__ == "__main__":
    unittest.main()
