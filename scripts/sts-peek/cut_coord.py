#!/usr/bin/env python3
"""sts-peek cut coordinator (Track C).

Watches a run_dir / event stream for:
  1. barge / pause (first user alnum or explicit barge mark)
  2. assistant.self_asr drain progress during user speech
  3. user transcript_committed → finalize production cut

Primary: drain-during-user-speech → force-align drained B to intended LLM text.
Fallback: last alignment pos + pad_words (default 2) from intended only.

Writes (once, immutable commit):
  run_dir/production_cut_text
  run_dir/metrics.json
  run_dir/commit.json
  run_dir/cut_decision.json
  run_dir/events.jsonl  (coordinator-local timeline append)

No protocol / daemon schema changes. Fail closed if B stream missing in follow
mode after timeout.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from cut import (
    DEFAULT_PAD_WORDS,
    apply_production_cut,
    commit_truncated_assistant_message,
    intended_prefix_at_word_index,
    is_prefix_of_intended,
    normalize_whitespace,
    score_three_way,
    tokenize_words,
    word_set_diff_count,
)

# ---------------------------------------------------------------------------
# run_dir integration contract (shared with Track L audio / Track U UI)
# ---------------------------------------------------------------------------
#
# Expected files written by audio/UI tracks (tolerate missing with clear wait):
#
#   assistant_intended.txt     — intended LLM text (or CLI --intended-text)
#   events.jsonl               — append-only event stream (see EVENT NAMES)
#   control/barge.json         — optional barge mark {ts_ms, emitted_words, ...}
#   control/user_commit.json   — optional finalize {user_transcript, ...}
#   control/drain.json         — optional drain snapshot
#                                {drained_asr_text, drain_complete, b_pos_words,
#                                 emitted_words_at_pause, last_aligned_pos_words}
#   drained_asr_text.txt       — optional plain drain text at finalize
#   assistant_self_asr.txt     — alias for drain text
#
# EVENT NAMES recognized in events.jsonl (any of):
#   barge / speech_out_barge_in / user_first_alphanumeric_token / pause_playback
#   assistant_self_asr_drain_started / assistant_self_asr_drain_progress
#   user_transcript_committed / transcript_committed
#
# Coordinator outputs (owned by Track C):
#   production_cut_text, metrics.json, commit.json, cut_decision.json
#   (and appends coordinator events to events.jsonl)
#
# Fail closed: follow mode with no B drain evidence by finalize → fallback cut
# is still written, but metrics.fail_closed_b_stream_missing=true when neither
# drain file nor drain events appeared.

LABEL_DEFAULT = "live_peek"

BARGE_EVENT_NAMES = frozenset(
    {
        "barge",
        "speech_out_barge_in",
        "user_first_alphanumeric_token",
        "pause_playback",
        "sts_peek_barge",
    }
)
DRAIN_EVENT_NAMES = frozenset(
    {
        "assistant_self_asr_drain_started",
        "assistant_self_asr_drain_progress",
        "assistant_self_asr_drain_complete",
        "nemotron_b_drain",
        "sts_peek_drain",
    }
)
USER_COMMIT_EVENT_NAMES = frozenset(
    {
        "user_transcript_committed",
        "transcript_committed",
        "sts_peek_user_commit",
    }
)


@dataclass
class CutCoordConfig:
    run_dir: Path
    intended_text: str
    pad_words: int = DEFAULT_PAD_WORDS
    label: str = LABEL_DEFAULT
    utterance_id: str = "sts-peek-assistant-1"
    # follow-mode polling
    poll_interval_s: float = 0.05
    wait_timeout_s: float = 30.0
    min_align_words: int = 1
    min_align_confidence: float = 0.25


@dataclass
class DrainState:
    drained_asr_text: str = ""
    drain_complete: bool = False
    b_pos_words: int = 0
    emitted_words_at_pause: int = 0
    last_aligned_pos_words: int = 0
    seen_b_stream: bool = False
    playback_pos_words: int | None = None


@dataclass
class CoordState:
    barged: bool = False
    barge_event: dict[str, Any] = field(default_factory=dict)
    user_committed: bool = False
    user_transcript: str = ""
    user_commit_event: dict[str, Any] = field(default_factory=dict)
    drain: DrainState = field(default_factory=DrainState)
    finalized: bool = False
    events_offset: int = 0


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")


def _write_json(path: Path, obj: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def _append_event(
    events_path: Path,
    event: dict[str, Any],
    *,
    from_coord: bool = False,
) -> None:
    event = dict(event)
    event.setdefault("label", LABEL_DEFAULT)
    event.setdefault("diagnostic_mono_ns", time.monotonic_ns())
    # Only mark coordinator-emitted events so ingest can ignore our own echoes
    # without dropping simulated/audio-track timeline events written via this helper.
    if from_coord:
        event.setdefault("source", "sts_peek_cut_coord")
    with events_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")


def _read_json_if_exists(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _read_text_if_exists(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def load_intended_text(run_dir: Path, cli_intended: str | None) -> str:
    """Resolve intended text: CLI wins, else run_dir/assistant_intended.txt."""
    if cli_intended is not None and normalize_whitespace(cli_intended):
        return normalize_whitespace(cli_intended)
    for name in ("assistant_intended.txt", "intended_text.txt", "intended.txt"):
        raw = _read_text_if_exists(run_dir / name)
        if raw is not None and normalize_whitespace(raw):
            return normalize_whitespace(raw)
    raise ValueError(
        "intended text required: pass --intended-text or write "
        f"{run_dir}/assistant_intended.txt"
    )


def _event_name(ev: dict[str, Any]) -> str:
    for key in ("event", "type", "name"):
        val = ev.get(key)
        if isinstance(val, str) and val:
            return val
    return ""


def _as_int(val: Any, default: int = 0) -> int:
    try:
        if val is None:
            return default
        return int(val)
    except (TypeError, ValueError):
        return default


def _as_bool(val: Any, default: bool = False) -> bool:
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    if isinstance(val, str):
        return val.strip().lower() in {"1", "true", "yes", "y", "on", "complete"}
    return default


def apply_event(state: CoordState, ev: dict[str, Any]) -> None:
    """Update coordinator state from one event dict."""
    name = _event_name(ev)

    if name in BARGE_EVENT_NAMES or _as_bool(ev.get("barge")):
        state.barged = True
        state.barge_event = ev
        emitted = _as_int(
            ev.get("emitted_words_at_pause", ev.get("emitted_pcm_words", ev.get("emitted_words"))),
            state.drain.emitted_words_at_pause,
        )
        if emitted:
            state.drain.emitted_words_at_pause = emitted
        b_pos = _as_int(ev.get("b_pos_at_pause", ev.get("b_pos_words")), 0)
        if b_pos and not state.drain.seen_b_stream:
            state.drain.b_pos_words = b_pos
            state.drain.last_aligned_pos_words = b_pos
        play = ev.get("playback_pos_words")
        if play is not None:
            state.drain.playback_pos_words = _as_int(play)

    if name in DRAIN_EVENT_NAMES or "drained_text" in ev or "drained_asr_text" in ev:
        state.drain.seen_b_stream = True
        drained = ev.get("drained_asr_text", ev.get("drained_text", ev.get("text")))
        if isinstance(drained, str):
            state.drain.drained_asr_text = normalize_whitespace(drained)
        b_pos = _as_int(
            ev.get("b_pos_words", ev.get("b_pos_at_commit", ev.get("nemotron_pos_words"))),
            state.drain.b_pos_words,
        )
        state.drain.b_pos_words = b_pos
        state.drain.last_aligned_pos_words = _as_int(
            ev.get("last_aligned_pos_words", b_pos),
            b_pos,
        )
        if "drain_complete" in ev:
            state.drain.drain_complete = _as_bool(ev.get("drain_complete"))
        elif name == "assistant_self_asr_drain_complete":
            state.drain.drain_complete = True
        emitted = _as_int(
            ev.get("emitted_words", ev.get("target_emitted_words", ev.get("emitted_words_at_pause"))),
            state.drain.emitted_words_at_pause,
        )
        if emitted:
            state.drain.emitted_words_at_pause = emitted
            if state.drain.b_pos_words >= emitted and emitted > 0:
                state.drain.drain_complete = True

    if name in USER_COMMIT_EVENT_NAMES:
        state.user_committed = True
        state.user_commit_event = ev
        ut = ev.get("user_transcript", ev.get("transcript", ev.get("text")))
        if isinstance(ut, str):
            state.user_transcript = normalize_whitespace(ut)
        # Allow commit event to carry final drain snapshot.
        if "drained_asr_text" in ev or "drained_text" in ev:
            apply_event(
                state,
                {
                    "event": "assistant_self_asr_drain_progress",
                    "drained_asr_text": ev.get("drained_asr_text", ev.get("drained_text")),
                    "drain_complete": ev.get("drain_complete"),
                    "b_pos_words": ev.get("b_pos_words", ev.get("b_pos_at_commit")),
                    "emitted_words": ev.get("emitted_words_at_pause", ev.get("emitted_words")),
                    "last_aligned_pos_words": ev.get("last_aligned_pos_words"),
                },
            )


def ingest_events_jsonl(state: CoordState, events_path: Path) -> int:
    """Read new lines from events.jsonl; return number of new events applied."""
    if not events_path.is_file():
        return 0
    try:
        raw = events_path.read_text(encoding="utf-8")
    except OSError:
        return 0
    lines = raw.splitlines()
    new_lines = lines[state.events_offset :]
    applied = 0
    for line in new_lines:
        state.events_offset += 1
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(ev, dict):
            # Ignore our own echo events for state machine (still advance offset).
            if ev.get("source") == "sts_peek_cut_coord":
                continue
            apply_event(state, ev)
            applied += 1
    return applied


def ingest_control_files(state: CoordState, run_dir: Path) -> None:
    """Poll optional control/*.json and drain text files."""
    control = run_dir / "control"

    barge = _read_json_if_exists(control / "barge.json")
    if barge is not None and not state.barged:
        barge = dict(barge)
        barge.setdefault("event", "sts_peek_barge")
        apply_event(state, barge)

    drain = _read_json_if_exists(control / "drain.json")
    if drain is not None:
        drain = dict(drain)
        drain.setdefault("event", "sts_peek_drain")
        apply_event(state, drain)

    # Plain text drain snapshots (audio track may write these at finalize).
    for name in (
        "drained_asr_text.txt",
        "assistant_self_asr.txt",
        "assistant_self_asr_drain.txt",
    ):
        raw = _read_text_if_exists(run_dir / name)
        if raw is not None and normalize_whitespace(raw):
            state.drain.seen_b_stream = True
            state.drain.drained_asr_text = normalize_whitespace(raw)
            # If file exists and non-empty, treat as available drain evidence.
            # Completeness still requires control/drain.json or events unless
            # drain_complete marker file present.
            break

    complete_marker = run_dir / "control" / "drain_complete"
    if complete_marker.is_file():
        state.drain.seen_b_stream = True
        state.drain.drain_complete = True

    user = _read_json_if_exists(control / "user_commit.json")
    if user is not None and not state.user_committed:
        user = dict(user)
        user.setdefault("event", "sts_peek_user_commit")
        apply_event(state, user)


def build_cut_artifacts(
    cfg: CutCoordConfig,
    state: CoordState,
) -> dict[str, Any]:
    """Compute cut + metrics + commit payloads from coordinator state."""
    intended = normalize_whitespace(cfg.intended_text)
    drain = state.drain

    last_pos = drain.last_aligned_pos_words
    if last_pos <= 0 and drain.b_pos_words > 0:
        last_pos = drain.b_pos_words

    # If we never saw B stream, fail closed into fallback with flag.
    b_missing = not drain.seen_b_stream
    drain_complete = bool(drain.drain_complete) and not b_missing
    drained_asr = drain.drained_asr_text if not b_missing else ""

    production_cut, source, detail = apply_production_cut(
        intended,
        drained_asr=drained_asr if drain_complete or drained_asr else drained_asr,
        drain_complete=drain_complete,
        last_aligned_pos_words=last_pos,
        pad_words=cfg.pad_words,
        min_align_words=cfg.min_align_words,
        min_align_confidence=cfg.min_align_confidence,
    )

    # Playback proxy: prefer explicit playback_pos, else emitted at pause.
    if drain.playback_pos_words is not None:
        playback_pos = drain.playback_pos_words
    elif drain.emitted_words_at_pause > 0:
        playback_pos = drain.emitted_words_at_pause
    else:
        playback_pos = last_pos

    nemotron_pos = drain.b_pos_words if drain.b_pos_words else last_pos
    three = score_three_way(
        intended,
        playback_pos_words=playback_pos,
        nemotron_pos_words=nemotron_pos,
        asr_recovered_at_stop=drained_asr,
        pad_words=cfg.pad_words,
        drain_complete=drain_complete,
        drained_asr=drained_asr,
        last_aligned_pos_words=last_pos,
        label=cfg.label,
        min_align_words=cfg.min_align_words,
        min_align_confidence=cfg.min_align_confidence,
    )
    # Prefer the production_cut we already selected (score_three_way re-runs;
    # keep them consistent — re-score uses same inputs so should match).
    if three.production_cut != production_cut or three.primary_cut_source != source:
        # Force metrics to match the decision we commit.
        cut_words = tokenize_words(production_cut)
        play_words = tokenize_words(three.intended_at_playback)
        three = score_three_way(
            intended,
            playback_pos_words=playback_pos,
            nemotron_pos_words=nemotron_pos,
            asr_recovered_at_stop=drained_asr,
            pad_words=cfg.pad_words,
            drain_complete=drain_complete,
            drained_asr=drained_asr,
            last_aligned_pos_words=last_pos,
            label=cfg.label,
            min_align_words=cfg.min_align_words,
            min_align_confidence=cfg.min_align_confidence,
        )
        # If still divergent (edge empty cases), patch fields below via metrics.

    prefix_valid = is_prefix_of_intended(production_cut, intended)
    cut_words = tokenize_words(production_cut)
    play_words = tokenize_words(intended_prefix_at_word_index(intended, playback_pos))
    overspeak = word_set_diff_count(cut_words, play_words)
    underspeak = word_set_diff_count(play_words, cut_words)

    commit = commit_truncated_assistant_message(
        production_cut,
        utterance_id=cfg.utterance_id,
        primary_cut_source=source,
        user_transcript=state.user_transcript or None,
        pad_words=cfg.pad_words,
        drain_complete=drain_complete,
        label=cfg.label,
    )

    cut_decision = {
        "event": "sts_peek_cut_decision",
        "label": cfg.label,
        "production_cut_text": production_cut,
        "primary_cut_source": source,
        "intended_text": intended,
        "drained_asr_text": drained_asr,
        "aligned_prefix": detail["aligned_prefix"],
        "aligned_word_count": detail["aligned_word_count"],
        "align_confidence": detail["align_confidence"],
        "fallback_text": detail["fallback_text"],
        "last_aligned_pos_words": last_pos,
        "pad_words": cfg.pad_words,
        "drain_complete": drain_complete,
        "prefix_valid": prefix_valid,
        "fail_closed_b_stream_missing": b_missing,
    }

    metrics: dict[str, Any] = {
        "label": cfg.label,
        "mode": "sts_peek_cut",
        "topology": {
            "nemotron_a": "user_mic_path",
            "nemotron_b": "assistant.self_asr",
            "shared_worker": False,
            "contract_rev": 4,
        },
        "founder_cut_rule": {
            "pause_on": "first_user_alphanumeric_token_or_barge",
            "finalize_on": "user_transcript_committed",
            "primary": "drained_asr_force_aligned_to_intended",
            "fallback": "last_pos_words + pad_words from intended",
            "pad_words": cfg.pad_words,
            "commit_once": True,
            "late_self_asr": "diagnostic_only",
        },
        "primary_cut_source": source,
        "production_cut_text": production_cut,
        "prefix_valid": prefix_valid,
        "pad_words": cfg.pad_words,
        "overspeak_words": overspeak,
        "underspeak_words": underspeak,
        "intended_at_playback": intended_prefix_at_word_index(intended, playback_pos),
        "asr_recovered_at_stop": drained_asr,
        "nemotron_pos_words": nemotron_pos,
        "playback_pos_words": playback_pos,
        "last_aligned_pos_words": last_pos,
        "drain_complete": drain_complete,
        "fail_closed_b_stream_missing": b_missing,
        "user_transcript": state.user_transcript,
        "streams": {
            "emitted_words_at_pause": drain.emitted_words_at_pause,
            "b_pos_words": drain.b_pos_words,
            "seen_b_stream": drain.seen_b_stream,
            "drained_asr_text": drained_asr,
        },
        "cut": cut_decision,
        "three_way": three.to_metrics_dict(),
    }

    return {
        "production_cut_text": production_cut,
        "primary_cut_source": source,
        "prefix_valid": prefix_valid,
        "metrics": metrics,
        "commit": commit,
        "cut_decision": cut_decision,
        "fail_closed_b_stream_missing": b_missing,
    }


def write_cut_artifacts(run_dir: Path, artifacts: dict[str, Any], cfg: CutCoordConfig) -> None:
    """Write production_cut_text, metrics.json, commit.json, cut_decision.json."""
    run_dir.mkdir(parents=True, exist_ok=True)
    _write_text(run_dir / "production_cut_text", artifacts["production_cut_text"])
    _write_text(run_dir / "assistant_intended.txt", normalize_whitespace(cfg.intended_text))
    if artifacts["metrics"].get("asr_recovered_at_stop") is not None:
        _write_text(
            run_dir / "drained_asr_text.txt",
            artifacts["metrics"].get("asr_recovered_at_stop") or "",
        )
    _write_json(run_dir / "metrics.json", artifacts["metrics"])
    _write_json(run_dir / "commit.json", artifacts["commit"])
    _write_json(run_dir / "cut_decision.json", artifacts["cut_decision"])

    events_path = run_dir / "events.jsonl"
    _append_event(events_path, artifacts["cut_decision"], from_coord=True)
    _append_event(events_path, artifacts["commit"], from_coord=True)
    _append_event(
        events_path,
        {
            "event": "sts_peek_cut_finalized",
            "primary_cut_source": artifacts["primary_cut_source"],
            "production_cut_text": artifacts["production_cut_text"],
            "prefix_valid": artifacts["prefix_valid"],
            "fail_closed_b_stream_missing": artifacts["fail_closed_b_stream_missing"],
            "immutable": True,
        },
        from_coord=True,
    )


def _load_existing_finalized(run_dir: Path) -> dict[str, Any] | None:
    """If commit.json is present and immutable, return existing artifacts (no revise)."""
    commit = _read_json_if_exists(run_dir / "commit.json")
    if not commit or not commit.get("immutable"):
        return None
    metrics = _read_json_if_exists(run_dir / "metrics.json") or {}
    cut_text = (_read_text_if_exists(run_dir / "production_cut_text") or "").strip()
    decision = _read_json_if_exists(run_dir / "cut_decision.json") or {}
    return {
        "production_cut_text": cut_text or commit.get("production_cut_text", ""),
        "primary_cut_source": metrics.get("primary_cut_source")
        or commit.get("primary_cut_source"),
        "prefix_valid": metrics.get("prefix_valid"),
        "metrics": metrics,
        "commit": commit,
        "cut_decision": decision,
        "fail_closed_b_stream_missing": metrics.get("fail_closed_b_stream_missing"),
        "already_finalized": True,
    }


def finalize(cfg: CutCoordConfig, state: CoordState) -> dict[str, Any]:
    """Compute + write cut artifacts once. Idempotent if already finalized.

    Disk immutability: if run_dir/commit.json exists with immutable=true,
    never rewrite production_cut_text / metrics / commit (late self-ASR
    non-revising).
    """
    existing = _load_existing_finalized(cfg.run_dir)
    if existing is not None:
        state.finalized = True
        return existing

    if state.finalized:
        # In-memory finalized but files missing — re-read best-effort.
        existing = _load_existing_finalized(cfg.run_dir)
        if existing is not None:
            return existing

    artifacts = build_cut_artifacts(cfg, state)
    write_cut_artifacts(cfg.run_dir, artifacts, cfg)
    state.finalized = True
    artifacts["already_finalized"] = False
    return artifacts


# ---------------------------------------------------------------------------
# Offline dry-run synthetic timelines
# ---------------------------------------------------------------------------


@dataclass
class SyntheticScenario:
    name: str
    intended_text: str
    pad_words: int = DEFAULT_PAD_WORDS
    emitted_words_at_pause: int = 6
    b_pos_at_pause: int = 4
    # If drain_complete: B reaches emitted by user commit.
    drain_complete: bool = True
    b_pos_at_user_commit: int | None = None
    drained_asr_override: str | None = None
    playback_pos_words: int | None = None
    user_transcript: str = "wait stop talking please"
    pause_at_ms: float = 800.0
    user_stop_at_ms: float = 2200.0
    # If True, omit all B drain evidence (fail-closed fallback path).
    omit_b_stream: bool = False


def run_synthetic_timeline(
    scenario: SyntheticScenario,
    run_dir: Path,
    *,
    label: str = LABEL_DEFAULT,
) -> dict[str, Any]:
    """Write a synthetic barge → drain → user_commit timeline and finalize cut.

    No daemons. Proves primary drain and fallback scenarios offline.
    """
    intended = normalize_whitespace(scenario.intended_text)
    words = tokenize_words(intended)
    if not words:
        raise ValueError("intended_text is empty")

    run_dir.mkdir(parents=True, exist_ok=True)
    events_path = run_dir / "events.jsonl"
    if events_path.exists():
        events_path.unlink()

    # Clear prior commit so re-runs are clean.
    for name in ("commit.json", "metrics.json", "production_cut_text", "cut_decision.json"):
        p = run_dir / name
        if p.exists():
            p.unlink()

    total = len(words)
    emitted = max(0, min(total, int(scenario.emitted_words_at_pause)))
    b_at_pause = max(0, min(emitted, int(scenario.b_pos_at_pause)))

    if scenario.omit_b_stream:
        b_at_commit = 0
        drained_text = ""
        drain_complete = False
    elif scenario.drain_complete:
        b_at_commit = emitted
        drained_text = (
            normalize_whitespace(scenario.drained_asr_override)
            if scenario.drained_asr_override is not None
            else " ".join(words[:b_at_commit])
        )
        drain_complete = emitted > 0
    else:
        if scenario.b_pos_at_user_commit is not None:
            b_at_commit = max(0, min(emitted, int(scenario.b_pos_at_user_commit)))
        else:
            b_at_commit = b_at_pause  # stuck
        drained_text = (
            normalize_whitespace(scenario.drained_asr_override)
            if scenario.drained_asr_override is not None
            else " ".join(words[:b_at_commit])
        )
        drain_complete = False

    playback_pos = (
        scenario.playback_pos_words
        if scenario.playback_pos_words is not None
        else max(0, emitted - 1)  # slight playback lag vs synthesis emit
    )

    _write_text(run_dir / "assistant_intended.txt", intended)

    # --- timeline events (as if written by audio/UI tracks) -----------------
    _append_event(
        events_path,
        {
            "event": "sts_peek_sim_started",
            "mode": "dry-run",
            "scenario": scenario.name,
            "topology": {
                "nemotron_a": "user_mic (simulated)",
                "nemotron_b": "assistant.self_asr (simulated)",
                "shared_worker": False,
            },
            "intended_word_count": total,
            "pad_words": scenario.pad_words,
        },
    )

    # Barge / pause
    _append_event(
        events_path,
        {
            "event": "user_first_alphanumeric_token",
            "stream": "nemotron_a_user",
            "token": "wait",
            "action": "pause_playback",
            "pause_ms": scenario.pause_at_ms,
            "emitted_words_at_pause": emitted,
            "b_pos_at_pause": b_at_pause,
            "playback_pos_words": playback_pos,
        },
    )
    _append_event(
        events_path,
        {
            "event": "speech_out_barge_in",
            "trigger": "transcript_token_committed",
            "action": "pause_and_cancel_further_synthesis",
            "pause_ms": scenario.pause_at_ms,
            "emitted_pcm_words": emitted,
        },
    )

    if not scenario.omit_b_stream:
        drain_window_ms = max(0.0, scenario.user_stop_at_ms - scenario.pause_at_ms)
        _append_event(
            events_path,
            {
                "event": "assistant_self_asr_drain_started",
                "stream": "nemotron_b_assistant",
                "stream_id": "assistant.self_asr",
                "drain_only_already_emitted": True,
                "emitted_words": emitted,
                "b_pos_at_pause": b_at_pause,
                "drain_window_ms": drain_window_ms,
            },
        )
        _append_event(
            events_path,
            {
                "event": "assistant_self_asr_drain_progress",
                "stream": "nemotron_b_assistant",
                "b_pos_words": b_at_commit,
                "target_emitted_words": emitted,
                "drain_complete": drain_complete and b_at_commit >= emitted,
                "drained_text": drained_text,
                "last_aligned_pos_words": b_at_commit,
            },
        )
        # Also drop control/drain.json for follow-mode compatibility tests.
        control = run_dir / "control"
        control.mkdir(parents=True, exist_ok=True)
        _write_json(
            control / "drain.json",
            {
                "drained_asr_text": drained_text,
                "drain_complete": drain_complete and b_at_commit >= emitted,
                "b_pos_words": b_at_commit,
                "emitted_words_at_pause": emitted,
                "last_aligned_pos_words": b_at_commit,
            },
        )
        _write_text(run_dir / "drained_asr_text.txt", drained_text)

    # User commit
    _append_event(
        events_path,
        {
            "event": "user_transcript_committed",
            "stream": "nemotron_a_user",
            "t0": "user_stop",
            "user_stop_ms": scenario.user_stop_at_ms,
            "user_transcript": scenario.user_transcript,
            "action": "finalize_truncated_assistant",
            "b_pos_at_commit": b_at_commit,
        },
    )
    control = run_dir / "control"
    control.mkdir(parents=True, exist_ok=True)
    _write_json(
        control / "user_commit.json",
        {
            "user_transcript": scenario.user_transcript,
            "user_stop_ms": scenario.user_stop_at_ms,
        },
    )
    _write_json(
        control / "barge.json",
        {
            "pause_ms": scenario.pause_at_ms,
            "emitted_words_at_pause": emitted,
            "b_pos_at_pause": b_at_pause,
            "playback_pos_words": playback_pos,
        },
    )

    cfg = CutCoordConfig(
        run_dir=run_dir,
        intended_text=intended,
        pad_words=scenario.pad_words,
        label=label,
        utterance_id=f"sts-peek-sim-{scenario.name}",
    )
    state = CoordState()
    ingest_events_jsonl(state, events_path)
    ingest_control_files(state, run_dir)

    if not state.user_committed:
        raise RuntimeError("synthetic timeline failed to set user_committed")

    result = finalize(cfg, state)
    result["scenario"] = scenario.name
    result["out_dir"] = str(run_dir)
    return result


def scenario_primary_drain(
    intended_text: str | None = None,
    pad_words: int = DEFAULT_PAD_WORDS,
) -> SyntheticScenario:
    text = intended_text or (
        "one two three four five six seven eight nine ten"
    )
    return SyntheticScenario(
        name="primary-drain",
        intended_text=text,
        pad_words=pad_words,
        emitted_words_at_pause=6,
        b_pos_at_pause=4,
        drain_complete=True,
        playback_pos_words=5,
    )


def scenario_fallback_incomplete(
    intended_text: str | None = None,
    pad_words: int = DEFAULT_PAD_WORDS,
) -> SyntheticScenario:
    text = intended_text or (
        "one two three four five six seven eight nine ten"
    )
    return SyntheticScenario(
        name="fallback-incomplete",
        intended_text=text,
        pad_words=pad_words,
        emitted_words_at_pause=7,
        b_pos_at_pause=2,
        drain_complete=False,
        b_pos_at_user_commit=2,  # stuck at pause pos
        playback_pos_words=6,
    )


def scenario_fallback_b_missing(
    intended_text: str | None = None,
    pad_words: int = DEFAULT_PAD_WORDS,
) -> SyntheticScenario:
    text = intended_text or (
        "one two three four five six seven eight nine ten"
    )
    return SyntheticScenario(
        name="fallback-b-missing",
        intended_text=text,
        pad_words=pad_words,
        emitted_words_at_pause=5,
        b_pos_at_pause=0,
        drain_complete=False,
        omit_b_stream=True,
        playback_pos_words=4,
    )


# ---------------------------------------------------------------------------
# Follow mode: poll run_dir until user commit, then finalize
# ---------------------------------------------------------------------------


class FollowTimeoutError(TimeoutError):
    """Raised when follow mode does not see user commit in time."""


def follow_run_dir(cfg: CutCoordConfig) -> dict[str, Any]:
    """Poll run_dir events/control until user_transcript_committed, then cut.

    Tolerates missing files while waiting. On timeout without user commit,
    raises FollowTimeoutError. If user commits but B stream never appeared,
    still finalizes with fallback and fail_closed_b_stream_missing=true.
    """
    cfg.run_dir.mkdir(parents=True, exist_ok=True)
    events_path = cfg.run_dir / "events.jsonl"
    state = CoordState()
    deadline = time.monotonic() + max(0.1, float(cfg.wait_timeout_s))

    _append_event(
        events_path,
        {
            "event": "sts_peek_cut_coord_follow_started",
            "wait_timeout_s": cfg.wait_timeout_s,
            "run_dir": str(cfg.run_dir),
        },
        from_coord=True,
    )

    while time.monotonic() < deadline:
        ingest_events_jsonl(state, events_path)
        ingest_control_files(state, cfg.run_dir)

        if state.user_committed:
            # Prefer to have barged; if not, still finalize (user commit is T0).
            if not state.barged:
                state.barged = True
            return finalize(cfg, state)

        time.sleep(cfg.poll_interval_s)

    # Final poll after deadline.
    ingest_events_jsonl(state, events_path)
    ingest_control_files(state, cfg.run_dir)
    if state.user_committed:
        return finalize(cfg, state)

    _append_event(
        events_path,
        {
            "event": "sts_peek_cut_coord_follow_timeout",
            "barged": state.barged,
            "user_committed": state.user_committed,
            "seen_b_stream": state.drain.seen_b_stream,
            "wait_timeout_s": cfg.wait_timeout_s,
        },
        from_coord=True,
    )
    raise FollowTimeoutError(
        f"follow timeout after {cfg.wait_timeout_s}s waiting for "
        f"user_transcript_committed in {cfg.run_dir} "
        f"(barged={state.barged}, seen_b_stream={state.drain.seen_b_stream}). "
        "Expected events.jsonl lines or control/user_commit.json from audio/UI tracks."
    )


def self_check() -> None:
    """In-process unit assertions (no disk required beyond /tmp)."""
    import tempfile

    # force-align happy path
    from cut import force_align_drained_to_intended, apply_fallback_cut

    intended = "one two three four five six seven eight nine ten"
    aligned, n, conf = force_align_drained_to_intended("one two three", intended)
    assert aligned == "one two three" and n == 3 and conf > 0, (aligned, n, conf)

    fb = apply_fallback_cut(intended, last_pos_words=2, pad_words=2)
    assert fb == "one two three four", fb

    cut, src, _ = apply_production_cut(
        intended,
        drained_asr="one two three four five six",
        drain_complete=True,
        last_aligned_pos_words=4,
        pad_words=2,
    )
    assert src == "drain", src
    assert cut == "one two three four five six", cut

    cut2, src2, _ = apply_production_cut(
        intended,
        drained_asr="one two",
        drain_complete=False,
        last_aligned_pos_words=2,
        pad_words=2,
    )
    assert src2 == "fallback", src2
    assert cut2 == "one two three four", cut2

    with tempfile.TemporaryDirectory(prefix="sts-peek-cut-selfcheck-") as td:
        root = Path(td)
        r1 = run_synthetic_timeline(scenario_primary_drain(), root / "primary")
        assert r1["primary_cut_source"] == "drain", r1
        assert r1["commit"]["immutable"] is True
        assert r1["commit"]["late_self_asr_revises"] is False

        r2 = run_synthetic_timeline(
            scenario_fallback_incomplete(), root / "fallback"
        )
        assert r2["primary_cut_source"] == "fallback", r2
        assert r2["production_cut_text"] == "one two three four", r2

    print("self-check: PASS")


# Re-export apply_production_cut for callers importing cut_coord only.
__all__ = [
    "CutCoordConfig",
    "CoordState",
    "DrainState",
    "FollowTimeoutError",
    "SyntheticScenario",
    "apply_event",
    "build_cut_artifacts",
    "finalize",
    "follow_run_dir",
    "ingest_control_files",
    "ingest_events_jsonl",
    "load_intended_text",
    "run_synthetic_timeline",
    "scenario_fallback_b_missing",
    "scenario_fallback_incomplete",
    "scenario_primary_drain",
    "self_check",
    "write_cut_artifacts",
]
