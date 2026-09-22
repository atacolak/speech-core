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


def _insert_conversation(store: Any, conversation_id: str, title: str | None = None) -> str:
    now = _now()
    store.execute(
        """
        INSERT INTO conversations (id, started_at, ended_at, saved, title, created_at)
        VALUES (?, ?, NULL, 0, ?, ?)
        """,
        (conversation_id, now, title, now),
    )
    store.commit()
    return conversation_id


def begin_session(
    store: Any,
    session_id: str | None = None,
    *,
    reuse_open: bool = True,
    title: str | None = None,
) -> str:
    open_id = open_conversation_id(store)
    if session_id is not None:
        existing = store.execute(
            "SELECT id, ended_at FROM conversations WHERE id = ?",
            (session_id,),
        ).fetchone()
        if existing is not None and existing["ended_at"] is None:
            if title:
                set_conversation_title(store, str(existing["id"]), title)
            return str(existing["id"])
        if open_id is not None:
            end_session(store)
        if existing is not None:
            return _insert_conversation(store, new_id("cv"), title)
        return _insert_conversation(store, session_id, title)
    if open_id is not None:
        if reuse_open:
            if title:
                set_conversation_title(store, open_id, title)
            return open_id
        end_session(store)
    return _insert_conversation(store, new_id("cv"), title)


def set_conversation_title(store: Any, conversation_id: str, title: str) -> None:
    cleaned = title.strip()
    if not cleaned:
        return
    store.execute(
        "UPDATE conversations SET title = ? WHERE id = ? AND (title IS NULL OR title = '')",
        (cleaned, conversation_id),
    )
    store.commit()


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


def keep_turn_as_take(store: Any, turn: dict[str, Any]) -> str:
    """Save one chosen variation as a generate keep-take on its voice."""
    from tts.wav import read_wav

    run_id = new_id("run")
    created = _now()
    artifact = store.get(turn["audio_artifact_id"])
    rate, samples = read_wav(artifact.path)
    snapshot = {
        "text": turn["text"],
        "steer": turn["steer"],
        "voice_profile_id": turn["voice_id"],
        "generation": turn["generation"],
        "conversation_turn_id": turn["id"],
        "role": turn.get("role"),
    }
    store.execute(
        """
        INSERT INTO runs (
            id, voice_id, request_json, output_artifact_id, effective_reference_json,
            latency_ms, first_audio_ms, duration_s, rating, tags_json, created_at,
            alignment_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            turn["voice_id"],
            json.dumps(snapshot),
            turn["audio_artifact_id"],
            json.dumps({}),
            0.0,
            None,
            float(len(samples)) / float(rate),
            "keep",
            json.dumps([]),
            created,
            turn["alignment_json"],
        ),
    )
    store.pin(turn["audio_artifact_id"], reason=f"run:{run_id}")
    store.execute(
        "UPDATE voices SET latest_take_id = ?, updated_at = ? WHERE id = ?",
        (run_id, created, turn["voice_id"]),
    )
    store.commit()
    return run_id


def delete_variation(store: Any, conversation_id: str, turn_id: str) -> None:
    turn = store.execute(
        "SELECT * FROM conversation_turns WHERE id = ? AND conversation_id = ?",
        (turn_id, conversation_id),
    ).fetchone()
    if turn is None:
        raise KeyError(turn_id)
    siblings = store.execute(
        """
        SELECT id FROM conversation_turns
        WHERE conversation_id = ? AND msg_seq = ? AND id != ?
        ORDER BY variation_seq
        """,
        (conversation_id, turn["msg_seq"], turn_id),
    ).fetchall()
    if turn["audio_artifact_id"]:
        store.unpin(str(turn["audio_artifact_id"]), reason=f"turn:{turn_id}")
    store.execute("DELETE FROM conversation_turns WHERE id = ?", (turn_id,))
    if turn["chosen"] and siblings:
        store.execute(
            "UPDATE conversation_turns SET chosen = 1 WHERE id = ?",
            (str(siblings[0]["id"]),),
        )
    store.commit()


def delete_conversation(store: Any, conversation_id: str) -> None:
    turns = store.execute(
        "SELECT id, audio_artifact_id FROM conversation_turns WHERE conversation_id = ?",
        (conversation_id,),
    ).fetchall()
    for turn in turns:
        if turn["audio_artifact_id"]:
            store.unpin(str(turn["audio_artifact_id"]), reason=f"turn:{turn['id']}")
    store.execute(
        "DELETE FROM conversation_turns WHERE conversation_id = ?", (conversation_id,)
    )
    store.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
    store.commit()
