"""Side recorder for live-call hop streams. not on the voicecat path."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any, Iterator

import numpy as np

from tts.lab.backend.services.conversations import insert_turn, open_conversation_id
from tts.lab.backend.services.run_alignment import pending_alignment
from tts.wav import write_wav

SAMPLE_RATE = 24000  # 24 kHz s16le mono, per the hop contract


class ConversationRecorder:
    def __init__(self, store: Any, alignments: Any) -> None:
        self._store = store
        self._alignments = alignments

    def wrap(self, request: dict[str, Any], chunks: Iterator[bytes]) -> Iterator[bytes]:
        conversation_id = open_conversation_id(self._store)
        if conversation_id is None:
            yield from chunks
            return
        captured = bytearray()
        try:
            for chunk in chunks:
                if chunk:
                    captured.extend(chunk)
                yield chunk
        finally:
            self._finalize(conversation_id, request, bytes(captured))

    def _finalize(self, conversation_id: str, request: dict[str, Any], pcm: bytes) -> None:
        """Store the uttered prefix as a pinned assistant turn. Never raises."""
        if not pcm:
            return
        try:
            samples = np.frombuffer(pcm, dtype=np.int16)
            dest = Path(tempfile.mkdtemp(prefix="tts-lab-turn-")) / "turn.wav"
            write_wav(dest, SAMPLE_RATE, samples)
            artifact = self._store.import_audio(dest)
            turn = insert_turn(
                self._store,
                conversation_id,
                role="assistant",
                text=str(request.get("text") or ""),
                audio_artifact_id=artifact.id,
                voice_id=request.get("voice_profile_id"),
                steer=request.get("steer"),
                generation=request.get("generation"),
            )
            self._store.execute(
                "UPDATE conversation_turns SET alignment_json = ? WHERE id = ?",
                (json.dumps(pending_alignment()), turn["id"]),
            )
            self._store.pin(artifact.id, reason=f"turn:{turn['id']}")
            self._store.commit()
            try:
                self._alignments.schedule_turn(self._store, turn["id"], artifact.id)
            except Exception:
                pass
        except Exception:
            return
