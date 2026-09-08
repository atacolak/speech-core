"""not on the voicecat path.

Durable audio/reference library and experiment-run provenance.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tts.hashes import sha256_file, sha256_json
from tts.packets import new_id
from tts.paths import ensure_lab_dirs
from tts.wav import duration_s, is_riff_wav, read_wav, write_wav


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class LibraryItem:
    id: str
    kind: str
    path: str
    name: str
    sha256: str
    sample_rate: int
    duration_s: float
    tags: list[str] = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)
    variants: dict[str, str] = field(default_factory=dict)
    created_at: str = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LibraryItem":
        return cls(**{k: data.get(k) for k in cls.__dataclass_fields__})


class ArtifactStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = ensure_lab_dirs(root)
        self.index_path = self.root / "library.json"

    def _index(self) -> dict[str, Any]:
        if not self.index_path.is_file():
            return {"items": []}
        return json.loads(self.index_path.read_text(encoding="utf-8"))

    def _save_index(self, payload: dict[str, Any]) -> None:
        self.index_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def items(self, kind: str | None = None) -> list[LibraryItem]:
        out = [LibraryItem.from_dict(item) for item in self._index().get("items") or []]
        if kind:
            out = [item for item in out if item.kind == kind]
        return out

    def get(self, item_id: str) -> LibraryItem:
        for item in self.items():
            if item.id == item_id:
                return item
        raise KeyError(item_id)

    def ingest_wav(
        self,
        path: Path | str,
        *,
        kind: str,
        name: str,
        tags: list[str] | None = None,
        provenance: dict[str, Any] | None = None,
        dest_name: str | None = None,
    ) -> LibraryItem:
        src = Path(path)
        if not is_riff_wav(src):
            raise ValueError(f"internal artifacts must be RIFF/WAV: {src}")
        sr, samples = read_wav(src)
        item_id = new_id("au")
        dest = self.root / ("references" if kind == "reference" else "generations")
        dest.mkdir(parents=True, exist_ok=True)
        stored = dest / f"{dest_name or item_id}.wav"
        write_wav(stored, sr, samples)
        item = LibraryItem(
            id=item_id,
            kind=kind,
            path=str(stored),
            name=name,
            sha256=sha256_file(stored),
            sample_rate=sr,
            duration_s=duration_s(sr, samples),
            tags=list(tags or []),
            provenance=dict(provenance or {}),
            variants={"original": str(stored)},
        )
        payload = self._index()
        payload.setdefault("items", []).append(item.to_dict())
        self._save_index(payload)
        return item

    def attach_variant(self, item_id: str, variant: str, path: Path | str) -> LibraryItem:
        src = Path(path)
        if not is_riff_wav(src):
            raise ValueError(f"variant must be RIFF/WAV: {src}")
        payload = self._index()
        for item in payload.get("items") or []:
            if item.get("id") == item_id:
                item.setdefault("variants", {})[variant] = str(src)
                self._save_index(payload)
                return LibraryItem.from_dict(item)
        raise KeyError(item_id)

    def delete(self, item_id: str, *, unlink: bool = False) -> None:
        payload = self._index()
        kept = []
        removed = None
        for item in payload.get("items") or []:
            if item.get("id") == item_id:
                removed = item
            else:
                kept.append(item)
        if removed is None:
            raise KeyError(item_id)
        payload["items"] = kept
        self._save_index(payload)
        if unlink:
            path = Path(removed["path"])
            if path.is_file():
                path.unlink()

    def write_run(self, record: dict[str, Any]) -> Path:
        run_id = str(record.get("run_id") or new_id("run"))
        record = dict(record)
        record["run_id"] = run_id
        record.setdefault("created_at", _now())
        dest = self.root / "runs" / f"{run_id}.json"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(record, indent=2, default=str) + "\n", encoding="utf-8")
        return dest


def copy_if_trusted_local(path: Path) -> bool:
    """True when path is a real local file, not a Gradio temp upload."""
    raw = str(path)
    if "/tmp/" in raw or "/gradio/" in raw or raw.startswith("/tmp"):
        return False
    return path.is_file()
