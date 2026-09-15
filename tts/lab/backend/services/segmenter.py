from __future__ import annotations

import re

SEGMENT_MIN_CHARS = 120
SEGMENT_MAX_CHARS = 420
SENTENCE_TERMINATORS = ".!?。！？"
_AFTER_ASCII = "\"')]}"
_PARAGRAPH = re.compile(r"\n\s*\n+")


def _is_abbreviation_dot(text: str, index: int) -> bool:
    """True for the dot of `e.g.` / `i.e.` / `U.S.`: it follows a lone letter."""
    previous = text[index - 1] if index else ""
    if not previous.isalpha():
        return False
    return index < 2 or not text[index - 2].isalpha()


def _sentence_end(text: str, start: int, stop: int) -> int | None:
    for index in range(start, stop):
        char = text[index]
        if char not in SENTENCE_TERMINATORS:
            continue
        if char in "。！？":
            return index + 1
        if char == "." and _is_abbreviation_dot(text, index):
            continue
        next_char = text[index + 1] if index + 1 < len(text) else ""
        if not next_char or next_char.isspace() or next_char.isupper() or next_char in _AFTER_ASCII:
            return index + 1
    return None


def segment_text(
    text: str,
    *,
    min_chars: int = SEGMENT_MIN_CHARS,
    max_chars: int = SEGMENT_MAX_CHARS,
) -> list[str]:
    if min_chars < 1 or max_chars <= min_chars:
        raise ValueError("segment bounds require 1 <= min_chars < max_chars")
    remaining = text.strip()
    parts: list[str] = []
    while remaining:
        if len(remaining) <= max_chars:
            parts.append(remaining)
            break
        window = remaining[:max_chars]
        paragraphs = [match.end() for match in _PARAGRAPH.finditer(window)]
        cut = next((end for end in reversed(paragraphs) if end >= min_chars), None)
        if cut is None:
            cut = _sentence_end(remaining, min_chars, max_chars)
        if cut is None:
            # No sentence boundary inside the window: keep the sentence whole
            # instead of slicing it at whitespace, then fall back.
            cut = _sentence_end(remaining, max_chars, len(remaining))
        if cut is None:
            spaces = [index for index, char in enumerate(window) if char.isspace()]
            cut = spaces[-1] if spaces else max_chars
        part = remaining[:cut].strip()
        if part:
            parts.append(part)
        remaining = remaining[cut:].strip()
    return parts
