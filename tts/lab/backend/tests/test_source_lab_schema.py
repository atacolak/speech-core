#!/usr/bin/env python3
"""Source-lab schema: media sources, range analyses, clips, speaker maps.

A source is not a voice. It exists unowned, carries range-scoped analyses with
source-local speakers, and each voice extracts its own clip from it. Overlap is
marked and excluded, never separated. No GPU: the analyzer is faked through the
service hook.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO))

from tts.lab.backend.app import create_app
from tts.lab.backend.store.db import connect
from tts.lab.backend.tests.test_voice_lab_schema import _legacy_db
from tts.wav import write_wav

ANALYZE_HOOK = "tts.lab.backend.services.speakers.analyze_hook"

# Two speakers, one shared 2.0–3.0 s overlap. Four turns over seven seconds.
FAKE = {
    "speakers": [
        {"id": "S1", "label": "Speaker 1", "duration_s": 2.0},
        {"id": "S2", "label": "Speaker 2", "duration_s": 3.0},
    ],
    "segments": [
        {"speaker_id": "S1", "start_s": 0.0, "end_s": 2.0, "text": "hello from one"},
        {"speaker_id": "S1", "start_s": 2.0, "end_s": 3.0, "text": "together"},
        {"speaker_id": "S2", "start_s": 2.0, "end_s": 3.0, "text": "together"},
        {"speaker_id": "S2", "start_s": 3.0, "end_s": 6.0, "text": "hello from two"},
    ],
}


class _RecordingAnalyzer:
    """Fake vibevoice: records the analyzed path, returns a canned payload."""

    def __init__(self, result: dict | None = None) -> None:
        self.paths: list[Path] = []
        self.result = result or FAKE

    def __call__(self, path: Path | str) -> dict:
        self.paths.append(Path(path))
        return json.loads(json.dumps(self.result))


def _wav(path: Path, *, seconds: float = 7.0, freq: float = 440.0) -> Path:
    sr = 24000
    write_wav(path, sr, 0.1 * np.sin(2 * np.pi * freq * np.arange(int(sr * seconds)) / sr))
    return path


def store_duration(root: Path, artifact_id: str) -> float:
    """Seconds of one stored artifact, straight from the live lab.sqlite3."""
    conn = sqlite3.connect(str(root / "lab.sqlite3"))
    try:
        row = conn.execute(
            "SELECT duration_s FROM artifacts WHERE id = ?", (artifact_id,)
        ).fetchone()
    finally:
        conn.close()
    return round(float(row[0]), 3)


class _SourceCase(unittest.TestCase):
    """Source + voice plumbing shared by the source-lab tests."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.client = TestClient(create_app(root=self.root, leftover_parked=True))

    def tearDown(self) -> None:
        self.client.close()
        self.tmp.cleanup()

    def _source(self, *, seconds: float = 7.0, title: str | None = None, name: str = "ww-01.wav") -> dict:
        wav = _wav(self.root / name, seconds=seconds)
        data = {} if title is None else {"title": title}
        with wav.open("rb") as handle:
            response = self.client.post(
                "/api/sources",
                data=data,
                files={"file": (name, handle, "audio/wav")},
            )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _voice(self, name: str) -> dict:
        wav = _wav(self.root / f"{name}.wav", seconds=4.0)
        with wav.open("rb") as handle:
            response = self.client.post(
                "/api/voices",
                data={"name": name, "transcript": "hello from ata", "tags": '["lab"]'},
                files={"audio": (f"{name}.wav", handle, "audio/wav")},
            )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _analyze(self, source_id: str, body: dict | None = None, analyzer: _RecordingAnalyzer | None = None) -> dict:
        with patch(ANALYZE_HOOK, analyzer or _RecordingAnalyzer()):
            response = self.client.post(f"/api/sources/{source_id}/analyze", json=body or {"all": True})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _map(self, source_id: str, local_id: str, voice_id: str | None) -> dict:
        response = self.client.post(
            f"/api/sources/{source_id}/speakers/{local_id}/map",
            json={"voice_id": voice_id},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _extract(self, source_id: str, local_id: str, ranges: list[dict]) -> dict:
        response = self.client.post(
            f"/api/sources/{source_id}/extract",
            json={"speaker_local_id": local_id, "ranges": ranges},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()


class SourceLabSources(_SourceCase):
    def test_a_source_exists_with_no_voice_and_no_clip(self) -> None:
        source = self._source(title="westworld clip 01")

        self.assertEqual(source["kind"], "file")
        self.assertEqual(source["title"], "westworld clip 01")
        self.assertEqual(source["duration_s"], 7.0)
        self.assertIsNotNone(source["audio_artifact_id"])
        self.assertEqual(source["coverage"], [])
        self.assertEqual(source["speakers"], [])
        self.assertEqual(source["clips"], [])
        self.assertEqual(self.client.get("/api/voices").json()["items"], [])

    def test_the_filename_is_the_default_title_and_meta_round_trips(self) -> None:
        wav = _wav(self.root / "austin-2026-01.wav")
        with wav.open("rb") as handle:
            response = self.client.post(
                "/api/sources",
                data={"meta": '{"channel": "ww"}'},
                files={"file": ("austin-2026-01.wav", handle, "audio/wav")},
            )
        self.assertEqual(response.status_code, 200, response.text)

        self.assertEqual(response.json()["title"], "austin-2026-01")
        self.assertEqual(response.json()["meta"], {"channel": "ww"})

    def test_a_listed_source_is_readable_by_id(self) -> None:
        created = self._source(title="westworld clip 01")

        listed = self.client.get("/api/sources").json()["items"]
        self.assertEqual([item["id"] for item in listed], [created["id"]])
        detail = self.client.get(f"/api/sources/{created['id']}").json()
        self.assertEqual(detail["id"], created["id"])
        self.assertEqual(detail["origin"], "ww-01.wav")

    def test_the_source_audio_plays_back_the_original_wav(self) -> None:
        created = self._source()

        audio = self.client.get(f"/api/sources/{created['id']}/audio")

        self.assertEqual(audio.status_code, 200, audio.text)
        self.assertEqual(audio.content[:4], b"RIFF")

    def test_an_uploaded_waveform_is_stored_alongside_the_source(self) -> None:
        wav = _wav(self.root / "ww-02.wav")
        peaks = self.root / "ww-02-peaks.json"
        peaks.write_text('{"peaks": [0.1, 0.2]}')
        with wav.open("rb") as audio, peaks.open("rb") as handle:
            response = self.client.post(
                "/api/sources",
                files={
                    "file": ("ww-02.wav", audio, "audio/wav"),
                    "waveform": ("ww-02-peaks.json", handle, "application/json"),
                },
            )

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertIsNotNone(body["waveform_artifact_id"])
        self.assertNotEqual(body["waveform_artifact_id"], body["audio_artifact_id"])


class SourceLabAnalysis(_SourceCase):
    def test_range_analysis_records_source_local_speakers_and_coverage(self) -> None:
        source = self._source()

        analyzed = self._analyze(source["id"])

        self.assertEqual(analyzed["coverage"], [{"start_s": 0.0, "end_s": 7.0}])
        self.assertEqual([item["local_id"] for item in analyzed["speakers"]], ["S1", "S2"])
        self.assertEqual([item["mapped_voice_id"] for item in analyzed["speakers"]], [None, None])
        self.assertEqual(
            [seg["text"] for seg in analyzed["analyses"][0]["result"]["segments"]],
            ["hello from one", "together", "together", "hello from two"],
        )
        self.assertEqual(
            analyzed["analyses"][0]["result"]["overlaps"],
            [{"start_s": 2.0, "end_s": 3.0, "speakers": ["S1", "S2"]}],
        )
        self.assertEqual(analyzed["analyses"][0]["processor"], "vibevoice-asr")

    def test_a_ranged_analysis_keeps_source_timeline_times(self) -> None:
        source = self._source()
        ranged = {"segments": [{"speaker_id": "S1", "start_s": 0.0, "end_s": 1.5, "text": "later"}]}

        analyzed = self._analyze(source["id"], {"start_s": 2.0, "end_s": 6.0}, _RecordingAnalyzer(ranged))

        self.assertEqual(analyzed["coverage"], [{"start_s": 2.0, "end_s": 6.0}])
        segment = analyzed["analyses"][0]["result"]["segments"][0]
        self.assertEqual((segment["start_s"], segment["end_s"]), (2.0, 3.5))

    def test_an_already_covered_range_never_reruns_the_processor(self) -> None:
        source = self._source()
        analyzer = _RecordingAnalyzer()
        first = self._analyze(source["id"], {"all": True}, analyzer)

        again = self._analyze(source["id"], {"all": True}, analyzer)
        inside = self._analyze(source["id"], {"start_s": 3.0, "end_s": 5.0}, analyzer)

        self.assertEqual(first["analyses_added"], 1)
        self.assertEqual(again["analyses_added"], 0)
        self.assertEqual(inside["analyses_added"], 0)
        self.assertEqual(len(inside["analyses"]), 1)
        self.assertEqual(len(analyzer.paths), 1)

    def test_coverage_survives_a_reopen(self) -> None:
        source = self._source()
        self._analyze(source["id"])
        self.client.close()

        conn = connect(self.root)
        self.addCleanup(conn.close)
        rows = conn.execute(
            "SELECT start_s, end_s, processor FROM source_analyses WHERE source_id = ?",
            (source["id"],),
        ).fetchall()

        self.assertEqual([(row["start_s"], row["end_s"]) for row in rows], [(0.0, 7.0)])
        self.assertEqual(rows[0]["processor"], "vibevoice-asr")


class SourceLabSpeakers(_SourceCase):
    def test_a_source_speaker_maps_to_a_voice(self) -> None:
        source = self._source()
        voice = self._voice("ford")
        self._analyze(source["id"])

        mapped = self._map(source["id"], "S1", voice["id"])

        by_id = {item["local_id"]: item["mapped_voice_id"] for item in mapped["speakers"]}
        self.assertEqual(by_id, {"S1": voice["id"], "S2": None})

    def test_mapping_an_unknown_speaker_or_voice_fails_closed(self) -> None:
        source = self._source()
        voice = self._voice("ford")
        self._analyze(source["id"])

        unknown_speaker = self.client.post(
            f"/api/sources/{source['id']}/speakers/S9/map", json={"voice_id": voice["id"]}
        )
        unknown_voice = self.client.post(
            f"/api/sources/{source['id']}/speakers/S1/map", json={"voice_id": "vp_missing"}
        )

        self.assertEqual(unknown_speaker.status_code, 404, unknown_speaker.text)
        self.assertEqual(unknown_voice.status_code, 404, unknown_voice.text)

    def test_a_later_range_analysis_keeps_the_existing_mapping(self) -> None:
        source = self._source(seconds=12.0)
        voice = self._voice("ford")
        self._analyze(source["id"], {"start_s": 0.0, "end_s": 6.0})
        self._map(source["id"], "S1", voice["id"])

        analyzed = self._analyze(source["id"], {"start_s": 6.0, "end_s": 12.0})

        by_id = {item["local_id"]: item["mapped_voice_id"] for item in analyzed["speakers"]}
        self.assertEqual(by_id, {"S1": voice["id"], "S2": None})
        # Coverage is the merged analyzed span: 0-6 plus 6-12 is all of it.
        self.assertEqual(analyzed["coverage"], [{"start_s": 0.0, "end_s": 12.0}])


class SourceLabExtract(_SourceCase):
    def test_two_voices_each_own_a_clip_from_one_source(self) -> None:
        source = self._source()
        ford = self._voice("ford")
        dolores = self._voice("dolores")
        self._analyze(source["id"])
        self._map(source["id"], "S1", ford["id"])
        self._map(source["id"], "S2", dolores["id"])

        ford_clip = self._extract(
            source["id"], "S1", [{"start_s": 0.0, "end_s": 2.0}]
        )
        dolores_clip = self._extract(
            source["id"], "S2", [{"start_s": 3.0, "end_s": 6.0}]
        )

        self.assertEqual(ford_clip["source_id"], source["id"])
        self.assertEqual(dolores_clip["source_id"], source["id"])
        self.assertEqual(ford_clip["voice_id"], ford["id"])
        self.assertEqual(dolores_clip["voice_id"], dolores["id"])
        self.assertEqual(ford_clip["ranges"], [{"start_s": 0.0, "end_s": 2.0}])
        self.assertEqual(ford_clip["clean_transcript"], "hello from one")
        self.assertEqual(dolores_clip["clean_transcript"], "hello from two")
        self.assertEqual(
            (self.client.get(f"/api/sources/{source['id']}").json())["clips"].__len__(), 2
        )
        detail = self.client.get(f"/api/sources/{source['id']}").json()
        clip_durations = sorted(
            store_duration(self.root, clip["audio_artifact_id"]) for clip in detail["clips"]
        )
        self.assertEqual(clip_durations, [2.0, 3.0])

    def test_extracted_clips_reach_each_voice(self) -> None:
        source = self._source()
        ford = self._voice("ford")
        dolores = self._voice("dolores")
        self._analyze(source["id"])
        self._map(source["id"], "S1", ford["id"])
        self._map(source["id"], "S2", dolores["id"])
        ford_clip = self._extract(source["id"], "S1", [{"start_s": 0.0, "end_s": 2.0}])
        dolores_clip = self._extract(source["id"], "S2", [{"start_s": 3.0, "end_s": 6.0}])

        ford_profile = self.client.get(f"/api/voices/{ford['id']}").json()
        dolores_profile = self.client.get(f"/api/voices/{dolores['id']}").json()

        self.assertEqual([clip["id"] for clip in ford_profile["clips"]], [ford_clip["id"]])
        self.assertEqual([clip["id"] for clip in dolores_profile["clips"]], [dolores_clip["id"]])
        self.assertEqual(ford_profile["clips"][0]["source_title"], "ww-01")

    def test_an_unmapped_speaker_extracts_an_unowned_clip(self) -> None:
        source = self._source()
        self._analyze(source["id"])

        clip = self._extract(source["id"], "S1", [{"start_s": 0.0, "end_s": 2.0}])

        self.assertIsNone(clip["voice_id"])
        self.assertEqual(self.client.get("/api/voices").json()["items"], [])

    def test_extract_rejects_overlapping_ranges(self) -> None:
        source = self._source()
        self._analyze(source["id"])

        response = self.client.post(
            f"/api/sources/{source['id']}/extract",
            json={
                "speaker_local_id": "S1",
                "ranges": [{"start_s": 0.0, "end_s": 2.0}, {"start_s": 1.0, "end_s": 3.0}],
            },
        )

        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["detail"]["code"], "overlapping_ranges")
        self.assertEqual(self.client.get(f"/api/sources/{source['id']}").json()["clips"], [])

    def test_extract_rejects_a_range_past_the_end_of_the_audio(self) -> None:
        source = self._source(seconds=7.0)
        self._analyze(source["id"])

        response = self.client.post(
            f"/api/sources/{source['id']}/extract",
            json={"speaker_local_id": "S1", "ranges": [{"start_s": 6.0, "end_s": 9.0}]},
        )

        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["detail"]["code"], "invalid_range")
        self.assertEqual(self.client.get(f"/api/sources/{source['id']}").json()["clips"], [])

    def test_extract_rejects_an_unknown_speaker(self) -> None:
        source = self._source()
        self._analyze(source["id"])

        response = self.client.post(
            f"/api/sources/{source['id']}/extract",
            json={"speaker_local_id": "S9", "ranges": [{"start_s": 0.0, "end_s": 2.0}]},
        )

        self.assertEqual(response.status_code, 404, response.text)

    def test_clean_transcript_joins_selected_turns_without_speaker_markup(self) -> None:
        source = self._source()
        marked = {
            "segments": [
                {"speaker_id": "S1", "start_s": 0.0, "end_s": 0.6, "text": "SPEAKER_00: hello"},
                {"speaker_id": "S1", "start_s": 0.6, "end_s": 2.0, "text": "from one"},
                {"speaker_id": "S2", "start_s": 3.0, "end_s": 6.0, "text": "SPEAKER_01: hello from two"},
            ]
        }
        self._analyze(source["id"], {"all": True}, _RecordingAnalyzer(marked))

        clip = self._extract(source["id"], "S1", [{"start_s": 0.0, "end_s": 2.0}])

        self.assertEqual(clip["clean_transcript"], "hello from one")
        self.assertNotIn("SPEAKER_", clip["clean_transcript"])
        self.assertEqual(
            [seg["text"] for seg in clip["segments"]], ["SPEAKER_00: hello", "from one"]
        )


class SourceLabLegacy(unittest.TestCase):
    """An operator's frozen 1:1 lab.sqlite3 gains the source tables untouched."""

    def test_legacy_voice_sources_survive_the_new_tables(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        _legacy_db(root)

        conn = connect(root)
        self.addCleanup(conn.close)

        for table in ("media_sources", "source_analyses", "source_speakers", "clips"):
            self.assertEqual(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"], 0)
        sources = conn.execute(
            "SELECT * FROM voice_sources WHERE voice_id = 'vp_1' ORDER BY rowid ASC"
        ).fetchall()
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["artifact_id"], "a_src1")

        reopened = connect(root)
        self.addCleanup(reopened.close)
        self.assertEqual(
            reopened.execute("SELECT COUNT(*) AS n FROM voice_sources").fetchone()["n"], 2
        )
        self.assertEqual(
            reopened.execute("SELECT COUNT(*) AS n FROM media_sources").fetchone()["n"], 0
        )


if __name__ == "__main__":
    unittest.main()
