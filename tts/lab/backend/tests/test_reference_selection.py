#!/usr/bin/env python3
"""A selected reference's origin transcript is the audio's truth. CPU only.

Activation picks the audio; synthesis resolves the same target for both the
reference audio and its clean transcript. A GENERATION never becomes a
reference.
"""

from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from breeze_tts_qual.configs import EngineConfig
from breeze_tts_qual.engine import BreezeEngine, FakeBackend
from fastapi import HTTPException
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "lab" / "scripts"))

from tts.lab.backend.app import create_app
from tts.lab.backend.models import Interval
from tts.lab.backend.routes.synthesis import SynthesisBody, resolve_synthesis_request
from tts.lab.backend.services.references import clean_ref_text
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

PROSE = "arrived by train and walked uphill"
SPOKEN = "hello there"

# Every timestamp form a stored transcript carries in the wild. The third column
# is the prose the composed ref_text must carry; the invariant in
# `_assert_prose_only` is what actually gates, so a shape nobody listed still
# fails the test when a clock token survives.
FORMAT_FIXTURES: tuple[tuple[str, str, str], ...] = (
    (
        "srt_two_cues_with_indices",
        f"1\n00:00:00,000 --> 00:00:02,000\narrived by train\n\n"
        f"2\n00:00:02,000 --> 00:00:04,500\nand walked uphill\n",
        PROSE,
    ),
    (
        "vtt_with_header",
        f"WEBVTT\n\n00:00:00.000 --> 00:00:02.000\narrived by train\n\n"
        f"00:00:02.000 --> 00:00:04.500\nand walked uphill\n",
        PROSE,
    ),
    ("comma_ms", f"00:00:00,000 --> 00:00:02,000 {PROSE}", PROSE),
    ("dot_ms", f"00:00:00.000 --> 00:00:02.000 {PROSE}", PROSE),
    ("bare_minutes", f"0:00 --> 0:02 {PROSE}", PROSE),
    ("square_bracket", f"[00:01.23] {PROSE}", PROSE),
    ("paren_bracket", f"(00:01.23) {PROSE}", PROSE),
    ("speaker_00", f"SPEAKER_00: {SPOKEN}", SPOKEN),
    ("speaker_short", f"S0: {SPOKEN}", SPOKEN),
    (
        "lone_clock_with_seconds_and_milliseconds",
        "00:00:01,000\nthe train arrived\n\n01:02:03,500\nand we walked uphill",
        "the train arrived and we walked uphill",
    ),
    ("lone_clock_with_fraction", f"00:00:01.500 {PROSE}", PROSE),
    ("lone_clock_with_seconds", f"01:02:03 {PROSE}", PROSE),
    ("bracketed_to_range", f"[00:00 to 00:02] {PROSE}", PROSE),
    ("bracketed_em_dash_range", f"[00:00 \u2014 00:02] {PROSE}", PROSE),
    (
        "vtt_with_bom",
        "\ufeffWEBVTT\n\n00:00:00.000 --> 00:00:02.000\narrived by train\n\n"
        "00:00:02.000 --> 00:00:04.500\nand walked uphill\n",
        PROSE,
    ),
    (
        "vtt_with_leading_blank_line",
        "\nWEBVTT\n\n00:00:00.000 --> 00:00:02.000\narrived by train\n\n"
        "00:00:02.000 --> 00:00:04.500\nand walked uphill\n",
        PROSE,
    ),
    ("clean_control", PROSE, PROSE),
)

# Prose the sanitiser must hand back byte-for-byte. Direction B: the operator's
# own words are not timestamp material, and deleting them diverges the picker
# quote from the sent `ref_text` by real words — silently. `9:00 to 5:00` and
# `the s3: bucket policy` are the shapes an over-eager pass ate before.
PROSE_SURVIVALS: tuple[str, ...] = (
    "it started at 3:30 pm",
    "note: bring water",
    "chapter 3: the river",
    "the ratio was 1:2",
    "speaker one said",
    "we met at 08:30, then walked",
    "he arrived at 4:15 and left at 5:45",
    "at 10:00 we stopped for lunch",
    "we open from 9:00 to 5:00 every weekday",
    "the shift runs 6:00 to 14:00 on the floor",
    "the 9:00 \u2014 5:00 shift",
    "the s3: bucket policy",
    "The Talker: a subtitle",
)

# The invariant, not the regex: Breeze gets prose. A clock-like token of any
# width (`\d{1,2}:\d{2}` already covers the seconds and fraction forms), a range
# arrow, a dash cue separator, the WebVTT token, or diarization markup reaching
# ref_text is the bug.
_CLOCK_LIKE = re.compile(r"\d{1,2}:\d{2}")
_SPEAKER_LIKE = re.compile(r"SPEAKER_\d+|S\d+\s*:")
_CUE_ARTIFACTS = ("-->", "->", "\u2192", "\u2014", "\u2013", "WEBVTT")


def _wav(path: Path, *, freq: float = 440.0) -> Path:
    sr = 24000
    write_wav(path, sr, 0.1 * np.sin(2 * np.pi * freq * np.arange(sr // 5) / sr))
    return path


class ReferenceResolutionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        engine = BreezeEngine(
            EngineConfig(name="E2", precision="bf16"), ckpt_dir=".", backend=FakeBackend()
        )
        self.client = TestClient(
            create_app(root=self.root, engine=engine, leftover_parked=True)
        )
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

    def _assert_prose_only(self, label: str, text: str) -> None:
        """The Breeze-facing invariant: a range, a clock or markup never survives."""
        for needle in _CUE_ARTIFACTS:
            self.assertNotIn(needle, text, f"{label}: {needle!r} in {text!r}")
        self.assertIsNone(_CLOCK_LIKE.search(text), f"{label}: clock token in {text!r}")
        self.assertIsNone(_SPEAKER_LIKE.search(text), f"{label}: markup in {text!r}")

    def _stored_transcript_rows(self, clip_id: str) -> dict[str, tuple]:
        voice = self.store.execute(
            "SELECT source_transcript, effective_transcript FROM voices WHERE id=?",
            (self.voice["id"],),
        ).fetchone()
        source = self.store.execute(
            "SELECT transcript FROM voice_sources WHERE id=?", (self.second_source,)
        ).fetchone()
        clip = self.store.execute(
            "SELECT clean_transcript FROM clips WHERE id=?", (clip_id,)
        ).fetchone()
        return {"voice": tuple(voice), "source": tuple(source), "clip": tuple(clip)}

    def test_every_timestamp_form_is_stripped_from_clean_ref_text(self) -> None:
        for name, stored, expected in FORMAT_FIXTURES:
            with self.subTest(fixture=name):
                composed = clean_ref_text(stored)

                self._assert_prose_only(f"clean_ref_text[{name}]", composed)
                self.assertEqual(composed, expected)

    def _write_transcript(self, origin: str, text: str, clip_id: str) -> None:
        """Store a transcript body the way the product lets it arrive."""
        if origin == "primary":
            patched = self.client.patch(
                f"/api/voices/{self.voice['id']}",
                json={"source_transcript": text, "effective_transcript": text},
            )
            self.assertEqual(patched.status_code, 200, patched.text)
            return
        if origin == "voice_sources":
            statement, row_id = (
                "UPDATE voice_sources SET transcript=? WHERE id=?",
                self.second_source,
            )
        else:
            statement, row_id = "UPDATE clips SET clean_transcript=? WHERE id=?", clip_id
        self.store.execute(statement, (text, row_id))
        self.store.commit()

    def test_every_origin_path_composes_prose_only_ref_text(self) -> None:
        clip = self._clip_fixture()
        clip_reference = self._reference(
            clip["id"], audio=clip["audio_artifact_id"], source=None
        )
        origin_paths = (
            ("primary", self.first_reference, self.voice["source_audio_artifact_id"]),
            ("voice_sources", self.second_reference, "art_second"),
            ("clip", clip_reference, clip["audio_artifact_id"]),
        )
        for name, stored, expected in FORMAT_FIXTURES:
            for origin, reference, artifact_id in origin_paths:
                with self.subTest(fixture=name, origin=origin):
                    self._write_transcript(origin, stored, clip["id"])
                    activated = self._activate(reference)
                    self.assertEqual(activated.status_code, 200, activated.text)
                    before = self._stored_transcript_rows(clip["id"])

                    resolved = resolve_synthesis_request(self.state, self._body())

                    self.assertEqual(resolved.artifact.id, artifact_id)
                    self._assert_prose_only(f"{origin}[{name}]", resolved.reference_text)
                    self.assertEqual(resolved.reference_text, expected)
                    self.assertEqual(self._stored_transcript_rows(clip["id"]), before)

    def test_legitimate_prose_survives_sanitising(self) -> None:
        for sample in PROSE_SURVIVALS:
            with self.subTest(prose=sample):
                self.assertEqual(clean_ref_text(sample), sample)

    def test_prose_with_times_survives_every_origin_path(self) -> None:
        """Direction B at the product surface: the shown quote is what is sent."""
        clip = self._clip_fixture()
        clip_reference = self._reference(
            clip["id"], audio=clip["audio_artifact_id"], source=None
        )
        origin_paths = (
            ("primary", self.first_reference),
            ("voice_sources", self.second_reference),
            ("clip", clip_reference),
        )
        for sample in PROSE_SURVIVALS:
            for origin, reference in origin_paths:
                with self.subTest(prose=sample, origin=origin):
                    self._write_transcript(origin, sample, clip["id"])
                    activated = self._activate(reference)
                    self.assertEqual(activated.status_code, 200, activated.text)
                    before = self._stored_transcript_rows(clip["id"])

                    resolved = resolve_synthesis_request(self.state, self._body())

                    self.assertEqual(resolved.reference_text, sample)
                    self.assertEqual(self._stored_transcript_rows(clip["id"]), before)

    def test_run_record_keeps_the_reference_transcript_the_request_sent(self) -> None:
        activated = self._activate(self.second_reference)
        self.assertEqual(activated.status_code, 200, activated.text)
        body = self._body()
        sent = resolve_synthesis_request(self.state, body)
        self.assertEqual(sent.artifact.id, "art_second")
        self.assertEqual(sent.reference_text, SECOND_TRANSCRIPT)
        self.assertNotEqual(sent.reference_text, FIRST_TRANSCRIPT)

        created = self.client.post(
            "/api/synthesize",
            json={
                "text": body.text,
                "steer": body.steer,
                "voice_profile_id": body.voice_profile_id,
            },
        )

        self.assertEqual(created.status_code, 200, created.text)
        run = self.client.get(f"/api/runs/{created.json()['id']}").json()
        snapshot = run["effective_reference_snapshot"]
        self.assertEqual(snapshot["artifact_id"], sent.artifact.id)
        self.assertEqual(snapshot["transcript"], sent.reference_text)
        self.assertNotEqual(snapshot["transcript"], FIRST_TRANSCRIPT)


if __name__ == "__main__":
    unittest.main()
