#!/usr/bin/env python3
"""Founder cut rule helpers for assistant-self-asr eval_only harness.

Production cut rule (contract rev 3/4, checkpoints 009–011):
  1. Pause playback on first user alphanumeric token (existing live-session behavior).
  2. Immediately start/continue assistant self-ASR drain on already-emitted audio
     during the user barge-in speech window (dual Nemotron: assistant stream B).
  3. At user stop / transcript_committed (T0):
       PRIMARY  — drained assistant prefix, force-aligned to intended LLM text.
       FALLBACK — intended prefix at last known alignment pos + pad_words (~2)
                  only if drain incomplete / lag / dual-stream starve.
  4. Commit once as truncated assistant message; late self-ASR must not revise.

All outputs of this module are labeled eval_only. No protocol/daemon schema changes.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Literal, Sequence

DEFAULT_PAD_WORDS = 2
CutSource = Literal["drain", "fallback"]

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
    """FALLBACK cut estimate: intended prefix at Nemotron pos + pad_words.

    Prefer :func:`apply_production_cut` which selects drain-primary vs this
    fallback. This helper remains for unit tests and explicit fallback paths.

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


def force_align_drained_to_intended(
    drained_asr: str,
    intended_text: str,
) -> tuple[str, int]:
    """Force-align drained assistant ASR onto a prefix of intended LLM text.

    Returns (aligned_intended_prefix, word_count). Only intended words are
    emitted — never free-form ASR invention (contract rev 3 error preference).

    Alignment is case-insensitive longest common word prefix after whitespace
    normalize. Divergent ASR tails are dropped rather than inventing text.
    """
    drained = tokenize_words(drained_asr)
    intended = tokenize_words(intended_text)
    n = 0
    while (
        n < len(drained)
        and n < len(intended)
        and drained[n].casefold() == intended[n].casefold()
    ):
        n += 1
    if n == 0:
        return "", 0
    return " ".join(intended[:n]), n


def apply_production_cut(
    intended_text: str,
    *,
    drained_asr: str | None = None,
    drain_complete: bool = False,
    last_aligned_pos_words: int = 0,
    pad_words: int = DEFAULT_PAD_WORDS,
) -> tuple[str, CutSource]:
    """Select production cut: drain-primary, pad fallback.

    PRIMARY when ``drain_complete`` and drained ASR aligns to a non-empty
    intended prefix (or empty intended). FALLBACK otherwise:
    ``intended_prefix(last_aligned_pos) + pad_words``.

    Returns ``(production_cut_text, primary_cut_source)`` with
    ``primary_cut_source`` in ``{"drain", "fallback"}``.
    """
    if pad_words < 0:
        raise ValueError("pad_words must be >= 0")

    intended_words = tokenize_words(intended_text)
    if not intended_words:
        return "", "drain" if drain_complete else "fallback"

    if drain_complete and drained_asr is not None:
        aligned, n = force_align_drained_to_intended(drained_asr, intended_text)
        # Empty drain that completed still counts as drain source (prefix "").
        if n > 0 or normalize_whitespace(drained_asr) == "":
            return aligned, "drain"
        # Drain claimed complete but aligned nowhere useful → fall through.

    fallback = apply_founder_cut(
        intended_text,
        nemotron_pos_words=last_aligned_pos_words,
        pad_words=pad_words,
    )
    return fallback, "fallback"


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
    primary_cut_source: CutSource = "fallback"
    label: str = "eval_only"

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
) -> ThreeWayCut:
    """Compute production cut (drain-primary) and three-way metrics.

    When ``drain_complete`` is True, production cut uses force-aligned drain
    (``drained_asr`` defaults to ``asr_recovered_at_stop``). Otherwise the
    pad fallback is used from ``last_aligned_pos_words`` (defaults to
    ``nemotron_pos_words``).
    """
    intended_at_playback = intended_prefix_at_word_index(
        intended_text, playback_pos_words
    )
    drain_text = (
        asr_recovered_at_stop if drained_asr is None else drained_asr
    )
    last_pos = (
        nemotron_pos_words
        if last_aligned_pos_words is None
        else last_aligned_pos_words
    )
    production_cut, cut_source = apply_production_cut(
        intended_text,
        drained_asr=drain_text,
        drain_complete=drain_complete,
        last_aligned_pos_words=last_pos,
        pad_words=pad_words,
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


def mock_drain_during_user_speech(
    intended_text: str,
    *,
    emitted_pos_words_at_pause: int,
    drain_words_per_second: float,
    user_speech_ms: float,
    drain_start_lag_ms: float = 0.0,
) -> tuple[str, int, bool]:
    """Simulate assistant Nemotron-B drain on already-emitted audio during user speech.

    Dual-Nemotron topology (rev 4): drain runs on a separate stream and cannot
    steal user ASR compute. Progress is limited to audio already emitted at
    pause — unplayed synthesis tail is not drained as heard.

    Returns ``(drained_text, drained_pos_words, drain_complete)`` where
    ``drain_complete`` means the drain caught up to emitted audio by user stop.
    """
    if drain_words_per_second < 0:
        raise ValueError("drain_words_per_second must be >= 0")
    words = tokenize_words(intended_text)
    emitted = max(0, min(len(words), int(emitted_pos_words_at_pause)))
    if emitted == 0 or not words:
        return "", 0, True

    effective_ms = max(0.0, float(user_speech_ms) - max(0.0, float(drain_start_lag_ms)))
    if drain_words_per_second == 0:
        drained_pos = 0
    else:
        drained_pos = int((effective_ms / 1000.0) * drain_words_per_second)
    drained_pos = max(0, min(emitted, drained_pos))
    drain_complete = drained_pos >= emitted
    drained_text = " ".join(words[:drained_pos]) if drained_pos else ""
    return drained_text, drained_pos, drain_complete


def commit_truncated_assistant_message(
    production_cut: str,
    *,
    utterance_id: str | None = None,
    primary_cut_source: CutSource | None = None,
) -> dict[str, Any]:
    """Log-only commit of truncated assistant message concept (eval_only).

    Does not emit protocol events. Late self-ASR must not revise this payload.
    """
    payload: dict[str, Any] = {
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
    if primary_cut_source is not None:
        payload["primary_cut_source"] = primary_cut_source
    return payload


def three_way_asdict(cut: ThreeWayCut) -> dict[str, Any]:
    return asdict(cut)
