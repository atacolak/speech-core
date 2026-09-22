"""Conversation sessions chat API. not on the voicecat path."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, ConfigDict

from tts.lab.backend.services.conversations import (
    clamp_ring_limit,
    insert_turn,
    keep_turn_as_take,
    prune_unsaved_conversations,
)
from tts.lab.backend.store.session import write_conversation_ring_limit
from tts.wav import write_wav

router = APIRouter()


def _json_field(row: Any, key: str) -> Any:
    raw = row[key]
    if not raw:
        return None
    return json.loads(raw)


def _summary(store: Any, row: Any) -> dict[str, Any]:
    count = store.execute(
        "SELECT COUNT(*) AS n FROM conversation_turns WHERE conversation_id = ?",
        (row["id"],),
    ).fetchone()
    return {
        "id": row["id"],
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "saved": bool(row["saved"]),
        "turn_count": int(count["n"]),
    }


def _turn(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "conversation_id": row["conversation_id"],
        "msg_seq": row["msg_seq"],
        "variation_seq": row["variation_seq"],
        "role": row["role"],
        "text": row["text"],
        "audio_artifact_id": row["audio_artifact_id"],
        "voice_id": row["voice_id"],
        "steer": row["steer"],
        "generation": _json_field(row, "generation_json"),
        "alignment": _json_field(row, "alignment_json"),
        "chosen": bool(row["chosen"]),
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "created_at": row["created_at"],
    }


def _get_conversation_row(store: Any, conversation_id: str) -> Any:
    row = store.execute(
        "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"conversation not found: {conversation_id}")
    return row


def _get_turn_row(store: Any, conversation_id: str, turn_id: str) -> Any:
    row = store.execute(
        "SELECT * FROM conversation_turns WHERE id = ? AND conversation_id = ?",
        (turn_id, conversation_id),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"turn not found: {turn_id}")
    return row


def _resume_pending_turns(state: Any, turns: list[Any]) -> None:
    """A pending turn read after a restart gets its alignment thread back."""
    for turn in turns:
        alignment = _json_field(turn, "alignment_json")
        artifact_id = turn["audio_artifact_id"]
        if alignment is None or alignment.get("status") != "pending" or not artifact_id:
            continue
        state.run_alignments.schedule_turn(state.store, str(turn["id"]), str(artifact_id))


@router.get("/api/conversations")
def list_conversations(request: Request) -> dict[str, Any]:
    store = request.app.state.lab.store
    rows = store.execute(
        "SELECT * FROM conversations ORDER BY created_at DESC, rowid DESC"
    ).fetchall()
    return {"items": [_summary(store, row) for row in rows]}


@router.get("/api/conversations/{conversation_id}")
def get_conversation(request: Request, conversation_id: str) -> dict[str, Any]:
    state = request.app.state.lab
    store = state.store
    row = _get_conversation_row(store, conversation_id)
    turns = store.execute(
        "SELECT * FROM conversation_turns WHERE conversation_id = ?"
        " ORDER BY msg_seq, variation_seq",
        (conversation_id,),
    ).fetchall()
    _resume_pending_turns(state, turns)
    return {**_summary(store, row), "turns": [_turn(turn) for turn in turns]}


class RingLimitBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ring_limit: int


@router.put("/api/conversations/config")
def put_config(request: Request, body: RingLimitBody) -> dict[str, Any]:
    store = request.app.state.lab.store
    limit = clamp_ring_limit(body.ring_limit)
    write_conversation_ring_limit(store.root, limit)
    return {"ring_limit": limit}


@router.post("/api/conversations/{conversation_id}/user-turn")
async def post_user_turn(
    request: Request,
    conversation_id: str,
    transcript: str = Form(...),
    audio: UploadFile | None = File(None),
    sample_rate: int | None = Form(None),
    started_at: float | None = Form(None),
    ended_at: float | None = Form(None),
) -> dict[str, Any]:
    store = request.app.state.lab.store
    row = store.execute(
        "SELECT id FROM conversations WHERE id = ?", (conversation_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"conversation not found: {conversation_id}")
    if not transcript.strip():
        raise HTTPException(status_code=422, detail="transcript_committed is empty")
    artifact_id = None
    if audio is not None:
        if sample_rate is None:
            raise HTTPException(status_code=422, detail="sample_rate is required with audio")
        pcm = await audio.read()
        samples = np.frombuffer(pcm, dtype=np.int16)
        dest = Path(tempfile.mkdtemp(prefix="tts-lab-user-")) / "user.wav"
        write_wav(dest, int(sample_rate), samples)
        artifact_id = store.import_audio(dest).id
    turn = insert_turn(
        store,
        conversation_id,
        role="user",
        text=transcript,
        audio_artifact_id=artifact_id,
        started_at=started_at,
        ended_at=ended_at,
    )
    if artifact_id is not None:
        store.pin(artifact_id, reason=f"turn:{turn['id']}")
        store.commit()
    return _turn(turn)


@router.post("/api/conversations/{conversation_id}/turns/{turn_id}/choose")
def choose_turn(request: Request, conversation_id: str, turn_id: str) -> dict[str, Any]:
    store = request.app.state.lab.store
    _get_conversation_row(store, conversation_id)
    turn = _get_turn_row(store, conversation_id, turn_id)
    store.execute(
        """
        UPDATE conversation_turns SET chosen = 0
        WHERE conversation_id = ? AND msg_seq = ?
        """,
        (conversation_id, turn["msg_seq"]),
    )
    store.execute(
        "UPDATE conversation_turns SET chosen = 1 WHERE id = ?",
        (turn_id,),
    )
    store.commit()
    row = store.execute(
        "SELECT * FROM conversation_turns WHERE id = ?", (turn_id,)
    ).fetchone()
    return _turn(row)


@router.post("/api/conversations/{conversation_id}/turns/{turn_id}/save")
def save_turn(request: Request, conversation_id: str, turn_id: str) -> dict[str, Any]:
    store = request.app.state.lab.store
    _get_conversation_row(store, conversation_id)
    turn = dict(_get_turn_row(store, conversation_id, turn_id))
    if (
        turn["role"] != "assistant"
        or not turn.get("audio_artifact_id")
        or not turn.get("voice_id")
    ):
        raise HTTPException(status_code=422, detail="turn is not a savable assistant variation")
    turn["generation"] = _json_field(turn, "generation_json")
    run_id = keep_turn_as_take(store, turn)
    return {"run_id": run_id}


@router.post("/api/conversations/{conversation_id}/save")
def save_conversation(request: Request, conversation_id: str) -> dict[str, Any]:
    store = request.app.state.lab.store
    row = _get_conversation_row(store, conversation_id)
    store.execute(
        "UPDATE conversations SET saved = 1 WHERE id = ?",
        (conversation_id,),
    )
    store.commit()
    turns = store.execute(
        "SELECT * FROM conversation_turns WHERE conversation_id = ?"
        " ORDER BY msg_seq, variation_seq",
        (conversation_id,),
    ).fetchall()
    for turn in turns:
        if (
            turn["role"] != "assistant"
            or not turn["chosen"]
            or not turn["audio_artifact_id"]
            or not turn["voice_id"]
        ):
            continue
        payload = dict(turn)
        payload["generation"] = _json_field(turn, "generation_json")
        keep_turn_as_take(store, payload)
    prune_unsaved_conversations(store)
    row = store.execute(
        "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
    ).fetchone()
    return _summary(store, row)
