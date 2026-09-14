#!/usr/bin/env python3
"""CPU tests for lab Parakeet transcribe helper. not on the voicecat path."""

from __future__ import annotations

import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent / "breeze_tts_qual"
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT))


class ParseCliText(unittest.TestCase):
    def test_takes_last_text_line(self) -> None:
        from breeze_tts_qual.transcribe import parse_cli_text

        stdout = (
            "audio: /tmp/in.wav\n"
            "model: /tmp/model.gguf -> ok\n"
            "run: ok\n"
            "text: Ask not what your country can do for you.\n"
        )
        self.assertEqual(
            parse_cli_text(stdout),
            "Ask not what your country can do for you.",
        )

    def test_empty_and_missing(self) -> None:
        from breeze_tts_qual.transcribe import parse_cli_text

        self.assertEqual(parse_cli_text("run: ok\ntext: (empty)\n"), "")
        with self.assertRaisesRegex(RuntimeError, "no text"):
            parse_cli_text("run: ok\n")


class AudioPrep(unittest.TestCase):
    def test_resample_and_write_16k_mono(self) -> None:
        import numpy as np

        from breeze_tts_qual.transcribe import write_16k_mono_wav

        # 24000 Hz stereo-ish (n, 2) 0.1s of a sine
        sr = 24000
        n = 2400
        t = np.arange(n, dtype=np.float32) / sr
        left = np.sin(2 * np.pi * 440 * t).astype(np.float32)
        audio = (sr, np.stack([left, left], axis=1))
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "out.wav"
            write_16k_mono_wav(audio, dest)
            with wave.open(str(dest), "rb") as wav_file:
                self.assertEqual(wav_file.getnchannels(), 1)
                self.assertEqual(wav_file.getsampwidth(), 2)
                self.assertEqual(wav_file.getframerate(), 16000)
                self.assertGreater(wav_file.getnframes(), 1000)

    def test_pick_audio_prefers_output(self) -> None:
        from breeze_tts_qual.transcribe import pick_audio

        out = (16000, [0, 1, 0])
        audio, source = pick_audio(out, "/no/such.wav")
        self.assertEqual(source, "output")
        self.assertIs(audio, out)

    def test_pick_audio_falls_back_to_clone_wav(self) -> None:
        import numpy as np

        from breeze_tts_qual.transcribe import pick_audio, write_16k_mono_wav

        with tempfile.TemporaryDirectory() as tmp:
            clone = Path(tmp) / "clone.wav"
            write_16k_mono_wav((16000, np.zeros(1600, dtype=np.float32)), clone)
            audio, source = pick_audio(None, str(clone))
            self.assertEqual(source, "clone")
            self.assertEqual(Path(audio), clone)

            audio, source = pick_audio((None, None), str(clone))
            self.assertEqual(source, "clone")
            self.assertEqual(Path(audio), clone)

    def test_coerce_mp3_file(self) -> None:
        import shutil
        import subprocess

        import numpy as np

        from breeze_tts_qual.transcribe import coerce_audio, write_16k_mono_wav

        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            self.skipTest("ffmpeg not on PATH")
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "tone.wav"
            mp3 = Path(tmp) / "tone.mp3"
            write_16k_mono_wav((16000, np.zeros(1600, dtype=np.float32)), wav)
            proc = subprocess.run(
                [
                    ffmpeg,
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-i",
                    str(wav),
                    "-q:a",
                    "9",
                    str(mp3),
                ],
                check=False,
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(mp3.is_file())
            self.assertNotEqual(mp3.read_bytes()[:4], b"RIFF")
            sr, samples = coerce_audio(str(mp3))
            self.assertEqual(sr, 16000)
            self.assertGreater(np.asarray(samples).size, 0)
            dest = Path(tmp) / "from-mp3.wav"
            write_16k_mono_wav(str(mp3), dest)
            with wave.open(str(dest), "rb") as wav_file:
                self.assertEqual(wav_file.getnchannels(), 1)
                self.assertEqual(wav_file.getframerate(), 16000)
                self.assertGreater(wav_file.getnframes(), 100)


class TranscribeAudio(unittest.TestCase):
    def test_invokes_cli_cpu_backend(self) -> None:
        import numpy as np

        from breeze_tts_qual.transcribe import transcribe_audio

        with tempfile.TemporaryDirectory() as tmp:
            cli = Path(tmp) / "transcribe-cli"
            model = Path(tmp) / "parakeet-tdt-0.6b-v2-Q8_0.gguf"
            cli.write_text("#!/bin/sh\n")
            cli.chmod(0o755)
            model.write_bytes(b"GGUF")

            class FakeProc:
                returncode = 0
                stdout = "run: ok\ntext: yes this is a test\n"
                stderr = ""

            with patch(
                "breeze_tts_qual.transcribe.subprocess.run",
                return_value=FakeProc(),
            ) as run:
                result = transcribe_audio(
                    (24000, np.zeros(2400, dtype=np.float32)),
                    cli=cli,
                    model=model,
                )
            cmd = run.call_args.args[0]
            self.assertEqual(cmd[0], str(cli))
            self.assertIn("-m", cmd)
            self.assertEqual(cmd[cmd.index("-m") + 1], str(model))
            self.assertIn("--backend", cmd)
            self.assertEqual(cmd[cmd.index("--backend") + 1], "cpu")
            self.assertTrue(cmd[-1].endswith(".wav"))
            self.assertEqual(result["text"], "yes this is a test")
            self.assertEqual(result["backend"], "cpu")
            self.assertEqual(cmd[cmd.index("--timestamps") + 1], "word")
            self.assertEqual(result["words"], [])


class ParseAlignment(unittest.TestCase):
    def test_prefers_word_lines(self) -> None:
        from breeze_tts_qual.transcribe import parse_cli_alignment

        stdout = (
            "run: ok\n"
            "text: Can the Greys run\n"
            "segments: 1\n"
            "  [   0.00 ->   24.00] Can the Greys run\n"
            "words: 4\n"
            "  [   0.00 ->    4.00] Can\n"
            "  [   4.00 ->    8.00] the\n"
            "  [   8.00 ->   12.00] Greys\n"
            "  [  12.00 ->   16.00] run\n"
        )
        aligned = parse_cli_alignment(stdout)
        self.assertEqual(aligned["text"], "Can the Greys run")
        self.assertEqual(len(aligned["words"]), 4)
        self.assertEqual(aligned["words"][2]["text"], "Greys")
        self.assertAlmostEqual(aligned["words"][2]["start_s"], 8.0)


if __name__ == "__main__":
    unittest.main()
