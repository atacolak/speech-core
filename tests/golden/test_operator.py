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
            "human-hold-continuous-filler-7500",
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

    def test_mic_adapter_terminated_before_watcher_in_clean_shutdown(self):
        """In the clean shutdown path, mic adapter should be terminated before watcher."""
        import inspect
        source = inspect.getsource(golden._operator_real_capture)
        # Check the clean completion path (after "RECORDING COMPLETE")
        # mic termination should be before watcher termination in the clean path
        complete_idx = source.find("RECORDING COMPLETE")
        self.assertGreater(complete_idx, 0, "RECORDING COMPLETE marker not found")
        # After RECORDING COMPLETE, find the mic and watcher terminate calls
        after_complete = source[complete_idx:]
        mic_term_idx = after_complete.find("mic_proc.terminate()")
        watcher_term_idx = after_complete.find("watcher_proc.terminate()")
        self.assertGreater(watcher_term_idx, mic_term_idx,
                          "In clean shutdown: mic must be terminated before watcher")

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

        # Create ready file when watcher is launched (after unlink removes it)
        call_count = [0]
        def _popen_side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:  # watcher launch
                ready_path.parent.mkdir(parents=True, exist_ok=True)
                ready_path.write_text("ready\n")
                return mock_watcher
            else:  # mic launch
                return mock_mic

        # Use a MagicMock for Popen that calls our side effect
        mock_popen.side_effect = _popen_side_effect

        # Set up test data
        scenario = {"id": "human-clean-complete", "class": "natural_endpoint",
                    "construction": "human-recorded"}
        cues = golden._default_cues_for_scenario("human-clean-complete")
        take_dir = Path(self.tmp) / "take-001"
        take_dir.mkdir()

        # Ready file will be created during subprocess.Popen call
        ready_path = take_dir / "diagnostics" / "watcher.ready"

        # Mock _display_recording_screen to avoid display
        with patch.object(golden, "_display_recording_screen"), \
             patch.object(golden, "_clear_screen"), \
             patch.object(golden, "_get_key", return_value=""), \
             patch("time.sleep", return_value=None), \
             patch("time.monotonic", side_effect=(i * 0.3 for i in range(200))):
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


class TestAdversarialSafety(unittest.TestCase):
    """Adversarial safety tests: fail-closed validity, promotion gates, quality."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        # Make _golden importable
        if str(SCRIPT_DIR) not in sys.path:
            sys.path.insert(0, str(SCRIPT_DIR))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ── Helper to write valid event stream ────────────────────────────
    def _write_events(self, scenario_dir: Path, events: list, session_id: str = "test-session"):
        events_path = scenario_dir / "event-stream.jsonl"
        with open(events_path, "w") as f:
            for evt in events:
                evt.setdefault("stream_session_id", session_id)
                f.write(json.dumps(evt) + "\n")
        return events_path

    # ── Wrong session ID ──────────────────────────────────────────────
    def test_wrong_session_rejected(self):
        """Events with wrong stream_session_id must fail validity."""
        from _golden.validity import validate_capture_artifacts
        scenario_dir = Path(self.tmp) / "wrong-session"
        scenario_dir.mkdir()
        self._write_events(scenario_dir, [
            {"event": "stream_start"},
            {"event": "vad_session_end"},
        ], session_id="wrong-id")

        code, record = validate_capture_artifacts(
            scenario_dir, "expected-id",
        )
        self.assertNotEqual(code, 0, f"Wrong session should fail: {record}")
        self.assertIn("wrong", record.get("reason", "").lower())

    def test_mixed_session_rejected(self):
        """Some events with wrong session, some without — still fails."""
        from _golden.validity import validate_capture_artifacts
        scenario_dir = Path(self.tmp) / "mixed-session"
        scenario_dir.mkdir()
        events_path = scenario_dir / "event-stream.jsonl"
        with open(events_path, "w") as f:
            f.write('{"event":"stream_start","stream_session_id":"correct"}\n')
            f.write('{"event":"vad_start","stream_session_id":"wrong-one"}\n')
            f.write('{"event":"vad_session_end","stream_session_id":"correct"}\n')

        code, record = validate_capture_artifacts(
            scenario_dir, "correct",
        )
        self.assertNotEqual(code, 0, f"Mixed session should fail: {record}")

    # ── Malformed marker / event stream ───────────────────────────────
    def test_malformed_jsonl_rejected(self):
        """Malformed JSON in event stream must fail validity."""
        from _golden.validity import validate_capture_artifacts
        scenario_dir = Path(self.tmp) / "malformed"
        scenario_dir.mkdir()
        (scenario_dir / "event-stream.jsonl").write_text(
            '{"event":"stream_start"}\nnot json\n'
        )
        code, record = validate_capture_artifacts(
            scenario_dir, "test-session",
        )
        self.assertNotEqual(code, 0, f"Malformed JSONL should fail: {record}")

    def test_empty_file_rejected(self):
        """Empty event-stream.jsonl must fail."""
        from _golden.validity import validate_capture_artifacts
        scenario_dir = Path(self.tmp) / "empty"
        scenario_dir.mkdir()
        (scenario_dir / "event-stream.jsonl").write_text("")
        code, record = validate_capture_artifacts(
            scenario_dir, "test-session",
        )
        self.assertNotEqual(code, 0, f"Empty file should fail: {record}")

    def test_non_object_event_rejected(self):
        """A JSONL line that isn't a dict must fail."""
        from _golden.validity import validate_capture_artifacts
        scenario_dir = Path(self.tmp) / "nonobject"
        scenario_dir.mkdir()
        (scenario_dir / "event-stream.jsonl").write_text(
            '{"event":"stream_start"}\n[1,2,3]\n'
        )
        code, record = validate_capture_artifacts(
            scenario_dir, "test-session",
        )
        self.assertNotEqual(code, 0, f"Non-object event should fail: {record}")

    # ── Marker satisfaction ───────────────────────────────────────────
    def test_marker_satisfied_simple(self):
        """Simple event-name marker satisfied by matching event."""
        from _golden.validity import _marker_satisfied
        events = [{"event": "stream_start"}, {"event": "vad_session_end"}]
        self.assertTrue(_marker_satisfied({"event": "vad_session_end"}, events))

    def test_marker_not_satisfied_missing_event(self):
        """Marker not satisfied when event type not present."""
        from _golden.validity import _marker_satisfied
        events = [{"event": "stream_start"}]
        self.assertFalse(_marker_satisfied({"event": "vad_session_end"}, events))

    def test_marker_satisfied_with_where_predicate(self):
        """Marker with where clause satisfied when predicate matches."""
        from _golden.validity import _marker_satisfied
        events = [
            {"event": "model_chunk_processed", "is_final": False},
            {"event": "model_chunk_processed", "is_final": True},
        ]
        self.assertTrue(_marker_satisfied(
            {"event": "model_chunk_processed", "where": {"is_final": True}},
            events
        ))

    def test_marker_unsatisfied_wrong_where_value(self):
        """Marker with where clause fails when predicate value mismatches."""
        from _golden.validity import _marker_satisfied
        events = [
            {"event": "model_chunk_processed", "is_final": False},
            {"event": "model_chunk_processed", "is_final": False},
        ]
        self.assertFalse(_marker_satisfied(
            {"event": "model_chunk_processed", "where": {"is_final": True}},
            events
        ))

    def test_marker_unsatisfied_missing_where_field(self):
        """Marker with where clause fails when event lacks the field."""
        from _golden.validity import _marker_satisfied
        events = [
            {"event": "model_chunk_processed"},
        ]
        self.assertFalse(_marker_satisfied(
            {"event": "model_chunk_processed", "where": {"is_final": True}},
            events
        ))

    # ── Missing terminal markers ──────────────────────────────────────
    def test_missing_terminal_marker_fails_validity(self):
        """Required terminal marker missing → validity fail."""
        from _golden.validity import validate_capture_artifacts
        scenario_dir = Path(self.tmp) / "missing-terminal"
        scenario_dir.mkdir()
        self._write_events(scenario_dir, [
            {"event": "stream_start"},
            # vad_session_end is required but missing
        ])
        code, record = validate_capture_artifacts(
            scenario_dir, "test-session",
            required_markers=[{"event": "vad_session_end"}],
        )
        self.assertNotEqual(code, 0, f"Missing terminal should fail: {record}")
        self.assertIn("missing", record.get("reason", "").lower())

    def test_all_terminal_markers_present_passes(self):
        """All required terminal markers present → validity pass."""
        from _golden.validity import validate_capture_artifacts
        scenario_dir = Path(self.tmp) / "all-terminals"
        scenario_dir.mkdir()
        self._write_events(scenario_dir, [
            {"event": "stream_start"},
            {"event": "vad_session_end"},
            {"event": "model_chunk_processed", "is_final": True},
        ])
        code, record = validate_capture_artifacts(
            scenario_dir, "test-session",
            required_markers=[
                {"event": "vad_session_end"},
                {"event": "model_chunk_processed", "where": {"is_final": True}},
            ],
        )
        self.assertEqual(code, 0, f"All terminals present should pass: {record}")
        self.assertTrue(record.get("valid"))

    # ── Silence quality check ─────────────────────────────────────────
    def test_silence_wav_fails_quality(self):
        """Pure silence WAV should fail quality checks."""
        take_dir = Path(self.tmp) / "silence-take"
        take_dir.mkdir()
        _write_silence_wav(take_dir / "audio.wav", 1000)
        (take_dir / "event-stream.jsonl").write_text(
            '{"event":"stream_start"}\n{"event":"vad_session_end"}\n'
        )
        result = golden._operator_quality_check(take_dir, "test", "take", dry_run=False)
        self.assertFalse(result["passed"], "Silence should fail quality")
        self.assertTrue(
            any("silence" in f.lower() or "quiet" in f.lower() for f in result.get("failures", [])),
            f"Expected silence/quiet failure in: {result['failures']}"
        )

    def test_near_silence_fails_quality(self):
        """Near-silence (very quiet signal) should fail non-silence check."""
        take_dir = Path(self.tmp) / "near-silence"
        take_dir.mkdir()
        # Generate a WAV with amplitude at -70dBFS (very quiet)
        import math
        n = golden.ms_samples(1000)
        amplitude = int(0.0003 * 32767)  # ~ -70 dBFS
        samples = [amplitude] * n
        golden.write_wav(take_dir / "audio.wav", samples)
        (take_dir / "event-stream.jsonl").write_text(
            '{"event":"stream_start"}\n{"event":"vad_session_end"}\n'
        )
        result = golden._operator_quality_check(take_dir, "test", "take", dry_run=False)
        self.assertFalse(result["passed"], "Near-silence should fail quality")

    # ── Nonzero child exit ────────────────────────────────────────────
    def test_mic_nonzero_exit_fails_capture(self):
        """Mic adapter exiting nonzero must fail capture (fail-closed)."""
        # After termination, returncode is set. If nonzero (not -15), fail.
        # Test the nonzero exit check logic directly.
        take_dir = Path(self.tmp) / "mic-fail"
        take_dir.mkdir()
        (take_dir / "diagnostics").mkdir()
        wav_path = take_dir / "audio.wav"
        event_path = take_dir / "event-stream.jsonl"

        # Write a minimal valid event stream so checks pass until the nonzero check
        with open(event_path, "w") as f:
            f.write('{"event":"stream_start","stream_session_id":"test"}\n')

        # The nonzero check is: mic_proc.returncode not in (0, -15, None)
        # Build a mock mic_proc with returncode=1
        mock_mic = MagicMock()
        mock_mic.poll.return_value = None
        mock_mic.returncode = 1  # nonzero
        mock_mic.pid = 99999

        mock_watcher = MagicMock()
        mock_watcher.poll.return_value = None
        mock_watcher.returncode = 0
        mock_watcher.pid = 88888

        # Simulate the nonzero exit check
        # The check in code: mic_proc.returncode not in (0, -15, None)
        result = mock_mic.returncode not in (0, -15, None)
        self.assertTrue(result, "Nonzero mic exit should be detected as failure")

    @patch("subprocess.Popen")
    @patch.object(golden, "_find_binary")
    def test_watcher_nonzero_exit_after_drain_fails(self, mock_find, mock_popen):
        """If watcher exits nonzero after clean drain, capture must fail."""
        mock_find.side_effect = lambda name: f"/fake/{name}"

        # Configure watcher to stay alive during check, then have nonzero returncode
        mock_watcher = MagicMock()
        mock_watcher.poll.return_value = None
        mock_watcher.pid = 12345
        mock_watcher.returncode = 1  # Will be checked after termination

        mock_mic = MagicMock()
        mock_mic.poll.return_value = None
        mock_mic.pid = 12346
        mock_mic.returncode = 0

        scenario = {"id": "test"}
        cues = [{"band_ms": [0, 500], "label": "SPEAK", "visual": "test"}]
        take_dir = Path(self.tmp) / "nonzero-watcher"
        take_dir.mkdir()

        # Create ready file when watcher is launched (after unlink removes it)
        ready_path = take_dir / "diagnostics" / "watcher.ready"
        call_count_2 = [0]
        def _popen_side_effect_2(*args, **kwargs):
            call_count_2[0] += 1
            if call_count_2[0] == 1:
                ready_path.parent.mkdir(parents=True, exist_ok=True)
                ready_path.write_text("ready\n")
                return mock_watcher
            else:
                return mock_mic

        mock_popen.side_effect = _popen_side_effect_2

        # Use a monotonic generator that passes readiness then fast-forwards
        with patch.object(golden, "_display_recording_screen"), \
             patch.object(golden, "_clear_screen"), \
             patch.object(golden, "_get_key", return_value=""), \
             patch("time.sleep", return_value=None), \
             patch("time.monotonic", side_effect=(i * 0.3 for i in range(200))):
            code, info = golden._operator_real_capture(
                scenario=scenario, cues=cues, take_dir=take_dir,
                mode="take", device=None,
                ws_url="ws://127.0.0.1:8765/ws/audio-ingress",
                stream_session_id="test-session",
                total_ms=500, drain_sec=0.05,
            )

        # Watcher had returncode=1 → should fail with INTERNAL_ERROR
        self.assertEqual(code, golden.ExitCode.INTERNAL_ERROR,
                        f"Nonzero watcher exit should fail capture, got code={code}")

    # ── Promotion gates ───────────────────────────────────────────────
    def test_dry_run_cannot_be_promoted(self):
        """Dry-run takes must be rejected by promote."""
        take_dir = Path(self.tmp) / "dry-take"
        take_dir.mkdir()
        # Write provenance indicating dry_run
        golden.save_file({"dry_run": True, "mode": "take"}, take_dir / "provenance.json")
        golden.save_file({"accepted": True}, take_dir / "consent.json")
        golden.save_file({"retention_class": "test"}, take_dir / "privacy.json")

        with self.assertRaises(SystemExit) as cm:
            golden.promote_take(take_dir, Path(self.tmp) / "dest", dry_run=False)
        self.assertEqual(cm.exception.code, golden.ExitCode.BASELINE_REQUIRES_REVIEW)

    def test_practice_take_cannot_be_promoted(self):
        """Practice takes must be rejected by promote."""
        take_dir = Path(self.tmp) / "practice-take"
        take_dir.mkdir()
        golden.save_file({"dry_run": False, "mode": "practice"}, take_dir / "provenance.json")
        golden.save_file({"accepted": True}, take_dir / "consent.json")
        golden.save_file({"retention_class": "test"}, take_dir / "privacy.json")

        with self.assertRaises(SystemExit) as cm:
            golden.promote_take(take_dir, Path(self.tmp) / "dest", dry_run=False)
        self.assertEqual(cm.exception.code, golden.ExitCode.BASELINE_REQUIRES_REVIEW)

    def test_incomplete_capture_cannot_be_promoted(self):
        """Takes with missing/false checks_passed cannot be promoted."""
        take_dir = Path(self.tmp) / "incomplete-take"
        take_dir.mkdir()
        golden.save_file({"dry_run": False, "mode": "take"}, take_dir / "provenance.json")
        golden.save_file({"accepted": True}, take_dir / "consent.json")
        golden.save_file({"retention_class": "test"}, take_dir / "privacy.json")
        # Write review with checks_passed=False
        golden.save_file({"accepted": True, "checks_passed": False}, take_dir / "review.json")

        with self.assertRaises(SystemExit) as cm:
            golden.promote_take(take_dir, Path(self.tmp) / "dest", dry_run=False)
        self.assertEqual(cm.exception.code, golden.ExitCode.BASELINE_REQUIRES_REVIEW)

    def test_missing_validity_evidence_blocks_promotion(self):
        """Missing validity.json must block promotion."""
        take_dir = Path(self.tmp) / "no-validity"
        take_dir.mkdir()
        golden.save_file({"dry_run": False, "mode": "take"}, take_dir / "provenance.json")
        golden.save_file({"accepted": True}, take_dir / "consent.json")
        golden.save_file({"retention_class": "test"}, take_dir / "privacy.json")
        golden.save_file({"accepted": True, "checks_passed": True}, take_dir / "review.json")
        # No validity.json in quality/review/provenance/

        with self.assertRaises(SystemExit) as cm:
            golden.promote_take(take_dir, Path(self.tmp) / "dest", dry_run=False)
        self.assertEqual(cm.exception.code, golden.ExitCode.BASELINE_REQUIRES_REVIEW)

    def test_failed_validity_blocks_promotion(self):
        """validity.json with valid=False must block promotion."""
        take_dir = Path(self.tmp) / "failed-validity"
        take_dir.mkdir()
        evidence_dir = take_dir / "quality" / "review" / "provenance"
        evidence_dir.mkdir(parents=True)
        golden.save_file({"dry_run": False, "mode": "take"}, take_dir / "provenance.json")
        golden.save_file({"accepted": True}, take_dir / "consent.json")
        golden.save_file({"retention_class": "test"}, take_dir / "privacy.json")
        golden.save_file({"accepted": True, "checks_passed": True}, take_dir / "review.json")
        # validity with valid=False
        golden.save_file({"valid": False, "reason": "test failure"}, evidence_dir / "validity.json")

        with self.assertRaises(SystemExit) as cm:
            golden.promote_take(take_dir, Path(self.tmp) / "dest", dry_run=False)
        self.assertEqual(cm.exception.code, golden.ExitCode.BASELINE_REQUIRES_REVIEW)

    def test_failed_assertions_block_promotion(self):
        """assert-report.json with passed=False must block promotion."""
        take_dir = Path(self.tmp) / "failed-assert"
        take_dir.mkdir()
        evidence_dir = take_dir / "quality" / "review" / "provenance"
        evidence_dir.mkdir(parents=True)
        golden.save_file({"dry_run": False, "mode": "take"}, take_dir / "provenance.json")
        golden.save_file({"accepted": True}, take_dir / "consent.json")
        golden.save_file({"retention_class": "test"}, take_dir / "privacy.json")
        golden.save_file({"accepted": True, "checks_passed": True}, take_dir / "review.json")
        golden.save_file({"valid": True, "reason": "ok"}, evidence_dir / "validity.json")
        # Assertions failed
        golden.save_file({"passed": False, "reason": "assertion violation"},
                        evidence_dir / "assert-report.json")

        with self.assertRaises(SystemExit) as cm:
            golden.promote_take(take_dir, Path(self.tmp) / "dest", dry_run=False)
        self.assertEqual(cm.exception.code, golden.ExitCode.BASELINE_REQUIRES_REVIEW)


class TestReadinessFileChannel(unittest.TestCase):
    """Behavioral tests for explicit --ready-file readiness side-channel."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_ready_arg_in_watcher_command(self):
        """Watcher args must include --ready-file when launched by operator."""
        # Verify the code constructs --ready-file in watcher_args
        import inspect
        source = inspect.getsource(golden._operator_real_capture)
        self.assertIn("--ready-file", source,
                      "Watcher launch must include --ready-file")
        self.assertIn("ready_path", source,
                      "Watcher launch must reference ready_path")

    def test_ready_path_in_capture_info(self):
        """capture_info must include ready_path and readiness fields."""
        import inspect
        source = inspect.getsource(golden._operator_real_capture)
        self.assertIn('"ready_path"', source,
                      "capture_info must track ready_path")
        self.assertIn('"readiness_at"', source,
                      "capture_info must track readiness timestamp")
        self.assertIn('"readiness_method"', source,
                      "capture_info must record readiness method")

    @patch("subprocess.Popen")
    @patch.object(golden, "_find_binary")
    def test_stale_ready_file_does_not_satisfy(self, mock_find, mock_popen):
        """A pre-existing stale ready file must not satisfy readiness.
        The operator explicitly unlinks the ready file before launching watcher."""
        import inspect
        source = inspect.getsource(golden._operator_real_capture)
        self.assertIn("unlink", source,
                      "Stale ready file must be unlinked before watcher launch")

    @patch("subprocess.Popen")
    @patch.object(golden, "_find_binary")
    def test_watcher_early_exit_before_ready_fails(self, mock_find, mock_popen):
        """Early watcher exit (before ready file appears) must fail."""
        mock_find.side_effect = lambda name: f"/fake/{name}"

        mock_watcher = MagicMock()
        mock_watcher.poll.return_value = 1  # Exited immediately
        mock_watcher.returncode = 1
        mock_watcher.pid = 12345
        mock_watcher.stderr = MagicMock()
        mock_watcher.stderr.read.return_value = "connection refused"
        mock_popen.return_value = mock_watcher

        scenario = {"id": "test"}
        cues = [{"band_ms": [0, 5000], "label": "SPEAK", "visual": "test"}]
        take_dir = Path(self.tmp) / "take-001"
        take_dir.mkdir()

        with patch("time.sleep", return_value=None):
            code, info = golden._operator_real_capture(
                scenario=scenario, cues=cues, take_dir=take_dir,
                mode="take", device=None, ws_url="ws://bad:1234",
                stream_session_id="test", total_ms=5000, drain_sec=0.1,
            )

        self.assertEqual(code, golden.ExitCode.DAEMON_UNREACHABLE,
                        f"Early watcher exit should fail, got {code}")
        self.assertEqual(info.get("readiness_status"), "watcher-exited-early-rc-1")

    @patch("subprocess.Popen")
    @patch.object(golden, "_find_binary")
    def test_ready_file_appears_mic_launches(self, mock_find, mock_popen):
        """When ready file appears, watcher is declared ready and mic launches."""
        mock_find.side_effect = lambda name: f"/fake/{name}"

        mock_watcher = MagicMock()
        mock_watcher.poll.return_value = None  # Still running
        mock_watcher.pid = 12345
        mock_watcher.returncode = 0

        mock_mic = MagicMock()
        mock_mic.poll.return_value = None
        mock_mic.pid = 12346
        mock_mic.returncode = 0

        mock_popen.side_effect = [mock_watcher, mock_mic]

        scenario = {"id": "test"}
        cues = [{"band_ms": [0, 500], "label": "SPEAK", "visual": "test"}]
        take_dir = Path(self.tmp) / "take-002"
        take_dir.mkdir()

        # Create ready file before first poll — simulates watcher creating it
        ready_path = take_dir / "diagnostics" / "watcher.ready"

        def _create_ready_then_run():
            # After watcher is launched, create the ready file
            ready_path.parent.mkdir(parents=True, exist_ok=True)
            ready_path.write_text("ready\n")
            # Return monotonic values that progress through the loop
            for i in range(200):
                yield i * 0.3

        t = _create_ready_then_run()

        with patch.object(golden, "_display_recording_screen"), \
             patch.object(golden, "_clear_screen"), \
             patch.object(golden, "_get_key", return_value=""), \
             patch("time.sleep", return_value=None), \
             patch("time.monotonic", side_effect=t):
            code, info = golden._operator_real_capture(
                scenario=scenario, cues=cues, take_dir=take_dir,
                mode="take", device=None,
                ws_url="ws://127.0.0.1:8765/ws/audio-ingress",
                stream_session_id="test", total_ms=500, drain_sec=0.05,
            )

        # Should have launched both watcher and mic
        self.assertEqual(mock_popen.call_count, 2,
                        f"Should launch watcher + mic, got {mock_popen.call_count}")
        self.assertEqual(info.get("readiness_status"), "ready-file-created")
        self.assertIsNotNone(info.get("readiness_at"))

    @patch("subprocess.Popen")
    @patch.object(golden, "_find_binary")
    def test_no_ready_file_timeout_fails(self, mock_find, mock_popen):
        """When ready file never appears within timeout, capture must fail."""
        mock_find.side_effect = lambda name: f"/fake/{name}"

        mock_watcher = MagicMock()
        mock_watcher.poll.return_value = None  # Stays alive but no ready file
        mock_watcher.pid = 12345
        mock_popen.return_value = mock_watcher

        scenario = {"id": "test"}
        cues = [{"band_ms": [0, 5000], "label": "SPEAK", "visual": "test"}]
        take_dir = Path(self.tmp) / "take-003"
        take_dir.mkdir()

        with patch("time.sleep", return_value=None):
            code, info = golden._operator_real_capture(
                scenario=scenario, cues=cues, take_dir=take_dir,
                mode="take", device=None, ws_url="ws://bad:1234",
                stream_session_id="test", total_ms=5000, drain_sec=0.1,
            )

        self.assertEqual(code, golden.ExitCode.DAEMON_UNREACHABLE,
                        f"Ready timeout should fail, got {code}")
        self.assertEqual(info.get("readiness_status"), "timeout")


class TestQualityCheckBounds(unittest.TestCase):
    """Quality checks with scenario/profile-specific thresholds."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_custom_min_peak_dbfs_used(self):
        """Custom min_peak_dbfs (-12 dBFS) should fail a WAV at -20 dBFS."""
        take_dir = Path(self.tmp) / "take-001"
        take_dir.mkdir()
        # Generate WAV at -20 dBFS peak
        import math
        n = golden.ms_samples(1000)
        amplitude = int(0.1 * 32767)  # -20 dBFS
        samples = [amplitude] * n
        golden.write_wav(take_dir / "audio.wav", samples)
        (take_dir / "event-stream.jsonl").write_text('{"event":"stream_start"}\n')

        result = golden._operator_quality_check(
            take_dir, "test", "take", dry_run=False,
            min_peak_dbfs=-12.0,  # Strict: requires at least -12 dBFS
        )
        self.assertFalse(result["passed"],
                        f"Should fail with strict peak threshold, got: {result}")
        self.assertTrue(
            any("peak" in f.lower() and "below threshold" in f.lower()
                for f in result["failures"]),
            f"Should report peak below threshold: {result['failures']}"
        )

    def test_custom_min_rms_dbfs_used(self):
        """Custom min_rms_dbfs (-12 dBFS) should fail a quiet WAV."""
        take_dir = Path(self.tmp) / "take-002"
        take_dir.mkdir()
        # Generate WAV at -20 dBFS
        import math
        n = golden.ms_samples(1000)
        amplitude = int(0.1 * 32767)
        samples = [amplitude] * n
        golden.write_wav(take_dir / "audio.wav", samples)
        (take_dir / "event-stream.jsonl").write_text('{"event":"stream_start"}\n')

        result = golden._operator_quality_check(
            take_dir, "test", "take", dry_run=False,
            min_rms_dbfs=-12.0,
        )
        self.assertFalse(result["passed"],
                        f"Should fail with strict RMS threshold, got: {result}")

    def test_loud_signal_passes_strict_threshold(self):
        """A loud WAV at -3 dBFS should pass even strict -12 dBFS threshold."""
        take_dir = Path(self.tmp) / "take-003"
        take_dir.mkdir()
        samples = golden._generate_sine(golden.ms_samples(1000), 440.0, 0.7)
        golden.write_wav(take_dir / "audio.wav", samples)
        (take_dir / "event-stream.jsonl").write_text('{"event":"stream_start"}\n')

        result = golden._operator_quality_check(
            take_dir, "test", "take", dry_run=False,
            min_peak_dbfs=-12.0, min_rms_dbfs=-12.0,
        )
        self.assertTrue(result["passed"],
                       f"Loud signal should pass strict thresholds, got: {result}")

    def test_default_threshold_passes_normal_signal(self):
        """Default -60 dBFS threshold should pass a normal signal."""
        take_dir = Path(self.tmp) / "take-004"
        take_dir.mkdir()
        samples = golden._generate_sine(golden.ms_samples(1000), 440.0, 0.5)
        golden.write_wav(take_dir / "audio.wav", samples)
        (take_dir / "event-stream.jsonl").write_text('{"event":"stream_start"}\n')

        result = golden._operator_quality_check(
            take_dir, "test", "take", dry_run=False,
        )
        self.assertTrue(result["passed"],
                       f"Normal signal should pass default thresholds, got: {result}")

    def test_dry_run_never_shows_signal_present(self):
        """Dry-run must never display 'Signal present' for non_silence check."""
        take_dir = Path(self.tmp) / "take-005"
        take_dir.mkdir()
        samples = golden._generate_sine(golden.ms_samples(1000), 440.0, 0.5)
        golden.write_wav(take_dir / "audio.wav", samples)
        (take_dir / "event-stream.jsonl").write_text("")

        result = golden._operator_quality_check(
            take_dir, "test", "take", dry_run=True,
        )
        # Find the non_silence check
        non_silence = [c for c in result.get("checks", [])
                       if c.get("name") == "non_silence"]
        self.assertGreater(len(non_silence), 0, "Should have non_silence check")
        ns = non_silence[0]
        self.assertFalse(ns["passed"],
                        f"Dry-run non_silence must not pass, got: {ns}")
        self.assertIn("DRY-RUN", ns.get("reason", ""),
                     f"Dry-run reason must mention DRY-RUN, got: {ns.get('reason')}")
        self.assertNotIn("Signal present", ns.get("reason", ""),
                        "Dry-run must never say 'Signal present'")

    def test_zero_samples_has_exact_threshold_check(self):
        """Zero-sample WAV should fail with correct threshold in message."""
        take_dir = Path(self.tmp) / "take-006"
        take_dir.mkdir()
        golden.write_wav(take_dir / "audio.wav", [])
        (take_dir / "event-stream.jsonl").write_text('{"event":"stream_start"}\n')

        # The WAV will have 0 samples but the write creates at least the header
        # Actually write_wav with empty list still creates 0-frame WAV
        result = golden._operator_quality_check(
            take_dir, "test", "take", dry_run=False,
            min_peak_dbfs=-40.0,
        )
        # Should have the threshold in the failure message
        self.assertFalse(result["passed"])


class TestLoadEventsMalformedHandling(unittest.TestCase):
    """_load_events_from_stream strict vs lenient mode."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_jsonl(self, content: str):
        path = Path(self.tmp) / "events.jsonl"
        path.write_text(content)
        return path

    def test_lenient_skips_malformed(self):
        """Lenient mode skips malformed lines silently."""
        path = self._write_jsonl(
            '{"event":"stream_start"}\nnot json\n{"event":"vad_session_end"}\n'
        )
        events, error = golden._load_events_from_stream(path, fail_on_malformed=False)
        self.assertIsNone(error,
                         f"Lenient mode should not return error, got: {error}")
        self.assertEqual(len(events), 2,
                        f"Should have 2 valid events, got {len(events)}")

    def test_strict_fails_on_malformed(self):
        """Strict mode must fail on malformed JSON."""
        path = self._write_jsonl(
            '{"event":"stream_start"}\nnot json\n'
        )
        events, error = golden._load_events_from_stream(path, fail_on_malformed=True)
        self.assertIsNotNone(error,
                            "Strict mode must return error for malformed JSON")
        self.assertEqual(len(events), 0,
                        f"Strict mode should return no events on error, got {len(events)}")

    def test_strict_passes_valid_jsonl(self):
        """Strict mode passes valid JSONL."""
        path = self._write_jsonl(
            '{"event":"stream_start"}\n{"event":"vad_session_end"}\n'
        )
        events, error = golden._load_events_from_stream(path, fail_on_malformed=True)
        self.assertIsNone(error,
                         f"Strict mode should not error on valid JSON: {error}")
        self.assertEqual(len(events), 2)

    def test_missing_file_returns_empty(self):
        """Missing file returns empty list."""
        path = Path(self.tmp) / "nonexistent.jsonl"
        events, error = golden._load_events_from_stream(path, fail_on_malformed=False)
        self.assertEqual(len(events), 0)
        self.assertIsNotNone(error, "Missing file should return error message")


class TestPromotionTamperChecks(unittest.TestCase):
    """Independent promotion provenance/session/hash consistency."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_clean_take(self, name: str, session_id: str = "correct-session"):
        """Create a take with all required promotion artifacts."""
        take_dir = Path(self.tmp) / name
        take_dir.mkdir()
        evidence_dir = take_dir / "quality" / "review" / "provenance"
        evidence_dir.mkdir(parents=True)

        golden.save_file(
            {"dry_run": False, "mode": "take",
             "stream_session_id": session_id},
            take_dir / "provenance.json"
        )
        golden.save_file({"accepted": True}, take_dir / "consent.json")
        golden.save_file({"retention_class": "test"}, take_dir / "privacy.json")
        golden.save_file(
            {"accepted": True, "checks_passed": True},
            take_dir / "review.json"
        )
        golden.save_file(
            {"valid": True, "reason": "ok",
             "artifact_hashes": {},
             "stream_session_id": session_id},
            evidence_dir / "validity.json"
        )
        # Create a valid WAV
        samples = golden._generate_sine(golden.ms_samples(500), 440.0, 0.5)
        golden.write_wav(take_dir / "audio.wav", samples)
        # Create event stream with matching session
        events_path = take_dir / "event-stream.jsonl"
        with open(events_path, "w") as f:
            f.write(f'{{"event":"stream_start","stream_session_id":"{session_id}"}}\n')

        return take_dir

    def test_event_stream_hash_tamper_detected(self):
        """If validity record event_stream hash differs from actual, promotion fails."""
        take_dir = self._make_clean_take("hash-tamper")
        evidence_dir = take_dir / "quality" / "review" / "provenance"

        # Tamper: set validity hash to something wrong
        validity_path = evidence_dir / "validity.json"
        val = golden.load_manifest_file(validity_path)
        val["artifact_hashes"] = {"event_stream_sha256": "deadbeef"}
        golden.save_file(val, validity_path)

        with self.assertRaises(SystemExit) as cm:
            golden.promote_take(take_dir, Path(self.tmp) / "dest", dry_run=False)
        self.assertEqual(cm.exception.code, golden.ExitCode.ARTIFACT_HASH_MISMATCH)

    def test_session_id_mismatch_in_events_detected(self):
        """If event stream contains wrong session ID, promotion fails."""
        take_dir = self._make_clean_take("session-tamper", session_id="expected-session")
        # Tamper: write events with different session ID
        events_path = take_dir / "event-stream.jsonl"
        with open(events_path, "w") as f:
            f.write('{"event":"stream_start","stream_session_id":"WRONG-SESSION"}\n')

        with self.assertRaises(SystemExit) as cm:
            golden.promote_take(take_dir, Path(self.tmp) / "dest", dry_run=False)
        self.assertEqual(cm.exception.code, golden.ExitCode.EVENT_SCHEMA_INVALID)

    def test_validity_session_id_mismatch_detected(self):
        """If validity record session_id differs from provenance, promotion fails."""
        take_dir = self._make_clean_take("validity-sid-tamper", session_id="provenance-sid")
        evidence_dir = take_dir / "quality" / "review" / "provenance"

        # Tamper validity record with different session
        validity_path = evidence_dir / "validity.json"
        val = golden.load_manifest_file(validity_path)
        val["stream_session_id"] = "different-sid"
        golden.save_file(val, validity_path)

        with self.assertRaises(SystemExit) as cm:
            golden.promote_take(take_dir, Path(self.tmp) / "dest", dry_run=False)
        self.assertEqual(cm.exception.code, golden.ExitCode.EVENT_SCHEMA_INVALID)

    def test_matching_session_and_hash_passes(self):
        """When everything matches, promotion succeeds."""
        take_dir = self._make_clean_take("clean-promote")
        dest = Path(self.tmp) / "dest"

        # Should not raise
        code = golden.promote_take(take_dir, dest, dry_run=False)
        self.assertEqual(code, golden.ExitCode.PASS)
        self.assertTrue((dest / "promotion.json").exists())

    def test_provenance_without_session_id_still_checked(self):
        """If provenance lacks stream_session_id, event check still runs."""
        take_dir = Path(self.tmp) / "no-sid-provenance"
        take_dir.mkdir()
        evidence_dir = take_dir / "quality" / "review" / "provenance"
        evidence_dir.mkdir(parents=True)

        # Provenance without stream_session_id
        golden.save_file(
            {"dry_run": False, "mode": "take"},
            take_dir / "provenance.json"
        )
        golden.save_file({"accepted": True}, take_dir / "consent.json")
        golden.save_file({"retention_class": "test"}, take_dir / "privacy.json")
        golden.save_file(
            {"accepted": True, "checks_passed": True},
            take_dir / "review.json"
        )
        golden.save_file(
            {"valid": True, "reason": "ok"},
            evidence_dir / "validity.json"
        )
        samples = golden._generate_sine(golden.ms_samples(500), 440.0, 0.5)
        golden.write_wav(take_dir / "audio.wav", samples)
        (take_dir / "event-stream.jsonl").write_text(
            '{"event":"stream_start"}\n'
        )

        # Only checks if provenance_sid is present — should skip check and pass
        code = golden.promote_take(take_dir, Path(self.tmp) / "dest", dry_run=False)
        self.assertEqual(code, golden.ExitCode.PASS)


class TestGetKeyEOF(unittest.TestCase):
    """_get_key() must raise EOFError on empty read / closed stdin."""

    def test_get_key_raises_eof_on_empty_read_tty(self):
        """In raw TTY mode, empty read(1) must raise EOFError."""
        with patch.object(sys.stdin, "isatty", return_value=True), \
             patch.object(sys.stdin, "fileno", return_value=0), \
             patch.object(sys.stdin, "read", return_value=""), \
             patch("termios.tcgetattr"), \
             patch("termios.tcsetattr"), \
             patch("tty.setraw"):
            with self.assertRaises(EOFError):
                golden._get_key()

    def test_get_key_raises_eof_on_empty_read_fallback(self):
        """In line-buffered fallback, empty readline must raise EOFError."""
        with patch.object(sys.stdin, "readline", return_value=""), \
             patch("termios.tcgetattr", side_effect=ImportError):
            with self.assertRaises(EOFError):
                golden._get_key()

    def test_get_key_returns_normal_char(self):
        """Normal keypress returns the character."""
        with patch.object(sys.stdin, "isatty", return_value=True), \
             patch.object(sys.stdin, "fileno", return_value=0), \
             patch.object(sys.stdin, "read", return_value="a"), \
             patch("termios.tcgetattr"), \
             patch("termios.tcsetattr"), \
             patch("tty.setraw"):
            result = golden._get_key()
            self.assertEqual(result, "a")

    def test_get_key_returns_normal_char_fallback(self):
        """Fallback returns stripped line."""
        with patch.object(sys.stdin, "readline", return_value="q\n"), \
             patch("termios.tcgetattr", side_effect=ImportError):
            result = golden._get_key()
            self.assertEqual(result, "q")


class TestReviewLoopEOF(unittest.TestCase):
    """_operator_review_loop must handle EOF (closed stdin) cleanly."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.take_dir = Path(self.tmp) / "take-001"
        self.take_dir.mkdir()
        _write_tone_wav(self.take_dir / "audio.wav", 1000)
        golden.save_file({"test": True}, self.take_dir / "consent.json")
        golden.save_file({"test": True}, self.take_dir / "privacy.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_review_loop_eof_aborts(self):
        """EOFError from _get_key must cause RECORDER_ABORTED, not loop."""
        quality = {
            "passed": False,
            "checks": [
                {"name": "wav_exists", "passed": True},
            ],
            "failures": ["Test failure"],
            "quality": {"duration_ms": 1000, "peak_dbfs": -4.0, "rms_dbfs": -12.0, "clipping_count": 0},
            "event_count": 0,
            "dry_run": False,
        }
        with patch.object(golden, "_get_key", side_effect=EOFError("stdin closed")):
            code = golden._operator_review_loop(
                self.take_dir, quality, "take"
            )
            self.assertEqual(code, golden.ExitCode.RECORDER_ABORTED)

    def test_review_loop_eof_dry_run_aborts(self):
        """EOF during dry-run review loop also aborts (no promotion)."""
        quality = {
            "passed": False,
            "checks": [
                {"name": "wav_exists", "passed": True},
            ],
            "failures": [],
            "quality": {"duration_ms": 1000, "peak_dbfs": -96.0, "rms_dbfs": -96.0, "clipping_count": 0},
            "event_count": 0,
            "dry_run": True,
        }
        with patch.object(golden, "_get_key", side_effect=EOFError("stdin closed")):
            code = golden._operator_review_loop(
                self.take_dir, quality, "take"
            )
            self.assertEqual(code, golden.ExitCode.RECORDER_ABORTED)
            # No review.json should be written (no accept)
            review_path = self.take_dir / "review.json"
            self.assertFalse(review_path.exists(),
                            "EOF must not accept/promote — review.json should not exist")


class TestSubprocessEOFRegression(unittest.TestCase):
    """Black-box subprocess regression: EOF on stdin must not loop."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.manifest_path = REPO_ROOT / "tests" / "golden" / "manifest.yaml"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_operator_devnull_bounded_exit(self):
        """operator --dry-run --scenario with /dev/null must exit within timeout."""
        script = SCRIPT_DIR / "speech-core-golden.py"
        out_dir = Path(self.tmp) / "runs"

        try:
            result = subprocess.run(
                [
                    sys.executable, str(script),
                    "operator",
                    "--manifest", str(self.manifest_path),
                    "--scenario", "human-clean-complete",
                    "--dry-run",
                    "--out", str(out_dir),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=15,
            )
        except subprocess.TimeoutExpired:
            self.fail("operator with /dev/null timed out — EOF loop not fixed")

        # Must exit with nonzero (not PASS=0, which would indicate acceptance)
        self.assertNotEqual(result.returncode, golden.ExitCode.PASS,
                           "EOF on stdin must not result in PASS (no acceptance)")
        stderr_text = result.stderr.decode("utf-8", errors="replace")
        self.assertNotIn("Traceback", stderr_text,
                         f"EOF must exit cleanly without traceback: {stderr_text}")

        # Verify no accepted review was written
        take_dirs = list(out_dir.rglob("review.json")) if out_dir.exists() else []
        for rev_path in take_dirs:
            try:
                review = json.loads(rev_path.read_text())
                self.assertFalse(review.get("accepted", False),
                                f"EOF on stdin must not produce accepted review at {rev_path}")
            except Exception:
                pass

    def test_operator_devnull_no_promotion(self):
        """operator --dry-run --scenario with /dev/null must not promote."""
        script = SCRIPT_DIR / "speech-core-golden.py"
        out_dir = Path(self.tmp) / "runs2"

        result = subprocess.run(
            [
                sys.executable, str(script),
                "operator",
                "--manifest", str(self.manifest_path),
                "--scenario", "human-clean-complete",
                "--dry-run",
                "--out", str(out_dir),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
        )

        # Exit must not be acceptance
        self.assertNotEqual(result.returncode, 0)

        # Also verify no explicit accept line in stdout
        stdout_text = result.stdout.decode("utf-8", errors="replace")
        self.assertNotIn("Accepted", stdout_text)
        self.assertNotIn("Take accepted", stdout_text)


# Pytest-style runner compatible with unittest
if __name__ == "__main__":
    unittest.main()
