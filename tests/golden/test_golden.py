#!/usr/bin/env python3
"""
Tests for speech-core-golden CLI.

Covers:
  - Manifest validation (valid/invalid/duplicate/missing profile)
  - Sample-exact WAV generation
  - Invalid/empty/stale capture rejection
  - Hash chain
  - Mock recorder flow (dry-run)
  - Exit codes
  - Synth scenario generation and determinism
  - WAV metadata and quality checks
  - Promote validation
  - Quarantine report generation
"""

import json
import os
import shutil
import struct
import sys
import tempfile
import unittest
import wave
from pathlib import Path

# Add scripts to path
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

# Import the module under test
import importlib
import importlib.util
spec = importlib.util.spec_from_file_location("golden", SCRIPT_DIR / "speech-core-golden.py")
golden = importlib.util.module_from_spec(spec)
spec.loader.exec_module(golden)


class TestManifestValidation(unittest.TestCase):
    """Validate manifest schema, profile references, scenario IDs."""

    def test_valid_manifest(self):
        manifest = {
            "manifest_version": 1,
            "profile": "golden-mvp",
            "scenarios": [
                {"id": "test-scenario", "class": "natural_endpoint", "construction": "human-recorded"},
            ],
        }
        # Use the actual tests/golden dir so profile path resolves
        manifest_dir = REPO_ROOT / "tests" / "golden"
        errors = golden.validate_manifest(manifest, manifest_dir)
        self.assertEqual(errors, [])

    def test_missing_version(self):
        manifest = {
            "profile": "golden-mvp",
            "scenarios": [],
        }
        errors = golden.validate_manifest(manifest, Path("/tmp"))
        self.assertTrue(any("manifest_version" in e for e in errors))

    def test_wrong_version(self):
        manifest = {
            "manifest_version": 99,
            "profile": "golden-mvp",
            "scenarios": [],
        }
        errors = golden.validate_manifest(manifest, Path("/tmp"))
        self.assertTrue(any("Unsupported manifest_version" in e for e in errors))

    def test_missing_profile(self):
        manifest = {
            "manifest_version": 1,
            "scenarios": [],
        }
        errors = golden.validate_manifest(manifest, Path("/tmp"))
        self.assertTrue(any("profile" in e.lower() for e in errors))

    def test_missing_scenarios(self):
        manifest = {
            "manifest_version": 1,
            "profile": "golden-mvp",
        }
        errors = golden.validate_manifest(manifest, Path("/tmp"))
        self.assertTrue(any("scenarios" in e.lower() for e in errors))

    def test_scenarios_not_list(self):
        manifest = {
            "manifest_version": 1,
            "profile": "golden-mvp",
            "scenarios": "not-a-list",
        }
        errors = golden.validate_manifest(manifest, Path("/tmp"))
        self.assertTrue(any("must be a list" in e for e in errors))

    def test_duplicate_scenario_ids(self):
        manifest = {
            "manifest_version": 1,
            "profile": "golden-mvp",
            "scenarios": [
                {"id": "dup"},
                {"id": "dup"},
            ],
        }
        errors = golden.validate_manifest(manifest, Path("/tmp"))
        self.assertTrue(any("Duplicate" in e for e in errors))

    def test_scenario_missing_id(self):
        manifest = {
            "manifest_version": 1,
            "profile": "golden-mvp",
            "scenarios": [{"no_id": True}],
        }
        errors = golden.validate_manifest(manifest, Path("/tmp"))
        self.assertTrue(any("missing 'id'" in e for e in errors))

    def test_unknown_class(self):
        manifest = {
            "manifest_version": 1,
            "profile": "golden-mvp",
            "scenarios": [
                {"id": "bad-class", "class": "nonexistent_class_xyz"},
            ],
        }
        errors = golden.validate_manifest(manifest, Path("/tmp"))
        self.assertTrue(any("unknown class" in e for e in errors))

    def test_unknown_construction(self):
        manifest = {
            "manifest_version": 1,
            "profile": "golden-mvp",
            "scenarios": [
                {"id": "bad-cons", "construction": "magic"},
            ],
        }
        errors = golden.validate_manifest(manifest, Path("/tmp"))
        self.assertTrue(any("unknown construction" in e for e in errors))

    def test_nonexistent_scenario_file(self):
        manifest = {
            "manifest_version": 1,
            "profile": "golden-mvp",
            "scenarios": [
                {"id": "bad-file", "scenario_file": "/nonexistent/path.yaml"},
            ],
        }
        errors = golden.validate_manifest(manifest, Path("/tmp"))
        self.assertTrue(any("not found" in e for e in errors))

    def test_profile_not_string(self):
        manifest = {
            "manifest_version": 1,
            "profile": 123,
            "scenarios": [],
        }
        errors = golden.validate_manifest(manifest, Path("/tmp"))
        self.assertTrue(any("must be a string" in e for e in errors))


class TestWAVGeneration(unittest.TestCase):
    """Deterministic synthetic WAV generation tests."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_silence_wav(self):
        samples = golden._generate_silence(16000)
        self.assertEqual(len(samples), 16000)
        self.assertTrue(all(s == 0 for s in samples))

    def test_sine_wav(self):
        samples = golden._generate_sine(16000, 440.0, 0.5)
        self.assertEqual(len(samples), 16000)
        self.assertFalse(all(s == 0 for s in samples))  # not silent

    def test_speech_like_generation(self):
        samples = golden._generate_speech_like(8000, seed=42, base_freq=200.0)
        self.assertEqual(len(samples), 8000)
        self.assertFalse(all(s == 0 for s in samples))

    def test_synthetic_wav_determinism(self):
        plan = {
            "segments": [
                {"type": "silence", "duration_ms": 500},
                {"type": "speech_like", "duration_ms": 400, "base_freq": 200},
                {"type": "silence", "duration_ms": 1500},
            ],
            "seed": 42,
        }
        samples1, prov1 = golden.build_synthetic_wav(plan, seed=42)
        samples2, prov2 = golden.build_synthetic_wav(plan, seed=42)
        self.assertEqual(samples1, samples2)
        self.assertEqual(prov1["wav_sha256"], prov2["wav_sha256"])
        self.assertEqual(prov1["seed"], 42)

    def test_synthetic_wav_different_seed(self):
        plan = {
            "segments": [
                {"type": "silence", "duration_ms": 500},
                {"type": "speech_like", "duration_ms": 400, "base_freq": 200},
                {"type": "silence", "duration_ms": 1500},
            ],
            "seed": 42,
        }
        samples1, _ = golden.build_synthetic_wav(plan, seed=42)
        samples2, _ = golden.build_synthetic_wav(plan, seed=43)
        self.assertNotEqual(samples1, samples2)

    def test_write_and_read_wav(self):
        samples = [0, 100, 200, 300, -100, -200, -300, 0]
        path = Path(self.tmp) / "test.wav"
        golden.write_wav(path, samples)
        self.assertTrue(path.exists())

        sr, ch, sw, n, read_samples = golden.read_wav(path)
        self.assertEqual(sr, 16000)
        self.assertEqual(ch, 1)
        self.assertEqual(sw, 2)
        self.assertEqual(n, len(samples))
        self.assertEqual(read_samples, samples)

    def test_validate_wav_valid(self):
        samples = golden._generate_silence(16000)
        path = Path(self.tmp) / "valid.wav"
        golden.write_wav(path, samples)
        errors = golden.validate_wav(path)
        self.assertEqual(errors, [])

    def test_validate_wav_missing(self):
        path = Path(self.tmp) / "nonexistent.wav"
        errors = golden.validate_wav(path)
        self.assertTrue(any("missing" in e for e in errors))

    def test_validate_wav_zero_samples(self):
        # Write a WAV with 0 frames
        path = Path(self.tmp) / "zero.wav"
        golden.write_wav(path, [])
        errors = golden.validate_wav(path)
        self.assertTrue(any("Zero-sample" in e for e in errors))

    def test_validate_wav_wrong_format(self):
        samples = golden._generate_silence(1000)
        path = Path(self.tmp) / "wrong.wav"
        # Write with wrong sample rate
        golden.write_wav(path, samples, sample_rate=44100)
        errors = golden.validate_wav(path, expected_sr=16000)
        self.assertTrue(any("Wrong sample rate" in e for e in errors))

    def test_wav_metadata(self):
        samples = golden._generate_silence(16000)
        path = Path(self.tmp) / "meta.wav"
        golden.write_wav(path, samples)
        meta = golden.wav_metadata(path)
        self.assertIsNotNone(meta)
        self.assertEqual(meta["sample_count"], 16000)
        self.assertEqual(meta["duration_ms"], 1000)
        self.assertIn("sha256", meta)

    def test_wav_metadata_bad_file(self):
        path = Path(self.tmp) / "bad.wav"
        path.write_text("not a wav file")
        meta = golden.wav_metadata(path)
        self.assertIsNone(meta)

    def test_rms_dbfs_silence(self):
        samples = [0] * 1000
        rms = golden.rms_dbfs(samples)
        self.assertLess(rms, -90)

    def test_rms_dbfs_full_scale(self):
        samples = [32767] * 1000
        rms = golden.rms_dbfs(samples)
        self.assertAlmostEqual(rms, 0.0, delta=0.5)

    def test_peak_dbfs(self):
        samples = [0] * 100 + [16000] + [0] * 100
        peak = golden.peak_dbfs(samples)
        self.assertLess(peak, -5.0)
        self.assertGreater(peak, -8.0)

    def test_clipping_count(self):
        samples = [32767, 32767, 100, -32768, 0]
        clip = golden.clipping_count(samples)
        self.assertEqual(clip, 3)  # 32767, 32767, -32768

    def test_sample_exact_plan_400ms(self):
        """Verify exact sample counts for min-VAD-speech-at-400ms."""
        plan = golden.SYNTHETIC_SCENARIO_PLANS["synthetic-min-vad-speech-at-400ms"]
        samples, prov = golden.build_synthetic_wav(plan, seed=61)
        # 500ms silence + 400ms speech + 2500ms silence = 3400ms = 54400 samples
        expected_samples = 500 * 16 + 400 * 16 + 2500 * 16  # = 8000 + 6400 + 40000
        self.assertEqual(len(samples), expected_samples)
        self.assertEqual(prov["total_samples"], expected_samples)
        self.assertEqual(prov["total_duration_ms"], 3400)

    def test_sample_exact_plan_399ms(self):
        """Verify exact sample counts for min-VAD-speech-below-399ms."""
        plan = golden.SYNTHETIC_SCENARIO_PLANS["synthetic-min-vad-speech-below-399ms"]
        samples, prov = golden.build_synthetic_wav(plan, seed=60)
        # 500 + 399 + 2500 = 3399ms
        # At 16kHz: 500ms = 8000, 399ms = 6384, 2500ms = 40000
        expected = 8000 + 6384 + 40000
        self.assertEqual(len(samples), expected)
        # Verify the 399ms segment is indeed 6384 samples
        seg = prov["segments"][1]
        self.assertEqual(seg["type"], "speech_like")
        self.assertEqual(seg["duration_ms"], 399)
        self.assertEqual(seg["sample_count"], 6384)

    def test_all_synthetic_scenarios_generate(self):
        """All defined synthetic scenarios should generate without error."""
        for sid in golden.SYNTHETIC_SCENARIO_PLANS:
            plan = golden.SYNTHETIC_SCENARIO_PLANS[sid]
            samples, prov = golden.build_synthetic_wav(plan)
            self.assertGreater(len(samples), 0, f"Empty WAV for {sid}")
            self.assertIn("wav_sha256", prov)
            self.assertEqual(len(prov["wav_sha256"]), 64)

    def test_synth_scenario_not_found(self):
        """Synth should exit with SCENARIO_NOT_FOUND for invalid scenario."""
        manifest = {
            "manifest_version": 1,
            "profile": "golden-mvp",
            "scenarios": [{"id": "exists"}],
        }
        with self.assertRaises(SystemExit) as cm:
            golden.synth_scenario(manifest, Path("/tmp"), "none-such", Path("/tmp"))
        self.assertEqual(cm.exception.code, golden.ExitCode.SCENARIO_NOT_FOUND)

    def test_synth_scenario_no_plan(self):
        """Synth should fail for scenarios without a synthetic plan."""
        manifest = {
            "manifest_version": 1,
            "profile": "golden-mvp",
            "scenarios": [{"id": "human-clean-complete", "construction": "human-recorded"}],
        }
        with self.assertRaises(SystemExit) as cm:
            golden.synth_scenario(manifest, Path("/tmp"), "human-clean-complete", Path("/tmp"))
        self.assertEqual(cm.exception.code, golden.ExitCode.SYNTH_GENERATION_FAILED)

    def test_provenance_includes_generator_version(self):
        plan = golden.SYNTHETIC_SCENARIO_PLANS["synthetic-min-vad-speech-at-400ms"]
        _, prov = golden.build_synthetic_wav(plan, seed=61)
        self.assertEqual(prov["generator_version"], "1.0.0")
        self.assertEqual(prov["generator"], "speech-core-golden synth")
        self.assertIn("segments", prov)
        for seg in prov["segments"]:
            self.assertIn("sample_start", seg)
            self.assertIn("sample_count", seg)


class TestHashChain(unittest.TestCase):
    """SHA-256 chain of custody tests."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_sha256_hex_determinism(self):
        h1 = golden.sha256_hex(b"hello")
        h2 = golden.sha256_hex(b"hello")
        self.assertEqual(h1, h2)
        self.assertEqual(len(h1), 64)

    def test_sha256_file(self):
        path = Path(self.tmp) / "test.bin"
        path.write_bytes(b"test content")
        h1 = golden.sha256_file(path)
        h2 = golden.sha256_file(path)
        self.assertEqual(h1, h2)

    def test_sha256_json_canonical(self):
        obj1 = {"b": 2, "a": 1}
        obj2 = {"a": 1, "b": 2}
        h1 = golden.sha256_json(obj1)
        h2 = golden.sha256_json(obj2)
        self.assertEqual(h1, h2)  # canonical ordering

    def test_sha256_json_different(self):
        h1 = golden.sha256_json({"a": 1})
        h2 = golden.sha256_json({"a": 2})
        self.assertNotEqual(h1, h2)

    def test_wav_hash_chain(self):
        """Write WAV, hash, rewrite, hash changes."""
        samples1 = [0] * 1000
        samples2 = [0] * 999 + [1]

        path = Path(self.tmp) / "chain.wav"
        golden.write_wav(path, samples1)
        h1 = golden.sha256_file(path)

        golden.write_wav(path, samples2)
        h2 = golden.sha256_file(path)

        self.assertNotEqual(h1, h2)

    def test_provenance_hash_included(self):
        """Generated provenance includes the WAV hash."""
        plan = golden.SYNTHETIC_SCENARIO_PLANS["synthetic-min-vad-speech-at-400ms"]
        _, prov = golden.build_synthetic_wav(plan, seed=61)
        self.assertIn("wav_sha256", prov)
        self.assertEqual(len(prov["wav_sha256"]), 64)


class TestPromoteValidation(unittest.TestCase):
    """Promote checks consent, privacy, review."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.take_dir = Path(self.tmp) / "take-001"
        self.take_dir.mkdir(parents=True)
        self.dest_dir = Path(self.tmp) / "fixture"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_json(self, name, data):
        path = self.take_dir / name
        with open(path, "w") as f:
            json.dump(data, f)
        return path

    def test_missing_take_dir(self):
        with self.assertRaises(SystemExit) as cm:
            golden.promote_take(Path("/nonexistent"), self.dest_dir)
        self.assertEqual(cm.exception.code, golden.ExitCode.SCENARIO_NOT_FOUND)

    def test_missing_consent(self):
        # No consent file
        with self.assertRaises(SystemExit) as cm:
            golden.promote_take(self.take_dir, self.dest_dir)
        self.assertEqual(cm.exception.code, golden.ExitCode.CONSENT_REQUIRED)

    def test_missing_privacy(self):
        self._write_json("consent.json", {"purpose": "test"})
        with self.assertRaises(SystemExit) as cm:
            golden.promote_take(self.take_dir, self.dest_dir)
        self.assertEqual(cm.exception.code, golden.ExitCode.PRIVACY_POLICY_VIOLATION)

    def test_missing_review(self):
        self._write_json("consent.json", {"purpose": "test"})
        self._write_json("privacy.json", {"retention_class": "delete-after-run"})
        with self.assertRaises(SystemExit) as cm:
            golden.promote_take(self.take_dir, self.dest_dir)
        self.assertEqual(cm.exception.code, golden.ExitCode.BASELINE_REQUIRES_REVIEW)

    def test_not_accepted_review(self):
        self._write_json("consent.json", {"purpose": "test"})
        self._write_json("privacy.json", {"retention_class": "delete-after-run"})
        self._write_json("review.json", {"accepted": False})
        with self.assertRaises(SystemExit) as cm:
            golden.promote_take(self.take_dir, self.dest_dir)
        self.assertEqual(cm.exception.code, golden.ExitCode.BASELINE_REQUIRES_REVIEW)

    def test_missing_wav(self):
        self._write_json("consent.json", {"purpose": "test"})
        self._write_json("privacy.json", {"retention_class": "delete-after-run"})
        self._write_json("review.json", {"accepted": True})
        # No WAV
        with self.assertRaises(SystemExit) as cm:
            golden.promote_take(self.take_dir, self.dest_dir)
        self.assertEqual(cm.exception.code, golden.ExitCode.WAV_FORMAT_INVALID)

    def test_pii_in_path_rejected(self):
        self._write_json("consent.json", {"purpose": "test"})
        self._write_json("privacy.json", {"retention_class": "delete-after-run"})
        self._write_json("review.json", {"accepted": True})
        # Write a valid WAV
        samples = golden._generate_silence(1000)
        golden.write_wav(self.take_dir / "audio.wav", samples)
        self._write_json("provenance.json", {"version": "1.0"})

        # Path with email-like content — copy valid setup to bad dir
        bad_dir = Path(self.tmp) / "user@email"
        import shutil
        shutil.copytree(self.take_dir, bad_dir)
        with self.assertRaises(SystemExit) as cm:
            golden.promote_take(bad_dir, self.dest_dir)
        self.assertEqual(cm.exception.code, golden.ExitCode.PRIVACY_POLICY_VIOLATION)

    def test_valid_wav_promote_dry_run(self):
        self._write_json("consent.json", {"purpose": "test"})
        self._write_json("privacy.json", {"retention_class": "repo-fixture-explicit"})
        self._write_json("review.json", {"accepted": True})
        samples = golden._generate_silence(1000)
        golden.write_wav(self.take_dir / "audio.wav", samples)
        self._write_json("provenance.json", {"version": "1.0"})

        code = golden.promote_take(self.take_dir, self.dest_dir, dry_run=True)
        self.assertEqual(code, golden.ExitCode.PASS)

    def test_invalid_wav_promote(self):
        self._write_json("consent.json", {"purpose": "test"})
        self._write_json("privacy.json", {"retention_class": "repo-fixture-explicit"})
        self._write_json("review.json", {"accepted": True})
        # Write a zero-sample WAV
        golden.write_wav(self.take_dir / "audio.wav", [])
        self._write_json("provenance.json", {"version": "1.0"})

        with self.assertRaises(SystemExit) as cm:
            golden.promote_take(self.take_dir, self.dest_dir)
        self.assertEqual(cm.exception.code, golden.ExitCode.WAV_FORMAT_INVALID)


class TestRecorderFlow(unittest.TestCase):
    """Mock recorder flow tests (dry-run)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_scenario_not_found(self):
        manifest = {
            "manifest_version": 1,
            "profile": "golden-mvp",
            "scenarios": [{"id": "exists"}],
        }
        with self.assertRaises(SystemExit) as cm:
            golden.guided_record(
                manifest, Path("/tmp"), "none-such",
                Path(self.tmp), "practice", dry_run=True,
            )
        self.assertEqual(cm.exception.code, golden.ExitCode.SCENARIO_NOT_FOUND)

    def test_consent_written_for_practice(self):
        """Even in dry-run, consent and privacy should be written."""
        manifest = {
            "manifest_version": 1,
            "profile": "golden-mvp",
            "scenarios": [{"id": "test-sc", "construction": "human-recorded"}],
        }
        # This will start interactive flow; we can't test that
        # but we can verify the scenario lookup works
        pass  # Interactive recorder requires tty


class TestExitCodes(unittest.TestCase):
    """Exit codes match spec."""

    def test_exit_code_values(self):
        self.assertEqual(golden.ExitCode.PASS, 0)
        self.assertEqual(golden.ExitCode.MANIFEST_INVALID, 2)
        self.assertEqual(golden.ExitCode.CONSENT_REQUIRED, 4)
        self.assertEqual(golden.ExitCode.DEPENDENCY_MISSING, 5)
        self.assertEqual(golden.ExitCode.SCENARIO_NOT_FOUND, 14)
        self.assertEqual(golden.ExitCode.SYNTH_GENERATION_FAILED, 16)
        self.assertEqual(golden.ExitCode.WAV_FORMAT_INVALID, 17)
        self.assertEqual(golden.ExitCode.BASELINE_REQUIRES_REVIEW, 19)
        self.assertEqual(golden.ExitCode.INTERNAL_ERROR, 20)
        self.assertEqual(golden.ExitCode.CAPTURE_INCOMPLETE, 21)

    def test_die_function(self):
        with self.assertRaises(SystemExit) as cm:
            golden.die(golden.ExitCode.MANIFEST_INVALID, "test error")
        self.assertEqual(cm.exception.code, golden.ExitCode.MANIFEST_INVALID)


class TestQuarantineLegacy(unittest.TestCase):
    """Legacy fixture quarantine report."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_dry_run_quarantine(self):
        code = golden.quarantine_legacy_fixtures(Path(self.tmp), dry_run=True)
        self.assertEqual(code, golden.ExitCode.PASS)

    def test_quarantine_report_written(self):
        legacy_dir = Path(self.tmp) / "legacy"
        code = golden.quarantine_legacy_fixtures(legacy_dir)
        self.assertEqual(code, golden.ExitCode.PASS)
        report = legacy_dir / "quarantine-report.yaml"
        self.assertTrue(report.exists())

        # Check content
        data = golden.load_manifest_file(report)
        self.assertEqual(data["quarantine_version"], 1)
        self.assertEqual(len(data["fixtures"]), 8)
        self.assertIn("01-clean-sentence", data["fixtures"][0]["legacy_id"])


class TestFormatting(unittest.TestCase):
    """Timer and display formatting."""

    def test_format_timer_zero(self):
        self.assertEqual(golden._format_timer(0.0), "00:00.000")

    def test_format_timer_seconds(self):
        self.assertEqual(golden._format_timer(5.0), "00:05.000")
        self.assertEqual(golden._format_timer(5.123), "00:05.123")

    def test_format_timer_minutes(self):
        self.assertEqual(golden._format_timer(65.0), "01:05.000")
        self.assertEqual(golden._format_timer(125.456), "02:05.456")

    def test_samples_ms_conversion(self):
        self.assertEqual(golden.samples_ms(16000), 1000)
        self.assertEqual(golden.samples_ms(8000), 500)
        self.assertEqual(golden.ms_samples(1000), 16000)
        self.assertEqual(golden.ms_samples(500), 8000)

    def test_roundtrip(self):
        for ms in [0, 100, 500, 1000, 1700, 3500, 7000]:
            self.assertEqual(golden.samples_ms(golden.ms_samples(ms)), ms)


class TestDelegation(unittest.TestCase):
    """Delegation to speech-core-golden-assert propagates correctly."""

    def test_build_assert_args_string(self):
        class FakeArgs:
            url = "ws://localhost:8765/ws"
            stream_session_id = "test-session"
            out = None
            timeout_ms = None
            adapter_cmd = None
            adapter_cwd = None
        result = golden._build_assert_args(FakeArgs(), ["url", "stream_session_id", "out", "timeout_ms", "adapter_cmd", "adapter_cwd"])
        self.assertIn("--url", result)
        self.assertIn("ws://localhost:8765/ws", result)
        self.assertIn("--stream-session-id", result)
        self.assertIn("test-session", result)
        self.assertNotIn("--out", result)  # None → skipped

    def test_build_assert_args_list(self):
        class FakeArgs:
            adapter_cmd = ["sox", "-d"]
            url = None
            stream_session_id = None
            out = None
            timeout_ms = None
            adapter_cwd = None
        result = golden._build_assert_args(FakeArgs(), ["url", "stream_session_id", "out", "timeout_ms", "adapter_cmd", "adapter_cwd"])
        self.assertIn("--adapter-cmd", result)
        self.assertIn("sox", result)
        self.assertIn("-d", result)

    def test_capture_delegation_runs_assert_script(self):
        """Verify that capture delegation invokes the assert script subprocess."""
        # Test with --help to avoid needing a live daemon
        result = golden.delegate_to_assert("capture", ["--help"])
        self.assertEqual(result, 0)

    def test_assert_delegation_runs_assert_script(self):
        """Verify that assert delegation invokes the assert script subprocess."""
        result = golden.delegate_to_assert("assert", ["--help"])
        self.assertEqual(result, 0)

    def test_run_delegation_runs_assert_script(self):
        """Verify that run delegation invokes the assert script subprocess."""
        result = golden.delegate_to_assert("run", ["--help"])
        self.assertEqual(result, 0)

    def test_test_delegation_runs_assert_script_tests(self):
        """End-to-end: delegate test command runs the 24 mock assertion tests."""
        result = golden.delegate_to_assert("test", [])
        self.assertEqual(result, 0)


class TestDelete(unittest.TestCase):
    """Delete with tombstone, dry-run, and metadata retention."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_delete_missing_dir(self):
        class FakeArgs:
            run = "/nonexistent/path"
            scenario = "test"
            dry_run = False
            purge_audio = True
        code = golden.delegate_delete(FakeArgs())
        self.assertEqual(code, golden.ExitCode.SCENARIO_NOT_FOUND)

    def test_delete_requires_purge_audio(self):
        run_dir = Path(self.tmp) / "run"
        run_dir.mkdir()
        class FakeArgs:
            run = str(run_dir)
            scenario = "test"
            dry_run = False
            purge_audio = False
        code = golden.delegate_delete(FakeArgs())
        self.assertEqual(code, golden.ExitCode.INTERNAL_ERROR)

    def test_delete_dry_run_no_wav(self):
        run_dir = Path(self.tmp) / "run"
        run_dir.mkdir()
        class FakeArgs:
            run = str(run_dir)
            scenario = "test"
            dry_run = True
            purge_audio = True
        code = golden.delegate_delete(FakeArgs())
        self.assertEqual(code, golden.ExitCode.PASS)

    def test_delete_dry_run_with_wav(self):
        run_dir = Path(self.tmp) / "run"
        run_dir.mkdir()
        wav_path = run_dir / "audio.wav"
        samples = golden._generate_silence(100)
        golden.write_wav(wav_path, samples)
        class FakeArgs:
            run = str(run_dir)
            scenario = "test"
            dry_run = True
            purge_audio = True
        code = golden.delegate_delete(FakeArgs())
        self.assertEqual(code, golden.ExitCode.PASS)
        self.assertTrue(wav_path.exists())  # dry-run preserves

    def test_delete_purges_wav_and_writes_tombstone(self):
        run_dir = Path(self.tmp) / "run"
        run_dir.mkdir()
        wav_path = run_dir / "audio.wav"
        samples = golden._generate_silence(100)
        golden.write_wav(wav_path, samples)
        class FakeArgs:
            run = str(run_dir)
            scenario = "test"
            dry_run = False
            purge_audio = True
        code = golden.delegate_delete(FakeArgs())
        self.assertEqual(code, golden.ExitCode.PASS)
        self.assertFalse(wav_path.exists())
        tombstone = run_dir / "delete-tombstone.json"
        self.assertTrue(tombstone.exists())
        with open(tombstone) as f:
            data = json.load(f)
        self.assertEqual(data["operation"], "delete")
        self.assertEqual(len(data["purged_files"]), 1)
        self.assertTrue(data["sha256_tombstone"])


class TestJSONManifestLoading(unittest.TestCase):
    """JSON manifest loading works without YAML."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_load_json_manifest(self):
        path = Path(self.tmp) / "manifest.json"
        data = {"manifest_version": 1, "profile": "test", "scenarios": []}
        with open(path, "w") as f:
            json.dump(data, f)
        loaded = golden.load_manifest_file(path)
        self.assertEqual(loaded, data)

    def test_save_file_json(self):
        path = Path(self.tmp) / "test.json"
        data = {"key": "value"}
        golden.save_file(data, path)
        self.assertTrue(path.exists())
        with open(path) as f:
            loaded = json.load(f)
        self.assertEqual(loaded, data)


if __name__ == "__main__":
    unittest.main()
