#!/usr/bin/env python3
"""A selected reference's origin transcript is the audio's truth. CPU only.

Activation picks the audio; synthesis resolves the same target for both the
reference audio and its clean transcript. A GENERATION never becomes a
reference.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from fastapi import HTTPException
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "lab" / "scripts"))

from tts.lab.backend.app import create_app
from tts.lab.backend.models import Interval
from tts.lab.backend.routes.synthesis import SynthesisBody, resolve_synthesis_request
from tts.lab.backend.services.candidates import (
    EXPERIMENT,
    REFERENCE,
    record_voice_artifact,
)
from tts.lab.backend.services.sources import (
    create_media_source,
    extract_clip,
    map_speaker,
    record_analysis,
)
from tts.lab.backend.services.voices import add_voice_source
from tts.wav import write_wav

CREATED = "2026-09-16T00:00:00Z"
FIRST_TRANSCRIPT = "first clean transcript"
SECOND_TRANSCRIPT = "second clean transcript"
ASR_TEXT = "asr of the other origin"


def _wav(path: Path, *, freq: float = 440.0) -> Path:
    sr = 24000
    write_wav(path, sr, 0.1 * np.sin(2 * np.pi * freq * np.arange(sr // 5) / sr))
    return path


class ReferenceResolutionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.client = TestClient(create_app(root=self.root, leftover_parked=True))
        self.state = self.client.app.state.lab
        self.store = self.state.store
        self.voice = self._create_voice()
        self.second_source = self._add_source()
        self.first_reference = self._reference(
            "ref_first",
            audio=self.voice["source_audio_artifact_id"],
            source=self.voice["sources"][0]["id"],
        )
        self.second_reference = self._reference(
            "ref_second", audio="art_second", source=self.second_source
        )

    def tearDown(self) -> None:
        self.client.close()
        self.tmp.cleanup()

    def _create_voice(self) -> dict:
        wav = _wav(self.root / "first.wav")
        with wav.open("rb") as handle:
            response = self.client.post(
                "/api/voices",
                data={"name": "ata", "transcript": FIRST_TRANSCRIPT},
                files={"audio": ("first.wav", handle, "audio/wav")},
            )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _artifact(self, artifact_id: str, *, freq: float) -> str:
        path = self.root / "objects" / f"{artifact_id}.wav"
        path.parent.mkdir(parents=True, exist_ok=True)
        write_wav(path, 24000, 0.1 * np.sin(2 * np.pi * freq * np.arange(24000 // 5) / 24000))
        self.store.execute(
            """
            INSERT INTO artifacts (id, sha256, path, suffix, bytes, sample_rate, duration_s, created_at)
            VALUES (?, ?, ?, '.wav', ?, 24000, 0.2, ?)
            """,
            (artifact_id, f"sha-{artifact_id}", str(path), path.stat().st_size, CREATED),
        )
        self.store.commit()
        return artifact_id

    def _add_source(self) -> str:
        """A second immutable source with its own clean transcript."""
        self._artifact("art_second", freq=660.0)
        return add_voice_source(
            self.store,
            self.voice["id"],
            artifact_id="art_second",
            transcript=SECOND_TRANSCRIPT,
        )

    def _reference(self, artifact_id: str, *, audio: str, source: str | None) -> str:
        return record_voice_artifact(
            self.store,
            self.voice["id"],
            artifact_id=artifact_id,
            role=REFERENCE,
            kind="original",
            name=f"Reference {artifact_id}",
            audio_artifact_id=audio,
            source_id=source,
        )

    def _clip_fixture(self) -> dict:
        """One extracted, voice-owned clip with a distinct clean transcript."""
        wav = _wav(self.root / "interview.wav", freq=520.0)
        source = create_media_source(
            self.store, kind="file", origin="upload", title="interview", audio_path=wav
        )
        record_analysis(
            self.store,
            source,
            Interval(start_s=0.0, end_s=0.2),
            {
                "speakers": [{"id": "S1", "label": "Ata", "duration_s": 0.2}],
                "segments": [
                    {"speaker_id": "S1", "start_s": 0.0, "end_s": 0.2, "text": "clip words only"}
                ],
                "overlaps": [],
            },
        )
        map_speaker(self.store, source, "S1", self.voice["id"])
        return extract_clip(self.store, source, "S1", [Interval(start_s=0.0, end_s=0.2)])

    def _run(self, artifact_id: str) -> dict:
        """One Breeze GENERATION: a run whose output is a fresh take artifact."""
        self._artifact(artifact_id, freq=880.0)
        self.store.execute(
            """
            INSERT INTO runs (id, voice_id, request_json, output_artifact_id,
                effective_reference_json, latency_ms, first_audio_ms, duration_s, rating,
                tags_json, created_at, alignment_json)
            VALUES ('run_generation', ?, '{}', ?, '{}', 1.0, NULL, 0.2, NULL, '[]', ?, NULL)
            """,
            (self.voice["id"], artifact_id, CREATED),
        )
        self.store.execute(
            "UPDATE voices SET latest_take_id = 'run_generation' WHERE id = ?", (self.voice["id"],)
        )
        self.store.commit()
        return {"id": "run_generation", "output_artifact_id": artifact_id}

    def _activate(self, reference_id: str):
        return self.client.post(
            f"/api/voices/{self.voice['id']}/reference/activate",
            json={"variant_id": reference_id},
        )

    def _voice(self, voice_id: str) -> dict:
        response = self.client.get(f"/api/voices/{voice_id}")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _body(self) -> SynthesisBody:
        return SynthesisBody(text="hello", steer="calm", voice_profile_id=self.voice["id"])

    def test_selected_source_transcript_is_display_and_ref_text_truth(self) -> None:
        response = self._activate(self.second_reference)

        self.assertEqual(response.status_code, 200, response.text)
        activated = response.json()
        self.assertEqual(activated["default_reference_id"], self.second_reference)
        shown = next(item for item in activated["sources"] if item["id"] == self.second_source)
        self.assertEqual(shown["transcript"], SECOND_TRANSCRIPT)

        resolved = resolve_synthesis_request(self.state, self._body())

        self.assertEqual(resolved.artifact.id, "art_second")
        self.assertEqual(resolved.reference_path, self.store.get("art_second").path)
        self.assertEqual(resolved.reference_text, SECOND_TRANSCRIPT)

    def test_explicit_request_id_naming_a_sources_audio_keeps_its_own_origin(self) -> None:
        body = SynthesisBody(
            text="hello",
            steer="calm",
            voice_profile_id=self.voice["id"],
            reference_variant_id="art_second",
        )

        resolved = resolve_synthesis_request(self.state, body)

        self.assertEqual(resolved.artifact.id, "art_second")
        self.assertEqual(resolved.reference_path, self.store.get("art_second").path)
        self.assertEqual(resolved.reference_text, SECOND_TRANSCRIPT)

    def test_clip_reference_uses_clean_transcript(self) -> None:
        clip = self._clip_fixture()
        reference = self._reference(clip["id"], audio=clip["audio_artifact_id"], source=None)

        response = self._activate(reference)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["default_reference_id"], reference)

        resolved = resolve_synthesis_request(self.state, self._body())

        self.assertEqual(resolved.artifact.id, clip["audio_artifact_id"])
        self.assertEqual(resolved.reference_path, self.store.get(clip["audio_artifact_id"]).path)
        self.assertEqual(resolved.reference_text, clip["clean_transcript"])

    def test_clip_audio_id_reference_resolves_to_the_clip_audio(self) -> None:
        clip = self._clip_fixture()
        body = SynthesisBody(
            text="hello",
            steer="calm",
            voice_profile_id=self.voice["id"],
            reference_variant_id=clip["audio_artifact_id"],
        )

        resolved = resolve_synthesis_request(self.state, body)

        self.assertEqual(resolved.artifact.id, clip["audio_artifact_id"])
        self.assertEqual(resolved.reference_path, self.store.get(clip["audio_artifact_id"]).path)
        self.assertEqual(resolved.reference_text, clip["clean_transcript"])

    def test_generation_artifact_cannot_be_activated_as_reference(self) -> None:
        activated = self._activate(self.second_reference)
        self.assertEqual(activated.status_code, 200, activated.text)
        run = self._run("art_generation")

        response = self._activate(run["output_artifact_id"])

        self.assertIn(response.status_code, (404, 422))
        self.assertEqual(
            self._voice(self.voice["id"])["default_reference_id"], self.second_reference
        )

    def _source_without_transcript(self, artifact_id: str) -> str:
        """A second immutable source whose clean transcript is still unknown."""
        self._artifact(artifact_id, freq=700.0)
        return add_voice_source(
            self.store, self.voice["id"], artifact_id=artifact_id, transcript=""
        )

    def test_asr_of_a_non_primary_origin_never_rewrites_the_primary_transcript(self) -> None:
        blank = self._source_without_transcript("art_blank")
        self._activate(self._reference("ref_blank", audio="art_blank", source=blank))
        before = self._voice(self.voice["id"])

        with patch(
            "breeze_tts_qual.transcribe.transcribe_audio",
            return_value={"text": ASR_TEXT, "words": []},
        ) as transcribe:
            resolved = resolve_synthesis_request(self.state, self._body())

        self.assertEqual(transcribe.call_count, 1)
        self.assertEqual(resolved.artifact.id, "art_blank")
        self.assertEqual(resolved.reference_text, ASR_TEXT)
        after = self._voice(self.voice["id"])
        self.assertEqual(after["source_transcript"], before["source_transcript"])
        self.assertEqual(after["effective_transcript"], before["effective_transcript"])
        primary = next(
            item for item in after["sources"] if item["id"] == self.voice["sources"][0]["id"]
        )
        self.assertEqual(primary["transcript"], FIRST_TRANSCRIPT)
        origin = next(item for item in after["sources"] if item["id"] == blank)
        self.assertEqual(origin["transcript"], ASR_TEXT)

        self._activate(self.first_reference)
        back = resolve_synthesis_request(self.state, self._body())

        primary_artifact = self.voice["source_audio_artifact_id"]
        self.assertEqual(back.artifact.id, primary_artifact)
        self.assertEqual(back.reference_path, self.store.get(primary_artifact).path)
        self.assertEqual(back.reference_text, FIRST_TRANSCRIPT)

    def test_asr_of_a_clip_origin_never_rewrites_the_primary_transcript(self) -> None:
        clip = self._clip_fixture()
        self.store.execute("UPDATE clips SET clean_transcript='' WHERE id=?", (clip["id"],))
        self.store.commit()
        self._activate(self._reference(clip["id"], audio=clip["audio_artifact_id"], source=None))
        before = self._voice(self.voice["id"])

        with patch(
            "breeze_tts_qual.transcribe.transcribe_audio",
            return_value={"text": ASR_TEXT, "words": []},
        ) as transcribe:
            resolved = resolve_synthesis_request(self.state, self._body())

        self.assertEqual(transcribe.call_count, 1)
        self.assertEqual(resolved.reference_text, ASR_TEXT)
        after = self._voice(self.voice["id"])
        self.assertEqual(after["source_transcript"], before["source_transcript"])
        self.assertEqual(after["effective_transcript"], before["effective_transcript"])
        stored = self.store.execute(
            "SELECT clean_transcript FROM clips WHERE id=?", (clip["id"],)
        ).fetchone()
        self.assertEqual(stored["clean_transcript"], ASR_TEXT)

    def test_unowned_artifact_audio_never_falls_back_to_the_primary_transcript(self) -> None:
        self._artifact("art_wrapped", freq=990.0)
        self._activate(self._reference("ref_wrapped", audio="art_wrapped", source=None))

        with patch(
            "breeze_tts_qual.transcribe.transcribe_audio",
            return_value={"text": ASR_TEXT, "words": []},
        ) as transcribe:
            resolved = resolve_synthesis_request(self.state, self._body())

        self.assertEqual(transcribe.call_count, 1)
        self.assertEqual(resolved.artifact.id, "art_wrapped")
        self.assertEqual(resolved.reference_path, self.store.get("art_wrapped").path)
        self.assertEqual(resolved.reference_text, ASR_TEXT)
        self.assertEqual(self._voice(self.voice["id"])["effective_transcript"], FIRST_TRANSCRIPT)

    def test_unowned_artifact_audio_fails_closed_instead_of_borrowing_primary_text(self) -> None:
        self._artifact("art_wrapped", freq=990.0)
        self._activate(self._reference("ref_wrapped", audio="art_wrapped", source=None))

        with patch(
            "breeze_tts_qual.transcribe.transcribe_audio",
            side_effect=FileNotFoundError("transcribe-cli missing"),
        ):
            with self.assertRaises(HTTPException) as caught:
                resolve_synthesis_request(self.state, self._body())

        self.assertEqual(caught.exception.status_code, 422)
        self.assertEqual(caught.exception.detail["code"], "missing_ref_text")

    def test_explicit_request_id_cannot_select_a_generation(self) -> None:
        run = self._run("art_generation")
        body = SynthesisBody(
            text="hello",
            steer="calm",
            voice_profile_id=self.voice["id"],
            reference_variant_id=run["output_artifact_id"],
        )

        with self.assertRaises(HTTPException) as caught:
            resolve_synthesis_request(self.state, body)

        self.assertIn(caught.exception.status_code, (404, 422))

    def test_explicit_request_id_cannot_select_an_experiment(self) -> None:
        run = self._run("art_generation")
        experiment = record_voice_artifact(
            self.store,
            self.voice["id"],
            artifact_id="ref_experiment",
            role=EXPERIMENT,
            kind="auk",
            name="AuK candidate",
            audio_artifact_id=run["output_artifact_id"],
            source_id=self.voice["sources"][0]["id"],
        )
        body = SynthesisBody(
            text="hello",
            steer="calm",
            voice_profile_id=self.voice["id"],
            reference_variant_id=experiment,
        )

        with self.assertRaises(HTTPException) as caught:
            resolve_synthesis_request(self.state, body)

        self.assertIn(caught.exception.status_code, (404, 422))

    def test_timestamps_and_markup_never_reach_ref_text(self) -> None:
        clip = self._clip_fixture()
        marked = "SPEAKER_00: [00:00.000 --> 00:01.000] clip words only [00:01.23]"
        self.store.execute(
            "UPDATE clips SET clean_transcript=? WHERE id=?", (marked, clip["id"])
        )
        self.store.commit()
        self._activate(self._reference(clip["id"], audio=clip["audio_artifact_id"], source=None))

        resolved = resolve_synthesis_request(self.state, self._body())

        self.assertEqual(resolved.reference_text, "clip words only")
        stored = self.store.execute(
            "SELECT clean_transcript FROM clips WHERE id=?", (clip["id"],)
        ).fetchone()
        self.assertEqual(stored["clean_transcript"], marked)


if __name__ == "__main__":
    unittest.main()
