#!/usr/bin/env python3
"""Voice-lab profile schema: sources, experiments, references, legacy migrate.

A voice is a profile: many immutable sources, many experiments, many approved
references, one optional default. No GPU, no AuK weights: candidates are
rendered locally so the store contract is testable without the model.
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
from tts.lab.backend.services import candidates, voices
from tts.lab.backend.store.db import connect
from tts.lab.backend.tests.test_auk_artifacts import _fake_denoise
from tts.wav import read_wav, write_wav

DENOISE_HOOK = "tts.lab.backend.services.resemble.denoise_hook"

INSTRUCTION = "keep the voice, drop the room"
PROVENANCE = {
    "auk_task": "enhance",
    "instruction": INSTRUCTION,
    "model_variant": "auk-base",
    "auk_precision": "bf16",
    "encoder_precision": "w4a8",
    "seed": 7,
    "settings": {"nfe": 32},
}


def _wav(path: Path, *, freq: float = 440.0) -> Path:
    sr = 24000
    write_wav(path, sr, 0.1 * np.sin(2 * np.pi * freq * np.arange(sr // 5) / sr))
    return path


class _LabCase(unittest.TestCase):
    """Voice + artifact plumbing shared by the profile schema tests."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.client = TestClient(create_app(root=self.root, leftover_parked=True))

    def tearDown(self) -> None:
        self.client.close()
        self.tmp.cleanup()

    @property
    def store(self):
        return self.client.app.state.lab.store

    def _create_voice(self, name: str = "ata", *, transcript: str = "hello from ata") -> dict:
        wav = _wav(self.root / f"{name}.wav")
        with wav.open("rb") as handle:
            response = self.client.post(
                "/api/voices",
                data={"name": name, "transcript": transcript, "tags": '["lab"]'},
                files={"audio": (f"{name}.wav", handle, "audio/wav")},
            )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _voice(self, voice_id: str) -> dict:
        response = self.client.get(f"/api/voices/{voice_id}")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _render(self, artifact_id: str, *, gain: float, tag: str):
        """A stand-in for an AuK render: read the source, write different bytes."""
        source = self.store.get(artifact_id)
        sr, samples = read_wav(source.path)
        out = self.root / f"render-{tag}.wav"
        write_wav(out, sr, (np.asarray(samples, dtype=np.float32) * gain).astype(np.float32))
        return self.store.import_audio(out)

    def _candidate(
        self,
        voice: dict,
        *,
        tag: str = "a",
        gain: float = 0.5,
        parent: str | None = None,
        source: str | None = None,
        **overrides,
    ) -> str:
        artifact = self._render(source or voice["source_audio_artifact_id"], gain=gain, tag=tag)
        return candidates.record_auk_candidate(
            self.store,
            voice["id"],
            audio_artifact_id=artifact.id,
            parent_variant_id=parent,
            **{**PROVENANCE, **overrides},
        )


class VoiceLabSources(_LabCase):
    def test_new_voice_has_one_source_and_no_artifacts(self) -> None:
        created = self._create_voice()

        self.assertIsNone(created["default_reference_id"])
        self.assertEqual(created["artifacts"], [])
        self.assertEqual(len(created["sources"]), 1)
        source = created["sources"][0]
        self.assertEqual(source["artifact_id"], created["source_audio_artifact_id"])
        self.assertEqual(source["transcript"], "hello from ata")
        self.assertEqual(source["keep_intervals"], created["keep_intervals"])
        self.assertGreater(source["duration_s"], 0)
        self.assertTrue(source["label"])
        self.assertTrue(source["id"])

    def test_adding_a_second_source_keeps_both(self) -> None:
        created = self._create_voice()
        other = self.store.import_audio(_wav(self.root / "second.wav", freq=220.0))

        voices.add_voice_source(
            self.store,
            created["id"],
            artifact_id=other.id,
            transcript="second take",
        )

        voice = self._voice(created["id"])
        self.assertEqual(len(voice["sources"]), 2)
        primary, added = voice["sources"]
        self.assertEqual(primary["artifact_id"], created["source_audio_artifact_id"])
        self.assertEqual(added["artifact_id"], other.id)
        self.assertEqual(added["transcript"], "second take")
        self.assertNotEqual(primary["id"], added["id"])
        # The legacy single-source pointer still names the primary source.
        self.assertEqual(voice["source_audio_artifact_id"], created["source_audio_artifact_id"])


class VoiceLabArtifacts(_LabCase):
    def test_candidate_is_an_experiment(self) -> None:
        voice = self._create_voice()

        variant_id = self._candidate(voice, tag="a")

        after = self._voice(voice["id"])
        self.assertEqual(len(after["artifacts"]), 1)
        item = after["artifacts"][0]
        self.assertEqual(item["id"], variant_id)
        self.assertEqual(item["role"], "experiment")
        self.assertEqual(item["kind"], "auk")
        self.assertEqual(item["name"], "AuK enhance")
        self.assertEqual(item["auk_task"], "enhance")
        self.assertEqual(item["instruction"], PROVENANCE["instruction"])
        self.assertEqual(item["seed"], PROVENANCE["seed"])
        self.assertEqual(item["settings"], PROVENANCE["settings"])
        self.assertEqual(item["source_id"], after["sources"][0]["id"])
        # Rendered from the source itself: the source row is the lineage.
        self.assertIsNone(item["parent_id"])
        self.assertFalse(item["approved"])
        self.assertIsNone(item["approved_at"])
        self.assertFalse(item["default"])
        self.assertFalse(item["stale"])
        self.assertIsNone(after["default_reference_id"])

    def test_two_experiments_become_two_references(self) -> None:
        voice = self._create_voice()
        first = self._candidate(voice, tag="a", gain=0.5)
        second = self._candidate(voice, tag="b", gain=0.25)

        both = self._voice(voice["id"])["artifacts"]
        self.assertEqual([item["role"] for item in both], ["experiment", "experiment"])

        candidates.set_candidate_approved(self.store, voice["id"], first)
        after_first = self._voice(voice["id"])["artifacts"]
        self.assertEqual([item["role"] for item in after_first], ["reference", "experiment"])
        self.assertIsNotNone(after_first[0]["approved_at"])
        self.assertIsNone(after_first[1]["approved_at"])

        candidates.set_candidate_approved(self.store, voice["id"], second)
        after_second = self._voice(voice["id"])["artifacts"]
        self.assertEqual([item["role"] for item in after_second], ["reference", "reference"])
        self.assertTrue(all(item["approved"] for item in after_second))
        # Approving is not choosing: the profile still has no default reference.
        self.assertIsNone(self._voice(voice["id"])["default_reference_id"])

    def test_artifact_lineage_records_its_parent(self) -> None:
        voice = self._create_voice()
        parent = self._candidate(voice, tag="a", gain=0.5)
        parent_audio = self._voice(voice["id"])["artifacts"][0]["audio_artifact_id"]

        child = self._candidate(voice, tag="b", gain=0.25, parent=parent, source=parent_audio)

        by_id = {item["id"]: item for item in self._voice(voice["id"])["artifacts"]}
        self.assertEqual(by_id[child]["parent_id"], parent)
        self.assertEqual(by_id[parent]["parent_id"], None)
        self.assertEqual(by_id[child]["source_id"], by_id[parent]["source_id"])

    def test_conversation_does_not_touch_the_source_bytes(self) -> None:
        voice = self._create_voice()
        artifact = self.store.get(voice["source_audio_artifact_id"])
        before = artifact.path.read_bytes()

        candidate = self._candidate(voice, tag="a")
        candidates.set_candidate_approved(self.store, voice["id"], candidate)
        self._candidate(voice, tag="b", gain=0.25, parent=candidate)

        after = self._voice(voice["id"])
        self.assertEqual(after["source_audio_artifact_id"], voice["source_audio_artifact_id"])
        self.assertEqual(len(after["sources"]), 1)
        self.assertEqual(after["sources"][0]["artifact_id"], voice["source_audio_artifact_id"])
        self.assertEqual(artifact.path.read_bytes(), before)
        self.assertEqual(self.store.get(voice["source_audio_artifact_id"]).sha256, artifact.sha256)
        self.assertEqual(len(after["variants"]), 3)

    def test_activating_a_reference_is_the_default(self) -> None:
        voice = self._create_voice()
        candidate = self._candidate(voice, tag="a")

        unapproved = self.client.post(
            f"/api/voices/{voice['id']}/reference/activate", json={"variant_id": candidate}
        )
        self.assertEqual(unapproved.status_code, 200, unapproved.text)
        self.assertIsNone(unapproved.json()["default_reference_id"])

        candidates.set_candidate_approved(self.store, voice["id"], candidate)
        response = self.client.post(
            f"/api/voices/{voice['id']}/reference/activate", json={"variant_id": candidate}
        )
        self.assertEqual(response.status_code, 200, response.text)
        activated = response.json()
        self.assertEqual(activated["default_reference_id"], candidate)
        defaults = {item["id"]: item["default"] for item in activated["artifacts"]}
        self.assertEqual(defaults, {candidate: True})

        source = self.client.post(
            f"/api/voices/{voice['id']}/reference/activate", json={"kind": "original"}
        )
        self.assertEqual(source.status_code, 200, source.text)
        self.assertIsNone(source.json()["default_reference_id"])

    def test_resemble_run_lands_as_an_experiment(self) -> None:
        voice = self._create_voice()

        with patch(DENOISE_HOOK, _fake_denoise):
            response = self.client.post(f"/api/voices/{voice['id']}/reference/denoise")

        self.assertEqual(response.status_code, 200, response.text)
        after = self._voice(voice["id"])
        item = next(item for item in after["artifacts"] if item["kind"] == "resemble")
        self.assertEqual(item["role"], "experiment")
        self.assertEqual(item["name"], "Resemble")
        self.assertEqual(item["source_id"], after["sources"][0]["id"])
        self.assertFalse(item["approved"])
        self.assertFalse(item["stale"])
        variant = next(row for row in after["variants"] if row["kind"] == "resemble")
        self.assertEqual(item["id"], variant["id"])
        self.assertEqual(item["audio_artifact_id"], variant["audio_artifact_id"])

    def test_delete_voice_takes_its_profile_rows(self) -> None:
        voice = self._create_voice()
        candidate = self._candidate(voice, tag="a")
        candidates.set_candidate_approved(self.store, voice["id"], candidate)
        voices.add_voice_source(
            self.store,
            voice["id"],
            artifact_id=self.store.import_audio(_wav(self.root / "second.wav", freq=220.0)).id,
        )

        response = self.client.delete(f"/api/voices/{voice['id']}")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            self.store.execute(
                "SELECT COUNT(*) AS n FROM voice_sources WHERE voice_id = ?", (voice["id"],)
            ).fetchone()["n"],
            0,
        )
        self.assertEqual(
            self.store.execute(
                "SELECT COUNT(*) AS n FROM voice_artifacts WHERE voice_id = ?", (voice["id"],)
            ).fetchone()["n"],
            0,
        )


class VoiceLabKeep(_LabCase):
    def test_keep_only_crop_follows_the_primary_source(self) -> None:
        voice = self._create_voice()
        half = round(float(voice["duration_s"]) / 2, 3)

        response = self.client.post(
            f"/api/voices/{voice['id']}/reference/keep-only",
            json={"start_s": 0.0, "end_s": half},
        )

        self.assertEqual(response.status_code, 200, response.text)
        cropped = response.json()
        self.assertEqual(cropped["keep_intervals"], [{"start_s": 0.0, "end_s": half}])
        self.assertEqual(cropped["sources"][0]["keep_intervals"], cropped["keep_intervals"])


LEGACY_SCHEMA = """
CREATE TABLE artifacts (
    id TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL,
    suffix TEXT,
    bytes INTEGER,
    duration_s REAL,
    created_at TEXT NOT NULL
);
CREATE TABLE voices (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    tags_json TEXT NOT NULL DEFAULT '[]',
    source_artifact_id TEXT NOT NULL,
    source_transcript TEXT NOT NULL DEFAULT '',
    keep_intervals_json TEXT NOT NULL DEFAULT '[]',
    effective_transcript TEXT NOT NULL DEFAULT '',
    active_reference_variant_id TEXT,
    original_artifact_id TEXT,
    original_format TEXT,
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    source_words_json TEXT,
    transcript_locked INTEGER NOT NULL DEFAULT 0,
    speaker_analysis_id TEXT
);
CREATE TABLE reference_variants (
    id TEXT PRIMARY KEY,
    voice_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    audio_artifact_id TEXT NOT NULL,
    processor_config_json TEXT,
    processor_cache_key TEXT,
    duration_s REAL NOT NULL,
    pinned INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    parent_variant_id TEXT,
    auk_task TEXT,
    instruction TEXT,
    model_variant TEXT,
    auk_precision TEXT,
    encoder_precision TEXT,
    seed INTEGER,
    settings_json TEXT,
    approved INTEGER NOT NULL DEFAULT 0
);
"""

CREATED = "2026-09-12T00:00:00Z"


def _legacy_db(root: Path) -> None:
    """An operator's 1:1 lab.sqlite3, frozen before the voice-lab tables."""
    legacy = sqlite3.connect(str(root / "lab.sqlite3"))
    legacy.executescript(LEGACY_SCHEMA)
    for artifact in ("a_src1", "a_src2", "a_res1", "a_auk1a", "a_auk1b"):
        legacy.execute(
            "INSERT INTO artifacts (id, sha256, suffix, bytes, duration_s, created_at)"
            " VALUES (?, ?, '.wav', 1, 4.0, ?)",
            (artifact, f"sha-{artifact}", CREATED),
        )
    keep = '[{"start_s": 0.0, "end_s": 4.0}]'
    words = '[{"word": "hello", "start_s": 0.0, "end_s": 0.4}]'
    for voice, source, active in (("vp_1", "a_src1", "rv_auk1a"), ("vp_2", "a_src2", "rv_orig2")):
        legacy.execute(
            """
            INSERT INTO voices (id, name, tags_json, source_artifact_id, source_transcript,
                keep_intervals_json, effective_transcript, active_reference_variant_id,
                original_artifact_id, original_format, notes, created_at, updated_at,
                source_words_json, transcript_locked)
            VALUES (?, ?, '["lab"]', ?, 'hello from ata', ?, 'hello from ata', ?, ?, 'wav',
                    NULL, ?, ?, ?, 1)
            """,
            (voice, voice, source, keep, active, source, CREATED, CREATED, words),
        )
    variants = (
        ("rv_orig1", "vp_1", "original", "a_src1", None, 1, None, None, None, None, None),
        ("rv_auk1a", "vp_1", "auk", "a_auk1a", "k1", 1, None, "enhance", INSTRUCTION, "auk-base", "bf16"),
        ("rv_auk1b", "vp_1", "auk", "a_auk1b", "k2", 0, "rv_auk1a", "denoise", "calmer", "auk-base", "bf16"),
        ("rv_res1", "vp_1", "resemble", "a_res1", "k3", 0, None, None, None, None, None),
        ("rv_orig2", "vp_2", "original", "a_src2", None, 1, None, None, None, None, None),
    )
    for variant_id, voice_id, kind, audio, key, approved, parent, task, instruction, model, precision in variants:
        legacy.execute(
            """
            INSERT INTO reference_variants (id, voice_id, kind, audio_artifact_id,
                processor_config_json, processor_cache_key, duration_s, pinned, created_at,
                parent_variant_id, auk_task, instruction, model_variant, auk_precision,
                encoder_precision, seed, settings_json, approved)
            VALUES (?, ?, ?, ?, NULL, ?, 4.0, 1, ?, ?, ?, ?, ?, ?, 'w4a8', 7, '{"nfe": 32}', ?)
            """,
            (
                variant_id,
                voice_id,
                kind,
                audio,
                key,
                CREATED,
                parent,
                task,
                instruction,
                model,
                precision,
                approved,
            ),
        )
    legacy.commit()
    legacy.close()


class VoiceLabLegacyMigration(unittest.TestCase):
    """An operator's 1:1 lab.sqlite3 becomes a profile without losing a field."""

    def _migrated(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        _legacy_db(root)
        conn = connect(root)
        self.addCleanup(conn.close)
        return conn

    def _row(self, conn, table: str, key: str):
        return conn.execute(f"SELECT * FROM {table} WHERE id = ?", (key,)).fetchone()

    def test_legacy_source_becomes_the_primary_source(self) -> None:
        conn = self._migrated()

        sources = conn.execute(
            "SELECT * FROM voice_sources WHERE voice_id = 'vp_1' ORDER BY rowid ASC"
        ).fetchall()
        self.assertEqual(len(sources), 1)
        source = sources[0]
        self.assertEqual(source["label"], "Source 1")
        self.assertEqual(source["artifact_id"], "a_src1")
        self.assertEqual(source["transcript"], "hello from ata")
        self.assertEqual(json.loads(source["keep_intervals_json"]), [{"start_s": 0.0, "end_s": 4.0}])
        self.assertEqual(json.loads(source["words_json"])[0]["word"], "hello")
        self.assertEqual(source["created_at"], CREATED)

    def test_original_variant_is_not_an_artifact(self) -> None:
        conn = self._migrated()

        self.assertIsNone(self._row(conn, "voice_artifacts", "rv_orig1"))
        self.assertIsNone(self._row(conn, "voice_artifacts", "rv_orig2"))

    def test_approved_auk_variant_migrates_as_a_reference(self) -> None:
        conn = self._migrated()

        reference = self._row(conn, "voice_artifacts", "rv_auk1a")
        self.assertEqual(reference["role"], "reference")
        self.assertEqual(reference["kind"], "auk")
        self.assertEqual(reference["name"], "AuK enhance")
        self.assertEqual(reference["audio_artifact_id"], "a_auk1a")
        self.assertEqual(reference["source_id"], self._source_id(conn, "vp_1"))
        self.assertEqual(reference["approved_at"], CREATED)
        self.assertEqual(reference["processor_cache_key"], "k1")
        self.assertEqual(reference["auk_task"], "enhance")
        self.assertEqual(reference["instruction"], INSTRUCTION)
        self.assertEqual(reference["model_variant"], "auk-base")
        self.assertEqual(reference["auk_precision"], "bf16")
        self.assertEqual(reference["encoder_precision"], "w4a8")
        self.assertEqual(reference["seed"], 7)
        self.assertEqual(json.loads(reference["settings_json"]), {"nfe": 32})
        self.assertEqual(reference["created_at"], CREATED)
        # Rendered from the source itself: the source row is the lineage.
        self.assertIsNone(reference["parent_id"])

    def test_unapproved_candidates_and_resemble_are_experiments(self) -> None:
        conn = self._migrated()

        candidate = self._row(conn, "voice_artifacts", "rv_auk1b")
        self.assertEqual(candidate["role"], "experiment")
        self.assertEqual(candidate["name"], "AuK denoise")
        self.assertIsNone(candidate["approved_at"])
        self.assertEqual(candidate["parent_id"], "rv_auk1a")
        self.assertEqual(candidate["source_id"], self._source_id(conn, "vp_1"))
        resemble = self._row(conn, "voice_artifacts", "rv_res1")
        self.assertEqual(resemble["role"], "experiment")
        self.assertEqual(resemble["kind"], "resemble")
        self.assertEqual(resemble["name"], "Resemble")
        self.assertEqual(resemble["processor_cache_key"], "k3")

    def test_active_reference_becomes_the_default(self) -> None:
        conn = self._migrated()

        self.assertEqual(self._row(conn, "voices", "vp_1")["default_reference_id"], "rv_auk1a")
        self.assertEqual(self._row(conn, "voice_artifacts", "rv_auk1a")["is_default"], 1)
        self.assertEqual(self._row(conn, "voice_artifacts", "rv_auk1b")["is_default"], 0)
        # vp_2's active variant is its own source audio: no default reference.
        self.assertIsNone(self._row(conn, "voices", "vp_2")["default_reference_id"])

    def test_reopening_the_database_changes_nothing(self) -> None:
        conn = self._migrated()
        before = (
            self._row(conn, "voice_sources", self._source_id(conn, "vp_1"))["id"],
            conn.execute("SELECT COUNT(*) AS n FROM voice_sources").fetchone()["n"],
            conn.execute("SELECT COUNT(*) AS n FROM voice_artifacts").fetchone()["n"],
        )
        root = Path(conn.execute("PRAGMA database_list").fetchone()[2]).parent
        conn.close()

        reopened = connect(root)
        self.addCleanup(reopened.close)
        after = (
            self._row(reopened, "voice_sources", self._source_id(reopened, "vp_1"))["id"],
            reopened.execute("SELECT COUNT(*) AS n FROM voice_sources").fetchone()["n"],
            reopened.execute("SELECT COUNT(*) AS n FROM voice_artifacts").fetchone()["n"],
        )
        self.assertEqual(before, after)
        self.assertEqual(self._row(reopened, "voices", "vp_1")["default_reference_id"], "rv_auk1a")

    def _source_id(self, conn, voice_id: str) -> str:
        row = conn.execute(
            "SELECT id FROM voice_sources WHERE voice_id = ? ORDER BY rowid ASC LIMIT 1",
            (voice_id,),
        ).fetchone()
        return str(row["id"])


if __name__ == "__main__":
    unittest.main()
