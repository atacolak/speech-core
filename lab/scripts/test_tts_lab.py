#!/usr/bin/env python3
"""CPU tests for tts laboratory artifacts. not on the voicecat path."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "lab" / "scripts"))


class WavAndCrop(unittest.TestCase):
    def test_write_is_riff_and_crop_region(self) -> None:
        from tts.wav import crop_seconds, is_riff_wav, read_wav, write_wav

        sr = 16000
        samples = np.linspace(-0.2, 0.2, sr, dtype=np.float32)
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "a.wav"
            write_wav(dest, sr, samples)
            self.assertTrue(is_riff_wav(dest))
            with dest.open("rb") as handle:
                self.assertEqual(handle.read(4), b"RIFF")
            got_sr, got = read_wav(dest)
            self.assertEqual(got_sr, sr)
            cropped = crop_seconds(sr, samples, 0.25, 0.75)
            self.assertEqual(cropped.size, sr // 2)
            whole = crop_seconds(sr, samples, 0, 0)
            self.assertEqual(whole.size, samples.size)
            with self.assertRaisesRegex(ValueError, "empty crop region"):
                crop_seconds(sr, samples, 0.8, 0.2)


class PacketsAndPronunciation(unittest.TestCase):
    def test_display_text_stays_canonical(self) -> None:
        from tts.packets import PronunciationOverride, UtterancePacket
        from tts.pronunciation import PronunciationResolver

        with tempfile.TemporaryDirectory() as tmp:
            resolver = PronunciationResolver(Path(tmp))
            packet = UtterancePacket(
                text="I need a minute amount of extra context.",
                steer="matter-of-fact",
                pronunciation_overrides=[
                    PronunciationOverride(
                        span="minute",
                        context="minute amount",
                        intended_reading="my-noot",
                        source="operator",
                    )
                ],
            )
            resolver.apply(packet)
            self.assertEqual(packet.display_text(), "I need a minute amount of extra context.")
            self.assertIn("my-noot", packet.engine_text())
            self.assertNotEqual(packet.display_text(), packet.engine_text())

    def test_harness_does_not_assume_ipa_brackets(self) -> None:
        from tts.pronunciation_harness import run_harness

        report = run_harness()
        self.assertEqual(report["default_adapter_strategy"], "respell-into-synthesis_text")
        self.assertFalse(report["cases"][0]["native_ipa_advertised"])
        minute = next(c for c in report["cases"] if c["id"] == "minute-detail")
        self.assertIn("my-noot", minute["arms"]["respell"]["synthesis_text"])
        self.assertEqual(minute["arms"]["ordinary"]["synthesis_text"], minute["text"])


class ProfilesAreDistinct(unittest.TestCase):
    def test_voice_and_delivery_are_separate(self) -> None:
        from tts.profiles import (
            DeliveryExemplar,
            DeliveryProfile,
            import_voice_from_audio,
            ProfileStore,
        )
        from tts.wav import write_wav

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = ProfileStore(root)
            wav = root / "src.wav"
            write_wav(wav, 16000, np.zeros(1600, dtype=np.float32))
            voice = import_voice_from_audio(
                store,
                audio_path=wav,
                transcript="hello from the reference",
                display_name="speaker-a",
            )
            persona = DeliveryProfile(name="dry-engineer")
            persona.exemplars.append(
                DeliveryExemplar(text="hello", steer="deadpan, slightly amused")
            )
            store.save_delivery(persona)
            self.assertTrue(voice.id.startswith("vp_"))
            self.assertTrue(persona.id.startswith("dp_"))
            self.assertNotEqual(voice.id, persona.id)
            loaded_v = store.load_voice(voice.id)
            loaded_d = store.load_delivery(persona.id)
            self.assertEqual(loaded_v.transcript, "hello from the reference")
            self.assertEqual(loaded_d.exemplars[0].steer, "deadpan, slightly amused")
            active = store.materialize_reference(loaded_v)
            self.assertTrue(active.is_file())


class StoreProvenance(unittest.TestCase):
    def test_run_and_library_roundtrip(self) -> None:
        from tts.packets import UtterancePacket
        from tts.store import ArtifactStore
        from tts.wav import is_riff_wav, write_wav

        with tempfile.TemporaryDirectory() as tmp:
            store = ArtifactStore(Path(tmp))
            wav = Path(tmp) / "out.wav"
            write_wav(wav, 24000, np.zeros(2400, dtype=np.float32))
            item = store.ingest_wav(wav, kind="generation", name="take", tags=["e2"])
            self.assertTrue(is_riff_wav(item.path))
            packet = UtterancePacket(text="hi", steer="dry")
            self.assertNotIn("conversation_id", packet.to_dict())
            path = store.write_run({"run_id": "run_x", "packet": packet.to_dict()})
            payload = json.loads(path.read_text())
            self.assertEqual(payload["run_id"], "run_x")
            self.assertFalse(hasattr(store, "append_conversation_turn"))
            self.assertFalse((Path(tmp) / "conversations").is_dir())


class StreamFmCache(unittest.TestCase):
    def test_cache_key_depends_on_content_and_config(self) -> None:
        from tts.preprocess import cache_key, compare_references, process_reference
        from tts.wav import write_wav

        a = cache_key(source_sha256="aaa")
        b = cache_key(source_sha256="bbb")
        c = cache_key(source_sha256="aaa", config={"x": 1})
        self.assertNotEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertEqual(a, cache_key(source_sha256="aaa"))
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "ref.wav"
            write_wav(wav, 16000, np.zeros(1600, dtype=np.float32))
            record = process_reference(wav, lab_root=Path(tmp))
            self.assertEqual(record["status"], "unavailable")
            cmp = compare_references(wav, lab_root=Path(tmp))
            self.assertIn("original", cmp["arms"])
            self.assertIn("stream.fm", cmp["arms"])


class DualCfgIsolation(unittest.TestCase):
    def test_generation_settings_mark_dual_experimental(self) -> None:
        from tts.generation import GenerationSettings, UPSTREAM_DEFAULTS

        settings = GenerationSettings()
        self.assertEqual(settings.cfg_scale, UPSTREAM_DEFAULTS["cfg_scale"])
        self.assertFalse(settings.sampling_differs_from_e2_warmup())
        dual = GenerationSettings(cfg_scale_ref=2.0, cfg_scale_ins=1.5)
        self.assertEqual(dual.cfg_mode, "dual_experimental")
        self.assertIn("cfg_scale_ref", dual.non_default())

    def test_selected_runtime_is_e2_only(self) -> None:
        from tts.breeze.runtime import E2_CONFIG, selected_config
        from tts.paths import SELECTED_RUNTIME

        self.assertEqual(SELECTED_RUNTIME, "E2")
        self.assertEqual(selected_config().name, "E2")
        self.assertTrue(E2_CONFIG.fast_depth_decoder)
        self.assertTrue(E2_CONFIG.fast_codec)
        self.assertFalse(E2_CONFIG.fast_backbone_decode)


class PlannerPersona(unittest.TestCase):
    def test_heuristic_planner_uses_persona_not_history(self) -> None:
        from tts.planner import build_planner_prompt, plan_utterance
        from tts.profiles import DeliveryProfile, DeliveryExemplar

        delivery = DeliveryProfile(name="dry")
        delivery.exemplars.append(
            DeliveryExemplar(text="old", steer="fast, dry, deadpan satisfaction")
        )
        prompt = build_planner_prompt(text="next line", delivery=delivery)
        self.assertNotIn("recent_delivery_history", prompt)
        self.assertNotIn("conversational_context", prompt)
        self.assertIn("deadpan", prompt)
        packet = plan_utterance(text="next line", delivery=delivery)
        self.assertEqual(packet.text, "next line")
        self.assertIn("deadpan", packet.steer)
        self.assertEqual(packet.provenance["planner"], "heuristic")
        self.assertNotIn("previous_delivery_packets", packet.provenance)

    def test_pronunciation_risk_flags_do_not_emit_backend_markup(self) -> None:
        from tts.planner import flag_pronunciation_risks, plan_utterance

        risks = flag_pronunciation_risks("TTFA in a minute amount")
        self.assertTrue(any(item.startswith("acronym-like:TTFA") for item in risks))
        self.assertIn("ambiguous:minute", risks)
        packet = plan_utterance(text="TTFA in a minute amount")
        self.assertNotIn("[", packet.steer)
        self.assertNotIn("/ˈ", packet.engine_text())


class EngineDualCfgForwarding(unittest.TestCase):
    def test_breeze_engine_forwards_dual_cfg_without_touching_fast_path(self) -> None:
        from breeze_tts_qual.configs import EngineConfig
        from breeze_tts_qual.engine import BreezeEngine, FakeBackend, PcmChunk
        from tts.breeze.runtime import synthesize
        from tts.generation import GenerationSettings
        from tts.packets import UtterancePacket

        captured: list[dict] = []

        class CaptureBackend(FakeBackend):
            def synthesize(self, **kwargs):
                captured.append(kwargs)
                yield from super().synthesize(**kwargs)

        engine = BreezeEngine(
            EngineConfig(name="E2", precision="bf16"),
            ckpt_dir=".",
            backend=CaptureBackend(),
        )
        settings = GenerationSettings(cfg_scale_ref=2.0, cfg_scale_ins=1.5, seed=9)
        packet = UtterancePacket(text="hello", steer="dry")
        chunks = list(
            synthesize(
                engine,
                packet=packet,
                reference_audio="/tmp/ref.wav",
                reference_text="ref",
                settings=settings,
            )
        )
        self.assertTrue(chunks)
        self.assertIsInstance(chunks[0], PcmChunk)
        self.assertEqual(captured[0]["cfg_scale_ref"], 2.0)
        self.assertEqual(captured[0]["cfg_scale_ins"], 1.5)
        self.assertEqual(captured[0]["text"], "hello")
        self.assertIsNotNone(captured[0]["generation_config"])



class ReferenceEdits(unittest.TestCase):
    def test_exclude_is_non_destructive_and_reversible(self) -> None:
        from tts.edits import EditSpec, apply_edits
        from tts.wav import write_wav, read_wav

        sr = 16000
        samples = np.concatenate(
            [
                np.full(sr, 0.2, dtype=np.float32),
                np.full(sr, -0.2, dtype=np.float32),
                np.full(sr, 0.4, dtype=np.float32),
            ]
        )
        spec = EditSpec().exclude(1.0, 2.0)
        out = apply_edits(sr, samples, spec)
        self.assertEqual(out.size, 2 * sr)
        self.assertGreater(float(out[:sr].mean()), 0.1)
        self.assertGreater(float(out[sr:].mean()), 0.3)
        undone = apply_edits(sr, samples, spec.undo())
        self.assertEqual(undone.size, samples.size)
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "orig.wav"
            write_wav(src, sr, samples)
            before = src.read_bytes()
            apply_edits(sr, samples, spec)
            self.assertEqual(src.read_bytes(), before)

    def test_keep_selection_and_empty_defaults(self) -> None:
        from tts.edits import EditSpec, apply_edits, spec_from_region

        sr = 8000
        samples = np.linspace(-0.3, 0.3, sr, dtype=np.float32)
        kept = apply_edits(sr, samples, EditSpec().keep(0.25, 0.75))
        self.assertEqual(kept.size, sr // 2)
        whole = apply_edits(sr, samples, spec_from_region(0, 0))
        self.assertEqual(whole.size, samples.size)
        with self.assertRaisesRegex(ValueError, "empty crop region"):
            EditSpec().exclude(0.8, 0.2)


class StreamFmNeverWritesBesideSource(unittest.TestCase):
    def test_process_stays_in_lab_cache_even_from_downloads(self) -> None:
        from tts.preprocess import cache_key, process_reference
        from tts.wav import write_wav

        a = cache_key(source_sha256="aaa")
        b = cache_key(source_sha256="aaa", edit_spec={"ops": [{"op": "exclude", "start_s": 1.0, "end_s": 2.0}]})
        self.assertNotEqual(a, b)
        with tempfile.TemporaryDirectory() as tmp:
            downloads = Path(tmp) / "Downloads"
            downloads.mkdir()
            wav = downloads / "foo.wav"
            write_wav(wav, 16000, np.zeros(1600, dtype=np.float32))
            lab = Path(tmp) / "lab"
            record = process_reference(wav, lab_root=lab)
            self.assertEqual(record["status"], "unavailable")
            self.assertTrue(str(record["output"]).startswith(str(lab)))
            leftovers = [p.name for p in downloads.iterdir() if "streamfm" in p.name.lower() or p.suffix == ".json"]
            self.assertEqual(leftovers, [])
            self.assertIn("edit_spec", record)


class WorkbenchTranscriptStale(unittest.TestCase):
    def test_exclude_marks_transcript_stale_until_edited(self) -> None:
        from tts.reference import ReferenceWorkbench
        from tts.wav import write_wav

        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "ref.wav"
            write_wav(wav, 16000, np.zeros(48000, dtype=np.float32))
            bench = ReferenceWorkbench(Path(tmp))
            bench.load_source(wav, "hello from the original take")
            snap = bench.exclude(0.5, 1.5)
            self.assertEqual(snap["transcript_status"], "stale")
            bench.set_transcripts(effective="hello from the remaining take")
            self.assertEqual(bench.snapshot()["transcript_status"], "edited")
            self.assertTrue(Path(snap["effective_path"]).is_file())
            self.assertEqual(wav.read_bytes(), Path(wav).read_bytes())


if __name__ == "__main__":
    unittest.main()
