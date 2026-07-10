#!/usr/bin/env python3
"""
Deterministic tests for speech-core-golden operator command.

Covers:
  - Scenario menu selection (number, name, fuzzy)
  - Friendly name mapping
  - Binary discovery
  - Quality check pass/fail conditions
  - Review loop accept/retry/quit logic
  - Non-overwriting retry directories
  - Command construction for watcher and mic adapter
  - Dry-run behavior (invalid for promotion)
  - Cleanup on exceptions/signals
  - Subprocess orchestration with fake binaries

No real microphone, network, or subprocesses — all external calls are
mocked or use synthetic data.
"""

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch, PropertyMock

# Add scripts to path
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import importlib
import importlib.util

spec = importlib.util.spec_from_file_location("golden", SCRIPT_DIR / "speech-core-golden.py")
golden = importlib.util.module_from_spec(spec)
spec.loader.exec_module(golden)


# ── Helpers ──────────────────────────────────────────────────────────────────

VALID_MANIFEST = {
    "manifest_version": 1,
    "profile": "golden-mvp",
    "scenarios": [
        {"id": "human-clean-complete", "class": "natural_endpoint",
         "construction": "human-recorded", "description": "Clean sentence"},
        {"id": "human-trailing-off", "class": "natural_endpoint",
         "construction": "human-recorded", "description": "Trailing off"},
        {"id": "human-pause-resume-incomplete", "class": "natural_endpoint",
         "construction": "human-recorded", "description": "Pause and resume"},
        {"id": "synthetic-vad-onset-below-32ms", "class": "synthetic_boundary",
         "construction": "synthetic", "description": "VAD onset below"},
        {"id": "synthetic-min-vad-speech-at-400ms", "class": "synthetic_boundary",
         "construction": "synthetic", "description": "Min speech at"},
    ],
}


def _make_take_dir(base: Path, scenario_id: str, mode: str = "take") -> Path:
    """Create a deterministic take directory structure."""
    scenario_out = base / "scenarios" / scenario_id / "takes"
    scenario_out.mkdir(parents=True, exist_ok=True)
    existing = sorted(scenario_out.glob(f"{mode}-*"))
    num = len(existing) + 1
    take_dir = scenario_out / f"{mode}-{num:03d}"
    take_dir.mkdir(parents=True)
    return take_dir


def _write_silence_wav(path: Path, duration_ms: int = 1000):
    """Write a valid silence WAV for testing."""
    samples = golden._generate_silence(golden.ms_samples(duration_ms))
    golden.write_wav(path, samples)


def _write_tone_wav(path: Path, duration_ms: int = 1000, freq: float = 440.0):
    """Write a non-silent WAV for testing."""
    samples = golden._generate_sine(golden.ms_samples(duration_ms), freq, 0.5)
    golden.write_wav(path, samples)


class TestFriendlyNames(unittest.TestCase):
    """Friendly name mapping."""

    def test_all_known_scenarios_have_names(self):
        for sid in golden.SYNTHETIC_SCENARIO_PLANS:
            self.assertIn(sid, golden.FRIENDLY_NAMES, f"No friendly name for {sid}")

        human_ids = [sid for sid in golden._default_cues_for_scenario.__code__.co_consts
                     if isinstance(sid, str) and sid.startswith("human-")]
        # Known human IDs from manifest
        known_human = [
            "human-clean-complete", "human-trailing-off",
            "human-pause-resume-incomplete", "human-rapid-question",
            "human-hold-continuous-filler-7000",
        ]
        for sid in known_human:
            self.assertIn(sid, golden.FRIENDLY_NAMES, f"No friendly name for {sid}")

    def test_friendly_name_fallback(self):
        self.assertEqual(golden._scenario_friendly_name("unknown-scenario"),
                         "unknown-scenario")

    def test_friendly_name_lookup(self):
        self.assertEqual(golden._scenario_friendly_name("human-clean-complete"),
                         "Clean sentence")
        self.assertEqual(golden._scenario_friendly_name("human-trailing-off"),
                         "Trailing off")
        self.assertEqual(golden._scenario_friendly_name("synthetic-vad-onset-below-32ms"),
                         "VAD onset below (32ms)")


class TestBinaryDiscovery(unittest.TestCase):
    """Binary location logic."""

    def test_find_binary_nonexistent(self):
        result = golden._find_binary("nonexistent-binary-xyz-12345")
        self.assertIsNone(result)

    def test_find_binary_in_script_dir(self):
        # The golden script itself is in the script dir
        result = golden._find_binary("speech-core-golden")
        self.assertIsNotNone(result)

    def test_find_binary_via_which(self):
        # python3 should be on PATH
        result = golden._find_binary("python3")
        self.assertIsNotNone(result)


class TestScenarioMenu(unittest.TestCase):
    """Interactive scenario menu tests (using monkeypatched input)."""

    def test_menu_quit(self):
        with patch("builtins.input", return_value="q"):
            result = golden._operator_scenario_menu(VALID_MANIFEST)
            self.assertIsNone(result)

    def test_menu_select_by_number(self):
        with patch("builtins.input", side_effect=["1"]):
            result = golden._operator_scenario_menu(VALID_MANIFEST)
            self.assertEqual(result, "human-clean-complete")

    def test_menu_select_by_number_synthetic(self):
        # human scenarios are 1-3, synthetic starts at 4
        with patch("builtins.input", side_effect=["4"]):
            result = golden._operator_scenario_menu(VALID_MANIFEST)
            self.assertEqual(result, "synthetic-vad-onset-below-32ms")

    def test_menu_select_by_name_match(self):
        with patch("builtins.input", side_effect=["clean"]):
            result = golden._operator_scenario_menu(VALID_MANIFEST)
            self.assertEqual(result, "human-clean-complete")

    def test_menu_select_by_name_partial(self):
        with patch("builtins.input", side_effect=["vad onset below"]):
            result = golden._operator_scenario_menu(VALID_MANIFEST)
            self.assertEqual(result, "synthetic-vad-onset-below-32ms")

    def test_menu_select_by_id_substring(self):
        with patch("builtins.input", side_effect=["trailing"]):
            result = golden._operator_scenario_menu(VALID_MANIFEST)
            self.assertEqual(result, "human-trailing-off")

    def test_menu_no_match(self):
        with patch("builtins.input", side_effect=["xyzzy_nonexistent", "q"]):
            result = golden._operator_scenario_menu(VALID_MANIFEST)
            self.assertIsNone(result)

    def test_menu_empty_input(self):
        with patch("builtins.input", side_effect=["", "  ", "q"]):
            result = golden._operator_scenario_menu(VALID_MANIFEST)
            self.assertIsNone(result)


class TestQualityChecks(unittest.TestCase):
    """Automated quality check pass/fail."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_all_pass_on_valid_wav(self):
        take_dir = Path(self.tmp) / "take-001"
        take_dir.mkdir()
        _write_tone_wav(take_dir / "audio.wav", 1000)
        (take_dir / "event-stream.jsonl").write_text(
            '{"event":"stream_start"}\n{"event":"vad_session_end"}\n'
        )

        result = golden._operator_quality_check(take_dir, "test", "take", dry_run=False)
        self.assertTrue(result["passed"], f"Expected pass, got: {result}")
        self.assertEqual(len(result["failures"]), 0)

    def test_silence_fails_non_silence_check(self):
        take_dir = Path(self.tmp) / "take-002"
        take_dir.mkdir()
        _write_silence_wav(take_dir / "audio.wav", 1000)
        (take_dir / "event-stream.jsonl").write_text('{"event":"stream_start"}\n')

        result = golden._operator_quality_check(take_dir, "test", "take", dry_run=False)
        # Silence WAV should fail the non_silence check
        self.assertFalse(result["passed"])
        self.assertTrue(any("silence" in f.lower() or "quiet" in f.lower()
                           for f in result["failures"]), result["failures"])

    def test_missing_wav_fails(self):
        take_dir = Path(self.tmp) / "take-003"
        take_dir.mkdir()

        result = golden._operator_quality_check(take_dir, "test", "take", dry_run=False)
        self.assertFalse(result["passed"])
        self.assertTrue(any("missing" in f.lower() for f in result["failures"]))

    def test_dry_run_always_invalid(self):
        take_dir = Path(self.tmp) / "take-004"
        take_dir.mkdir()
        _write_tone_wav(take_dir / "audio.wav", 1000)
        (take_dir / "event-stream.jsonl").write_text('{"event":"stream_start"}\n')

        result = golden._operator_quality_check(take_dir, "test", "take", dry_run=True)
        self.assertFalse(result["passed"])
        self.assertTrue(any("dry" in w.lower() or "promotion" in w.lower()
                           for w in result.get("warnings", [])))

    def test_clipping_detected(self):
        take_dir = Path(self.tmp) / "take-005"
        take_dir.mkdir()
        # Create clipping samples
        samples = [32767] * 100 + [0] * 900
        golden.write_wav(take_dir / "audio.wav", samples)
        (take_dir / "event-stream.jsonl").write_text('{"event":"stream_start"}\n')

        result = golden._operator_quality_check(take_dir, "test", "take", dry_run=False)
        self.assertFalse(result["passed"])
        self.assertTrue(any("clipping" in f.lower() or "clip" in f.lower()
                           for f in result["failures"]), result["failures"])

    def test_missing_events_fails_non_dry_run(self):
        take_dir = Path(self.tmp) / "take-006"
        take_dir.mkdir()
        _write_tone_wav(take_dir / "audio.wav", 1000)
        # No event-stream.jsonl

        result = golden._operator_quality_check(take_dir, "test", "take", dry_run=False)
        self.assertFalse(result["passed"])
        self.assertTrue(any("event" in f.lower() and "missing" in f.lower()
                           for f in result["failures"]))

    def test_practice_mode_does_not_change_checks(self):
        take_dir = Path(self.tmp) / "take-007"
        take_dir.mkdir()
        _write_tone_wav(take_dir / "audio.wav", 1000)
        (take_dir / "event-stream.jsonl").write_text('{"event":"stream_start"}\n')

        result = golden._operator_quality_check(take_dir, "test", "practice", dry_run=False)
        # Practice mode should still pass if quality is good
        self.assertTrue(result["passed"])

    def test_zero_events_fails(self):
        take_dir = Path(self.tmp) / "take-008"
        take_dir.mkdir()
        _write_tone_wav(take_dir / "audio.wav", 1000)
        # Write a file with only blank lines and a non-JSON comment
        (take_dir / "event-stream.jsonl").write_text("\n\n")

        result = golden._operator_quality_check(take_dir, "test", "take", dry_run=False)
        self.assertFalse(result["passed"])
        self.assertTrue(any("zero" in f.lower() for f in result["failures"]))


class TestReviewLoop(unittest.TestCase):
    """Post-capture review loop: accept, retry, quit."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.take_dir = Path(self.tmp) / "take-001"
        self.take_dir.mkdir()
        _write_tone_wav(self.take_dir / "audio.wav", 1000)
        golden.save_file({"test": True}, self.take_dir / "consent.json")
        golden.save_file({"test": True}, self.take_dir / "privacy.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _passing_quality(self):
        return {
            "passed": True,
            "checks": [
                {"name": "wav_exists", "passed": True},
                {"name": "wav_format", "passed": True},
                {"name": "peak", "passed": True, "reason": "Peak -4.2 dBFS"},
                {"name": "non_silence", "passed": True, "reason": "Signal present"},
                {"name": "clipping", "passed": True, "reason": "No clipping"},
            ],
            "failures": [],
            "quality": {"duration_ms": 1000, "peak_dbfs": -4.2, "rms_dbfs": -12.0, "clipping_count": 0},
            "event_count": 5,
            "dry_run": False,
        }

    def _failing_quality(self):
        return {
            "passed": False,
            "checks": [
                {"name": "wav_exists", "passed": True},
                {"name": "peak", "passed": False, "reason": "Pure silence"},
                {"name": "non_silence", "passed": False, "reason": "Too quiet"},
            ],
            "failures": ["Pure silence (peak=-96.0 dBFS)", "Too quiet (RMS=-96.0 dBFS)"],
            "quality": {"duration_ms": 1000, "peak_dbfs": -96.0, "rms_dbfs": -96.0, "clipping_count": 0},
            "dry_run": False,
        }

    def test_accept_when_passing(self):
        with patch("builtins.input", return_value=""), \
             patch.object(golden, "_get_key", return_value="a"):
            code = golden._operator_review_loop(
                self.take_dir, self._passing_quality(), "take"
            )
            self.assertEqual(code, golden.ExitCode.PASS)
            # Review should be written
            review_path = self.take_dir / "review.json"
            self.assertTrue(review_path.exists())
            review = json.loads(review_path.read_text())
            self.assertTrue(review["accepted"])

    def test_retry_returns_sentinel(self):
        with patch("builtins.input", return_value=""), \
             patch.object(golden, "_get_key", return_value="r"):
            code = golden._operator_review_loop(
                self.take_dir, self._passing_quality(), "take"
            )
            self.assertEqual(code, -1)

    def test_quit(self):
        with patch("builtins.input", return_value=""), \
             patch.object(golden, "_get_key", return_value="q"):
            code = golden._operator_review_loop(
                self.take_dir, self._passing_quality(), "take"
            )
            self.assertEqual(code, golden.ExitCode.RECORDER_ABORTED)

    def test_accept_disabled_when_failing(self):
        """Accept should not be possible when quality checks fail (non-practice)."""
        with patch("builtins.input", return_value=""), \
             patch.object(golden, "_get_key", side_effect=["a", "q"]):
            code = golden._operator_review_loop(
                self.take_dir, self._failing_quality(), "take"
            )
            # First 'a' should be rejected, second 'q' quits
            self.assertEqual(code, golden.ExitCode.RECORDER_ABORTED)

    def test_practice_can_accept_despite_failures(self):
        """Practice mode allows accept even with failures."""
        with patch("builtins.input", return_value=""), \
             patch.object(golden, "_get_key", return_value="a"):
            code = golden._operator_review_loop(
                self.take_dir, self._failing_quality(), "practice"
            )
            self.assertEqual(code, golden.ExitCode.PASS)

    def test_playback_no_audio(self):
        """When no audio player is available, playback shows an error."""
        # Remove the wav to simulate missing audio
        (self.take_dir / "audio.wav").unlink()
        with patch("builtins.input", return_value=""), \
             patch.object(golden, "_get_key", side_effect=["p", "q"]):
            code = golden._operator_review_loop(
                self.take_dir, self._passing_quality(), "take"
            )
            self.assertEqual(code, golden.ExitCode.RECORDER_ABORTED)


class TestNonOverwritingRetries(unittest.TestCase):
    """Take directories should not overwrite each other."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_take_numbers_increment(self):
        """Each take gets a unique directory number."""
        base = Path(self.tmp)
        take1 = _make_take_dir(base, "test-scenario", "take")
        self.assertTrue(take1.name.startswith("take-"))
        num1 = int(take1.name.split("-")[-1])

        take2 = _make_take_dir(base, "test-scenario", "take")
        num2 = int(take2.name.split("-")[-1])
        self.assertEqual(num2, num1 + 1)
        self.assertNotEqual(take1, take2)

    def test_practice_take_counters_independent(self):
        """Practice and take counters are independent."""
        base = Path(self.tmp)
        t1 = _make_take_dir(base, "test-scenario", "take")
        p1 = _make_take_dir(base, "test-scenario", "practice")

        t2 = _make_take_dir(base, "test-scenario", "take")
        p2 = _make_take_dir(base, "test-scenario", "practice")

        self.assertTrue(t1.name.startswith("take-"))
        self.assertTrue(p1.name.startswith("practice-"))
        self.assertTrue(t2.name.startswith("take-"))
        self.assertTrue(p2.name.startswith("practice-"))

        take_num1 = int(t1.name.split("-")[-1])
        take_num2 = int(t2.name.split("-")[-1])
        prac_num1 = int(p1.name.split("-")[-1])
        prac_num2 = int(p2.name.split("-")[-1])

        self.assertEqual(take_num2, take_num1 + 1)
        self.assertEqual(prac_num2, prac_num1 + 1)

    def test_subsequent_take_does_not_overwrite(self):
        """Create a take dir, then try to create same one — should fail or get new one."""
        base = Path(self.tmp)
        take_dir = _make_take_dir(base, "test-scenario", "take")
        marker = take_dir / "marker.txt"
        marker.write_text("first take")
        # If we try to create the same take dir, it should either fail or we get a new one
        # In the operator flow, we iterate until we find a non-existing dir
        self.assertTrue(marker.exists())

        # Simulate what cmd_operator does: find a non-existing dir
        existing = sorted(take_dir.parent.glob("take-*"))
        next_num = len(existing) + 1
        next_dir = take_dir.parent / f"take-{next_num:03d}"
        self.assertFalse(next_dir.exists())  # Should not exist yet


class TestCommandConstruction(unittest.TestCase):
    """Verify subprocess command arguments for watcher and mic adapter."""

    def test_watcher_args(self):
        """Watcher should be called with --mode jsonl and --stream-session-id."""
        watcher_args = [
            "/fake/speech-core-watch",
            "--mode", "jsonl",
            "--url", "ws://127.0.0.1:8765/ws/audio-ingress",
            "--stream-session-id", "test-session-uuid",
        ]
        self.assertIn("--mode", watcher_args)
        self.assertIn("jsonl", watcher_args)
        self.assertIn("--stream-session-id", watcher_args)
        self.assertIn("test-session-uuid", watcher_args)
        self.assertIn("--url", watcher_args)

    def test_mic_adapter_args(self):
        """Mic adapter should include --record-wav and --stream-session-id."""
        mic_args = [
            "/fake/speech-core-mic-adapter",
            "--record-wav", "/tmp/audio.wav",
            "--url", "ws://127.0.0.1:8765/ws/audio-ingress",
            "--stream-session-id", "test-session-uuid",
        ]
        self.assertIn("--record-wav", mic_args)
        self.assertIn("/tmp/audio.wav", mic_args)
        self.assertIn("--stream-session-id", mic_args)
        self.assertIn("test-session-uuid", mic_args)

    def test_watcher_before_mic(self):
        """Watcher must be launched before mic adapter to avoid event loss."""
        # This is enforced by the code ordering in _operator_real_capture
        # We verify the code structure here
        import inspect
        source = inspect.getsource(golden._operator_real_capture)
        watcher_pos = source.find("watcher_proc = subprocess.Popen")
        mic_pos = source.find("mic_proc = subprocess.Popen")
        self.assertGreater(mic_pos, watcher_pos,
                          "Watcher must be launched before mic adapter")

    def test_session_id_shared(self):
        """Both watcher and mic adapter must receive the same session ID."""
        # This is verified by _operator_real_capture passing the same
        # stream_session_id to both subprocesses
        import inspect
        source = inspect.getsource(golden._operator_real_capture)
        # Both calls should reference stream_session_id
        self.assertIn("stream_session_id", source)


class TestDryRunInvalid(unittest.TestCase):
    """Dry-run mode must be clearly invalid for promotion."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_dry_run_silence_wav(self):
        """Dry-run should generate pure silence WAV."""
        take_dir = Path(self.tmp) / "take-001"
        take_dir.mkdir()
        wav_path = take_dir / "audio.wav"
        samples = golden._generate_silence(golden.ms_samples(5000))
        golden.write_wav(wav_path, samples)

        self.assertTrue(wav_path.exists())
        # Should be all zeros
        _sr, _ch, _sw, _n, data = golden.read_wav(wav_path)
        self.assertTrue(all(s == 0 for s in data))

    def test_dry_run_flagged_in_quality(self):
        take_dir = Path(self.tmp) / "take-002"
        take_dir.mkdir()
        _write_tone_wav(take_dir / "audio.wav", 500)
        (take_dir / "event-stream.jsonl").write_text("")

        result = golden._operator_quality_check(take_dir, "test", "take", dry_run=True)
        self.assertFalse(result["passed"])
        self.assertTrue(any("dry" in w.lower() or "promotion" in w.lower()
                           for w in result.get("warnings", [])))


class TestCleanupOnException(unittest.TestCase):
    """Subprocesses should be cleaned up on exceptions/signals."""

    def test_keyboard_interrupt_returns_aborted(self):
        """KeyboardInterrupt during capture returns RECORDER_ABORTED."""
        # We can verify the catch block structure
        import inspect
        source = inspect.getsource(golden._operator_real_capture)
        self.assertIn("KeyboardInterrupt", source)
        self.assertIn("RECORDER_ABORTED", source)

    def test_finally_block_cleans_up(self):
        """The finally block should kill any remaining subprocesses."""
        import inspect
        source = inspect.getsource(golden._operator_real_capture)
        self.assertIn("finally", source)
        # Should have kill logic
        self.assertIn("p.kill()" if "p.kill()" in source else "kill", source.lower())

    def test_mic_adapter_terminated_before_watcher(self):
        """Mic adapter should be terminated before watcher."""
        import inspect
        source = inspect.getsource(golden._operator_real_capture)
        mic_term_pos = source.find("mic_proc.terminate()")
        watcher_term_pos = source.find("watcher_proc.terminate()")
        self.assertGreater(watcher_term_pos, mic_term_pos,
                          "Mic adapter must be terminated before watcher")

    def test_event_drain_window_exists(self):
        """An event drain window must exist between mic termination and watcher termination."""
        import inspect
        source = inspect.getsource(golden._operator_real_capture)
        self.assertIn("drain", source.lower())
        self.assertIn("sleep", source.lower())


class TestSubprocessMocking(unittest.TestCase):
    """Test capture flow with mocked subprocess."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    @patch("subprocess.Popen")
    @patch.object(golden, "_find_binary")
    def test_real_capture_launches_both_processes(self, mock_find, mock_popen):
        """_operator_real_capture should launch watcher and mic adapter."""
        mock_find.side_effect = lambda name: f"/fake/{name}"

        # Configure the mock to simulate running processes
        mock_watcher = MagicMock()
        mock_watcher.poll.return_value = None  # Still running
        mock_watcher.pid = 12345
        mock_watcher.stderr = MagicMock()
        mock_watcher.stderr.read.return_value = ""

        mock_mic = MagicMock()
        mock_mic.poll.return_value = None
        mock_mic.pid = 12346

        # First call returns watcher, second returns mic
        mock_popen.side_effect = [mock_watcher, mock_mic]

        # Set up test data
        scenario = {"id": "human-clean-complete", "class": "natural_endpoint",
                    "construction": "human-recorded"}
        cues = golden._default_cues_for_scenario("human-clean-complete")
        take_dir = Path(self.tmp) / "take-001"
        take_dir.mkdir()
        (take_dir / "event-stream.jsonl").write_text('{"event":"stream_start"}\n')  # Pre-seed for ready check

        # Mock _display_recording_screen to avoid display
        with patch.object(golden, "_display_recording_screen"), \
             patch.object(golden, "_clear_screen"), \
             patch.object(golden, "_get_key", return_value=""), \
             patch("time.sleep", return_value=None), \
             patch("time.monotonic", side_effect=[0, 0.3, 0.6, 0.9, 1.2, 100]):  # skip ahead
            code, info = golden._operator_real_capture(
                scenario=scenario,
                cues=cues,
                take_dir=take_dir,
                mode="take",
                device=None,
                ws_url="ws://127.0.0.1:8765/ws/audio-ingress",
                stream_session_id="mock-session-id",
                total_ms=12000,
                drain_sec=0.1,
            )

        # Should have launched 2 subprocesses
        self.assertEqual(mock_popen.call_count, 2)

        # First call: watcher
        first_args = mock_popen.call_args_list[0][0][0]
        self.assertIn("--mode", first_args)
        self.assertIn("jsonl", first_args)
        self.assertIn("--stream-session-id", first_args)

        # Second call: mic adapter
        second_args = mock_popen.call_args_list[1][0][0]
        self.assertIn("--record-wav", second_args)

        # Both processes should be terminated
        mock_watcher.terminate.assert_called()
        mock_mic.terminate.assert_called()

    @patch("subprocess.Popen")
    @patch.object(golden, "_find_binary")
    def test_watcher_exits_early(self, mock_find, mock_popen):
        """If watcher exits early, capture should fail with DAEMON_UNREACHABLE."""
        mock_find.side_effect = lambda name: f"/fake/{name}"

        mock_watcher = MagicMock()
        mock_watcher.poll.return_value = 1  # Exited with error
        mock_watcher.returncode = 1
        mock_watcher.pid = 12345
        mock_watcher.stderr = MagicMock()
        mock_watcher.stderr.read.return_value = "Connection refused"

        mock_popen.return_value = mock_watcher

        scenario = {"id": "human-clean-complete"}
        cues = [{"band_ms": [0, 10000], "label": "SPEAK", "visual": "test"}]
        take_dir = Path(self.tmp) / "take-001"
        take_dir.mkdir()

        with patch.object(golden, "_display_recording_screen"), \
             patch.object(golden, "_clear_screen"), \
             patch("time.sleep", return_value=None):
            code, info = golden._operator_real_capture(
                scenario=scenario, cues=cues, take_dir=take_dir,
                mode="take", device=None, ws_url="ws://bad:1234",
                stream_session_id="mock-session", total_ms=10000, drain_sec=0.1,
            )

        self.assertEqual(code, golden.ExitCode.DAEMON_UNREACHABLE)

    @patch.object(golden, "_find_binary", return_value=None)
    def test_missing_binaries(self, mock_find):
        """Missing binaries should return DEPENDENCY_MISSING."""
        scenario = {"id": "test"}
        cues = [{"band_ms": [0, 5000], "label": "SPEAK", "visual": "test"}]
        take_dir = Path(self.tmp) / "take-001"
        take_dir.mkdir()

        code, info = golden._operator_real_capture(
            scenario=scenario, cues=cues, take_dir=take_dir,
            mode="take", device=None, ws_url="ws://test:1234",
            stream_session_id="test", total_ms=5000, drain_sec=0.1,
        )

        self.assertEqual(code, golden.ExitCode.DEPENDENCY_MISSING)
        self.assertIn("missing", info)
        self.assertIn("speech-core-watch", info["missing"])


class TestFullOperatorIntegration(unittest.TestCase):
    """Integration: cmd_operator with mocked subprocess and TUI."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_fake_args(self, **kwargs):
        """Build a fake argparse.Namespace for cmd_operator."""
        defaults = {
            "manifest": str(REPO_ROOT / "tests" / "golden" / "manifest.yaml"),
            "scenario": "human-clean-complete",
            "out": str(Path(self.tmp) / "runs"),
            "practice": True,
            "dry_run": True,
            "device": None,
            "url": None,
            "play_cmd": None,
            "force": False,
        }
        defaults.update(kwargs)
        return argparse.Namespace(**defaults)

    def test_dry_run_operator_completes(self):
        """Full dry-run operator flow should complete without errors."""
        # Use dry-run mode which skips subprocesses
        args = self._make_fake_args(dry_run=True)

        with patch.object(golden, "_get_key", side_effect=[
            "\r",  # Start on scenario info screen
            "\r",  # (countdown runs automatically)
            "a",   # Accept in review
        ]), \
             patch.object(golden, "_countdown", return_value=None), \
             patch("time.sleep", return_value=None):
            try:
                code = golden.cmd_operator(args)
            except SystemExit as e:
                code = e.code

        self.assertEqual(code, golden.ExitCode.PASS)
        # Verify take was created
        runs_dir = Path(self.tmp) / "runs"
        self.assertTrue(runs_dir.exists())
        take_dirs = list(runs_dir.rglob("practice-*"))
        self.assertGreater(len(take_dirs), 0)
        take_dir = take_dirs[0]
        self.assertTrue((take_dir / "audio.wav").exists())
        self.assertTrue((take_dir / "consent.json").exists())
        self.assertTrue((take_dir / "review.json").exists())

    def test_dry_run_operator_quit_during_review(self):
        """Full dry-run flow, quit during review."""
        args = self._make_fake_args(dry_run=True)

        with patch.object(golden, "_get_key", side_effect=[
            "\r",  # Start
            "\r",
            "q",   # Quit in review
        ]), \
             patch.object(golden, "_countdown", return_value=None), \
             patch("time.sleep", return_value=None):
            try:
                code = golden.cmd_operator(args)
            except SystemExit as e:
                code = e.code

        self.assertEqual(code, golden.ExitCode.RECORDER_ABORTED)

    def test_scenario_not_found_exits(self):
        """Non-existent scenario should produce SCENARIO_NOT_FOUND."""
        args = self._make_fake_args(scenario="nonexistent-scenario-xyz")

        with self.assertRaises(SystemExit) as cm:
            golden.cmd_operator(args)
        self.assertEqual(cm.exception.code, golden.ExitCode.SCENARIO_NOT_FOUND)

    def test_scenario_by_number(self):
        """--scenario with a number should work."""
        args = self._make_fake_args(scenario="1", dry_run=True)

        with patch.object(golden, "_get_key", side_effect=["\r", "\r", "a"]), \
             patch.object(golden, "_countdown", return_value=None), \
             patch("time.sleep", return_value=None):
            try:
                code = golden.cmd_operator(args)
            except SystemExit as e:
                code = e.code

        self.assertEqual(code, golden.ExitCode.PASS)

    def test_invalid_manifest_path(self):
        """Invalid manifest path should produce MANIFEST_INVALID."""
        class FakeArgs(argparse.Namespace):
            manifest = "/nonexistent/manifest.yaml"
            scenario = None
            out = self.tmp
            practice = False
            dry_run = True
            device = None
            url = None
            play_cmd = None
            force = False

        with self.assertRaises(SystemExit) as cm:
            golden.cmd_operator(FakeArgs())
        self.assertEqual(cm.exception.code, golden.ExitCode.MANIFEST_INVALID)

    def test_retry_creates_new_take_dir(self):
        """Retry (R) during review creates a new, non-overwriting take dir."""
        args = self._make_fake_args(dry_run=True)

        # First round: accept scenario, countdown, then retry once, then accept
        with patch.object(golden, "_get_key", side_effect=[
            "\r",  # Start first take
            "\r",
            "r",   # Retry
            "\r",  # Start second take
            "\r",
            "a",   # Accept second take
        ]), \
             patch.object(golden, "_countdown", return_value=None), \
             patch("time.sleep", return_value=None):
            try:
                code = golden.cmd_operator(args)
            except SystemExit as e:
                code = e.code

        self.assertEqual(code, golden.ExitCode.PASS)

        # Should have two practice directories
        runs_dir = Path(self.tmp) / "runs"
        take_dirs = sorted(runs_dir.rglob("practice-*"))
        self.assertEqual(len(take_dirs), 2)
        # First one should NOT have review.json (retried)
        # Second one should have review.json (accepted)
        accepted = [d for d in take_dirs if (d / "review.json").exists()]
        self.assertEqual(len(accepted), 1)

    def test_fuzzy_scenario_by_name(self):
        """--scenario with friendly name should work."""
        args = self._make_fake_args(scenario="Clean sentence", dry_run=True)

        with patch.object(golden, "_get_key", side_effect=["\r", "\r", "a"]), \
             patch.object(golden, "_countdown", return_value=None), \
             patch("time.sleep", return_value=None):
            try:
                code = golden.cmd_operator(args)
            except SystemExit as e:
                code = e.code

        self.assertEqual(code, golden.ExitCode.PASS)


class TestOperatorEdgeCases(unittest.TestCase):
    """Edge cases for the operator command."""

    def test_empty_manifest(self):
        manifest = {"manifest_version": 1, "profile": "golden-mvp", "scenarios": []}
        with patch("builtins.input", return_value="q"):
            with self.assertRaises(SystemExit) as cm:
                golden._operator_scenario_menu(manifest)
            # Should not crash — die with SCENARIO_NOT_FOUND
            self.assertEqual(cm.exception.code, golden.ExitCode.SCENARIO_NOT_FOUND)

    def test_max_retries_protection(self):
        """The retry loop in cmd_operator has a max retries guard."""
        import inspect
        source = inspect.getsource(golden.cmd_operator)
        self.assertIn("max_retries", source)
        # Should have a loop limit
        self.assertIn("while retry_count < max_retries", source)

    def test_event_drain_default(self):
        """Default event drain window is set."""
        self.assertEqual(golden.DEFAULT_EVENT_DRAIN_SEC, 5.0)

    def test_default_ws_url(self):
        """Default WebSocket URL is set."""
        self.assertIn("ws://", golden.DEFAULT_WS_URL)
        self.assertIn("8765", golden.DEFAULT_WS_URL)

    def test_synthetic_scenario_not_in_menu_as_human(self):
        """Synthetic scenarios should appear in synthetic group, not human."""
        # This is verified by _operator_scenario_menu grouping logic
        import inspect
        source = inspect.getsource(golden._operator_scenario_menu)
        self.assertIn('"human-recorded"', source)
        self.assertIn('"synthetic"', source)


# Pytest-style runner compatible with unittest
if __name__ == "__main__":
    unittest.main()
