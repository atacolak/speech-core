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

    def test_choose_recommendation_shelf_hybrid_and_e(self) -> None:
        from breeze_tts_qual.report import choose_recommendation

        rows = [
            {"configuration": "A", "p50_ttfa_s": 0.40, "rtf": 0.8, "peak_vram_gib": 7.7},
            {"configuration": "C1", "p50_ttfa_s": 0.20, "rtf": 0.6, "peak_vram_gib": 8.5},
            {"configuration": "B_winner", "p50_ttfa_s": 0.22, "rtf": 0.7, "peak_vram_gib": 9.0},
        ]
        self.assertEqual(choose_recommendation(rows, quality_ok=True), "SHELF")
        rows_e = [
            {"configuration": "A", "p50_ttfa_s": 0.40, "rtf": 0.8, "peak_vram_gib": 7.7},
            {"configuration": "C1", "p50_ttfa_s": 0.18, "rtf": 0.6, "peak_vram_gib": 9.1},
            {"configuration": "E1", "p50_ttfa_s": 0.19, "rtf": 0.65, "peak_vram_gib": 8.8},
        ]
        self.assertEqual(choose_recommendation(rows_e, quality_ok=True), "SHELF")
        rows_over = [
            {"configuration": "A", "p50_ttfa_s": 0.40, "rtf": 0.8, "peak_vram_gib": 7.7},
            {"configuration": "C1", "p50_ttfa_s": 0.18, "rtf": 0.6, "peak_vram_gib": 9.1},
        ]
        self.assertNotEqual(choose_recommendation(rows_over, quality_ok=True), "SHELF")

    def test_choose_recommendation_promising_and_reject(self) -> None:
        from breeze_tts_qual.report import choose_recommendation

        promising = [
            {"configuration": "A", "p50_ttfa_s": 0.40, "rtf": 0.8, "peak_vram_gib": 7.7},
            {"configuration": "C1", "p50_ttfa_s": 0.30, "rtf": 0.6, "peak_vram_gib": 8.5},
        ]
        self.assertEqual(
            choose_recommendation(promising, quality_ok=True),
            "PROMISING — NEEDS ONE MORE EXPERIMENT",
        )
        self.assertEqual(choose_recommendation(promising, quality_ok=False), "REJECT")

    def test_report_only_writes_compact_artifacts(self) -> None:
        import tempfile
        from breeze_tts_qual import run_benchmark

        metrics = {
            "run_id": "full-30",
            "qual_root": "/home/sf/.cache/speech-out/breeze-tts-qual-sc-breeze-hybrid-81p",
            "e_rejected_for_our_purposes": False,
            "direction_notes": {
                "C2": {"calm": "comparable", "amused": "comparable", "urgent": "comparable", "quiet": "comparable"},
                "E2": {"calm": "comparable", "amused": "comparable", "urgent": "comparable", "quiet": "comparable"},
            },
            "configs": {
                "A": {
                    "name": "A",
                    "p50_ttfa_s": 0.396,
                    "p95_ttfa_s": 0.406,
                    "rtf": 2.05,
                    "gap_p95_s": 0.178,
                    "peak_allocated_gib": 7.78,
                },
                "B_depth": {
                    "name": "B_depth",
                    "p50_ttfa_s": 0.193,
                    "p95_ttfa_s": 0.199,
                    "rtf": 0.78,
                    "gap_p95_s": -0.033,
                    "peak_allocated_gib": 8.02,
                },
                "B_codec": {
                    "name": "B_codec",
                    "p50_ttfa_s": 0.233,
                    "p95_ttfa_s": 0.244,
                    "rtf": 2.02,
                    "gap_p95_s": 0.087,
                    "peak_allocated_gib": 8.75,
                },
                "B_backbone_decode": {
                    "name": "B_backbone_decode",
                    "p50_ttfa_s": 0.384,
                    "p95_ttfa_s": 0.406,
                    "rtf": 1.87,
                    "gap_p95_s": 0.149,
                    "peak_allocated_gib": 8.05,
                },
                "B_backbone_prefill": {
                    "name": "B_backbone_prefill",
                    "not_run": True,
                    "stop_reason": "unsafe_vram",
                    "p50_ttfa_s": None,
                    "peak_allocated_gib": 8.04,
                },
                "B_winner": {
                    "name": "B_winner",
                    "p50_ttfa_s": 0.396,
                    "gap_p95_s": 0.178,
                    "peak_allocated_gib": 8.75,
                },
                "C0": {
                    "name": "C0",
                    "p50_ttfa_s": 0.879,
                    "p95_ttfa_s": 0.912,
                    "rtf": 4.43,
                    "gap_p95_s": 0.562,
                    "peak_allocated_gib": 7.41,
                },
                "C1": {
                    "name": "C1",
                    "p50_ttfa_s": 0.659,
                    "p95_ttfa_s": 0.677,
                    "rtf": 3.05,
                    "gap_p95_s": 0.328,
                    "peak_allocated_gib": 7.41,
                },
                "C2": {
                    "name": "C2",
                    "p50_ttfa_s": 0.422,
                    "p95_ttfa_s": 0.433,
                    "rtf": 2.99,
                    "gap_p95_s": 0.161,
                    "peak_allocated_gib": 7.42,
                },
                "C3": {
                    "name": "C3",
                    "stop_reason": "correctness_changed",
                    "traceback": "/home/sf/.cache/speech-out/breeze-tts-qual-sc-breeze-hybrid-81p/src/breeze-tts/boom",
                    "n": 0,
                },
                "C4": {
                    "name": "C4",
                    "not_run": True,
                    "stop_reason": "correctness_changed",
                    "p50_ttfa_s": None,
                },
                "E1": {
                    "name": "E1",
                    "p50_ttfa_s": 0.188,
                    "p95_ttfa_s": 0.203,
                    "rtf": 0.76,
                    "gap_p95_s": -0.031,
                    "peak_allocated_gib": 8.02,
                },
                "E2": {
                    "name": "E2",
                    "p50_ttfa_s": 0.139,
                    "p95_ttfa_s": 0.149,
                    "rtf": 0.79,
                    "gap_p95_s": -0.014,
                    "peak_allocated_gib": 8.77,
                },
                "E3": {"name": "E3", "not_run": True, "stop_reason": "jitter_worse", "p50_ttfa_s": None},
                "E4": {"name": "E4", "not_run": True, "stop_reason": "jitter_worse", "p50_ttfa_s": None},
                "E5": {"name": "E5", "not_run": True, "stop_reason": "jitter_worse", "p50_ttfa_s": None},
                "D": {
                    "name": "D",
                    "not_run": True,
                    "stop_reason": "d_killed",
                    "p50_ttfa_s": 2.54,
                    "rtf": 14.6,
                    "peak_allocated_gib": 7.18,
                },
                "E_winner": {
                    "name": "E2",
                    "p50_ttfa_s": 0.139,
                    "p95_ttfa_s": 0.149,
                    "rtf": 0.79,
                    "gap_p95_s": -0.014,
                    "peak_allocated_gib": 8.77,
                },
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            run_dir = tmp_path / "runs" / "full-30"
            run_dir.mkdir(parents=True)
            (run_dir / "metrics.json").write_text(json.dumps(metrics), encoding="utf-8")
            out_md = tmp_path / "out.md"
            out_json = tmp_path / "out.json"
            code = run_benchmark.main(
                [
                    "--report-only",
                    "--qual-root",
                    str(tmp_path),
                    "--run-id",
                    "full-30",
                    "--out-md",
                    str(out_md),
                    "--out-json",
                    str(out_json),
                ]
            )
            self.assertEqual(code, 0)
            md = out_md.read_text(encoding="utf-8")
            payload = json.loads(out_json.read_text(encoding="utf-8"))
        self.assertNotIn("/home/sf", md)
        self.assertNotIn("/home/sf", json.dumps(payload))
        self.assertIn("SHELF", md)
        self.assertEqual(payload["recommendation"], "SHELF")
        for name in (
            "A",
            "B_depth",
            "B_codec",
            "B_backbone_decode",
            "B_backbone_prefill",
            "B_winner",
            "C0",
            "C1",
            "C2",
            "C3",
            "C4",
            "E1",
            "E2",
            "E3",
            "E4",
            "E5",
            "D",
        ):
            self.assertIn(name, md)
        self.assertEqual(payload["best_e_le_9gib"], "E2")
        self.assertEqual(payload["best_hybrid_le_9gib"], "C2")
        self.assertFalse(payload["e_rejected_for_our_purposes"])
        self.assertIn("not permission to integrate", md)
        self.assertIn("~70 ms", md)
        self.assertIn("2.4 GB", md)
        self.assertTrue(
            payload.get("stage_trace", {}).get("trace_unavailable")
            or "trace_unavailable" in md
        )



class ParkLeftover(unittest.TestCase):
    def test_park_stops_only_ata_speech_tts(self) -> None:
        from unittest.mock import patch
        from breeze_tts_qual.park_leftover import UNIT, park, restore

        self.assertEqual(UNIT, "ata-speech-tts.service")
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            from types import SimpleNamespace
            if argv[-2:] == ["is-active", UNIT]:
                return SimpleNamespace(returncode=0, stdout="active\n", stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=fake_run):
            park()
            restore()
        self.assertEqual(
            calls,
            [
                ["systemctl", "--user", "is-active", UNIT],
                ["systemctl", "--user", "stop", UNIT],
                ["systemctl", "--user", "start", UNIT],
            ],
        )
        joined = " ".join(" ".join(c) for c in calls)
        self.assertNotIn("qwentts-tts-server.service", joined)
        self.assertNotIn("edit", joined)


class OfficialMapping(unittest.TestCase):
    def test_fast_flags_map(self) -> None:
        from breeze_tts_qual.configs import CONFIGS
        from breeze_tts_qual.engine import fast_streaming_kwargs

        a = next(c for c in CONFIGS if c.name == "A")
        kw = fast_streaming_kwargs(a)
        self.assertFalse(kw["fast_depth_decoder"])
        self.assertFalse(kw["fast_codec"])
        self.assertFalse(kw["fast_backbone_decode"])
        self.assertFalse(kw["fast_backbone_prefill"])
        self.assertFalse(kw.get("fast_text_encoder", False))
        self.assertFalse(kw.get("fast_all", False) not in (None, False) and kw.get("fast_all"))
        c2 = next(c for c in CONFIGS if c.name == "C2")
        kw2 = fast_streaming_kwargs(c2)
        self.assertTrue(kw2["fast_depth_decoder"])
        self.assertTrue(kw2["fast_codec"])
        self.assertFalse(kw2["fast_backbone_decode"])
        self.assertFalse(kw2.get("fast_text_encoder", False))
        e5 = next(c for c in CONFIGS if c.name == "E5")
        kw5 = fast_streaming_kwargs(e5)
        self.assertTrue(kw5["fast_text_encoder"])
        e1 = next(c for c in CONFIGS if c.name == "E1")
        self.assertFalse(fast_streaming_kwargs(e1).get("fast_text_encoder", False))

    def test_checkpoint_for_precision(self) -> None:
        from pathlib import Path
        from breeze_tts_qual.engine import checkpoint_for_precision

        root = Path("/tmp/qual")
        self.assertIn("int8-hybrid", str(checkpoint_for_precision(root, "hybrid_int8")))
        self.assertIn("int8-convrot", str(checkpoint_for_precision(root, "full_int8")))



class ComposeB(unittest.TestCase):
    def test_compose_independent_winners(self) -> None:
        from breeze_tts_qual.protocol import compose_b_winner

        arms = {
            "A": {"p50_ttfa_s": 0.30, "gap_p95_s": 0.05, "peak_allocated_gib": 7.7, "fast": []},
            "B_depth": {"p50_ttfa_s": 0.22, "gap_p95_s": 0.04, "peak_allocated_gib": 8.0, "fast": ["depth"]},
            "B_codec": {"p50_ttfa_s": 0.28, "gap_p95_s": 0.04, "peak_allocated_gib": 8.2, "fast": ["codec"]},
            "B_backbone_decode": {"p50_ttfa_s": 0.31, "gap_p95_s": 0.05, "peak_allocated_gib": 8.5, "fast": ["backbone_decode"]},
            "B_backbone_prefill": {"p50_ttfa_s": 0.29, "gap_p95_s": 0.06, "peak_allocated_gib": 8.1, "fast": ["backbone_prefill"]},
        }
        winner = compose_b_winner(arms)
        # depth improved; codec improved; backbone_decode did not; prefill jitter worse vs current
        self.assertIn("depth", winner["fast"])
        self.assertIn("codec", winner["fast"])
        self.assertNotIn("backbone_decode", winner["fast"])
        self.assertNotIn("backbone_prefill", winner["fast"])
        unsafe = {
            "A": {"p50_ttfa_s": 0.30, "gap_p95_s": 0.05, "peak_allocated_gib": 7.7, "fast": []},
            "B_depth": {"p50_ttfa_s": 0.10, "gap_p95_s": 0.04, "peak_allocated_gib": 9.1, "fast": ["depth"]},
            "B_codec": {"p50_ttfa_s": 0.28, "gap_p95_s": 0.04, "peak_allocated_gib": 8.2, "fast": ["codec"]},
            "B_backbone_decode": {"p50_ttfa_s": 0.31, "gap_p95_s": 0.05, "peak_allocated_gib": 8.5, "fast": ["backbone_decode"]},
            "B_backbone_prefill": {"p50_ttfa_s": 0.29, "gap_p95_s": 0.06, "peak_allocated_gib": 8.1, "fast": ["backbone_prefill"]},
        }
        ceiling = compose_b_winner(unsafe)
        self.assertNotIn("depth", ceiling["fast"])
        self.assertIn("codec", ceiling["fast"])
        self.assertLessEqual(ceiling["peak_allocated_gib"], 9.0)


class SmokeC0Crash(unittest.TestCase):
    def test_hybrid_crash_records_correctness_changed_without_bf16_fallback(self) -> None:
        import tempfile
        from unittest.mock import patch

        from breeze_tts_qual.configs import CONFIGS
        from breeze_tts_qual import run_benchmark

        c0 = next(cfg for cfg in CONFIGS if cfg.name == "C0")
        self.assertEqual(c0.precision, "hybrid_int8")

        class BoomBackend:
            def __init__(self, *args: object, **kwargs: object) -> None:
                raise RuntimeError("ConvRot kernel exploded")

            def close(self) -> None:
                return None

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ref = tmp_path / "ref.wav"
            ref.write_bytes(b"RIFF")
            try:
                with patch.object(run_benchmark, "OfficialBackend", BoomBackend):
                    row, code = run_benchmark._run_smoke_config(
                        config=c0,
                        qual_root=tmp_path,
                        run_dir=tmp_path,
                        ref_audio=ref,
                        ref_text="hello",
                    )
            except Exception as exc:  # noqa: BLE001 — RED: crash must be recorded, not raised
                self.fail(f"smoke must record crash, not raise: {exc}")

        self.assertEqual(row["precision"], "hybrid_int8")
        self.assertNotEqual(row["precision"], "bf16")
        self.assertEqual(row["backend"], "OfficialBackend")
        self.assertTrue(row["correctness_changed"])
        self.assertIn("ConvRot kernel exploded", row.get("traceback") or "")
        self.assertNotEqual(code, 0)



class CumulativeC(unittest.TestCase):
    def test_c_ladder_marks_overflowing_stage_and_later_not_run(self) -> None:
        import json
        import tempfile
        from unittest.mock import patch

        from breeze_tts_qual.configs import CONFIGS
        from breeze_tts_qual import run_benchmark

        for cfg in CONFIGS:
            if cfg.name.startswith("C"):
                self.assertFalse(cfg.fast_text_encoder, f"{cfg.name} must not enable fast_text_encoder")

        calls: list[str] = []

        def fake_measured(*, config, **_kwargs):
            calls.append(config.name)
            if config.name == "C0":
                return {
                    "name": "C0",
                    "precision": "hybrid_int8",
                    "backend": "OfficialBackend",
                    "fast": [],
                    "unsafe_vram": False,
                    "n": 5,
                    "p50_ttfa_s": 0.80,
                    "gap_p95_s": 0.10,
                    "peak_allocated_gib": 7.2,
                }, 0
            if config.name == "C1":
                return {
                    "name": "C1",
                    "precision": "hybrid_int8",
                    "backend": "OfficialBackend",
                    "fast": ["depth"],
                    "unsafe_vram": True,
                    "stop_reason": "unsafe_vram",
                    "n": 0,
                    "p50_ttfa_s": None,
                    "gap_p95_s": None,
                    "peak_allocated_gib": 9.2,
                }, 0
            self.fail(f"must not measure {config.name} after overflowing C1")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ref = tmp_path / "ref.wav"
            ref.write_bytes(b"RIFF")
            with patch.object(run_benchmark, "_run_measured_config", fake_measured):
                code = run_benchmark.main(
                    [
                        "--qual-root",
                        str(tmp_path),
                        "--configs",
                        "C0,C1,C2,C3,C4",
                        "--n",
                        "5",
                        "--run-id",
                        "c-incr",
                        "--ref-audio",
                        str(ref),
                        "--ref-text",
                        "hello",
                    ]
                )
            metrics_path = tmp_path / "runs" / "c-incr" / "metrics.json"
            self.assertTrue(metrics_path.is_file())
            payload = json.loads(metrics_path.read_text(encoding="utf-8"))

        self.assertEqual(code, 0)
        self.assertEqual(calls, ["C0", "C1"])
        for name in ("C0", "C1", "C2", "C3", "C4"):
            self.assertIn(name, payload["configs"], name)
        self.assertFalse(payload["configs"]["C0"].get("not_run"))
        for name in ("C1", "C2", "C3", "C4"):
            row = payload["configs"][name]
            self.assertTrue(row["not_run"], name)
            self.assertEqual(row["stop_reason"], "unsafe_vram")

    def test_c_ladder_stops_remaining_when_latency_does_not_improve(self) -> None:
        import json
        import tempfile
        from unittest.mock import patch

        from breeze_tts_qual import run_benchmark

        calls: list[str] = []

        def fake_measured(*, config, **_kwargs):
            calls.append(config.name)
            if config.name == "C0":
                return {
                    "name": "C0",
                    "precision": "hybrid_int8",
                    "backend": "OfficialBackend",
                    "fast": [],
                    "unsafe_vram": False,
                    "n": 5,
                    "p50_ttfa_s": 0.80,
                    "gap_p95_s": 0.10,
                    "peak_allocated_gib": 7.2,
                }, 0
            if config.name == "C1":
                return {
                    "name": "C1",
                    "precision": "hybrid_int8",
                    "backend": "OfficialBackend",
                    "fast": ["depth"],
                    "unsafe_vram": False,
                    "n": 5,
                    "p50_ttfa_s": 0.81,
                    "gap_p95_s": 0.09,
                    "peak_allocated_gib": 7.4,
                }, 0
            self.fail(f"must not measure {config.name} after latency stop")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ref = tmp_path / "ref.wav"
            ref.write_bytes(b"RIFF")
            with patch.object(run_benchmark, "_run_measured_config", fake_measured):
                run_benchmark.main(
                    [
                        "--qual-root",
                        str(tmp_path),
                        "--configs",
                        "C0,C1,C2,C3,C4",
                        "--n",
                        "5",
                        "--run-id",
                        "c-incr",
                        "--ref-audio",
                        str(ref),
                        "--ref-text",
                        "hello",
                    ]
                )
            payload = json.loads(
                (tmp_path / "runs" / "c-incr" / "metrics.json").read_text(encoding="utf-8")
            )

        self.assertEqual(calls, ["C0", "C1"])
        self.assertFalse(payload["configs"]["C1"].get("not_run"))
        self.assertEqual(payload["configs"]["C1"]["p50_ttfa_s"], 0.81)
        for name in ("C2", "C3", "C4"):
            row = payload["configs"][name]
            self.assertTrue(row["not_run"], name)
            self.assertEqual(row["stop_reason"], "latency_no_improve")


class VoiceCatWarmup(unittest.TestCase):
    def test_profile_uses_utterance_buckets_not_fast_json(self) -> None:
        import json
        from breeze_tts_qual.protocol import voicecat_warmup_profile_dict
        from breeze_tts_qual.utterances import LONG, MEDIUM, SHORT, TINY

        payload = voicecat_warmup_profile_dict()
        blob = json.dumps(payload)
        self.assertNotIn("fast.json", blob)
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(sorted(payload["service"]["cfg_scales"]), [1.0, 4.0])
        self.assertEqual(payload["service"]["concurrency"], 1)
        self.assertIn("yeah.", payload["warmup_request"]["text"])
        self.assertNotIn(LONG[0], blob)
        self.assertTrue(payload["stages"]["text_encoder"]["graphs"])
        self.assertTrue(payload["stages"]["backbone_prefill"]["graphs"])
        for graph in payload["stages"]["text_encoder"]["graphs"]:
            self.assertEqual(graph["token_length"] % 32, 0)
        for graph in payload["stages"]["backbone_prefill"]["graphs"]:
            self.assertEqual(graph["sequence_length"] % 32, 0)
        self.assertEqual(
            set(g["branch_batch_size"] for g in payload["stages"]["backbone_decode"]["graphs"]),
            {1, 2},
        )

    def test_e_warmup_parses_voicecat_profile_not_fast_json(self) -> None:
        import sys
        from types import ModuleType, SimpleNamespace
        from unittest.mock import MagicMock

        from breeze_tts_qual.configs import CONFIGS
        from breeze_tts_qual.engine import OfficialBackend

        e1 = next(c for c in CONFIGS if c.name == "E1")
        backend = OfficialBackend.__new__(OfficialBackend)
        backend.config = e1
        backend.qual_root = Path("/tmp/qual")
        runtime = MagicMock()
        runtime.codec_chunk_frames = 2
        captured: dict = {}

        warmup_mod = ModuleType("models.warmup_profile")

        def parse_warmup_profile(payload, *, source=None):
            captured["payload"] = payload
            captured["source"] = source
            return SimpleNamespace(name=payload["name"], source=source)

        def load_warmup_profile(path):
            captured["loaded"] = str(path)
            raise AssertionError(f"E warmup must not load {path}")

        warmup_mod.parse_warmup_profile = parse_warmup_profile
        warmup_mod.load_warmup_profile = load_warmup_profile
        models_mod = ModuleType("models")
        models_mod.warmup_profile = warmup_mod

        breeze_pkg = ModuleType("breeze_infer")
        templates_mod = ModuleType("breeze_infer.templates")
        templates_mod.prepare_inputs = lambda *args, **kwargs: None
        breeze_pkg.templates = templates_mod

        prev = {
            name: sys.modules.get(name)
            for name in (
                "models",
                "models.warmup_profile",
                "breeze_infer",
                "breeze_infer.templates",
            )
        }
        sys.modules["models"] = models_mod
        sys.modules["models.warmup_profile"] = warmup_mod
        sys.modules["breeze_infer"] = breeze_pkg
        sys.modules["breeze_infer.templates"] = templates_mod
        try:
            backend._maybe_warmup(runtime, breeze_src=MagicMock())
        finally:
            for name, module in prev.items():
                if module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module


        self.assertNotIn("loaded", captured)
        self.assertEqual(captured["source"], "voicecat")
        self.assertEqual(captured["payload"]["name"], "voicecat-tiny-short-medium")
        self.assertNotIn("fast.json", str(captured["payload"]))
        runtime.warmup_from_profile.assert_called_once()

    def test_attach_clone_reference_fills_ref_edit_fields(self) -> None:
        import tempfile

        from breeze_tts_qual.engine import attach_clone_reference

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixtures = root / "fixtures"
            fixtures.mkdir()
            (fixtures / "ref.wav").write_bytes(b"RIFF")
            (fixtures / "ref.txt").write_text("clone prompt\n", encoding="utf-8")
            out = attach_clone_reference({"text": "yeah.", "instruction": "Speak clearly and naturally."}, root)
        self.assertTrue(out["ref_audio_path"].endswith("ref.wav"))
        self.assertEqual(out["ref_text"], "clone prompt")
        self.assertEqual(out["text"], "yeah.")




class CumulativeE(unittest.TestCase):
    def test_e_ladder_marks_overflowing_stage_and_later_not_run(self) -> None:
        import json
        import tempfile
        from unittest.mock import patch

        from breeze_tts_qual import run_benchmark

        calls: list[str] = []

        def fake_measured(*, config, **_kwargs):
            calls.append(config.name)
            if config.name == "E1":
                return {
                    "name": "E1",
                    "precision": "bf16",
                    "backend": "OfficialBackend",
                    "fast": ["depth"],
                    "unsafe_vram": False,
                    "n": 5,
                    "p50_ttfa_s": 0.22,
                    "gap_p95_s": 0.04,
                    "peak_allocated_gib": 8.2,
                }, 0
            if config.name == "E2":
                return {
                    "name": "E2",
                    "precision": "bf16",
                    "backend": "OfficialBackend",
                    "fast": ["depth", "codec"],
                    "unsafe_vram": True,
                    "stop_reason": "unsafe_vram",
                    "n": 0,
                    "p50_ttfa_s": None,
                    "gap_p95_s": None,
                    "peak_allocated_gib": 9.3,
                }, 0
            self.fail(f"must not measure {config.name} after overflowing E2")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ref = tmp_path / "ref.wav"
            ref.write_bytes(b"RIFF")
            with patch.object(run_benchmark, "_run_measured_config", fake_measured):
                code = run_benchmark.main(
                    [
                        "--qual-root",
                        str(tmp_path),
                        "--configs",
                        "E1,E2,E3,E4,E5",
                        "--n",
                        "5",
                        "--run-id",
                        "e-incr",
                        "--ref-audio",
                        str(ref),
                        "--ref-text",
                        "hello",
                    ]
                )
            payload = json.loads(
                (tmp_path / "runs" / "e-incr" / "metrics.json").read_text(encoding="utf-8")
            )

        self.assertEqual(code, 0)
        self.assertEqual(calls, ["E1", "E2"])
        for name in ("E1", "E2", "E3", "E4", "E5"):
            self.assertIn(name, payload["configs"], name)
        self.assertFalse(payload["configs"]["E1"].get("not_run"))
        for name in ("E2", "E3", "E4", "E5"):
            row = payload["configs"][name]
            self.assertTrue(row["not_run"], name)
            self.assertEqual(row["stop_reason"], "unsafe_vram")
        self.assertEqual(payload["configs"]["E_winner"]["name"], "E1")
        self.assertFalse(payload["e_rejected_for_our_purposes"])

    def test_e_rejected_when_no_in_envelope_row(self) -> None:
        import json
        import tempfile
        from unittest.mock import patch

        from breeze_tts_qual import run_benchmark

        def fake_measured(*, config, **_kwargs):
            if config.name != "E1":
                self.fail(f"must not measure {config.name} after overflowing E1")
            return {
                "name": "E1",
                "precision": "bf16",
                "backend": "OfficialBackend",
                "fast": ["depth"],
                "unsafe_vram": True,
                "stop_reason": "unsafe_vram",
                "n": 0,
                "p50_ttfa_s": None,
                "gap_p95_s": None,
                "peak_allocated_gib": 9.4,
            }, 0

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ref = tmp_path / "ref.wav"
            ref.write_bytes(b"RIFF")
            with patch.object(run_benchmark, "_run_measured_config", fake_measured):
                run_benchmark.main(
                    [
                        "--qual-root",
                        str(tmp_path),
                        "--configs",
                        "E1,E2,E3,E4,E5",
                        "--n",
                        "5",
                        "--run-id",
                        "e-incr",
                        "--ref-audio",
                        str(ref),
                        "--ref-text",
                        "hello",
                    ]
                )
            payload = json.loads(
                (tmp_path / "runs" / "e-incr" / "metrics.json").read_text(encoding="utf-8")
            )

        for name in ("E1", "E2", "E3", "E4", "E5"):
            row = payload["configs"][name]
            self.assertTrue(row["not_run"], name)
            self.assertEqual(row["stop_reason"], "unsafe_vram")
        self.assertNotIn("E_winner", payload["configs"])
        self.assertTrue(payload["e_rejected_for_our_purposes"])

class CheapD(unittest.TestCase):
    def test_cli_measures_full_int8_d_and_kills_when_not_faster_than_c0(self) -> None:
        import tempfile
        from unittest.mock import patch

        from breeze_tts_qual import run_benchmark

        calls: list[str] = []

        def fake_measured(*, config, **_kwargs):
            calls.append(config.name)
            self.assertEqual(config.precision, "full_int8")
            self.assertFalse(config.fast_depth_decoder)
            self.assertFalse(config.fast_codec)
            self.assertFalse(config.fast_backbone_decode)
            self.assertFalse(config.fast_backbone_prefill)
            self.assertFalse(config.fast_text_encoder)
            return {
                "name": "D",
                "precision": "full_int8",
                "backend": "OfficialBackend",
                "fast": [],
                "unsafe_vram": False,
                "n": 5,
                "p50_ttfa_s": 0.90,
                "gap_p95_s": 0.10,
                "peak_allocated_gib": 7.2,
            }, 0

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ref = tmp_path / "ref.wav"
            ref.write_bytes(b"RIFF")
            c_incr = tmp_path / "runs" / "c-incr"
            c_incr.mkdir(parents=True)
            (c_incr / "metrics.json").write_text(
                json.dumps({"configs": {"C0": {"p50_ttfa_s": 0.878}}}),
                encoding="utf-8",
            )
            with patch.object(run_benchmark, "_run_measured_config", fake_measured):
                code = run_benchmark.main(
                    [
                        "--qual-root",
                        str(tmp_path),
                        "--configs",
                        "D",
                        "--n",
                        "5",
                        "--run-id",
                        "smoke-D",
                        "--ref-audio",
                        str(ref),
                        "--ref-text",
                        "hello",
                    ]
                )
            metrics_path = tmp_path / "runs" / "smoke-D" / "metrics.json"
            self.assertTrue(metrics_path.is_file())
            payload = json.loads(metrics_path.read_text(encoding="utf-8"))

        self.assertEqual(code, 0)
        self.assertEqual(calls, ["D"])
        row = payload["configs"]["D"]
        self.assertEqual(row["precision"], "full_int8")
        self.assertFalse(row.get("fast_depth_decoder"))
        self.assertEqual(row.get("fast") or [], [])
        self.assertTrue(row["d_killed"])
        self.assertTrue(payload["d_killed"])
        self.assertNotIn("D1", payload["configs"])
        self.assertFalse(row.get("not_run"))

    def test_d_not_killed_when_faster_than_c0(self) -> None:
        import tempfile
        from unittest.mock import patch

        from breeze_tts_qual import run_benchmark

        def fake_measured(*, config, **_kwargs):
            return {
                "name": "D",
                "precision": "full_int8",
                "backend": "OfficialBackend",
                "fast": [],
                "unsafe_vram": False,
                "n": 5,
                "p50_ttfa_s": 0.80,
                "gap_p95_s": 0.10,
                "peak_allocated_gib": 7.2,
            }, 0

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ref = tmp_path / "ref.wav"
            ref.write_bytes(b"RIFF")
            c_incr = tmp_path / "runs" / "c-incr"
            c_incr.mkdir(parents=True)
            (c_incr / "metrics.json").write_text(
                json.dumps({"configs": {"C0": {"p50_ttfa_s": 0.878}}}),
                encoding="utf-8",
            )
            with patch.object(run_benchmark, "_run_measured_config", fake_measured):
                run_benchmark.main(
                    [
                        "--qual-root",
                        str(tmp_path),
                        "--configs",
                        "D",
                        "--n",
                        "5",
                        "--run-id",
                        "smoke-D",
                        "--ref-audio",
                        str(ref),
                        "--ref-text",
                        "hello",
                    ]
                )
            payload = json.loads(
                (tmp_path / "runs" / "smoke-D" / "metrics.json").read_text(encoding="utf-8")
            )

        self.assertFalse(payload["configs"]["D"]["d_killed"])
        self.assertFalse(payload["d_killed"])

    def test_d_killed_when_unsafe_vram(self) -> None:
        import tempfile
        from unittest.mock import patch

        from breeze_tts_qual import run_benchmark

        def fake_measured(*, config, **_kwargs):
            return {
                "name": "D",
                "precision": "full_int8",
                "backend": "OfficialBackend",
                "fast": [],
                "unsafe_vram": True,
                "stop_reason": "unsafe_vram",
                "n": 0,
                "p50_ttfa_s": None,
                "gap_p95_s": None,
                "peak_allocated_gib": 9.4,
            }, 0

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ref = tmp_path / "ref.wav"
            ref.write_bytes(b"RIFF")
            c_incr = tmp_path / "runs" / "c-incr"
            c_incr.mkdir(parents=True)
            (c_incr / "metrics.json").write_text(
                json.dumps({"configs": {"C0": {"p50_ttfa_s": 0.878}}}),
                encoding="utf-8",
            )
            with patch.object(run_benchmark, "_run_measured_config", fake_measured):
                run_benchmark.main(
                    [
                        "--qual-root",
                        str(tmp_path),
                        "--configs",
                        "D",
                        "--n",
                        "5",
                        "--run-id",
                        "smoke-D",
                        "--ref-audio",
                        str(ref),
                        "--ref-text",
                        "hello",
                    ]
                )
            payload = json.loads(
                (tmp_path / "runs" / "smoke-D" / "metrics.json").read_text(encoding="utf-8")
            )

        row = payload["configs"]["D"]
        self.assertTrue(row["unsafe_vram"])
        self.assertTrue(row["not_run"])
        self.assertTrue(row["d_killed"])
        self.assertTrue(payload["d_killed"])

    def test_d_crash_records_correctness_changed_without_bf16_fallback(self) -> None:
        import tempfile
        from unittest.mock import patch

        from breeze_tts_qual.configs import CONFIGS
        from breeze_tts_qual import run_benchmark

        d_cfg = next(cfg for cfg in CONFIGS if cfg.name == "D")
        self.assertEqual(d_cfg.precision, "full_int8")

        def boom(*, config, **_kwargs):
            raise RuntimeError("ConvRot kernel exploded")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ref = tmp_path / "ref.wav"
            ref.write_bytes(b"RIFF")
            with patch.object(run_benchmark, "_run_measured_config", boom):
                code = run_benchmark.main(
                    [
                        "--qual-root",
                        str(tmp_path),
                        "--configs",
                        "D",
                        "--n",
                        "5",
                        "--run-id",
                        "smoke-D",
                        "--ref-audio",
                        str(ref),
                        "--ref-text",
                        "hello",
                    ]
                )
            payload = json.loads(
                (tmp_path / "runs" / "smoke-D" / "metrics.json").read_text(encoding="utf-8")
            )

        row = payload["configs"]["D"]
        self.assertEqual(row["precision"], "full_int8")
        self.assertNotEqual(row["precision"], "bf16")
        self.assertNotEqual(row["precision"], "hybrid_int8")
        self.assertTrue(row["correctness_changed"])
        self.assertIn("ConvRot kernel exploded", row.get("traceback") or "")
        self.assertNotEqual(code, 0)


class FullProtocol(unittest.TestCase):
    def test_full_measured_config_records_thirty_tiny_and_short(self) -> None:
        import tempfile
        from unittest.mock import patch

        from breeze_tts_qual.configs import CONFIGS
        from breeze_tts_qual.engine import FakeBackend
        from breeze_tts_qual import run_benchmark

        class OfficialBackend(FakeBackend):
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

        cfg = next(c for c in CONFIGS if c.name == "A")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            run_dir = tmp_path / "runs" / "full-30"
            run_dir.mkdir(parents=True)
            ref = tmp_path / "ref.wav"
            ref.write_bytes(b"RIFF")
            with patch.object(run_benchmark, "OfficialBackend", OfficialBackend):
                row, code = run_benchmark._run_measured_config(
                    config=cfg,
                    qual_root=tmp_path,
                    run_dir=run_dir,
                    ref_audio=ref,
                    ref_text="hello",
                    n=5,
                    warmup=3,
                    full=True,
                )
            self.assertEqual(code, 0)
            self.assertEqual(row["classes"]["tiny"]["n_measured"], 30)
            self.assertEqual(row["classes"]["short"]["n_measured"], 30)
            self.assertEqual(row["classes"]["medium"]["n_measured"], 3)
            self.assertEqual(row["classes"]["long"]["n_measured"], 3)
            self.assertIn("peak_allocated_mb", row)
            self.assertIn("peak_reserved_mb", row)
            self.assertIn("gap_p50_s", row)
            self.assertIn("gap_p95_s", row)
            self.assertIn("max_stall_s", row)
            wavs = run_dir / "wavs"
            self.assertTrue((wavs / "A_tiny_0.wav").is_file())
            self.assertTrue((wavs / "A_short_0.wav").is_file())
            self.assertTrue((wavs / "A_medium_0.wav").is_file())
            self.assertTrue((wavs / "A_long_0.wav").is_file())

    def test_full_cli_expands_b_skips_killed_d_and_stopped_stages(self) -> None:
        import tempfile
        from unittest.mock import patch

        from breeze_tts_qual import run_benchmark

        calls: list[str] = []

        def fake_measured(*, config, **kwargs):
            self.assertTrue(kwargs.get("full"))
            calls.append(config.name)
            peak = 7.2
            p50 = 0.80
            gap = 0.10
            if config.name == "B_backbone_prefill":
                return {
                    "name": config.name,
                    "precision": config.precision,
                    "backend": "OfficialBackend",
                    "fast": ["backbone_prefill"],
                    "unsafe_vram": True,
                    "not_run": True,
                    "stop_reason": "unsafe_vram",
                    "n": 0,
                    "p50_ttfa_s": None,
                    "gap_p95_s": None,
                    "peak_allocated_gib": 8.0,
                    "peak_allocated_mb": 8000.0,
                    "peak_reserved_mb": 10000.0,
                    "error": "cuda_oom",
                }, 0
            if config.name == "C3":
                return {
                    "name": "C3",
                    "precision": "hybrid_int8",
                    "backend": "OfficialBackend",
                    "fast": ["depth", "codec", "backbone_decode"],
                    "unsafe_vram": False,
                    "n": 0,
                    "correctness_changed": True,
                    "stop_reason": "correctness_changed",
                    "p50_ttfa_s": None,
                    "gap_p95_s": None,
                }, 3
            if config.name.startswith("C"):
                p50 = {"C0": 0.80, "C1": 0.70, "C2": 0.50}[config.name]
                gap = {"C0": 0.10, "C1": 0.09, "C2": 0.08}[config.name]
            if config.name.startswith("E"):
                p50 = {"E1": 0.20, "E2": 0.13}[config.name]
                gap = {"E1": 0.04, "E2": 0.05}[config.name]
            classes = {
                "tiny": {"n_measured": 30, "gap_p50_s": 0.01, "gap_p95_s": 0.02, "max_stall_s": 0.03},
                "short": {"n_measured": 30, "gap_p50_s": 0.01, "gap_p95_s": 0.02, "max_stall_s": 0.03},
                "medium": {"n_measured": 3},
                "long": {"n_measured": 3},
            }
            return {
                "name": config.name,
                "precision": config.precision,
                "backend": "OfficialBackend",
                "fast": list(getattr(config, "name") and []),
                "unsafe_vram": False,
                "n": 30,
                "p50_ttfa_s": p50,
                "gap_p95_s": gap,
                "gap_p50_s": 0.01,
                "max_stall_s": 0.03,
                "peak_allocated_gib": peak,
                "peak_allocated_mb": 7200.0,
                "peak_reserved_mb": 7800.0,
                "classes": classes,
            }, 0

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ref = tmp_path / "ref.wav"
            ref.write_bytes(b"RIFF")
            c_incr = tmp_path / "runs" / "c-incr"
            c_incr.mkdir(parents=True)
            (c_incr / "metrics.json").write_text(
                json.dumps({"configs": {"C0": {"p50_ttfa_s": 0.878}}}),
                encoding="utf-8",
            )
            smoke_d = tmp_path / "runs" / "smoke-D"
            smoke_d.mkdir(parents=True)
            (smoke_d / "metrics.json").write_text(
                json.dumps(
                    {
                        "d_killed": True,
                        "configs": {
                            "D": {
                                "name": "D",
                                "p50_ttfa_s": 2.54,
                                "d_killed": True,
                                "c0_p50_ttfa_s": 0.878,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(run_benchmark, "_run_measured_config", fake_measured):
                code = run_benchmark.main(
                    [
                        "--qual-root",
                        str(tmp_path),
                        "--full",
                        "--run-id",
                        "full-30",
                        "--ref-audio",
                        str(ref),
                        "--ref-text",
                        "hello",
                    ]
                )
            payload = json.loads(
                (tmp_path / "runs" / "full-30" / "metrics.json").read_text(encoding="utf-8")
            )

        self.assertEqual(code, 3)
        self.assertIn("A", calls)
        self.assertIn("B_depth", calls)
        self.assertIn("B_codec", calls)
        self.assertIn("B_backbone_decode", calls)
        self.assertIn("B_backbone_prefill", calls)
        self.assertIn("C0", calls)
        self.assertIn("C3", calls)
        self.assertNotIn("C4", calls)
        self.assertIn("E1", calls)
        self.assertIn("E2", calls)
        self.assertNotIn("E3", calls)
        self.assertNotIn("E4", calls)
        self.assertNotIn("E5", calls)
        self.assertNotIn("D", calls)
        self.assertTrue(payload["configs"]["D"]["not_run"])
        self.assertTrue(payload["configs"]["D"]["d_killed"])
        self.assertNotEqual(
            payload["configs"]["D"].get("classes", {}).get("short", {}).get("n_measured"),
            30,
        )
        self.assertTrue(payload["configs"]["C4"]["not_run"])
        self.assertNotEqual(
            payload["configs"]["C4"].get("classes", {}).get("short", {}).get("n_measured"),
            30,
        )
        self.assertTrue(payload["configs"]["E3"]["not_run"])
        self.assertEqual(payload["configs"]["A"]["classes"]["short"]["n_measured"], 30)
        self.assertIn("B_winner", payload["configs"])
        self.assertIn("E_winner", payload["configs"])






class DualCfgEagerDtype(unittest.TestCase):
    def test_guard_casts_float_into_bf16_linear(self) -> None:
        try:
            import torch
            from torch import nn
        except ImportError:
            self.skipTest("torch missing")

        from breeze_tts_qual.engine import _eager_generate_dtype_guard

        layer = nn.Linear(4, 4, bias=False)
        layer.weight.data = layer.weight.data.to(torch.bfloat16)
        model = nn.Sequential(layer)
        float_in = torch.ones(2, 4, dtype=torch.float32)
        with self.assertRaises(RuntimeError):
            model(float_in)
        with _eager_generate_dtype_guard(model):
            out = model(float_in)
        self.assertEqual(out.dtype, torch.bfloat16)

    def test_guard_casts_bf16_into_fp32_linear(self) -> None:
        try:
            import torch
            from torch import nn
        except ImportError:
            self.skipTest("torch missing")

        from breeze_tts_qual.engine import _eager_generate_dtype_guard

        layer = nn.Linear(4, 4, bias=False)
        layer.weight.data = layer.weight.data.float()
        model = nn.Sequential(layer)
        bf16_in = torch.ones(2, 4, dtype=torch.bfloat16)
        with self.assertRaises(RuntimeError):
            model(bf16_in)
        with _eager_generate_dtype_guard(model):
            out = model(bf16_in)
        self.assertEqual(out.dtype, torch.float32)

    def test_guard_casts_direct_linear_and_addmm(self) -> None:
        try:
            import torch
            from torch import nn
        except ImportError:
            self.skipTest("torch missing")

        from breeze_tts_qual.engine import _eager_generate_dtype_guard

        weight = torch.ones(4, 4, dtype=torch.float32)
        bf16_in = torch.ones(2, 4, dtype=torch.bfloat16)
        with self.assertRaises(RuntimeError):
            torch.nn.functional.linear(bf16_in, weight)
        with _eager_generate_dtype_guard(nn.Identity()):
            out = torch.nn.functional.linear(bf16_in, weight)
        self.assertEqual(out.dtype, torch.float32)

        bias = torch.zeros(4, dtype=torch.float32)
        mat1 = torch.ones(2, 4, dtype=torch.bfloat16)
        mat2 = torch.ones(4, 4, dtype=torch.float32)
        with self.assertRaises(RuntimeError):
            torch.addmm(bias, mat1, mat2)
        with _eager_generate_dtype_guard(nn.Identity()):
            added = torch.addmm(bias, mat1, mat2)
        self.assertEqual(added.dtype, torch.float32)

    def test_force_eager_runs_compiled_fullgraph_without_recompile_limit(self) -> None:
        try:
            import torch
        except ImportError:
            self.skipTest("torch missing")

        from breeze_tts_qual.engine import _force_eager_compile

        compiled = torch.compile(lambda x: x + 1, fullgraph=True)
        with _force_eager_compile():
            for length in range(1, 12):
                out = compiled(torch.ones(length))
                self.assertEqual(tuple(out.shape), (length,))


if __name__ == "__main__":
    unittest.main()
