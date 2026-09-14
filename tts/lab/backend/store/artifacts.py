"""Content-addressed audio objects. not on the voicecat path.

Durable blobs live under objects/. Cache entries are rebuildable.
A pin is the only thing that keeps an object through cleanup.
"""

from __future__ import annotations

import json
import shutil
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tts.hashes import sha256_file
from tts.lab.backend.store.db import connect
from tts.wav import duration_s, is_riff_wav, read_wav


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Artifact:
    id: str
    sha256: str
    path: Path
    suffix: str
    bytes: int
    sample_rate: int | None
    duration_s: float | None
    created_at: str


def _row_to_artifact(root: Path, row: Any) -> Artifact:
    return Artifact(
        id=str(row["id"]),
        sha256=str(row["sha256"]),
        path=root / str(row["path"]),
        suffix=str(row["suffix"]),
        bytes=int(row["bytes"]),
        sample_rate=None if row["sample_rate"] is None else int(row["sample_rate"]),
        duration_s=None if row["duration_s"] is None else float(row["duration_s"]),
        created_at=str(row["created_at"]),
    )


class ArtifactStore:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = connect(self.root)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> Any:
        with self._lock:
            return self._conn.execute(sql, params)

    def commit(self) -> None:
        with self._lock:
            self._conn.commit()

    def import_audio(self, path: Path | str) -> Artifact:
        src = Path(path)
        if not src.is_file():
            raise FileNotFoundError(src)
        digest = sha256_file(src)
        existing = self.execute(
            "SELECT * FROM artifacts WHERE sha256 = ?", (digest,)
        ).fetchone()
        if existing is not None:
            artifact = _row_to_artifact(self.root, existing)
            if artifact.path.is_file():
                return artifact
        suffix = src.suffix.lower() or ".wav"
        rel = Path("objects") / f"{digest}{suffix}"
        dest = self.root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            shutil.copy2(src, dest)
        sample_rate = None
        length = None
        if is_riff_wav(dest):
            sr, samples = read_wav(dest)
            sample_rate = int(sr)
            length = duration_s(sr, samples)
        created = _now()
        self.execute(
            """
            INSERT INTO artifacts (id, sha256, path, suffix, bytes, sample_rate, duration_s, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET path=excluded.path
            """,
            (
                digest,
                digest,
                str(rel),
                suffix,
                dest.stat().st_size,
                sample_rate,
                length,
                created,
            ),
        )
        self.commit()
        return self.get(digest)

    def get(self, artifact_id: str) -> Artifact:
        row = self.execute(
            "SELECT * FROM artifacts WHERE id = ?", (artifact_id,)
        ).fetchone()
        if row is None:
            raise KeyError(artifact_id)
        return _row_to_artifact(self.root, row)

    def path_for(self, artifact_id: str) -> Path:
        try:
            return self.get(artifact_id).path
        except KeyError:
            return self.root / "objects" / artifact_id

    def pin(self, artifact_id: str, *, reason: str) -> None:
        self.get(artifact_id)
        self.execute(
            """
            INSERT INTO pins (artifact_id, reason, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(artifact_id, reason) DO NOTHING
            """,
            (artifact_id, reason, _now()),
        )
        self.commit()

    def unpin(self, artifact_id: str, *, reason: str) -> None:
        self.execute(
            "DELETE FROM pins WHERE artifact_id = ? AND reason = ?",
            (artifact_id, reason),
        )
        self.commit()

    def remember_cache(
        self,
        *,
        cache_key: str,
        artifact_id: str,
        processor: str,
        config: dict[str, Any],
    ) -> None:
        self.get(artifact_id)
        self.execute(
            """
            INSERT INTO cache_entries (cache_key, artifact_id, processor, config_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                artifact_id=excluded.artifact_id,
                processor=excluded.processor,
                config_json=excluded.config_json
            """,
            (cache_key, artifact_id, processor, json.dumps(config, sort_keys=True), _now()),
        )
        self.commit()

    def cleanup_cache(self) -> list[str]:
        rows = self.execute(
            """
            SELECT cache_entries.cache_key, cache_entries.artifact_id, artifacts.path
            FROM cache_entries
            JOIN artifacts ON artifacts.id = cache_entries.artifact_id
            WHERE cache_entries.artifact_id NOT IN (SELECT artifact_id FROM pins)
            """
        ).fetchall()
        removed: list[str] = []
        for row in rows:
            artifact_id = str(row["artifact_id"])
            blob = self.root / str(row["path"])
            self.execute(
                "DELETE FROM cache_entries WHERE cache_key = ?", (row["cache_key"],)
            )
            still_cached = self.execute(
                "SELECT 1 FROM cache_entries WHERE artifact_id = ?", (artifact_id,)
            ).fetchone()
            if still_cached is None:
                self.execute("DELETE FROM artifacts WHERE id = ?", (artifact_id,))
                if blob.is_file():
                    blob.unlink()
                removed.append(artifact_id)
        self.commit()
        return removed
