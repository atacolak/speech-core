"""Conversation sessions chat API. not on the voicecat path."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, ConfigDict

from tts.lab.backend.routes.synthesis import SynthesisBody, resolve_synthesis_request
from tts.lab.backend.runtime.types import LiveCallActive, RuntimeBusy, RuntimeUnloaded
from tts.lab.backend.services.conversations import (
    clamp_ring_limit,
    delete_conversation,
    delete_variation,
    insert_turn,
    keep_turn_as_take,
    prune_unsaved_conversations,
    set_conversation_title,
)
from tts.lab.backend.services.run_alignment import pending_alignment
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
    title = row["title"] if "title" in row.keys() else None
    return {
        "id": row["id"],
        "title": title,
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
    set_conversation_title(store, conversation_id, transcript)
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
    if not turn.get("audio_artifact_id"):
        raise HTTPException(status_code=422, detail="turn has no audio")
    voice_id = turn.get("voice_id")
    if not voice_id:
        from tts.lab.backend.store.session import read_active_voice_id

        voice_id = read_active_voice_id(store.root)
    if not voice_id:
        raise HTTPException(status_code=422, detail="turn has no voice")
    turn["voice_id"] = voice_id
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


class RegenerateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    generation: dict[str, Any] | None = None


class TitleBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str


@router.post("/api/conversations/{conversation_id}/turns/{turn_id}/regenerate")
def regenerate_turn(
    request: Request,
    conversation_id: str,
    turn_id: str,
    body: RegenerateBody | None = None,
) -> dict[str, Any]:
    state = request.app.state.lab
    store = state.store
    _get_conversation_row(store, conversation_id)
    turn = dict(_get_turn_row(store, conversation_id, turn_id))
    if (
        turn["role"] != "assistant"
        or not turn.get("audio_artifact_id")
        or not turn.get("voice_id")
    ):
        raise HTTPException(status_code=422, detail="turn is not a regenerable assistant variation")
    remaining = float(getattr(state.runtime, "live_call_remaining_s", lambda: 0.0)())
    if remaining > 0:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "live_call_active",
                "message": "live call owns the engine",
                "remaining_s": remaining,
            },
        )
    generation = dict(_json_field(turn, "generation_json") or {})
    if body and body.generation:
        generation.update(body.generation)
    synth_body = SynthesisBody(
        text=str(turn["text"] or ""),
        steer=str(turn["steer"] or ""),
        voice_profile_id=str(turn["voice_id"]),
        generation=generation,
    )
    resolved = resolve_synthesis_request(state, synth_body)
    try:
        payload = state.runtime.synthesize(
            {
                "text": synth_body.text,
                "steer": synth_body.steer,
                "synthesis_text": synth_body.synthesis_text,
                "voice_profile_id": synth_body.voice_profile_id,
                "reference_audio": str(resolved.reference_path),
                "reference_text": resolved.reference_text,
                "generation": resolved.settings.to_dict(),
            }
        )
    except RuntimeUnloaded as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "runtime_unloaded", "runtime": "e2", "state": exc.state},
        ) from exc
    except RuntimeBusy as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "runtime_busy", "runtime": "e2", "state": exc.state},
        ) from exc
    except LiveCallActive as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "live_call_active",
                "message": "live call owns the engine",
                "remaining_s": exc.remaining_s,
            },
        ) from exc
    except RuntimeError as exc:
        message = str(exc)
        missing = "missing template fields" in message or "ref_text" in message
        raise HTTPException(
            status_code=422 if missing else 500,
            detail={
                "code": "missing_ref_text" if missing else "synthesize_failed",
                "message": message,
            },
        ) from exc
    artifact = store.import_audio(Path(payload["wav_path"]))
    next_seq = store.execute(
        """
        SELECT COALESCE(MAX(variation_seq) + 1, 0) AS next_seq
        FROM conversation_turns
        WHERE conversation_id = ? AND msg_seq = ?
        """,
        (conversation_id, turn["msg_seq"]),
    ).fetchone()
    new_turn = insert_turn(
        store,
        conversation_id,
        role="assistant",
        text=str(turn["text"] or ""),
        msg_seq=turn["msg_seq"],
        variation_seq=int(next_seq["next_seq"]),
        chosen=0,
        audio_artifact_id=artifact.id,
        voice_id=turn["voice_id"],
        steer=turn["steer"],
        generation=generation,
    )
    store.execute(
        "UPDATE conversation_turns SET alignment_json = ? WHERE id = ?",
        (json.dumps(pending_alignment()), new_turn["id"]),
    )
    store.pin(artifact.id, reason=f"turn:{new_turn['id']}")
    store.commit()
    state.run_alignments.schedule_turn(store, str(new_turn["id"]), str(artifact.id))
    row = store.execute(
        "SELECT * FROM conversation_turns WHERE id = ?", (new_turn["id"],)
    ).fetchone()
    return _turn(row)


@router.patch("/api/conversations/{conversation_id}")
def patch_conversation(request: Request, conversation_id: str, body: TitleBody) -> dict[str, Any]:
    store = request.app.state.lab.store
    _get_conversation_row(store, conversation_id)
    store.execute(
        "UPDATE conversations SET title = ? WHERE id = ?",
        (body.title.strip(), conversation_id),
    )
    store.commit()
    row = store.execute(
        "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
    ).fetchone()
    return _summary(store, row)


@router.delete("/api/conversations/{conversation_id}/turns/{turn_id}")
def delete_turn(request: Request, conversation_id: str, turn_id: str) -> dict[str, Any]:
    store = request.app.state.lab.store
    _get_conversation_row(store, conversation_id)
    _get_turn_row(store, conversation_id, turn_id)
    try:
        delete_variation(store, conversation_id, turn_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"turn not found: {turn_id}") from exc
    return {"ok": True}


@router.delete("/api/conversations/{conversation_id}")
def remove_conversation(request: Request, conversation_id: str) -> dict[str, Any]:
    store = request.app.state.lab.store
    _get_conversation_row(store, conversation_id)
    delete_conversation(store, conversation_id)
    return {"ok": True}
