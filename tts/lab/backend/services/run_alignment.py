"""Cached CPU Parakeet word alignment for settled runs. not on the voicecat path.

One daemon CPU thread per distinct pending run aligns the finished take once and
caches the result on the run row. It takes no runtime, processor, or GPU lease:
Breeze supplies no word timestamps, so this is the only timing source.
"""

from __future__ import annotations

import json
import math
import threading
from typing import Any

from tts.lab.backend.services.voices import transcribe_alignment

PENDING = "pending"
READY = "ready"
UNAVAILABLE = "unavailable"


def pending_alignment() -> dict[str, Any]:
    """The cache a run commits with, before any CPU work starts."""
    return {"status": PENDING}


def unavailable_alignment() -> dict[str, Any]:
    """The durable cache for missing tooling or a failed alignment."""
    return {"status": UNAVAILABLE, "text": "", "words": []}


def _words(raw: Any) -> list[dict[str, Any]]:
    """Finite, in-order {text,start_s,end_s} words; anything else is dropped."""
    cleaned: list[dict[str, Any]] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        try:
            start = float(item.get("start_s"))
            end = float(item.get("end_s"))
        except (TypeError, ValueError):
            continue
        if not text or not math.isfinite(start) or not math.isfinite(end) or end < start:
            continue
        cleaned.append({"text": text, "start_s": start, "end_s": end})
    cleaned.sort(key=lambda word: (word["start_s"], word["end_s"]))
    return cleaned


def align_run(store: Any, run_id: str, artifact_id: str) -> None:
    """Align one settled take on CPU and cache it on the run. Never raises."""
    try:
        result = transcribe_alignment(store.get(artifact_id).path)
        words = _words(result.get("words"))
        if words:
            alignment: dict[str, Any] = {
                "status": READY,
                "text": str(result.get("text") or "").strip(),
                "words": words,
            }
        else:
            alignment = unavailable_alignment()
    except Exception:
        # Fail closed through the same cached shape the missing-tooling path uses.
        alignment = unavailable_alignment()
    try:
        store.execute(
            "UPDATE runs SET alignment_json = ? WHERE id = ?",
            (json.dumps(alignment), run_id),
        )
        store.commit()
    except Exception:
        return


class RunAlignments:
    """Deduplicated CPU alignment of settled runs; one daemon thread per run id."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._active: set[str] = set()

    def schedule(self, store: Any, run_id: str, artifact_id: str) -> None:
        """Start alignment for a pending run unless it is already running."""
        with self._lock:
            if run_id in self._active:
                return
            self._active.add(run_id)
        threading.Thread(
            target=self._run,
            args=(store, run_id, artifact_id),
            name=f"run-align-{run_id}",
            daemon=True,
        ).start()

    def _run(self, store: Any, run_id: str, artifact_id: str) -> None:
        try:
            align_run(store, run_id, artifact_id)
        finally:
            with self._lock:
                self._active.discard(run_id)
