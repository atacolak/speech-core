#!/usr/bin/env python3
"""Founder cut rule helpers for assistant-self-asr eval_only harness.

Production cut rule (contract rev 2 / checkpoint-006):
  1. Pause playback on first user alphanumeric token (existing live-session behavior).
  2. Finalize truncated assistant message at user stop / transcript_committed (T0).
  3. Base = Nemotron alignment position through the intended assistant stream at T0.
  4. Pad = +pad_words (default 2) from intended LLM text (not free-form ASR).
  5. Commit once; late self-ASR is diagnostic only and must not revise the commit.

All outputs of this module are labeled eval_only. No protocol/daemon schema changes.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Sequence

DEFAULT_PAD_WORDS = 2

# Tokenization for cut math: whitespace-split after light normalization.
# Keep punctuation attached so "hello," stays one word unit; prefix checks use
# the same normalization on both sides so metrics stay comparable.
_WS_RE = re.compile(r"\s+")
_ALNUM_RE = re.compile(r"[A-Za-z0-9]")


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


def intended_prefix_at_word_index(intended_text: str, word_index: int) -> str:
    """Return intended text made of the first `word_index` words (clamped)."""
    words = tokenize_words(intended_text)
    if word_index <= 0:
        return ""
    if word_index >= len(words):
        return normalize_whitespace(intended_text)
    return " ".join(words[:word_index])


def apply_founder_cut(
    intended_text: str,
    nemotron_pos_words: int,
    pad_words: int = DEFAULT_PAD_WORDS,
) -> str:
    """Production cut estimate: intended prefix at Nemotron pos + pad_words.

    Never invents words outside the normalized intended stream. Pad is applied
    within intended text only (clamped at end).
    """
    if pad_words < 0:
        raise ValueError("pad_words must be >= 0")
    words = tokenize_words(intended_text)
    if not words:
        return ""
    base = max(0, int(nemotron_pos_words))
    cut_index = min(len(words), base + int(pad_words))
    return " ".join(words[:cut_index])


def is_prefix_of_intended(cut_text: str, intended_text: str) -> bool:
    cut_words = tokenize_words(cut_text)
    intended_words = tokenize_words(intended_text)
    if len(cut_words) > len(intended_words):
        return False
    return cut_words == intended_words[: len(cut_words)]


def word_set_diff_count(left: Sequence[str], right: Sequence[str]) -> int:
    """Count words present in left (as multiset prefix alignment) missing from right.

    Uses ordered longest common prefix style: walk both lists; count extras in
    left after shared prefix, plus remaining left words. This matches the
    contract preference for overspeak/underspeak vs intended_at_playback.
    """
    i = 0
    while i < len(left) and i < len(right) and left[i] == right[i]:
        i += 1
    # After shared prefix, remaining left words are "extra" relative to right.
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
    label: str = "eval_only"

    def to_metrics_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "pad_words": self.pad_words,
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
) -> ThreeWayCut:
    """Compute production cut and three-way metrics vs playback/ASR."""
    intended_at_playback = intended_prefix_at_word_index(
        intended_text, playback_pos_words
    )
    production_cut = apply_founder_cut(
        intended_text, nemotron_pos_words, pad_words=pad_words
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
    )


def mock_alignment_clock(
    intended_text: str,
    *,
    words_per_second: float = 3.0,
    elapsed_ms: float,
) -> int:
    """Stub Nemotron/playback alignment clock for offline dry-run.

    TODO(live): replace with real assistant-tracking Nemotron position at
    transcript_committed, and with instrumented play samples/chunks for
    playback_pos_words. This mock is eval_only and intentionally linear.
    """
    if words_per_second <= 0:
        raise ValueError("words_per_second must be > 0")
    words = tokenize_words(intended_text)
    if not words or elapsed_ms <= 0:
        return 0
    pos = int((elapsed_ms / 1000.0) * words_per_second)
    return min(len(words), max(0, pos))


def commit_truncated_assistant_message(
    production_cut: str,
    *,
    utterance_id: str | None = None,
) -> dict[str, Any]:
    """Log-only commit of truncated assistant message concept (eval_only).

    Does not emit protocol events. Late self-ASR must not revise this payload.
    """
    return {
        "event": "assistant_turn_truncated_eval_only",
        "label": "eval_only",
        "immutable": True,
        "late_self_asr_revises": False,
        "utterance_id": utterance_id,
        "production_cut_text": production_cut,
        "note": (
            "Concept commit for harness calibration only. "
            "Not a speech-core-protocol event."
        ),
    }


def three_way_asdict(cut: ThreeWayCut) -> dict[str, Any]:
    return asdict(cut)
