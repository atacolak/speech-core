#!/usr/bin/env python3
"""Founder cut rule helpers for sts-peek cut coordinator (Track C).

Contract rev 3/4 production cut:
  PRIMARY  — drained assistant ASR force-aligned to intended LLM text.
  FALLBACK — intended prefix at last known alignment pos + pad_words (~2).

Never invents non-intended words. Late self-ASR must not revise a commit.
This module is harness / live_peek only — no protocol schema changes.

Logic is adapted from scripts/assistant_self_asr_cut.py and
scripts/barge-in-dual-asr/cut.py (those files are frozen; do not edit them).
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Literal, Sequence

DEFAULT_PAD_WORDS = 2
CutSource = Literal["drain", "fallback"]

_WS_RE = re.compile(r"\s+")
_ALNUM_RE = re.compile(r"[A-Za-z0-9]")
_PUNCT_RE = re.compile(r"^[^A-Za-z0-9]+|[^A-Za-z0-9]+$")


def normalize_whitespace(text: str) -> str:
    return _WS_RE.sub(" ", (text or "").strip())


def tokenize_words(text: str) -> list[str]:
    normalized = normalize_whitespace(text)
    if not normalized:
        return []
    return normalized.split(" ")


def is_alphanumeric_token(token: str) -> bool:
    """Mirror live-session speech-evidence: first alnum token pauses playback."""
    return bool(_ALNUM_RE.search(token or ""))


def _norm_token(token: str) -> str:
    """Lowercase + strip edge punctuation for force-align compare."""
    t = (token or "").strip().casefold()
    t = _PUNCT_RE.sub("", t)
    return t


def intended_prefix_at_word_index(intended_text: str, word_index: int) -> str:
    """Return intended text made of the first `word_index` words (clamped)."""
    words = tokenize_words(intended_text)
    if word_index <= 0:
        return ""
    if word_index >= len(words):
        return normalize_whitespace(intended_text)
    return " ".join(words[:word_index])


def apply_fallback_cut(
    intended_text: str,
    last_pos_words: int,
    pad_words: int = DEFAULT_PAD_WORDS,
) -> str:
    """FALLBACK cut: intended prefix at last known pos + pad_words (clamped).

    Never invents words outside the normalized intended stream.
    """
    if pad_words < 0:
        raise ValueError("pad_words must be >= 0")
    words = tokenize_words(intended_text)
    if not words:
        return ""
    base = max(0, int(last_pos_words))
    cut_index = min(len(words), base + int(pad_words))
    return " ".join(words[:cut_index])


# Alias matching assistant_self_asr_cut naming for callers that expect it.
apply_founder_cut = apply_fallback_cut


def force_align_drained_to_intended(
    drained_asr: str,
    intended_text: str,
    *,
    min_match_words: int = 1,
) -> tuple[str, int, float]:
    """Force-align drained assistant ASR onto a prefix of intended LLM text.

    Returns (aligned_intended_prefix, word_count, confidence).
    Only intended words are emitted — never free-form ASR invention.

    Alignment strategy (greedy sequential, adapted from barge-in-dual-asr):
      - casefold + strip edge punctuation for compare
      - small head-skip on drained stream for ASR preamble noise
      - one-token ASR insertion skip
      - hard stop on mismatch so result is always a true intended prefix
    """
    intended_words = tokenize_words(intended_text)
    drained_words = tokenize_words(drained_asr)
    if not intended_words or not drained_words:
        return "", 0, 0.0

    intended_norm = [_norm_token(w) for w in intended_words]
    drained_norm = [_norm_token(w) for w in drained_words]

    best_match = 0
    max_head_skip = min(3, len(drained_norm))
    for head in range(0, max_head_skip + 1):
        i = 0
        j = head
        matched = 0
        while i < len(intended_norm) and j < len(drained_norm):
            if not drained_norm[j]:
                j += 1
                continue
            if intended_norm[i] == drained_norm[j]:
                matched += 1
                i += 1
                j += 1
                continue
            # One-token ASR insertion: skip drained token once.
            if (
                j + 1 < len(drained_norm)
                and intended_norm[i] == drained_norm[j + 1]
            ):
                j += 1
                continue
            # Hard stop: alignment must remain a true prefix of intended.
            break
        if matched > best_match:
            best_match = matched

    if best_match < min_match_words:
        return "", 0, 0.0

    prefix = " ".join(intended_words[:best_match])
    conf = best_match / max(1, len(drained_norm))
    conf = max(0.0, min(1.0, conf))
    return prefix, best_match, conf


def apply_production_cut(
    intended_text: str,
    *,
    drained_asr: str | None = None,
    drain_complete: bool = False,
    last_aligned_pos_words: int = 0,
    pad_words: int = DEFAULT_PAD_WORDS,
    min_align_words: int = 1,
    min_align_confidence: float = 0.25,
) -> tuple[str, CutSource, dict[str, Any]]:
    """Select production cut: drain-primary, pad fallback.

    PRIMARY when ``drain_complete`` and drained ASR aligns to a non-empty
    intended prefix with sufficient confidence (or empty intended / empty
    completed drain). FALLBACK otherwise:
    ``intended_prefix(last_aligned_pos) + pad_words``.

    Returns ``(production_cut_text, primary_cut_source, detail)``.
    """
    if pad_words < 0:
        raise ValueError("pad_words must be >= 0")

    intended = normalize_whitespace(intended_text)
    intended_words = tokenize_words(intended)
    drained = normalize_whitespace(drained_asr or "")
    fallback = apply_fallback_cut(
        intended, last_aligned_pos_words, pad_words=pad_words
    )

    aligned = ""
    n_match = 0
    conf = 0.0
    if drained:
        aligned, n_match, conf = force_align_drained_to_intended(
            drained, intended, min_match_words=min_align_words
        )

    use_drain = False
    if drain_complete and drained_asr is not None:
        if not intended_words:
            # Empty intended with completed drain → empty cut, source drain.
            use_drain = True
            aligned = ""
            n_match = 0
            conf = 1.0
        elif normalize_whitespace(drained_asr) == "" and n_match == 0:
            # Completed empty drain of empty emit path.
            use_drain = True
            aligned = ""
        elif (
            bool(aligned)
            and n_match >= min_align_words
            and conf >= min_align_confidence
        ):
            use_drain = True

    if use_drain:
        cut = aligned
        source: CutSource = "drain"
    elif intended_words:
        cut = fallback
        source = "fallback"
    else:
        cut = ""
        source = "fallback"

    detail = {
        "aligned_prefix": aligned,
        "aligned_word_count": n_match,
        "align_confidence": conf,
        "fallback_text": fallback,
        "last_aligned_pos_words": int(last_aligned_pos_words),
        "drain_complete": bool(drain_complete),
        "drained_asr_text": drained,
    }
    return cut, source, detail


def is_prefix_of_intended(cut_text: str, intended_text: str) -> bool:
    cut_words = tokenize_words(cut_text)
    intended_words = tokenize_words(intended_text)
    if len(cut_words) > len(intended_words):
        return False
    return cut_words == intended_words[: len(cut_words)]


def word_set_diff_count(left: Sequence[str], right: Sequence[str]) -> int:
    """Count ordered extras in left after shared prefix (overspeak/underspeak)."""
    i = 0
    while i < len(left) and i < len(right) and left[i] == right[i]:
        i += 1
    return len(left) - i


@dataclass(frozen=True)
class ThreeWayCut:
    """Three-way eval alignment sample (contract metrics table)."""

    intended_at_playback: str
    asr_recovered_at_stop: str
    production_cut: str
    pad_words: int
    nemotron_pos_words: int
    playback_pos_words: int
    prefix_valid: bool
    overspeak_words: int
    underspeak_words: int
    primary_cut_source: CutSource = "fallback"
    label: str = "live_peek"

    def to_metrics_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "pad_words": self.pad_words,
            "primary_cut_source": self.primary_cut_source,
            "nemotron_pos_words": self.nemotron_pos_words,
            "playback_pos_words": self.playback_pos_words,
            "intended_at_playback": self.intended_at_playback,
            "asr_recovered_at_stop": self.asr_recovered_at_stop,
            "production_cut_text": self.production_cut,
            "prefix_valid": self.prefix_valid,
            "overspeak_words": self.overspeak_words,
            "underspeak_words": self.underspeak_words,
        }


def score_three_way(
    intended_text: str,
    *,
    playback_pos_words: int,
    nemotron_pos_words: int,
    asr_recovered_at_stop: str,
    pad_words: int = DEFAULT_PAD_WORDS,
    drain_complete: bool = False,
    drained_asr: str | None = None,
    last_aligned_pos_words: int | None = None,
    label: str = "live_peek",
    min_align_words: int = 1,
    min_align_confidence: float = 0.25,
) -> ThreeWayCut:
    """Compute production cut (drain-primary) and three-way metrics."""
    intended_at_playback = intended_prefix_at_word_index(
        intended_text, playback_pos_words
    )
    drain_text = asr_recovered_at_stop if drained_asr is None else drained_asr
    last_pos = (
        nemotron_pos_words
        if last_aligned_pos_words is None
        else last_aligned_pos_words
    )
    production_cut, cut_source, _detail = apply_production_cut(
        intended_text,
        drained_asr=drain_text,
        drain_complete=drain_complete,
        last_aligned_pos_words=last_pos,
        pad_words=pad_words,
        min_align_words=min_align_words,
        min_align_confidence=min_align_confidence,
    )
    prefix_valid = is_prefix_of_intended(production_cut, intended_text)

    cut_words = tokenize_words(production_cut)
    play_words = tokenize_words(intended_at_playback)
    overspeak = word_set_diff_count(cut_words, play_words)
    underspeak = word_set_diff_count(play_words, cut_words)

    return ThreeWayCut(
        intended_at_playback=intended_at_playback,
        asr_recovered_at_stop=normalize_whitespace(asr_recovered_at_stop),
        production_cut=production_cut,
        pad_words=pad_words,
        nemotron_pos_words=int(nemotron_pos_words),
        playback_pos_words=int(playback_pos_words),
        prefix_valid=prefix_valid,
        overspeak_words=overspeak,
        underspeak_words=underspeak,
        primary_cut_source=cut_source,
        label=label,
    )


def commit_truncated_assistant_message(
    production_cut: str,
    *,
    utterance_id: str | None = None,
    primary_cut_source: CutSource | None = None,
    user_transcript: str | None = None,
    pad_words: int | None = None,
    drain_complete: bool | None = None,
    label: str = "live_peek",
) -> dict[str, Any]:
    """Immutable commit of truncated assistant message concept.

    Does not emit protocol events. Late self-ASR must not revise this payload.
    """
    payload: dict[str, Any] = {
        "event": "assistant_turn_truncated_eval_only",
        "label": label,
        "immutable": True,
        "late_self_asr_revises": False,
        "utterance_id": utterance_id,
        "production_cut_text": production_cut,
        "note": (
            "sts-peek cut coordinator commit. Not a speech-core-protocol event. "
            "Commit once; late self-ASR must not revise."
        ),
    }
    if primary_cut_source is not None:
        payload["primary_cut_source"] = primary_cut_source
    if user_transcript is not None:
        payload["user_transcript"] = user_transcript
    if pad_words is not None:
        payload["pad_words"] = int(pad_words)
    if drain_complete is not None:
        payload["drain_complete"] = bool(drain_complete)
    return payload


def three_way_asdict(cut: ThreeWayCut) -> dict[str, Any]:
    return asdict(cut)
