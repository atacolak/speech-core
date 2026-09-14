#!/usr/bin/env python3
"""AuK candidate artifacts: lineage, provenance, approval, staleness.

No AuK weights, no runtime, no GPU: the candidate wav is rendered locally so the
lineage and keep contracts are testable without the model.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from fastapi.testclient import TestClient
from pydantic import ValidationError

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO))

from tts.lab.backend.app import create_app
from tts.lab.backend.models import Interval, ReferenceVariant
from tts.lab.backend.services.candidates import record_auk_candidate, set_candidate_approved
from tts.lab.backend.store.cache import (
    AUK_PROCESSOR,
    AUK_PROVENANCE_FIELDS,
    canonical_processor_config,
    processor_cache_key,
)
from tts.lab.backend.store.db import connect
from tts.wav import read_wav, write_wav

RESEMBLE_CONFIG = {"checkpoint": "resemble-denoise", "preprocess_version": "v1"}
# Frozen before AuK provenance joined the cache identity: resemble keys must not
# move when the AuK case starts forking on candidate provenance.
RESEMBLE_KEY = "3ea87f595e353eb2bd7db6d96669ff982c3a4ef1b3793660c503972b86112559"
DENOISE_HOOK = "tts.lab.backend.services.resemble.denoise_hook"

INSTRUCTION = "keep the voice, drop the room"
SETTINGS = {"nfe": 32, "cfg": 2.0, "sway": -1.0}
PROVENANCE = {
    "auk_task": "enhance",
    "instruction": INSTRUCTION,
    "model_variant": "auk-base",
    "auk_precision": "bf16",
    "encoder_precision": "w4a8",
    "seed": 7,
    "settings": SETTINGS,
}


def _wav(path: Path, *, freq: float = 440.0) -> Path:
    sr = 24000
    write_wav(path, sr, 0.1 * np.sin(2 * np.pi * freq * np.arange(sr // 5) / sr))
    return path


def _fake_denoise(dwav, sr, device):
    """Half-amplitude 'denoise'. Runs without resemble weights or a GPU."""
    return np.asarray(dwav, dtype=np.float32) * 0.5, sr


class _AukCase(unittest.TestCase):
    """Voice + candidate plumbing shared by the artifact contract tests."""

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

    def _create_voice(self, name: str = "ata") -> dict:
        wav = _wav(self.root / f"{name}.wav")
        with wav.open("rb") as handle:
            response = self.client.post(
                "/api/voices",
                data={"name": name, "transcript": "hello from ata", "tags": '["lab"]'},
                files={"audio": ("ata.wav", handle, "audio/wav")},
            )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _render(self, voice: dict, *, gain: float, tag: str):
        """A stand-in for an AuK render: read the source, write different bytes."""
        source = self.store.get(voice["source_audio_artifact_id"])
        sr, samples = read_wav(source.path)
        out = self.root / f"{voice['id']}-{tag}.wav"
        write_wav(out, sr, (np.asarray(samples, dtype=np.float32) * gain).astype(np.float32))
        return self.store.import_audio(out)

    def _candidate(
        self,
        voice: dict,
        *,
        tag: str = "a",
        gain: float = 0.5,
        parent: str | None = None,
        **provenance,
    ) -> str:
        artifact = self._render(voice, gain=gain, tag=tag)
        return record_auk_candidate(
            self.store,
            voice["id"],
            audio_artifact_id=artifact.id,
            parent_variant_id=parent,
            **{**PROVENANCE, **provenance},
        )

    def _voice(self, voice_id: str) -> dict:
        response = self.client.get(f"/api/voices/{voice_id}")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _row(self, voice: dict, variant_id: str) -> dict:
        return next(item for item in voice["variants"] if item["id"] == variant_id)

    def _variant(self, voice: dict, kind: str) -> dict:
        return next(item for item in voice["variants"] if item["kind"] == kind)


class AukCandidateArtifacts(_AukCase):
    def test_candidate_leaves_source_and_original_variant_untouched(self) -> None:
        created = self._create_voice()
        source = self.store.get(created["source_audio_artifact_id"])
        source_bytes = source.path.read_bytes()
        original_row = self._variant(created, "original")
        original_bytes = self.store.get(created["original_artifact_id"]).path.read_bytes()

        candidate_id = self._candidate(created)

        after = self._voice(created["id"])
        self.assertEqual({item["kind"] for item in after["variants"]}, {"original", "auk"})
        self.assertEqual(after["original_artifact_id"], created["original_artifact_id"])
        self.assertEqual(self.store.get(created["source_audio_artifact_id"]).sha256, source.sha256)
        self.assertEqual(source.path.read_bytes(), source_bytes)
        self.assertEqual(
            self.store.get(created["original_artifact_id"]).path.read_bytes(), original_bytes
        )
        kept = self._variant(after, "original")
        self.assertEqual(kept["id"], original_row["id"])
        self.assertEqual(kept["audio_artifact_id"], original_row["audio_artifact_id"])
        self.assertEqual(kept["processor_cache_key"], original_row["processor_cache_key"])
        self.assertFalse(kept["stale"])
        candidate = self._row(after, candidate_id)
        self.assertEqual(candidate["kind"], "auk")
        self.assertNotEqual(candidate["audio_artifact_id"], original_row["audio_artifact_id"])
        self.assertNotEqual(self.store.get(candidate["audio_artifact_id"]).sha256, source.sha256)

    def test_candidate_provenance_persists_across_reopen(self) -> None:
        created = self._create_voice()
        candidate_id = self._candidate(created, seed=11)

        row = self._row(self._voice(created["id"]), candidate_id)
        self.assertIsNone(row["parent_variant_id"])
        self.assertEqual(row["auk_task"], "enhance")
        self.assertEqual(row["instruction"], INSTRUCTION)
        self.assertEqual(row["model_variant"], "auk-base")
        self.assertEqual(row["auk_precision"], "bf16")
        self.assertEqual(row["encoder_precision"], "w4a8")
        self.assertEqual(row["seed"], 11)
        self.assertEqual(row["settings"], SETTINGS)
        self.assertFalse(row["approved"])
        self.assertFalse(row["stale"])

        self.client.close()
        self.client = TestClient(create_app(root=self.root, leftover_parked=True))
        self.assertEqual(self._row(self._voice(created["id"]), candidate_id), row)

    def test_candidate_branches_from_a_candidate(self) -> None:
        created = self._create_voice()
        first = self._candidate(created, tag="a")
        second = self._candidate(created, tag="b", gain=0.25, parent=first, auk_task="denoise", seed=99)

        voice = self._voice(created["id"])
        self.assertEqual(len(voice["variants"]), 3)
        self.assertIsNone(self._row(voice, first)["parent_variant_id"])
        self.assertEqual(self._row(voice, second)["parent_variant_id"], first)
        self.assertEqual(self._row(voice, second)["auk_task"], "denoise")
        self.assertNotEqual(
            self._row(voice, second)["audio_artifact_id"],
            self._row(voice, first)["audio_artifact_id"],
        )

    def test_unknown_voice_or_parent_is_rejected(self) -> None:
        created = self._create_voice()
        with self.assertRaises(LookupError):
            self._candidate(created, parent="rv_missing")
        with self.assertRaises(LookupError):
            self._candidate({"id": "vp_missing", "source_audio_artifact_id": created["source_audio_artifact_id"]})

    def test_approve_marks_the_candidate_only(self) -> None:
        created = self._create_voice()
        candidate_id = self._candidate(created)

        set_candidate_approved(self.store, created["id"], candidate_id)

        voice = self._voice(created["id"])
        self.assertTrue(self._row(voice, candidate_id)["approved"])
        self.assertFalse(self._variant(voice, "original")["approved"])
        with self.assertRaises(LookupError):
            set_candidate_approved(self.store, created["id"], "rv_missing")

    def test_keep_change_stales_the_candidate_without_deleting_it(self) -> None:
        created = self._create_voice()
        candidate_id = self._candidate(created)
        self.assertFalse(self._row(self._voice(created["id"]), candidate_id)["stale"])

        cropped = self.client.post(
            f"/api/voices/{created['id']}/reference/keep-only",
            json={"start_s": 0.0, "end_s": 0.08},
        )
        self.assertEqual(cropped.status_code, 200, cropped.text)

        after = self._voice(created["id"])
        stale = self._row(after, candidate_id)
        self.assertTrue(stale["stale"])
        self.assertFalse(self._variant(after, "original")["stale"])
        self.assertEqual(len(after["variants"]), 2)
        self.assertTrue(self.store.get(stale["audio_artifact_id"]).path.is_file())

        refreshed = self._candidate(after, tag="b", gain=0.25)
        fresh = self._voice(created["id"])
        self.assertFalse(self._row(fresh, refreshed)["stale"])
        self.assertTrue(self._row(fresh, candidate_id)["stale"])

    def test_legacy_resemble_and_other_rows_default_the_new_columns(self) -> None:
        created = self._create_voice()
        with patch(DENOISE_HOOK, _fake_denoise):
            response = self.client.post(f"/api/voices/{created['id']}/reference/denoise")
        self.assertEqual(response.status_code, 200, response.text)

        voice = self._voice(created["id"])
        denoised = self._variant(voice, "resemble")
        self.assertFalse(denoised["stale"])
        for key in AUK_PROVENANCE_FIELDS:
            self.assertIsNone(denoised[key], key)
        self.assertFalse(denoised["approved"])

        self.store.execute(
            """
            INSERT INTO reference_variants
                (id, voice_id, kind, audio_artifact_id, duration_s, pinned, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("rv_other", created["id"], "other", denoised["audio_artifact_id"], 0.2, 0, "2026-09-12T00:00:00Z"),
        )
        self.store.commit()
        other = self._row(self._voice(created["id"]), "rv_other")
        for key in AUK_PROVENANCE_FIELDS:
            self.assertIsNone(other[key], key)
        self.assertFalse(other["approved"])

    def test_kind_auk_is_a_variant_and_streamfm_is_not(self) -> None:
        variant = ReferenceVariant(
            id="rv_1",
            voice_profile_id="vp_1",
            kind="auk",
            audio_artifact_id="a_1",
            duration_s=1.0,
            parent_variant_id="rv_0",
            auk_task="enhance",
            instruction=INSTRUCTION,
            model_variant="auk-base",
            auk_precision="bf16",
            encoder_precision="w4a8",
            seed=7,
            settings={"nfe": 32},
        )
        self.assertEqual(variant.kind, "auk")
        self.assertFalse(variant.approved)
        self.assertFalse(variant.stale)
        with self.assertRaises(ValidationError):
            ReferenceVariant(
                id="rv_2",
                voice_profile_id="vp_1",
                kind="streamfm",
                audio_artifact_id="a_2",
                duration_s=1.0,
            )


class AukCacheIdentity(unittest.TestCase):
    keep = [Interval(start_s=0.0, end_s=4.0)]

    def _key(self, *, keep=None, **overrides) -> str:
        return processor_cache_key(
            AUK_PROCESSOR, "deadbeef", self.keep if keep is None else keep, {**PROVENANCE, **overrides}
        )

    def test_provenance_forks_the_key(self) -> None:
        base = self._key()
        for field, value in (
            ("auk_task", "denoise"),
            ("instruction", "different words"),
            ("model_variant", "auk-flash"),
            ("auk_precision", "int8"),
            ("encoder_precision", "fp16"),
            ("seed", 8),
            ("settings", {"nfe": 16}),
            ("parent_variant_id", "rv_parent"),
        ):
            with self.subTest(field=field):
                self.assertNotEqual(self._key(**{field: value}), base)

    def test_keep_still_forks_the_key(self) -> None:
        self.assertNotEqual(self._key(keep=[Interval(start_s=0.0, end_s=2.0)]), self._key())

    def test_settings_key_order_does_not_fork_the_key(self) -> None:
        self.assertEqual(
            self._key(settings={"nfe": 32, "cfg": 2.0}),
            self._key(settings={"cfg": 2.0, "nfe": 32}),
        )


class AukSchemaMigration(unittest.TestCase):
    """An operator's existing lab.sqlite3 gains the provenance columns in place."""

    def test_existing_variant_rows_gain_provenance_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = sqlite3.connect(str(root / "lab.sqlite3"))
            legacy.execute("CREATE TABLE voices (id TEXT PRIMARY KEY, name TEXT NOT NULL)")
            legacy.execute(
                """
                CREATE TABLE reference_variants (
                    id TEXT PRIMARY KEY,
                    voice_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    audio_artifact_id TEXT NOT NULL,
                    processor_config_json TEXT,
                    processor_cache_key TEXT,
                    duration_s REAL NOT NULL,
                    pinned INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                )
                """
            )
            legacy.execute(
                """
                INSERT INTO reference_variants
                    (id, voice_id, kind, audio_artifact_id, duration_s, pinned, created_at)
                VALUES ('rv_1', 'vp_1', 'resemble', 'a_1', 1.0, 0, '2026-09-12T00:00:00Z')
                """
            )
            legacy.commit()
            legacy.close()

            conn = connect(root)
            try:
                columns = {
                    str(row[1]) for row in conn.execute("PRAGMA table_info(reference_variants)")
                }
                added = {
                    field for field in AUK_PROVENANCE_FIELDS if field != "settings"
                } | {"settings_json", "approved"}
                self.assertTrue(added <= columns, sorted(added - columns))
                row = conn.execute("SELECT * FROM reference_variants WHERE id = 'rv_1'").fetchone()
                for column in sorted(added):
                    if column == "approved":
                        self.assertEqual(row[column], 0, column)
                    else:
                        self.assertIsNone(row[column], column)
            finally:
                conn.close()


class ResembleCacheIdentity(unittest.TestCase):
    def test_resemble_key_is_unchanged_by_auk_provenance(self) -> None:
        self.assertEqual(
            processor_cache_key(
                "resemble", "deadbeef", [Interval(start_s=0.0, end_s=4.0)], RESEMBLE_CONFIG
            ),
            RESEMBLE_KEY,
        )
        self.assertEqual(
            canonical_processor_config("resemble", RESEMBLE_CONFIG),
            {
                "processor": "resemble",
                "checkpoint": "resemble-denoise",
                "config": {},
                "preprocess_version": "v1",
                "model_id": "",
                "model_revision": "",
            },
        )


if __name__ == "__main__":
    unittest.main()
