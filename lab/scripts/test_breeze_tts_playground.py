#!/usr/bin/env python3
"""CPU tests for lab Gradio playground. not on the voicecat path."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent / "breeze_tts_qual"
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT))


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

    def test_blocks_default_preset_is_e2_shelf_winner(self) -> None:
        from breeze_tts_qual.playground import build_interface

        demo = build_interface(Path.home())
        radios = [
            c
            for c in demo.blocks.values()
            if type(c).__name__ == "Radio"
        ]
        self.assertTrue(radios, "expected a preset Radio")
        radio = radios[0]
        values = []
        for choice in radio.choices:
            if isinstance(choice, (tuple, list)) and len(choice) >= 2:
                values.append(choice[1])
            else:
                values.append(choice)
        self.assertIn("E2", values)
        self.assertEqual(radio.value, "E2")
        labels = " ".join(str(choice[0] if isinstance(choice, (tuple, list)) else choice) for choice in radio.choices)
        self.assertIn("SHELF", labels)

    def test_blocks_expose_text_instruction_cfg_seed_clone(self) -> None:
        import tempfile

        from breeze_tts_qual.playground import build_interface
        from breeze_tts_qual.utterances import DIRECTIONS, SHORT

        with tempfile.TemporaryDirectory() as tmp:
            qual_root = Path(tmp)
            fixtures = qual_root / "fixtures"
            fixtures.mkdir()
            ref_wav = fixtures / "ref.wav"
            ref_txt = fixtures / "ref.txt"
            ref_wav.write_bytes(b"RIFF")
            ref_txt.write_text("clone speaker text\n", encoding="utf-8")
            demo = build_interface(qual_root)
            by_label = {
                getattr(c, "label", None): c
                for c in demo.blocks.values()
                if getattr(c, "label", None)
            }
            self.assertIn("Text", by_label)
            self.assertEqual(by_label["Text"].value, SHORT[0])
            self.assertIn("How it speaks", by_label)
            self.assertEqual(
                by_label["How it speaks"].value, "Speak clearly and naturally."
            )
            chips = str(getattr(by_label["How it speaks"], "info", "") or "") + " ".join(
                str(getattr(c, "value", "")) + str(getattr(c, "label", ""))
                for c in demo.blocks.values()
            )
            for direction in DIRECTIONS:
                self.assertIn(direction, chips)
            self.assertIn("cfg_scale", by_label)
            slider = by_label["cfg_scale"]
            self.assertEqual(slider.value, 1.0)
            self.assertEqual(slider.minimum, 1.0)
            self.assertEqual(slider.maximum, 8.0)
            self.assertEqual(slider.step, 0.5)
            info = str(slider.info or "")
            self.assertIn("1.0", info)
            self.assertIn("4.0", info)
            self.assertIn("seed", by_label)
            self.assertEqual(by_label["seed"].value, 42)
            self.assertIn("Clone audio", by_label)
            self.assertIn("Clone transcript", by_label)
            clone_val = by_label["Clone audio"].value
            if isinstance(clone_val, dict):
                self.assertEqual(clone_val.get("orig_name"), "ref.wav")
                self.assertTrue(str(clone_val.get("path", "")).endswith("ref.wav"))
            else:
                self.assertEqual(Path(clone_val).name, "ref.wav")
            self.assertEqual(
                by_label["Clone transcript"].value, "clone speaker text"
            )

    def test_blocks_have_generate_status_and_killed_advanced(self) -> None:
        from breeze_tts_qual.playground import build_interface

        demo = build_interface(Path.home())
        by_label = {
            getattr(c, "label", None): c
            for c in demo.blocks.values()
            if getattr(c, "label", None)
        }
        kinds = {type(c).__name__ for c in demo.blocks.values()}
        self.assertIn("Audio", kinds)
        buttons = [
            c for c in demo.blocks.values() if type(c).__name__ == "Button"
        ]
        button_values = [str(getattr(b, "value", "")) for b in buttons]
        self.assertTrue(any("Generate" in v for v in button_values), button_values)
        self.assertIn("Status", by_label)
        accs = [
            c for c in demo.blocks.values() if type(c).__name__ == "Accordion"
        ]
        titles = " ".join(str(getattr(a, "label", "")) for a in accs)
        self.assertTrue(accs, "expected Advanced accordion")
        self.assertTrue("killed" in titles.lower() or "unsafe" in titles.lower(), titles)
        checkboxes = [
            c for c in demo.blocks.values() if type(c).__name__ == "Checkbox"
        ]
        self.assertTrue(checkboxes, "expected confirm checkbox for killed presets")
        self.assertFalse(bool(checkboxes[0].value))
        radios = [
            c for c in demo.blocks.values() if type(c).__name__ == "Radio"
        ]
        self.assertGreaterEqual(len(radios), 2)
        killed_values = []
        for choice in radios[1].choices:
            if isinstance(choice, (tuple, list)) and len(choice) >= 2:
                killed_values.append(choice[1])
            else:
                killed_values.append(choice)
        for name in ("C3", "C4", "D", "E3", "E4", "E5", "B_backbone_prefill"):
            self.assertIn(name, killed_values)
        main_values = []
        for choice in radios[0].choices:
            if isinstance(choice, (tuple, list)) and len(choice) >= 2:
                main_values.append(choice[1])
            else:
                main_values.append(choice)
        for name in ("C3", "C4", "D", "E3"):
            self.assertNotIn(name, main_values)

class PlaygroundSessionTests(unittest.TestCase):
    def test_start_parks_before_load_and_close_restores(self) -> None:
        from breeze_tts_qual.playground import PlaygroundSession

        order: list[str] = []

        class FakeEngine:
            def __init__(self) -> None:
                order.append("load")

            def close(self) -> None:
                order.append("engine.close")

        def factory(config, **kwargs):
            order.append(f"factory:{config.name}")
            return FakeEngine()

        with (
            patch(
                "breeze_tts_qual.playground.park_leftover.park",
                side_effect=lambda: order.append("park"),
            ) as park,
            patch(
                "breeze_tts_qual.playground.park_leftover.restore",
                side_effect=lambda: order.append("restore"),
            ) as restore,
        ):
            session = PlaygroundSession(Path("."), engine_factory=factory)
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
        from breeze_tts_qual.playground import PlaygroundSession

        def factory(config, **kwargs):
            raise RuntimeError("no 3B")

        with (
            patch("breeze_tts_qual.playground.park_leftover.park") as park,
            patch("breeze_tts_qual.playground.park_leftover.restore") as restore,
        ):
            session = PlaygroundSession(Path("."), engine_factory=factory)
            with self.assertRaisesRegex(RuntimeError, "no 3B"):
                session.start()
            session.close()
        park.assert_called_once()
        restore.assert_called_once()
        self.assertIsNone(session.engine)

    def test_ensure_preset_reloads_only_on_change(self) -> None:
        from breeze_tts_qual.playground import PlaygroundSession

        loads: list[str] = []

        class FakeEngine:
            def close(self) -> None:
                loads.append("close")

        def factory(config, **kwargs):
            loads.append(config.name)
            return FakeEngine()

        with (
            patch("breeze_tts_qual.playground.park_leftover.park"),
            patch("breeze_tts_qual.playground.park_leftover.restore"),
        ):
            session = PlaygroundSession(Path("."), engine_factory=factory)
            session.start("E2")
            first = session.engine
            session.ensure_preset("E2")
            self.assertIs(session.engine, first)
            session.ensure_preset("A")
            self.assertEqual(session.loaded_preset, "A")
            self.assertIsNot(session.engine, first)
            session.close()
        self.assertEqual(loads, ["E2", "close", "A", "close"])

    def test_resolve_preset_requires_confirm_for_killed(self) -> None:
        from breeze_tts_qual.playground import PlaygroundSession

        session = PlaygroundSession(Path("."), engine_factory=lambda *a, **k: None)
        self.assertEqual(session.resolve_preset("E2", None, False), "E2")
        with self.assertRaisesRegex(ValueError, "confirm"):
            session.resolve_preset("E2", "C3", False)
        self.assertEqual(session.resolve_preset("E2", "C3", True), "C3")
        self.assertEqual(session.resolve_preset("A", None, True), "A")

    def test_generate_is_sequential_and_forwards_controls(self) -> None:
        from breeze_tts_qual.engine import PcmChunk
        from breeze_tts_qual.playground import PlaygroundSession

        calls: list[dict] = []
        silent = (b"\x00\x00") * 200
        audible = (b"\xe8\x03") * 200  # 1000 as s16le

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

        with (
            patch("breeze_tts_qual.playground.park_leftover.park"),
            patch("breeze_tts_qual.playground.park_leftover.restore"),
        ):
            session = PlaygroundSession(Path("."), engine_factory=factory)
            session.start("E2")
            audio, status = session.generate(
                text="hello there",
                instruction="slightly amused",
                cfg_scale=4.0,
                seed=7,
                clone_audio="/tmp/ref.wav",
                clone_text="clone speaker",
                safe_preset="E2",
                killed_preset=None,
                confirm_killed=False,
            )
            session.close()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["text"], "hello there")
        self.assertEqual(calls[0]["instruction"], "slightly amused")
        self.assertEqual(calls[0]["cfg_scale"], 4.0)
        self.assertEqual(calls[0]["seed"], 7)
        self.assertEqual(str(calls[0]["reference_audio"]), "/tmp/ref.wav")
        self.assertEqual(calls[0]["reference_text"], "clone speaker")
        self.assertIsInstance(audio, tuple)
        self.assertEqual(audio[0], 24000)
        self.assertIn("E2", status)
        self.assertIn("first_pcm", status)
        self.assertIn("first_nonsilent", status)
        self.assertTrue(hasattr(session, "_lock"))

    def test_launch_kwargs_bind_localhost_without_share(self) -> None:
        from breeze_tts_qual.playground import launch_kwargs

        kwargs = launch_kwargs(host="127.0.0.1", port=7860)
        self.assertEqual(kwargs["server_name"], "127.0.0.1")
        self.assertEqual(kwargs["server_port"], 7860)
        self.assertFalse(kwargs.get("share", False))
        self.assertNotIn(True, [kwargs.get("share")])

    def test_serve_restores_leftover_after_launch_exits(self) -> None:
        from breeze_tts_qual.playground import main

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
                "breeze_tts_qual.playground.park_leftover.park",
                side_effect=lambda: order.append("park"),
            ),
            patch(
                "breeze_tts_qual.playground.park_leftover.restore",
                side_effect=lambda: order.append("restore"),
            ),
            patch(
                "breeze_tts_qual.playground._default_engine_factory",
                side_effect=factory,
            ),
            patch(
                "breeze_tts_qual.playground.build_interface",
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














if __name__ == "__main__":
    unittest.main()
