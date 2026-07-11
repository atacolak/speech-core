"""Cut / force-align helpers for dual-Nemotron barge-in.

Primary path (contract rev 3/4): drained assistant ASR force-aligned to a
prefix of the intended LLM text. Fallback: last known alignment position
+ pad_words (~2) from intended text. Never invents non-intended words.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Literal, Sequence

DEFAULT_PAD_WORDS = 2

_WS_RE = re.compile(r"\s+")
_ALNUM_RE = re.compile(r"[A-Za-z0-9]")
# Strip light punctuation for alignment compare while keeping original intended words.
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
    t = (token or "").strip().lower()
    t = _PUNCT_RE.sub("", t)
    return t


def intended_prefix_at_word_index(intended_text: str, word_index: int) -> str:
    words = tokenize_words(intended_text)
    if word_index <= 0:
        return ""
    if word_index >= len(words):
        return normalize_whitespace(intended_text)
    return " ".join(words[:word_index])


def is_prefix_of_intended(cut_text: str, intended_text: str) -> bool:
    cut_words = tokenize_words(cut_text)
    intended_words = tokenize_words(intended_text)
    if len(cut_words) > len(intended_words):
        return False
    return cut_words == intended_words[: len(cut_words)]


def apply_fallback_cut(
    intended_text: str,
    last_pos_words: int,
    pad_words: int = DEFAULT_PAD_WORDS,
) -> str:
    """Fallback: intended prefix at last known pos + pad_words (clamped).

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


def force_align_to_intended(
    drained_asr_text: str,
    intended_text: str,
    *,
    min_match_words: int = 1,
) -> tuple[str, int, float]:
    """Force-align drained ASR to a prefix of intended text.

    Strategy (greedy sequential): walk drained tokens; advance intended cursor
    when normalized tokens match. Stops at first hard mismatch after at least
    one match (allows short ASR noise tokens to be skipped at the head).

    Returns:
      (aligned_intended_prefix, matched_word_count, confidence in [0,1])
    """
    intended_words = tokenize_words(intended_text)
    drained_words = tokenize_words(drained_asr_text)
    if not intended_words or not drained_words:
        return "", 0, 0.0

    intended_norm = [_norm_token(w) for w in intended_words]
    drained_norm = [_norm_token(w) for w in drained_words]

    # Skip leading empty/non-alnum drained tokens.
    d = 0
    while d < len(drained_norm) and not drained_norm[d]:
        d += 1

    # Allow a small head-skip on drained stream to absorb ASR preamble noise.
    best_match = 0
    best_start = 0
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
            # One-token ASR deletion: skip intended token once (rare; do not
            # skip more than one consecutively without a match).
            if (
                i + 1 < len(intended_norm)
                and intended_norm[i + 1] == drained_norm[j]
            ):
                # Do not skip intended — that would invent a non-heard prefix.
                # Hard stop: alignment must be a true prefix of intended.
                break
            break
        if matched > best_match:
            best_match = matched
            best_start = head  # noqa: F841 — retained for debug symmetry

    if best_match < min_match_words:
        return "", 0, 0.0

    prefix = " ".join(intended_words[:best_match])
    conf = best_match / max(1, len(drained_norm))
    # Cap confidence by how much of drained we explained.
    conf = max(0.0, min(1.0, conf))
    return prefix, best_match, conf


CutSource = Literal["drain", "fallback", "empty"]


@dataclass(frozen=True)
class CutDecision:
    """Production cut decision for dual-Nemotron barge-in."""

    production_cut_text: str
    source: CutSource
    intended_text: str
    drained_asr_text: str
    aligned_prefix: str
    aligned_word_count: int
    align_confidence: float
    last_pos_words: int
    pad_words: int
    fallback_text: str
    drain_complete: bool
    prefix_valid: bool
    label: str = "eval_only"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["event"] = "barge_in_cut_decision"
        return d


def production_cut(
    intended_text: str,
    *,
    drained_asr_text: str = "",
    last_pos_words: int = 0,
    pad_words: int = DEFAULT_PAD_WORDS,
    drain_complete: bool = False,
    min_align_words: int = 1,
    min_align_confidence: float = 0.25,
) -> CutDecision:
    """Choose primary (drain+align) or fallback (last_pos + pad) cut.

    Rules:
      - If drain_complete and alignment yields a non-empty intended prefix with
        sufficient confidence → primary = aligned prefix.
      - Else → fallback = intended at last_pos_words + pad_words.
      - Empty intended → empty cut.
    """
    intended = normalize_whitespace(intended_text)
    drained = normalize_whitespace(drained_asr_text)
    fallback = apply_fallback_cut(intended, last_pos_words, pad_words=pad_words)

    aligned, n_match, conf = force_align_to_intended(
        drained, intended, min_match_words=min_align_words
    )

    use_drain = (
        bool(drain_complete)
        and bool(aligned)
        and n_match >= min_align_words
        and conf >= min_align_confidence
    )

    if use_drain:
        cut = aligned
        source: CutSource = "drain"
    elif intended:
        cut = fallback
        source = "fallback"
    else:
        cut = ""
        source = "empty"

    return CutDecision(
        production_cut_text=cut,
        source=source,
        intended_text=intended,
        drained_asr_text=drained,
        aligned_prefix=aligned,
        aligned_word_count=n_match,
        align_confidence=conf,
        last_pos_words=int(last_pos_words),
        pad_words=int(pad_words),
        fallback_text=fallback,
        drain_complete=bool(drain_complete),
        prefix_valid=is_prefix_of_intended(cut, intended) if cut else True,
    )


def commit_truncated_assistant_message(
    decision: CutDecision,
    *,
    utterance_id: str | None = None,
    user_transcript: str | None = None,
) -> dict[str, Any]:
    """Log-only commit of truncated assistant message (harness / eval_only).

    Does not emit protocol events. Late self-ASR must not revise this payload.
    """
    return {
        "event": "assistant_turn_truncated_eval_only",
        "label": "eval_only",
        "immutable": True,
        "late_self_asr_revises": False,
        "utterance_id": utterance_id,
        "production_cut_text": decision.production_cut_text,
        "cut_source": decision.source,
        "drain_complete": decision.drain_complete,
        "aligned_word_count": decision.aligned_word_count,
        "align_confidence": decision.align_confidence,
        "pad_words": decision.pad_words,
        "last_pos_words": decision.last_pos_words,
        "user_transcript": user_transcript,
        "note": (
            "Harness commit for dual-Nemotron barge-in. "
            "Not a speech-core-protocol event. Commit once; never revise."
        ),
    }


def word_set_diff_count(left: Sequence[str], right: Sequence[str]) -> int:
    i = 0
    while i < len(left) and i < len(right) and left[i] == right[i]:
        i += 1
    return len(left) - i
