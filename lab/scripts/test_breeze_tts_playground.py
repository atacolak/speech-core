#!/usr/bin/env python3
"""CPU tests for lab Gradio playground. not on the voicecat path."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent / "breeze_tts_qual"
REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT))


def _labels(demo) -> dict:
    return {
        getattr(c, "label", None): c
        for c in demo.blocks.values()
        if getattr(c, "label", None)
    }


def _buttons(demo) -> list[str]:
    return [
        str(getattr(b, "value", ""))
        for b in demo.blocks.values()
        if type(b).__name__ == "Button"
    ]


def _tab_labels(demo) -> list[str]:
    out = []
    for component in demo.blocks.values():
        if type(component).__name__ in {"Tab", "TabItem"}:
            out.append(str(getattr(component, "label", "") or ""))
    return out


class PlaygroundCli(unittest.TestCase):
    def test_parse_args_defaults_localhost_and_dry_run(self) -> None:
        from breeze_tts_qual.playground import parse_args

        args = parse_args(["--dry-run"])
        self.assertTrue(args.dry_run)
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, 7860)

    def test_dry_run_constructs_blocks_without_parking_or_loading(self) -> None:
        from breeze_tts_qual.playground import main

        with (
            patch("breeze_tts_qual.park_leftover.park") as park,
            patch("breeze_tts_qual.park_leftover.restore") as restore,
            patch("breeze_tts_qual.engine.OfficialBackend") as backend,
            patch("breeze_tts_qual.engine.BreezeEngine") as engine,
        ):
            code = main(["--dry-run"])
        self.assertEqual(code, 0)
        park.assert_not_called()
        restore.assert_not_called()
        backend.assert_not_called()
        engine.assert_not_called()

    def test_blocks_are_e2_only_no_legacy_preset_radio(self) -> None:
        from breeze_tts_qual.playground import build_interface
        from tts.paths import SELECTED_RUNTIME

        demo = build_interface(Path.home())
        radios = [c for c in demo.blocks.values() if type(c).__name__ == "Radio"]
        radio_values = []
        for radio in radios:
            for choice in radio.choices:
                if isinstance(choice, (tuple, list)) and len(choice) >= 2:
                    radio_values.append(str(choice[1]))
                else:
                    radio_values.append(str(choice))
        for name in ("A", "B_depth", "C2", "C3", "E1", "E3"):
            self.assertNotIn(name, radio_values)
        markdown = " ".join(
            str(getattr(c, "value", ""))
            for c in demo.blocks.values()
            if type(c).__name__ == "Markdown"
        )
        self.assertIn(SELECTED_RUNTIME, markdown)
        self.assertIn("selected", markdown.lower())

    def test_blocks_expose_text_steer_cfg_seed_clone(self) -> None:
        from breeze_tts_qual.playground import build_interface
        from tts.planner import load_steer_fixtures

        with tempfile.TemporaryDirectory() as tmp:
            qual_root = Path(tmp)
            fixtures = qual_root / "fixtures"
            fixtures.mkdir()
            ref_wav = fixtures / "ref.wav"
            ref_txt = fixtures / "ref.txt"
            ref_wav.write_bytes(b"RIFF")
            ref_txt.write_text("clone speaker text\n", encoding="utf-8")
            demo = build_interface(qual_root)
            by_label = _labels(demo)
            self.assertIn("Text", by_label)
            fixtures_steers = load_steer_fixtures()
            self.assertEqual(by_label["Text"].value, fixtures_steers[0]["text"])
            self.assertIn("Steer (natural-language direction)", by_label)
            self.assertEqual(
                by_label["Steer (natural-language direction)"].value,
                fixtures_steers[0]["steer"],
            )
            self.assertNotIn("How it speaks", by_label)
            self.assertIn("cfg_scale", by_label)
            slider = by_label["cfg_scale"]
            self.assertEqual(slider.value, 1.0)
            self.assertEqual(slider.minimum, 1.0)
            self.assertEqual(slider.maximum, 8.0)
            self.assertEqual(slider.step, 0.5)
            info = str(slider.info or "")
            self.assertIn("1.0", info)
            self.assertIn("seed", by_label)
            self.assertEqual(by_label["seed"].value, 42)
            self.assertIn("Original reference", by_label)
            self.assertIn("Effective transcript", by_label)
            clone_val = by_label["Original reference"].value
            if isinstance(clone_val, dict):
                self.assertTrue(str(clone_val.get("path", "")).endswith("ref.wav"))
            else:
                self.assertEqual(Path(clone_val).name, "ref.wav")
            self.assertEqual(by_label["Effective transcript"].value, "clone speaker text")
            self.assertIn("reference conditioning", by_label)
            self.assertIn("instruction conditioning", by_label)
            self.assertIn("stream.fm cleanup", by_label)
            self.assertNotIn("Conversational context", by_label)
            self.assertNotIn("Delivery history", by_label)
            buttons = _buttons(demo)
            self.assertTrue(any(v == "Generate steer" for v in buttons), buttons)
            self.assertFalse(any("history" in v.lower() for v in buttons), buttons)
            self.assertFalse(any("New conversation" in v for v in buttons), buttons)
            tabs = _tab_labels(demo)
            for name in ("Synthesize", "Reference lab", "Experiments", "Library"):
                self.assertTrue(any(name == tab for tab in tabs), tabs)

    def test_blocks_have_generate_status_and_experimental_accordions(self) -> None:
        from breeze_tts_qual.playground import build_interface

        demo = build_interface(Path.home())
        by_label = _labels(demo)
        kinds = {type(c).__name__ for c in demo.blocks.values()}
        self.assertIn("Audio", kinds)
        buttons = _buttons(demo)
        self.assertTrue(any(v == "Generate" for v in buttons), buttons)
        self.assertTrue(any(v == "Use selection" for v in buttons), buttons)
        self.assertTrue(any(v == "Exclude selection" for v in buttons), buttons)
        self.assertIn("Status", by_label)
        self.assertIn("Inspect provenance", {getattr(a, "label", None) for a in demo.blocks.values()})
        accs = [c for c in demo.blocks.values() if type(c).__name__ == "Accordion"]
        titles = " ".join(str(getattr(a, "label", "")) for a in accs).lower()
        self.assertIn("dual-cfg", titles)
        self.assertIn("inspect provenance", titles)
        self.assertNotIn("killed", titles)
        self.assertNotIn("unsafe", titles)
        self.assertNotIn("conversation", titles)


class PlaygroundSessionTests(unittest.TestCase):
    def test_start_parks_before_load_and_close_restores(self) -> None:
        from tts.playground.app import PlaygroundSession

        order: list[str] = []

        class FakeEngine:
            def __init__(self) -> None:
                order.append("load")

            def close(self) -> None:
                order.append("engine.close")

        def factory(config, **kwargs):
            order.append(f"factory:{config.name}")
            return FakeEngine()

        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch(
                    "tts.playground.app.park_leftover.park",
                    side_effect=lambda: order.append("park"),
                ) as park,
                patch(
                    "tts.playground.app.park_leftover.restore",
                    side_effect=lambda: order.append("restore"),
                ) as restore,
            ):
                session = PlaygroundSession(
                    Path("."), engine_factory=factory, lab=Path(tmp)
                )
                session.start()
                self.assertEqual(session.loaded_preset, "E2")
                session.close()

        self.assertEqual(order[0], "park")
        self.assertIn("factory:E2", order)
        self.assertLess(order.index("park"), order.index("factory:E2"))
        self.assertEqual(order[-1], "restore")
        self.assertLess(order.index("engine.close"), order.index("restore"))
        park.assert_called_once()
        restore.assert_called_once()

    def test_close_restores_even_if_load_failed(self) -> None:
        from tts.playground.app import PlaygroundSession

        def factory(config, **kwargs):
            raise RuntimeError("no 3B")

        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch("tts.playground.app.park_leftover.park") as park,
                patch("tts.playground.app.park_leftover.restore") as restore,
            ):
                session = PlaygroundSession(
                    Path("."), engine_factory=factory, lab=Path(tmp)
                )
                with self.assertRaisesRegex(RuntimeError, "no 3B"):
                    session.start()
                session.close()
        park.assert_called_once()
        restore.assert_called_once()
        self.assertIsNone(session.engine)

    def test_start_rejects_legacy_presets(self) -> None:
        from tts.playground.app import PlaygroundSession

        with tempfile.TemporaryDirectory() as tmp:
            session = PlaygroundSession(
                Path("."), engine_factory=lambda *a, **k: None, lab=Path(tmp)
            )
            with self.assertRaisesRegex(ValueError, "E2"):
                session.start("A")
            self.assertFalse(hasattr(session, "ensure_preset"))
            self.assertFalse(hasattr(session, "resolve_preset"))

    def test_generate_is_sequential_and_forwards_controls(self) -> None:
        from breeze_tts_qual.engine import PcmChunk
        from tts.playground.app import PlaygroundSession
        from tts.wav import is_riff_wav, write_wav
        import numpy as np

        calls: list[dict] = []
        silent = (b"\x00\x00") * 200
        audible = (b"\xe8\x03") * 200

        class FakeEngine:
            sample_rate = 24000

            def synthesize(self, **kwargs):
                calls.append(kwargs)
                yield PcmChunk(silent, 24000, 200, False, 0.050, {"first_pcm": 0.04})
                yield PcmChunk(audible, 24000, 200, True, 0.120, {})

            def close(self) -> None:
                return None

        def factory(config, **kwargs):
            return FakeEngine()

        with tempfile.TemporaryDirectory() as tmp:
            ref = Path(tmp) / "ref.wav"
            write_wav(ref, 16000, np.zeros(1600, dtype=np.float32))
            with (
                patch("tts.playground.app.park_leftover.park"),
                patch("tts.playground.app.park_leftover.restore"),
            ):
                session = PlaygroundSession(
                    Path("."), engine_factory=factory, lab=Path(tmp)
                )
                session.start("E2")
                audio, status, run = session.generate(
                    text="hello there",
                    instruction="slightly amused",
                    cfg_scale=4.0,
                    seed=7,
                    clone_audio=str(ref),
                    clone_text="clone speaker",
                    start_s=0,
                    end_s=0,
                )
                session.close()
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["text"], "hello there")
            self.assertEqual(calls[0]["instruction"], "slightly amused")
            self.assertEqual(calls[0]["cfg_scale"], 4.0)
            self.assertEqual(calls[0]["seed"], 7)
            self.assertEqual(calls[0]["reference_text"], "clone speaker")
            self.assertIsInstance(audio, tuple)
            self.assertEqual(audio[0], 24000)
            self.assertIn("E2", status)
            self.assertNotIn("empty crop", status)
            self.assertIn("first_pcm", status)
            self.assertIn("first_nonsilent", status)
            self.assertIn("run_id", run)
            self.assertNotIn("conversation_id", run)
            self.assertNotIn("conversation_id", run.get("packet") or {})
            self.assertEqual((run.get("reference") or {}).get("variant"), "original")
            self.assertTrue(hasattr(session, "_lock"))
            self.assertFalse(hasattr(session, "conversation_id"))
            self.assertTrue(is_riff_wav(Path(run["output_audio"]["path"])))

    def test_launch_kwargs_bind_localhost_without_share(self) -> None:
        from breeze_tts_qual.playground import launch_kwargs
        from tts.paths import lab_root, qual_root

        kwargs = launch_kwargs(host="127.0.0.1", port=7860)
        self.assertEqual(kwargs["server_name"], "127.0.0.1")
        self.assertEqual(kwargs["server_port"], 7860)
        self.assertFalse(kwargs.get("share", False))
        self.assertNotIn("980px", kwargs.get("css") or "")
        allowed = kwargs.get("allowed_paths") or []
        self.assertIn(str(lab_root()), allowed)
        self.assertIn(str(lab_root() / "cache"), allowed)
        self.assertIn(str(qual_root()), allowed)

    def test_blocks_fill_width(self) -> None:
        from tts.playground.app import PLAYGROUND_CSS, build_interface

        demo = build_interface(Path.home())
        self.assertTrue(getattr(demo, "fill_width", False))
        self.assertNotIn("980px", PLAYGROUND_CSS)

    def test_gradio_audio_returns_numpy_not_cache_path(self) -> None:
        from tts.playground.app import _gradio_audio
        from tts.wav import write_wav
        import numpy as np

        self.assertIsNone(_gradio_audio(None))
        self.assertIsNone(_gradio_audio(""))
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "ref.wav"
            write_wav(wav, 16000, np.zeros(1600, dtype=np.float32))
            sr, samples = _gradio_audio(str(wav))
            self.assertEqual(sr, 16000)
            self.assertEqual(int(getattr(samples, "size", len(samples))), 1600)

    def test_serve_restores_leftover_after_launch_exits(self) -> None:
        from tts.playground.app import main

        order: list[str] = []

        class FakeEngine:
            def close(self) -> None:
                order.append("engine.close")

        def factory(config, **kwargs):
            order.append(f"load:{config.name}")
            return FakeEngine()

        class FakeDemo:
            def launch(self, **kwargs):
                order.append("launch")
                self.assert_kwargs = kwargs
                raise KeyboardInterrupt

        demo = FakeDemo()

        with (
            patch(
                "tts.playground.app.park_leftover.park",
                side_effect=lambda: order.append("park"),
            ),
            patch(
                "tts.playground.app.park_leftover.restore",
                side_effect=lambda: order.append("restore"),
            ),
            patch(
                "tts.playground.app._default_engine_factory",
                side_effect=factory,
            ),
            patch(
                "tts.playground.app.build_interface",
                return_value=demo,
            ),
        ):
            code = main(["--host", "127.0.0.1", "--port", "7860"])
        self.assertEqual(code, 0)
        self.assertEqual(order[0], "park")
        self.assertIn("load:E2", order)
        self.assertLess(order.index("park"), order.index("load:E2"))
        self.assertLess(order.index("load:E2"), order.index("launch"))
        self.assertEqual(order[-1], "restore")
        self.assertLess(order.index("engine.close"), order.index("restore"))
        self.assertEqual(demo.assert_kwargs["server_name"], "127.0.0.1")
        self.assertEqual(demo.assert_kwargs["server_port"], 7860)
        self.assertFalse(demo.assert_kwargs.get("share", False))


class PlaygroundTranscribeUi(unittest.TestCase):
    def test_blocks_expose_transcribe_into_clone_transcript(self) -> None:
        from breeze_tts_qual.playground import build_interface

        demo = build_interface(Path.home())
        by_label = _labels(demo)
        self.assertIn("Effective transcript", by_label)
        self.assertIn("Source transcript", by_label)
        self.assertNotIn(
            "Transcript (Parakeet TDT 0.6B v2 via transcribe.cpp)", by_label
        )
        buttons = _buttons(demo)
        self.assertTrue(any(v == "Transcribe" for v in buttons), buttons)
        info = str(getattr(by_label["Effective transcript"], "info", "") or "")
        self.assertIn("Transcribe", info)

    def test_session_transcribe_uses_clone_and_stays_cpu(self) -> None:
        from tts.playground.app import PlaygroundSession

        calls: list[object] = []

        def fake_transcribe(audio, **kwargs):
            calls.append(audio)
            return {
                "text": "hello from parakeet",
                "wall_s": 0.42,
                "cli": "/tmp/transcribe-cli",
                "model": "/tmp/parakeet-tdt-0.6b-v2-Q8_0.gguf",
                "backend": "cpu",
            }

        with tempfile.TemporaryDirectory() as tmp:
            session = PlaygroundSession(
                Path("."), engine_factory=lambda *a, **k: None, lab=Path(tmp)
            )
            clone = "/tmp/clone.wav"
            with patch(
                "tts.playground.app.transcribe_audio",
                side_effect=fake_transcribe,
            ):
                text, status = session.transcribe(clone)
        self.assertEqual(text, "hello from parakeet")
        self.assertIn("cpu", status)
        self.assertIn("clone", status)
        self.assertNotIn("output", status)
        self.assertIn("parakeet-tdt-0.6b-v2", status)
        self.assertEqual(calls, [clone])


if __name__ == "__main__":
    unittest.main()
