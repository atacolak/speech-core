#!/usr/bin/env python3
"""Offline vibevoice speaker analysis + use-speaker keep. No GPU, no weights.

Diarization is faked through the module hook; overlap is not source separation,
so overlapping slices never reach automatic use-speaker keep.
"""

from __future__ import annotations

import importlib
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO))

from tts.lab.backend.app import create_app
from tts.lab.backend.runtime.leftover import NoopLeftover
from tts.lab.backend.runtime.manager import E2RuntimeManager
from tts.lab.backend.runtime.vram import FixedVramProbe
from tts.lab.backend.runtime.worker import CountingWorkerFactory
from tts.lab.backend.services.resemble import ProcessorUnavailable
from tts.lab.backend.services.speakers import (
    LEASE_NAME,
    PROCESSOR,
    PROCESSOR_CONFIG,
    default_analyze,
    keep_for_speaker,
    mark_overlaps,
)
from tts.lab.backend.store.cache import analysis_cache_key
from tts.wav import write_wav

GIB = 1024 ** 3
ANALYZE_HOOK = "tts.lab.backend.services.speakers.analyze_hook"

FAKE = {
    "speakers": [
        {"id": "S1", "label": "Speaker 1", "duration_s": 4.0},
        {"id": "S2", "label": "Speaker 2", "duration_s": 3.0},
    ],
    "segments": [
        {"speaker_id": "S1", "start_s": 0.0, "end_s": 2.0, "text": "hello from one", "overlap": False},
        {"speaker_id": "S1", "start_s": 2.0, "end_s": 3.0, "text": "together", "overlap": True},
        {"speaker_id": "S2", "start_s": 2.0, "end_s": 3.0, "text": "together", "overlap": True},
        {"speaker_id": "S2", "start_s": 3.0, "end_s": 6.0, "text": "hello from two", "overlap": False},
    ],
    "overlaps": [{"start_s": 2.0, "end_s": 3.0, "speakers": ["S1", "S2"]}],
}


def _wav(path: Path, seconds: float = 7.0) -> Path:
    sr = 24000
    write_wav(path, sr, 0.1 * np.sin(2 * np.pi * 440 * np.arange(int(sr * seconds)) / sr))
    return path


def _fake_analysis() -> dict:
    return json.loads(json.dumps(FAKE))


class _RecordingAnalyzer:
    """Fake vibevoice: records the analyzed path, returns the canned two-speaker result."""

    def __init__(self, result: dict | None = None) -> None:
        self.paths: list[Path] = []
        self.result = result or _fake_analysis()

    def __call__(self, path: Path | str) -> dict:
        self.paths.append(Path(path))
        return json.loads(json.dumps(self.result))


def _vibevoice_installed() -> bool:
    try:
        transformers = importlib.import_module("transformers")
    except ImportError:
        return False
    return hasattr(transformers, "VibeVoiceAsrForConditionalGeneration")


class SpeakerMath(unittest.TestCase):
    def test_mark_overlaps_flags_both_speakers_and_emits_range(self) -> None:
        segments = [dict(seg) for seg in FAKE["segments"]]
        for seg in segments:
            seg.pop("overlap")
        marked, overlaps = mark_overlaps(segments)
        self.assertEqual(overlaps, [{"start_s": 2.0, "end_s": 3.0, "speakers": ["S1", "S2"]}])
        flags = {(seg["speaker_id"], seg["start_s"]): seg["overlap"] for seg in marked}
        self.assertTrue(flags[("S1", 2.0)])
        self.assertTrue(flags[("S2", 2.0)])
        self.assertFalse(flags[("S1", 0.0)])
        self.assertFalse(flags[("S2", 3.0)])

    def test_mark_overlaps_ignores_a_speaker_talking_over_itself(self) -> None:
        segments = [
            {"speaker_id": "S1", "start_s": 0.0, "end_s": 2.0, "text": "a"},
            {"speaker_id": "S1", "start_s": 1.0, "end_s": 3.0, "text": "b"},
        ]
        marked, overlaps = mark_overlaps(segments)
        self.assertEqual(overlaps, [])
        self.assertEqual([seg["overlap"] for seg in marked], [False, False])

    def test_keep_for_speaker_excludes_overlap_slices(self) -> None:
        keep = keep_for_speaker(FAKE, "S2", 7.0)
        self.assertEqual([(iv.start_s, iv.end_s) for iv in keep], [(3.0, 6.0)])

    def test_keep_for_speaker_merges_adjacent_slices(self) -> None:
        analysis = {
            "speakers": [{"id": "S1", "label": "Speaker 1", "duration_s": 5.0}],
            "segments": [
                {"speaker_id": "S1", "start_s": 0.0, "end_s": 2.0, "text": "a"},
                {"speaker_id": "S1", "start_s": 2.0, "end_s": 5.0, "text": "b"},
            ],
            "overlaps": [],
        }
        keep = keep_for_speaker(analysis, "S1", 7.0)
        self.assertEqual([(iv.start_s, iv.end_s) for iv in keep], [(0.0, 5.0)])

    def test_keep_for_speaker_rejects_unknown_speaker(self) -> None:
        with self.assertRaises(KeyError):
            keep_for_speaker(FAKE, "S9", 7.0)

    def test_parse_analysis_accepts_vibevoice_capitalized_keys(self) -> None:
        """Live NF4 decode uses Start/End/Speaker/Content, often nested in a list."""
        from tts.lab.backend.services.speakers import parse_analysis

        raw = [
            [
                {
                    "Start": 0,
                    "End": 11.52,
                    "Speaker": 0,
                    "Content": "hello from one",
                },
                {
                    "Start": 11.52,
                    "End": 29.57,
                    "Speaker": 1,
                    "Content": "hello from two",
                },
                {
                    "Start": 29.57,
                    "End": 40.0,
                    "Speaker": 0,
                    "Content": "hello from one again",
                },
            ]
        ]
        parsed = parse_analysis(raw)
        ids = [seg["speaker_id"] for seg in parsed["segments"]]
        self.assertEqual(ids, ["S1", "S2", "S1"])
        self.assertEqual(
            [(seg["start_s"], seg["end_s"]) for seg in parsed["segments"]],
            [(0.0, 11.52), (11.52, 29.57), (29.57, 40.0)],
        )
        self.assertEqual(parsed["segments"][0]["text"], "hello from one")
        self.assertEqual({s["id"] for s in parsed["speakers"]}, {"S1", "S2"})
        self.assertEqual(parsed["overlaps"], [])


class SpeakerAnalysisApi(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.client = TestClient(create_app(root=self.root, leftover_parked=True))

    def tearDown(self) -> None:
        self.client.close()
        self.tmp.cleanup()

    def _create(self, name: str = "ata", transcript: str = "hello from one") -> dict:
        wav = _wav(self.root / f"{name}.wav")
        with wav.open("rb") as handle:
            response = self.client.post(
                "/api/voices",
                data={"name": name, "transcript": transcript, "tags": '["lab"]'},
                files={"audio": ("ata.wav", handle, "audio/wav")},
            )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _analyze(self, voice_id: str, analyzer: _RecordingAnalyzer | None = None) -> dict:
        with patch(ANALYZE_HOOK, analyzer or _RecordingAnalyzer()):
            response = self.client.post(f"/api/voices/{voice_id}/speakers/analyze")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_analyze_stores_analysis_and_returns_it_on_the_voice(self) -> None:
        created = self._create()
        self.assertIsNone(created["speaker_analysis"])
        body = self._analyze(created["id"])
        analysis = body["speaker_analysis"]
        self.assertTrue(analysis["id"])
        self.assertFalse(analysis["stale"])
        self.assertEqual(analysis["speakers"], FAKE["speakers"])
        self.assertEqual(len(analysis["segments"]), 4)
        self.assertEqual(analysis["overlaps"], FAKE["overlaps"])
        store = self.client.app.state.lab.store
        rows = store.execute(
            "SELECT * FROM speaker_analyses WHERE voice_id = ?", (created["id"],)
        ).fetchall()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        source = store.get(created["source_audio_artifact_id"])
        self.assertEqual(row["source_artifact_id"], source.id)
        self.assertEqual(row["processor"], PROCESSOR)
        self.assertEqual(row["model_id"], PROCESSOR_CONFIG["model_id"])
        self.assertEqual(row["cache_key"], analysis_cache_key(source.sha256, PROCESSOR, PROCESSOR_CONFIG))
        stored = json.loads(row["result_json"])
        self.assertEqual(len(stored["segments"]), 4)
        voice_row = store.execute(
            "SELECT speaker_analysis_id FROM voices WHERE id = ?", (created["id"],)
        ).fetchone()
        self.assertEqual(voice_row["speaker_analysis_id"], analysis["id"])
        self.assertEqual(self.client.get(f"/api/voices/{created['id']}").json()["speaker_analysis"], analysis)

    def test_analyze_reads_the_source_not_the_keep_crop(self) -> None:
        created = self._create()
        cropped = self.client.post(
            f"/api/voices/{created['id']}/reference/keep-only",
            json={"start_s": 0.0, "end_s": 1.0},
        )
        self.assertEqual(cropped.status_code, 200, cropped.text)
        analyzer = _RecordingAnalyzer()
        self._analyze(created["id"], analyzer)
        store = self.client.app.state.lab.store
        source_path = store.get(created["source_audio_artifact_id"]).path
        self.assertEqual(analyzer.paths, [source_path])

    def test_analyze_reuses_a_cached_analysis_for_the_same_source(self) -> None:
        created = self._create()
        analyzer = _RecordingAnalyzer()
        first = self._analyze(created["id"], analyzer)
        second = self._analyze(created["id"], analyzer)
        self.assertEqual(len(analyzer.paths), 1)
        self.assertEqual(second["speaker_analysis"]["id"], first["speaker_analysis"]["id"])
        store = self.client.app.state.lab.store
        count = store.execute(
            "SELECT COUNT(*) AS n FROM speaker_analyses WHERE voice_id = ?", (created["id"],)
        ).fetchone()["n"]
        self.assertEqual(count, 1)

    def test_analysis_is_stale_when_the_source_artifact_moves(self) -> None:
        created = self._create()
        analysis = self._analyze(created["id"])["speaker_analysis"]
        store = self.client.app.state.lab.store
        replacement = store.import_audio(_wav(self.root / "other.wav", seconds=3.0))
        store.execute(
            "UPDATE voices SET source_artifact_id = ? WHERE id = ?",
            (replacement.id, created["id"]),
        )
        store.commit()
        got = self.client.get(f"/api/voices/{created['id']}").json()
        self.assertEqual(got["speaker_analysis"]["id"], analysis["id"])
        self.assertTrue(got["speaker_analysis"]["stale"])
        still_there = store.execute(
            "SELECT COUNT(*) AS n FROM speaker_analyses WHERE id = ?", (analysis["id"],)
        ).fetchone()["n"]
        self.assertEqual(still_there, 1)

    def test_analyze_maps_missing_weights_to_501(self) -> None:
        created = self._create()
        with patch(ANALYZE_HOOK, side_effect=ProcessorUnavailable("vibevoice is not installed")):
            response = self.client.post(f"/api/voices/{created['id']}/speakers/analyze")
        self.assertEqual(response.status_code, 501, response.text)
        self.assertEqual(response.json()["detail"]["code"], "processor_unavailable")
        self.assertIsNone(self.client.get(f"/api/voices/{created['id']}").json()["speaker_analysis"])

    def test_default_analyzer_fails_closed_without_weights(self) -> None:
        if _vibevoice_installed():
            self.skipTest("vibevoice is installed on this box")
        with self.assertRaises(ProcessorUnavailable):
            default_analyze(_wav(self.root / "probe.wav", seconds=1.0))

    def _create_unlocked(self, name: str = "source") -> dict:
        """Auto-transcribed voice: unlocked, so use-speaker refreshes its words."""
        words = [
            {"text": "hello", "start_s": 0.0, "end_s": 1.0},
            {"text": "from", "start_s": 1.0, "end_s": 2.0},
            {"text": "one", "start_s": 2.0, "end_s": 6.0},
        ]
        with patch(
            "tts.lab.backend.services.voices.transcribe_alignment",
            return_value={"text": "hello from one", "words": words},
        ):
            return self._create(name=name, transcript="")

    def test_use_speaker_drops_overlap(self) -> None:
        created = self._create_unlocked()
        self.assertFalse(created["transcript_locked"])
        self.assertEqual(created["effective_transcript"], "hello from one")
        self._analyze(created["id"])
        used = self.client.post(
            f"/api/voices/{created['id']}/speakers/use", json={"speaker_id": "S2"}
        )
        self.assertEqual(used.status_code, 200, used.text)
        body = used.json()
        self.assertEqual(body["keep_intervals"], [{"start_s": 3.0, "end_s": 6.0}])
        self.assertIn("hello from two", body["effective_transcript"])
        self.assertNotIn("hello from one", body["effective_transcript"])

    def test_use_speaker_does_not_clobber_a_locked_transcript(self) -> None:
        created = self._create()
        self._analyze(created["id"])
        locked = self.client.patch(
            f"/api/voices/{created['id']}", json={"effective_transcript": "LOCKED"}
        )
        self.assertEqual(locked.status_code, 200, locked.text)
        self.assertTrue(locked.json()["transcript_locked"])
        used = self.client.post(
            f"/api/voices/{created['id']}/speakers/use", json={"speaker_id": "S2"}
        )
        self.assertEqual(used.status_code, 200, used.text)
        body = used.json()
        self.assertEqual(body["effective_transcript"], "LOCKED")
        self.assertEqual(body["keep_intervals"], [{"start_s": 3.0, "end_s": 6.0}])
        self.assertEqual(body["source_words"], [])

    def test_use_speaker_without_analysis_is_422(self) -> None:
        created = self._create()
        response = self.client.post(
            f"/api/voices/{created['id']}/speakers/use", json={"speaker_id": "S2"}
        )
        self.assertEqual(response.status_code, 422, response.text)

    def test_use_speaker_with_unknown_speaker_is_422(self) -> None:
        created = self._create()
        self._analyze(created["id"])
        response = self.client.post(
            f"/api/voices/{created['id']}/speakers/use", json={"speaker_id": "S9"}
        )
        self.assertEqual(response.status_code, 422, response.text)

    def test_deleting_a_voice_drops_its_analysis(self) -> None:
        created = self._create()
        analysis = self._analyze(created["id"])["speaker_analysis"]
        deleted = self.client.delete(f"/api/voices/{created['id']}")
        self.assertEqual(deleted.status_code, 200, deleted.text)
        store = self.client.app.state.lab.store
        left = store.execute(
            "SELECT COUNT(*) AS n FROM speaker_analyses WHERE id = ?", (analysis["id"],)
        ).fetchone()["n"]
        self.assertEqual(left, 0)

    def test_unlocked_transcribe_keeps_the_analysis(self) -> None:
        created = self._create_unlocked()
        analysis = self._analyze(created["id"])["speaker_analysis"]
        fake = types.ModuleType("breeze_tts_qual.transcribe")
        fake.transcribe_audio = lambda path: {"text": "fresh words", "words": []}
        with patch.dict(
            sys.modules,
            {
                "breeze_tts_qual": types.ModuleType("breeze_tts_qual"),
                "breeze_tts_qual.transcribe": fake,
            },
        ):
            response = self.client.post(f"/api/voices/{created['id']}/transcribe")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["source_transcript"], "fresh words")
        self.assertEqual(body["speaker_analysis"]["id"], analysis["id"])
        self.assertFalse(body["speaker_analysis"]["stale"])

    def test_transcribe_is_409_on_a_locked_transcript(self) -> None:
        created = self._create()
        self.client.patch(f"/api/voices/{created['id']}", json={"effective_transcript": "LOCKED"})
        response = self.client.post(f"/api/voices/{created['id']}/transcribe")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"]["code"], "transcript_locked")
        self.assertEqual(
            self.client.get(f"/api/voices/{created['id']}").json()["effective_transcript"], "LOCKED"
        )


class SpeakerAnalyzeWithRuntime(unittest.TestCase):
    """Analysis owns the single GPU behind ProcessorLease as the vibevoice occupant."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.factory = CountingWorkerFactory()
        self.runtime = E2RuntimeManager(
            vram=FixedVramProbe(free=12 * GIB),
            leftover=NoopLeftover(),
            worker_factory=self.factory,
            required_vram_bytes=8 * GIB,
            vram_margin_bytes=0,
        )
        self.client = TestClient(
            create_app(root=self.root, leftover_parked=False, e2_runtime=self.runtime)
        )

    def tearDown(self) -> None:
        self.client.close()
        self.tmp.cleanup()

    def _create(self, name: str = "ata") -> dict:
        wav = _wav(self.root / f"{name}.wav")
        with wav.open("rb") as handle:
            response = self.client.post(
                "/api/voices",
                data={"name": name, "transcript": "hello", "tags": "[]"},
                files={"audio": ("ata.wav", handle, "audio/wav")},
            )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_analyze_holds_the_vibevoice_lease_and_releases_it(self) -> None:
        created = self._create()
        seen: list[tuple[str, str | None]] = []

        def analyzer(path: Path | str) -> dict:
            status = self.runtime.status()
            seen.append((status.state, status.processor))
            return _fake_analysis()

        with patch(ANALYZE_HOOK, analyzer):
            response = self.client.post(f"/api/voices/{created['id']}/speakers/analyze")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(seen, [("unloaded", LEASE_NAME)])
        status = self.client.get("/api/runtime").json()
        self.assertIsNone(status["processor"])
        self.assertEqual(status["state"], "unloaded")
        self.assertEqual(self.factory.spawn_count, 0)

    def test_analyze_is_blocked_during_a_live_call(self) -> None:
        created = self._create()
        held = self.client.post("/api/runtime/live-call", json={"ttl_s": 45})
        self.assertEqual(held.status_code, 200, held.text)
        analyzer = _RecordingAnalyzer()
        with patch(ANALYZE_HOOK, analyzer):
            denied = self.client.post(f"/api/voices/{created['id']}/speakers/analyze")
        self.assertEqual(denied.status_code, 409, denied.text)
        self.assertEqual(denied.json()["detail"]["code"], "processor_blocked_live_call")
        self.assertEqual(analyzer.paths, [])
        self.assertIsNone(self.client.get(f"/api/voices/{created['id']}").json()["speaker_analysis"])
        self.assertEqual(self.factory.spawn_count, 0)


if __name__ == "__main__":
    unittest.main()
