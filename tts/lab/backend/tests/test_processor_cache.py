#!/usr/bin/env python3
"""Processor and analysis cache identity. not on the voicecat path."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO))

from tts.lab.backend.models import Interval, ReferenceVariant
from tts.lab.backend.store.cache import (
    analysis_cache_key,
    canonical_processor_config,
    processor_cache_key,
)
from tts.lab.backend.store.db import connect

RESEMBLE_CONFIG = {"checkpoint": "resemble-denoise", "preprocess_version": "v1"}


class ProcessorCacheKey(unittest.TestCase):
    def test_resemble_key_includes_keep_and_processor(self) -> None:
        keep_a = [Interval(start_s=0.0, end_s=4.0)]
        keep_b = [Interval(start_s=0.0, end_s=3.0)]
        a = processor_cache_key("resemble", "deadbeef", keep_a, RESEMBLE_CONFIG)
        b = processor_cache_key("resemble", "deadbeef", keep_b, RESEMBLE_CONFIG)
        c = processor_cache_key(
            "resemble",
            "deadbeef",
            [dict(interval) for interval in keep_a],
            RESEMBLE_CONFIG,
        )
        self.assertNotEqual(a, b)
        self.assertEqual(a, c)
        canon = canonical_processor_config("resemble", RESEMBLE_CONFIG)
        self.assertEqual(canon["processor"], "resemble")

    def test_keep_interval_order_does_not_fork_key(self) -> None:
        source = "deadbeef"
        keep = [Interval(start_s=0.0, end_s=4.0), Interval(start_s=7.0, end_s=11.0)]
        a = processor_cache_key("resemble", source, keep, RESEMBLE_CONFIG)
        b = processor_cache_key("resemble", source, list(reversed(keep)), RESEMBLE_CONFIG)
        self.assertEqual(a, b)

    def test_processor_identity_forks_key(self) -> None:
        keep = [Interval(start_s=0.0, end_s=4.0)]
        resemble = processor_cache_key("resemble", "deadbeef", keep, {"checkpoint": "same"})
        other = processor_cache_key("vibevoice-asr", "deadbeef", keep, {"checkpoint": "same"})
        self.assertNotEqual(resemble, other)

    def test_processor_config_change_forks_key(self) -> None:
        keep = [Interval(start_s=0.0, end_s=4.0)]
        v1 = processor_cache_key("resemble", "deadbeef", keep, RESEMBLE_CONFIG)
        v2 = processor_cache_key(
            "resemble",
            "deadbeef",
            keep,
            {"checkpoint": "resemble-denoise", "preprocess_version": "v2"},
        )
        self.assertNotEqual(v1, v2)

    def test_config_key_order_does_not_fork_key(self) -> None:
        keep = [Interval(start_s=0.0, end_s=4.0)]
        left = processor_cache_key(
            "resemble", "deadbeef", keep, {"checkpoint": "c", "config": {"a": 1}}
        )
        right = processor_cache_key(
            "resemble", "deadbeef", keep, {"config": {"a": 1}, "checkpoint": "c"}
        )
        self.assertEqual(left, right)


class AnalysisCacheKey(unittest.TestCase):
    def test_analysis_key_ignores_keep(self) -> None:
        config = {"model_id": "Dubedo/VibeVoice-ASR-HF-NF4"}
        a = analysis_cache_key("deadbeef", "vibevoice-asr", config)
        b = analysis_cache_key("deadbeef", "vibevoice-asr", config)
        self.assertEqual(a, b)
        self.assertNotEqual(a, analysis_cache_key("feedface", "vibevoice-asr", config))

    def test_analysis_key_tracks_model_revision(self) -> None:
        base = {"model_id": "Dubedo/VibeVoice-ASR-HF-NF4", "model_revision": "rev-a"}
        bumped = {"model_id": "Dubedo/VibeVoice-ASR-HF-NF4", "model_revision": "rev-b"}
        self.assertNotEqual(
            analysis_cache_key("deadbeef", "vibevoice-asr", base),
            analysis_cache_key("deadbeef", "vibevoice-asr", bumped),
        )


class VariantKind(unittest.TestCase):
    def test_resemble_kind_is_the_cleanup_variant(self) -> None:
        variant = ReferenceVariant(
            id="rv_1",
            voice_profile_id="vp_1",
            kind="resemble",
            audio_artifact_id="a_1",
            duration_s=1.0,
        )
        self.assertEqual(variant.kind, "resemble")
        with self.assertRaises(ValidationError):
            ReferenceVariant(
                id="rv_2",
                voice_profile_id="vp_1",
                kind="streamfm",
                audio_artifact_id="a_2",
                duration_s=1.0,
            )

    def test_auk_kind_is_a_candidate(self) -> None:
        variant = ReferenceVariant(
            id="rv_3",
            voice_profile_id="vp_1",
            kind="auk",
            audio_artifact_id="a_3",
            duration_s=1.0,
        )
        self.assertEqual(variant.kind, "auk")
        self.assertIsNone(variant.parent_variant_id)
        self.assertIsNone(variant.settings)
        self.assertFalse(variant.approved)


class SpeakerAnalysisSchema(unittest.TestCase):
    def test_connect_creates_speaker_analyses_and_voice_column(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp))
            tables = {
                str(row[0])
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            self.assertIn("speaker_analyses", tables)
            analysis_cols = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(speaker_analyses)")
            }
            self.assertEqual(
                analysis_cols,
                {
                    "id",
                    "voice_id",
                    "source_artifact_id",
                    "processor",
                    "model_id",
                    "model_revision",
                    "config_json",
                    "cache_key",
                    "result_json",
                    "created_at",
                },
            )
            voice_cols = {str(row[1]) for row in conn.execute("PRAGMA table_info(voices)")}
            self.assertIn("speaker_analysis_id", voice_cols)
            conn.close()

    def test_existing_voice_db_gains_speaker_analysis_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = sqlite3.connect(str(root / "lab.sqlite3"))
            legacy.execute("CREATE TABLE voices (id TEXT PRIMARY KEY, name TEXT NOT NULL)")
            legacy.commit()
            legacy.close()
            conn = connect(root)
            voice_cols = {str(row[1]) for row in conn.execute("PRAGMA table_info(voices)")}
            self.assertIn("speaker_analysis_id", voice_cols)
            tables = {
                str(row[0])
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            self.assertIn("speaker_analyses", tables)
            conn.close()


if __name__ == "__main__":
    unittest.main()
