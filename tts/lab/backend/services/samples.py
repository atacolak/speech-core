"""Default dogfood sample voices. not on the voicecat path."""

from __future__ import annotations

import json
from pathlib import Path

from tts.lab.backend.services.voices import VoiceImportError, import_voice_from_path
from tts.lab.backend.store.artifacts import ArtifactStore
from tts.paths import REPO_ROOT

SAMPLE_VOICE_ID = "vp_sample_george_hotz"
SAMPLE_VARIANT_ID = "rv_sample_george_hotz"
SAMPLE_NAME = "George Hotz"
SAMPLE_TAGS = ["sample", "dogfood"]
SAMPLE_NOTES = "seed:george-hotz"
SAMPLE_FILENAME = "george-hotz-how-they-keep-you-trapped.mp3"
SAMPLE_FIXTURE = (
    REPO_ROOT / "tts" / "lab" / "fixtures" / "voices" / SAMPLE_FILENAME
)
SAMPLE_OPERATOR_SOURCE = Path.home() / "Documents" / SAMPLE_FILENAME


def sample_audio_path() -> Path | None:
    if SAMPLE_FIXTURE.is_file():
        return SAMPLE_FIXTURE
    if SAMPLE_OPERATOR_SOURCE.is_file():
        return SAMPLE_OPERATOR_SOURCE
    return None


def _already_seeded(store: ArtifactStore) -> bool:
    row = store.execute(
        "SELECT id FROM voices WHERE id = ?", (SAMPLE_VOICE_ID,)
    ).fetchone()
    if row is not None:
        return True
    rows = store.execute("SELECT id, name, tags_json, notes FROM voices").fetchall()
    for item in rows:
        tags = json.loads(item["tags_json"] or "[]")
        notes = item["notes"] or ""
        if SAMPLE_NOTES in notes or (
            item["name"] == SAMPLE_NAME and "sample" in tags
        ):
            return True
    return False


def ensure_sample_voices(store: ArtifactStore) -> str | None:
    if _already_seeded(store):
        return SAMPLE_VOICE_ID
    src = sample_audio_path()
    if src is None:
        print("sample voice seed skipped: george-hotz fixture missing", flush=True)
        return None
    try:
        return import_voice_from_path(
            store,
            src,
            name=SAMPLE_NAME,
            transcript="",
            tags=list(SAMPLE_TAGS),
            notes=SAMPLE_NOTES,
            voice_id=SAMPLE_VOICE_ID,
            variant_id=SAMPLE_VARIANT_ID,
        )
    except VoiceImportError as exc:
        print(f"sample voice seed skipped: {exc}", flush=True)
        return None
