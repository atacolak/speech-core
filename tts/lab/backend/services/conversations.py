"""Conversation sessions and turns. not on the voicecat path."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from tts.lab.backend.store.session import read_conversation_ring_limit
from tts.packets import new_id

DEFAULT_RING_LIMIT = 3
MAX_RING_LIMIT = 50


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def clamp_ring_limit(value: Any) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return DEFAULT_RING_LIMIT
    return max(1, min(MAX_RING_LIMIT, limit))


def open_conversation_id(store: Any) -> str | None:
    row = store.execute(
        "SELECT id FROM conversations WHERE ended_at IS NULL ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    return None if row is None else str(row["id"])


def _insert_conversation(store: Any, conversation_id: str) -> str:
    now = _now()
    store.execute(
        """
        INSERT INTO conversations (id, started_at, ended_at, saved, created_at)
        VALUES (?, ?, NULL, 0, ?)
        """,
        (conversation_id, now, now),
    )
    store.commit()
    return conversation_id


def begin_session(
    store: Any, session_id: str | None = None, *, reuse_open: bool = True
) -> str:
    open_id = open_conversation_id(store)
    if session_id is not None:
        existing = store.execute(
            "SELECT id, ended_at FROM conversations WHERE id = ?",
            (session_id,),
        ).fetchone()
        if existing is not None and existing["ended_at"] is None:
            return str(existing["id"])
        if open_id is not None:
            end_session(store)
        if existing is not None:
            return _insert_conversation(store, new_id("cv"))
        return _insert_conversation(store, session_id)
    if open_id is not None:
        if reuse_open:
            return open_id
        end_session(store)
    return _insert_conversation(store, new_id("cv"))


def end_session(store: Any) -> str | None:
    open_id = open_conversation_id(store)
    if open_id is None:
        return None
    store.execute(
        "UPDATE conversations SET ended_at = ? WHERE id = ? AND ended_at IS NULL",
        (_now(), open_id),
    )
    store.commit()
    prune_unsaved_conversations(store)
    return open_id


def insert_turn(
    store: Any,
    conversation_id: str,
    *,
    role: str,
    text: str,
    msg_seq: int | None = None,
    audio_artifact_id: str | None = None,
    voice_id: str | None = None,
    steer: str | None = None,
    generation: dict[str, Any] | None = None,
    variation_seq: int = 0,
    chosen: int = 1,
    started_at: float | None = None,
    ended_at: float | None = None,
) -> dict[str, Any]:
    allocated = msg_seq
    if allocated is None:
        row = store.execute(
            """
            SELECT COALESCE(MAX(msg_seq) + 1, 0) AS next_seq
            FROM conversation_turns
            WHERE conversation_id = ?
            """,
            (conversation_id,),
        ).fetchone()
        allocated = int(row["next_seq"])
    if chosen == 1:
        store.execute(
            """
            UPDATE conversation_turns
            SET chosen = 0
            WHERE conversation_id = ? AND msg_seq = ?
            """,
            (conversation_id, allocated),
        )
    turn_id = new_id("ct")
    generation_json = None if generation is None else json.dumps(generation)
    store.execute(
        """
        INSERT INTO conversation_turns (
            id, conversation_id, msg_seq, variation_seq, role, text,
            audio_artifact_id, voice_id, steer, generation_json, alignment_json,
            chosen, started_at, ended_at, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)
        """,
        (
            turn_id,
            conversation_id,
            allocated,
            variation_seq,
            role,
            text,
            audio_artifact_id,
            voice_id,
            steer,
            generation_json,
            chosen,
            started_at,
            ended_at,
            _now(),
        ),
    )
    store.commit()
    row = store.execute(
        "SELECT * FROM conversation_turns WHERE id = ?", (turn_id,)
    ).fetchone()
    return dict(row)


def prune_unsaved_conversations(store: Any, ring_limit: int | None = None) -> int:
    limit = clamp_ring_limit(
        ring_limit if ring_limit is not None else read_conversation_ring_limit(store.root)
    )
    rows = store.execute(
        "SELECT id FROM conversations WHERE saved = 0 AND ended_at IS NOT NULL"
        " ORDER BY created_at DESC, rowid DESC"
    ).fetchall()
    extras = list(rows)[limit:]
    for row in extras:
        turns = store.execute(
            "SELECT id, audio_artifact_id FROM conversation_turns WHERE conversation_id = ?",
            (row["id"],),
        ).fetchall()
        for turn in turns:
            if turn["audio_artifact_id"]:
                store.unpin(str(turn["audio_artifact_id"]), reason=f"turn:{turn['id']}")
        store.execute(
            "DELETE FROM conversation_turns WHERE conversation_id = ?", (row["id"],)
        )
        store.execute("DELETE FROM conversations WHERE id = ?", (row["id"],))
    if extras:
        store.commit()
    return len(extras)
