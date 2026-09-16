"""Sqlite bootstrap for the lab store. not on the voicecat path."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from tts.packets import new_id

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def db_path(root: Path) -> Path:
    return Path(root) / "lab.sqlite3"


def _backfill_voice_sources(conn: sqlite3.Connection) -> None:
    """Give every 1:1 voice its primary voice_sources row. Once per voice, ever."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(voices)")}
    if not {"source_artifact_id", "source_transcript", "keep_intervals_json", "created_at"} <= columns:
        return
    words = "source_words_json" if "source_words_json" in columns else "NULL"
    rows = conn.execute(
        f"""
        SELECT id, source_artifact_id, source_transcript, keep_intervals_json,
               created_at, {words} AS words_json
        FROM voices
        """
    ).fetchall()
    for row in rows:
        has_source = conn.execute(
            "SELECT 1 FROM voice_sources WHERE voice_id = ? LIMIT 1", (row["id"],)
        ).fetchone()
        if has_source:
            continue
        conn.execute(
            """
            INSERT INTO voice_sources
                (id, voice_id, label, artifact_id, transcript, words_json,
                 keep_intervals_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("vs"),
                row["id"],
                "Source 1",
                row["source_artifact_id"],
                row["source_transcript"] or "",
                row["words_json"],
                row["keep_intervals_json"],
                row["created_at"],
            ),
        )


def _backfill_voice_artifacts(conn: sqlite3.Connection) -> None:
    """reference_variants → voice_artifacts. `original` stays the source audio.

    An approved AuK candidate is a reference; every other kind is an experiment
    (resemble is a parked specialist, but its rows stay as experiments).
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(reference_variants)")}
    if not {"kind", "approved", "parent_variant_id", "auk_task", "created_at"} <= columns:
        return
    rows = conn.execute(
        "SELECT * FROM reference_variants WHERE kind <> 'original' ORDER BY created_at ASC, rowid ASC"
    ).fetchall()
    for row in rows:
        if conn.execute("SELECT 1 FROM voice_artifacts WHERE id = ?", (row["id"],)).fetchone():
            continue
        source = conn.execute(
            "SELECT id FROM voice_sources WHERE voice_id = ? ORDER BY rowid ASC LIMIT 1",
            (row["voice_id"],),
        ).fetchone()
        if source is None:
            # A voice with no primary source cannot own artifacts: the FK holds.
            continue
        if not conn.execute(
            "SELECT 1 FROM artifacts WHERE id = ?", (row["audio_artifact_id"],)
        ).fetchone():
            # A dangling variant audio cannot be represented under the FK either.
            continue
        parent = None
        if row["parent_variant_id"]:
            parent = conn.execute(
                "SELECT id FROM reference_variants WHERE id = ? AND kind <> 'original'",
                (row["parent_variant_id"],),
            ).fetchone()
        reference = row["kind"] == "auk" and bool(row["approved"])
        conn.execute(
            """
            INSERT INTO voice_artifacts
                (id, voice_id, role, kind, name, audio_artifact_id, parent_id, source_id,
                 processor_config_json, processor_cache_key, auk_task, instruction,
                 model_variant, auk_precision, encoder_precision, seed, settings_json,
                 approved_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["id"],
                row["voice_id"],
                "reference" if reference else "experiment",
                row["kind"],
                f"AuK {row['auk_task'] or 'candidate'}" if row["kind"] == "auk" else row["kind"].capitalize(),
                row["audio_artifact_id"],
                None if parent is None else parent["id"],
                None if source is None else source["id"],
                row["processor_config_json"],
                row["processor_cache_key"],
                row["auk_task"],
                row["instruction"],
                row["model_variant"],
                row["auk_precision"],
                row["encoder_precision"],
                row["seed"],
                row["settings_json"],
                row["created_at"] if reference else None,
                row["created_at"],
            ),
        )


def _backfill_default_references(conn: sqlite3.Connection) -> None:
    """The activated approved candidate becomes the profile's default reference."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(voices)")}
    if not {"active_reference_variant_id", "default_reference_id"} <= columns:
        return
    rows = conn.execute(
        """
        SELECT id, active_reference_variant_id FROM voices
        WHERE active_reference_variant_id IS NOT NULL AND default_reference_id IS NULL
        """
    ).fetchall()
    for row in rows:
        artifact = conn.execute(
            "SELECT id, role FROM voice_artifacts WHERE id = ? AND voice_id = ?",
            (row["active_reference_variant_id"], row["id"]),
        ).fetchone()
        if artifact is None or artifact["role"] != "reference":
            continue
        conn.execute(
            "UPDATE voices SET default_reference_id = ? WHERE id = ?", (artifact["id"], row["id"])
        )
        conn.execute("UPDATE voice_artifacts SET is_default = 1 WHERE id = ?", (artifact["id"],))


def _migrate(conn: sqlite3.Connection) -> None:
    voice_cols = {row[1] for row in conn.execute("PRAGMA table_info(voices)")}
    if "original_artifact_id" not in voice_cols:
        conn.execute("ALTER TABLE voices ADD COLUMN original_artifact_id TEXT")
    if "original_format" not in voice_cols:
        conn.execute("ALTER TABLE voices ADD COLUMN original_format TEXT")
    if "notes" not in voice_cols:
        conn.execute("ALTER TABLE voices ADD COLUMN notes TEXT")
    if "generation_json" not in voice_cols:
        conn.execute("ALTER TABLE voices ADD COLUMN generation_json TEXT")
    if "take_limit" not in voice_cols:
        conn.execute("ALTER TABLE voices ADD COLUMN take_limit INTEGER NOT NULL DEFAULT 5")
    if "source_words_json" not in voice_cols:
        conn.execute("ALTER TABLE voices ADD COLUMN source_words_json TEXT")
    if "transcript_locked" not in voice_cols:
        conn.execute("ALTER TABLE voices ADD COLUMN transcript_locked INTEGER NOT NULL DEFAULT 0")
    if "speaker_analysis_id" not in voice_cols:
        conn.execute("ALTER TABLE voices ADD COLUMN speaker_analysis_id TEXT")
    if "default_reference_id" not in voice_cols:
        conn.execute("ALTER TABLE voices ADD COLUMN default_reference_id TEXT")
    if "latest_take_id" not in voice_cols:
        conn.execute("ALTER TABLE voices ADD COLUMN latest_take_id TEXT")

    run_cols = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
    if "alignment_json" not in run_cols:
        conn.execute("ALTER TABLE runs ADD COLUMN alignment_json TEXT")

    variant_cols = {row[1] for row in conn.execute("PRAGMA table_info(reference_variants)")}
    for column, ddl in (
        ("parent_variant_id", "TEXT"),
        ("auk_task", "TEXT"),
        ("instruction", "TEXT"),
        ("model_variant", "TEXT"),
        ("auk_precision", "TEXT"),
        ("encoder_precision", "TEXT"),
        ("seed", "INTEGER"),
        ("settings_json", "TEXT"),
        ("approved", "INTEGER NOT NULL DEFAULT 0"),
    ):
        # Existing rows stay valid: null provenance, approved = 0.
        if column not in variant_cols:
            conn.execute(f"ALTER TABLE reference_variants ADD COLUMN {column} {ddl}")

    # Order matters: artifacts and defaults read the sources written above.
    _backfill_voice_sources(conn)
    _backfill_voice_artifacts(conn)
    _backfill_default_references(conn)


def connect(root: Path) -> sqlite3.Connection:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    (root / "objects").mkdir(parents=True, exist_ok=True)
    (root / "cache" / "resemble").mkdir(parents=True, exist_ok=True)
    (root / "exports").mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path(root)), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    _migrate(conn)
    conn.commit()
    return conn
