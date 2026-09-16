#!/usr/bin/env python3
"""Bounded source enrolment: retained-artifact ingest, cap, lineage.

A retained take/artifact is registered as a material by pointing at the object
the run already owns: the audio is never copied and no other run is imported.
Extracting a mapped speaker enrols one immutable voice source plus its ORIGINAL
lineage root; the voice's `source_limit` refuses further enrolment without
materializing audio or writing any row. Crop and processor children carry
`parent_id` + `source_id` and leave the parent bytes and rows untouched.

No GPU, no runtime, no weights: denoise is faked at its module hook.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "lab" / "scripts"))

from breeze_tts_qual.configs import EngineConfig
from breeze_tts_qual.engine import BreezeEngine, FakeBackend

from tts.lab.backend.tests.test_auk_artifacts import _fake_denoise
from tts.lab.backend.tests.test_source_lab_schema import _SourceCase, _wav
from tts.packets import new_id

DENOISE_HOOK = "tts.lab.backend.services.resemble.denoise_hook"
LEASE = "tts.lab.backend.routes.voices.ProcessorLease"


def _objects(root: Path) -> set[str]:
    return {path.name for path in (root / "objects").iterdir()}


class SourceArtifactIngestTest(_SourceCase):
    """A retained run becomes a material; its audio object is reused as-is."""

    def _engine(self) -> Any:
        return BreezeEngine(
            EngineConfig(name="E2", precision="bf16"), ckpt_dir=".", backend=FakeBackend()
        )

    def _synth(self, voice_id: str, text: str) -> dict:
        response = self.client.post(
            "/api/synthesize",
            json={
                "text": text,
                "steer": "dry",
                "voice_profile_id": voice_id,
                "generation": {
                    "guidance": {"mode": "single", "cfg": 4.0},
                    "seed": 7,
                    "temperature": 0.8,
                },
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _stray_run(self, voice_id: str) -> dict:
        """A second retained take with its own bytes: ingest must not touch it."""
        wav = _wav(self.root / "stray-run.wav", seconds=1.5, freq=880.0)
        artifact = self.store.import_audio(wav)
        self.store.pin(artifact.id, reason="run:stray")
        run_id = new_id("run")
        self.store.execute(
            """
            INSERT INTO runs (
                id, voice_id, request_json, output_artifact_id, effective_reference_json,
                latency_ms, first_audio_ms, duration_s, rating, tags_json, created_at,
                alignment_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                voice_id,
                json.dumps({"text": "a stray take"}),
                artifact.id,
                "{}",
                12.0,
                None,
                float(artifact.duration_s or 0.0),
                "keep",
                "[]",
                "2026-09-16T00:00:00Z",
                None,
            ),
        )
        self.store.commit()
        return {"id": run_id, "output_artifact_id": artifact.id}

    def test_retained_run_registers_source_without_copying_audio(self) -> None:
        voice = self._voice("ata")
        stray = self._stray_run(voice["id"])
        run = self._synth(voice["id"], "one process that does not suck.")
        before = _objects(self.root)

        response = self.client.post(
            "/api/sources/from-artifact",
            json={
                "artifact_id": run["output_artifact_id"],
                "run_id": run["id"],
                "title": "kept generation",
            },
        )

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["audio_artifact_id"], run["output_artifact_id"])
        self.assertEqual(body["title"], "kept generation")
        self.assertEqual(body["kind"], "file")
        self.assertEqual(body["meta"]["run_id"], run["id"])
        self.assertEqual(body["meta"]["artifact_id"], run["output_artifact_id"])
        self.assertIsNotNone(body["waveform_artifact_id"])
        self.assertNotEqual(body["waveform_artifact_id"], body["audio_artifact_id"])
        self.assertGreater(body["duration_s"], 0.0)
        row = self.store.execute(
            "SELECT audio_artifact_id FROM media_sources WHERE id = ?", (body["id"],)
        ).fetchone()
        self.assertEqual(row["audio_artifact_id"], run["output_artifact_id"])
        # The audio object is reused, never copied. Only its peaks artifact is new.
        after = _objects(self.root)
        self.assertEqual(
            {name for name in after if name.endswith(".wav")},
            {name for name in before if name.endswith(".wav")},
        )
        # One explicit registration: no run history was bulk-imported.
        listed = self.client.get("/api/sources").json()["items"]
        self.assertEqual([item["id"] for item in listed], [body["id"]])
        self.assertNotIn(
            stray["output_artifact_id"], [item["audio_artifact_id"] for item in listed]
        )
        # Ingest into the library never enrols onto a voice.
        self.assertEqual(len(self.client.get(f"/api/voices/{voice['id']}").json()["sources"]), 1)

    def test_an_artifact_no_run_owns_is_refused(self) -> None:
        voice = self._voice("ata")
        stray = self._stray_run(voice["id"])
        run = self._synth(voice["id"], "one process that does not suck.")

        mismatch = self.client.post(
            "/api/sources/from-artifact",
            json={
                "artifact_id": stray["output_artifact_id"],
                "run_id": run["id"],
                "title": "not this run's audio",
            },
        )
        unknown_artifact = self.client.post(
            "/api/sources/from-artifact", json={"artifact_id": "a_missing"}
        )
        unknown_run = self.client.post(
            "/api/sources/from-artifact",
            json={"artifact_id": run["output_artifact_id"], "run_id": "run_missing"},
        )

        self.assertEqual(mismatch.status_code, 422, mismatch.text)
        self.assertEqual(mismatch.json()["detail"]["code"], "artifact_run_mismatch")
        self.assertEqual(unknown_artifact.status_code, 404, unknown_artifact.text)
        self.assertEqual(unknown_run.status_code, 404, unknown_run.text)
        self.assertEqual(self.client.get("/api/sources").json()["items"], [])

    def test_a_retained_source_is_readable_and_playable(self) -> None:
        voice = self._voice("ata")
        run = self._synth(voice["id"], "one process that does not suck.")

        created = self.client.post(
            "/api/sources/from-artifact",
            json={"artifact_id": run["output_artifact_id"], "run_id": run["id"]},
        )
        self.assertEqual(created.status_code, 200, created.text)
        source_id = created.json()["id"]

        detail = self.client.get(f"/api/sources/{source_id}")
        audio = self.client.get(f"/api/sources/{source_id}/audio")

        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(detail.json()["audio_artifact_id"], run["output_artifact_id"])
        self.assertEqual(audio.status_code, 200, audio.text)
        self.assertEqual(audio.content[:4], b"RIFF")


class SourceEnrollmentTest(_SourceCase):
    """Mapped extraction enrols one immutable source and its lineage root."""

    @property
    def store(self) -> Any:
        return self.client.app.state.lab.store

    def _count(self, table: str, voice_id: str) -> int:
        row = self.store.execute(
            f"SELECT COUNT(*) AS n FROM {table} WHERE voice_id = ?", (voice_id,)
        ).fetchone()
        return int(row["n"])

    def _object_sha(self, artifact_id: str) -> str:
        return hashlib.sha256(self.store.get(artifact_id).path.read_bytes()).hexdigest()

    def _row(self, table: str, row_id: str) -> dict[str, Any]:
        return dict(self.store.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone())

    def _profile(self, voice_id: str) -> dict[str, Any]:
        response = self.client.get(f"/api/voices/{voice_id}")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _enrol(self, source_name: str, freq: float = 440.0) -> tuple[dict, dict]:
        """One source, one mapped clip: the explicit enrolment the bench performs."""
        source = self._source(title=source_name.replace(".wav", ""), name=source_name, freq=freq)
        voice = self._voice("ford")
        self._analyze(source["id"])
        self._map(source["id"], "S1", voice["id"])
        clip = self._extract(source["id"], "S1", [{"start_s": 0.0, "end_s": 2.0}])
        return voice, clip

    def _denoise(self, voice_id: str, source_id: str | None = None) -> tuple[Any, list[str]]:
        """Denoise the enrolled source (or the primary one) with fakes: no weights."""
        held: list[str] = []

        class _Lease:
            def __init__(self, runtime: Any) -> None:
                self.runtime = runtime

            def acquire(self, name: str):
                held.append(name)
                return contextlib.nullcontext()

        path = (
            f"/api/voices/{voice_id}/reference/denoise"
            if source_id is None
            else f"/api/voices/{voice_id}/sources/{source_id}/denoise"
        )
        with patch(DENOISE_HOOK, _fake_denoise), patch(LEASE, _Lease):
            response = self.client.post(path)
        return response, held

    def test_extract_enrols_source_and_reference_with_clean_transcript(self) -> None:
        voice, clip = self._enrol("ww-a.wav")

        self.assertIsNotNone(clip["voice_source_id"])
        self.assertIsNotNone(clip["voice_artifact_id"])
        profile = self._profile(voice["id"])
        self.assertEqual([item["id"] for item in profile["sources"]][1:], [clip["voice_source_id"]])
        enrolled = next(item for item in profile["sources"] if item["id"] == clip["voice_source_id"])
        self.assertEqual(enrolled["label"], "ww-a")
        self.assertEqual(enrolled["artifact_id"], clip["audio_artifact_id"])
        self.assertEqual(enrolled["transcript"], "hello from one")
        self.assertFalse(enrolled["transcript_locked"])
        original = next(item for item in profile["artifacts"] if item["id"] == clip["voice_artifact_id"])
        self.assertEqual(original["kind"], "original")
        self.assertEqual(original["role"], "reference")
        self.assertEqual(original["source_id"], clip["voice_source_id"])
        self.assertEqual(original["audio_artifact_id"], clip["audio_artifact_id"])
        self.assertIsNone(original["parent_id"])
        self.assertFalse(original["stale"])
        # The new original is never selected for the operator.
        self.assertFalse(original["default"])
        self.assertIsNone(profile["default_reference_id"])
        self.assertEqual(self._count("voice_sources", voice["id"]), 2)
        self.assertEqual(self._count("voice_artifacts", voice["id"]), 1)
        self.assertEqual(
            self._row("voice_sources", clip["voice_source_id"])["artifact_id"],
            clip["audio_artifact_id"],
        )

    def test_an_unmapped_speaker_enrols_nothing(self) -> None:
        source = self._source(title="ww-a")
        self._analyze(source["id"])

        clip = self._extract(source["id"], "S1", [{"start_s": 0.0, "end_s": 2.0}])

        self.assertIsNone(clip["voice_source_id"])
        self.assertIsNone(clip["voice_artifact_id"])
        self.assertEqual(
            self.store.execute("SELECT COUNT(*) AS n FROM voice_artifacts").fetchone()["n"], 0
        )

    def test_source_limit_rejects_atomically(self) -> None:
        first = self._source(title="ww-a", name="ww-a.wav")
        voice = self._voice("ford")
        # The import already owns one slot, so two is this voice's whole budget.
        patched = self.client.patch(f"/api/voices/{voice['id']}", json={"source_limit": 2})
        self.assertEqual(patched.status_code, 200, patched.text)
        self.assertEqual(patched.json()["source_limit"], 2)
        self._analyze(first["id"])
        self._map(first["id"], "S1", voice["id"])
        enrolled = self._extract(first["id"], "S1", [{"start_s": 0.0, "end_s": 2.0}])
        self.assertIsNotNone(enrolled["voice_source_id"])
        self.assertEqual(self._count("voice_sources", voice["id"]), 2)
        second = self._source(title="ww-b", name="ww-b.wav", freq=660.0)
        self._analyze(second["id"])
        self._map(second["id"], "S1", voice["id"])
        before = {
            table: self._count(table, voice["id"])
            for table in ("voice_sources", "voice_artifacts", "clips")
        }
        objects_before = _objects(self.root)

        response = self.client.post(
            f"/api/sources/{second['id']}/extract",
            json={"speaker_local_id": "S1", "ranges": [{"start_s": 0.0, "end_s": 2.0}]},
        )

        self.assertEqual(response.status_code, 409, response.text)
        detail = response.json()["detail"]
        self.assertEqual(detail["code"], "source_limit_reached")
        self.assertIn("2", detail["message"])
        self.assertEqual(
            {table: self._count(table, voice["id"]) for table in before}, before
        )
        self.assertEqual(_objects(self.root), objects_before)
        self.assertEqual(
            self.client.get(f"/api/sources/{second['id']}").json()["clips"], []
        )

    def test_crop_creates_child_and_preserves_parent(self) -> None:
        voice, clip = self._enrol("ww-a.wav")
        parent_audio = clip["audio_artifact_id"]
        before_sha = self._object_sha(parent_audio)
        before_path = self.store.get(parent_audio).path
        before_source_row = self._row("voice_sources", clip["voice_source_id"])
        before_parent_row = self._row("voice_artifacts", clip["voice_artifact_id"])
        before_parent = next(
            item
            for item in self._profile(voice["id"])["artifacts"]
            if item["id"] == clip["voice_artifact_id"]
        )

        response = self.client.post(
            f"/api/voices/{voice['id']}/sources/{clip['voice_source_id']}/crop",
            json={"intervals": [{"start_s": 0.5, "end_s": 1.5}], "name": "tight crop"},
        )

        self.assertEqual(response.status_code, 200, response.text)
        profile = response.json()
        children = [
            item for item in profile["artifacts"] if item["parent_id"] == clip["voice_artifact_id"]
        ]
        self.assertEqual(len(children), 1)
        child = children[0]
        self.assertEqual(child["source_id"], clip["voice_source_id"])
        self.assertEqual(child["keep_intervals"], [{"start_s": 0.5, "end_s": 1.5}])
        self.assertEqual(child["name"], "tight crop")
        self.assertEqual(child["role"], "experiment")
        self.assertNotEqual(child["audio_artifact_id"], parent_audio)
        self.assertEqual(
            round(self.store.get(child["audio_artifact_id"]).duration_s or 0.0, 2), 1.0
        )
        # The parent audio, its object and both rows are untouched.
        self.assertEqual(self._object_sha(parent_audio), before_sha)
        self.assertEqual(self.store.get(parent_audio).path, before_path)
        self.assertEqual(self._row("voice_sources", clip["voice_source_id"]), before_source_row)
        self.assertEqual(self._row("voice_artifacts", clip["voice_artifact_id"]), before_parent_row)
        self.assertEqual(
            next(
                item
                for item in profile["artifacts"]
                if item["id"] == clip["voice_artifact_id"]
            ),
            before_parent,
        )
        self.assertEqual(self._count("voice_sources", voice["id"]), 2)
        self.assertEqual(self._count("voice_artifacts", voice["id"]), 2)

    def test_crop_rejects_an_empty_interval_list(self) -> None:
        voice, clip = self._enrol("ww-a.wav")

        response = self.client.post(
            f"/api/voices/{voice['id']}/sources/{clip['voice_source_id']}/crop",
            json={"intervals": [], "name": "nothing"},
        )

        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self._count("voice_artifacts", voice["id"]), 1)

    def test_source_denoise_creates_child_and_preserves_parent(self) -> None:
        voice, clip = self._enrol("ww-a.wav")
        parent_audio = clip["audio_artifact_id"]
        before_sha = self._object_sha(parent_audio)
        before_path = self.store.get(parent_audio).path
        before_source_row = self._row("voice_sources", clip["voice_source_id"])
        before_parent_row = self._row("voice_artifacts", clip["voice_artifact_id"])

        response, held = self._denoise(voice["id"], clip["voice_source_id"])

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(held, ["resemble"])
        profile = response.json()
        children = [
            item
            for item in profile["artifacts"]
            if item["parent_id"] == clip["voice_artifact_id"] and item["kind"] == "resemble"
        ]
        self.assertEqual(len(children), 1)
        child = children[0]
        self.assertEqual(child["role"], "experiment")
        self.assertEqual(child["source_id"], clip["voice_source_id"])
        self.assertNotEqual(child["audio_artifact_id"], parent_audio)
        self.assertEqual(
            round(self.store.get(child["audio_artifact_id"]).duration_s or 0.0, 2), 2.0
        )
        self.assertEqual(self._object_sha(parent_audio), before_sha)
        self.assertEqual(self.store.get(parent_audio).path, before_path)
        self.assertEqual(self._row("voice_sources", clip["voice_source_id"]), before_source_row)
        self.assertEqual(self._row("voice_artifacts", clip["voice_artifact_id"]), before_parent_row)

    def test_legacy_denoise_still_cleans_the_primary_source(self) -> None:
        voice = self._voice("ford")
        profile = self._profile(voice["id"])
        primary = profile["sources"][0]
        self.assertEqual(len(profile["sources"]), 1)

        response, held = self._denoise(voice["id"])

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(held, ["resemble"])
        refreshed = response.json()
        children = [
            item for item in refreshed["artifacts"] if item["kind"] == "resemble"
        ]
        self.assertEqual(len(children), 1)
        child = children[0]
        self.assertEqual(child["source_id"], primary["id"])
        self.assertIsNotNone(child["parent_id"])
        self.assertEqual(
            self._row("voice_artifacts", child["parent_id"])["audio_artifact_id"],
            primary["artifact_id"],
        )

    def test_source_transcript_lock_is_per_source(self) -> None:
        voice, clip = self._enrol("ww-a.wav")
        second = self._source(title="ww-b", name="ww-b.wav", freq=660.0)
        self._analyze(second["id"])
        self._map(second["id"], "S1", voice["id"])
        other = self._extract(second["id"], "S1", [{"start_s": 0.0, "end_s": 2.0}])
        primary = self._profile(voice["id"])["sources"][0]

        patched = self.client.patch(
            f"/api/voices/{voice['id']}/sources/{clip['voice_source_id']}",
            json={"transcript": "manual words for the enrolled source"},
        )

        self.assertEqual(patched.status_code, 200, patched.text)
        body = patched.json()
        by_id = {item["id"]: item for item in body["sources"]}
        self.assertEqual(
            by_id[clip["voice_source_id"]]["transcript"], "manual words for the enrolled source"
        )
        self.assertTrue(by_id[clip["voice_source_id"]]["transcript_locked"])
        self.assertEqual(by_id[other["voice_source_id"]]["transcript"], "hello from one")
        self.assertFalse(by_id[other["voice_source_id"]]["transcript_locked"])
        self.assertEqual(by_id[primary["id"]]["transcript"], "hello from ata")
        self.assertFalse(by_id[primary["id"]]["transcript_locked"])
        # The legacy columns belong to the primary source, not the edited one.
        self.assertEqual(body["source_transcript"], "hello from ata")
        self.assertEqual(body["effective_transcript"], "hello from ata")
        self.assertFalse(body["transcript_locked"])

        mirrored = self.client.patch(
            f"/api/voices/{voice['id']}/sources/{primary['id']}",
            json={"transcript": "manual words for the primary source"},
        )

        self.assertEqual(mirrored.status_code, 200, mirrored.text)
        primary_body = mirrored.json()
        primary_status = {item["id"]: item for item in primary_body["sources"]}
        self.assertEqual(
            primary_status[primary["id"]]["transcript"], "manual words for the primary source"
        )
        self.assertTrue(primary_status[primary["id"]]["transcript_locked"])
        self.assertEqual(primary_body["source_transcript"], "manual words for the primary source")
        self.assertEqual(primary_body["effective_transcript"], "manual words for the primary source")
        self.assertTrue(primary_body["transcript_locked"])
        self.assertEqual(
            primary_status[clip["voice_source_id"]]["transcript"],
            "manual words for the enrolled source",
        )
        self.assertTrue(primary_status[clip["voice_source_id"]]["transcript_locked"])

    def test_patching_an_unknown_source_fails_closed(self) -> None:
        voice, clip = self._enrol("ww-a.wav")
        other = self._voice("dolores")

        missing = self.client.patch(
            f"/api/voices/{voice['id']}/sources/vs_missing", json={"transcript": "x"}
        )
        foreign = self.client.patch(
            f"/api/voices/{other['id']}/sources/{clip['voice_source_id']}",
            json={"transcript": "x"},
        )

        self.assertEqual(missing.status_code, 404, missing.text)
        self.assertEqual(foreign.status_code, 404, foreign.text)
        self.assertEqual(
            self._row("voice_sources", clip["voice_source_id"])["transcript"], "hello from one"
        )


if __name__ == "__main__":
    unittest.main()
