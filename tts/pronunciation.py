"""not on the voicecat path.

Backend-independent pronunciation representation.
Breeze has no documented IPA/lexicon input; the adapter may compile
overrides into synthesis_text while preserving canonical display text.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tts.hashes import sha256_json
from tts.packets import PronunciationOverride, UtterancePacket
from tts.paths import ensure_lab_dirs


def compile_synthesis_text(
    text: str,
    overrides: list[PronunciationOverride],
    *,
    strategy: str = "respell",
) -> str:
    """Compile overrides into backend text. Display text stays untouched."""
    if strategy == "ordinary":
        return text
    if strategy == "instruction":
        return text
    compiled = text
    for item in overrides:
        if not item.span:
            continue
        replacement = (item.intended_reading or item.span).strip()
        if not replacement:
            continue
        if item.context and item.span in item.context:
            compiled = compiled.replace(item.context, item.context.replace(item.span, replacement, 1), 1)
        else:
            compiled = re.sub(rf"\b{re.escape(item.span)}\b", replacement, compiled, count=1)
    return compiled


def instruction_hint(overrides: list[PronunciationOverride]) -> str:
    parts = []
    for item in overrides:
        reading = item.intended_reading or item.ipa
        if not reading:
            continue
        ctx = f' in "{item.context}"' if item.context else ""
        parts.append(f'pronounce "{item.span}"{ctx} as {reading}')
    return "; ".join(parts)


@dataclass
class LexiconEntry:
    override: PronunciationOverride
    cache_key: str

    def to_dict(self) -> dict[str, Any]:
        return {"cache_key": self.cache_key, **self.override.to_dict()}


class PronunciationResolver:
    def __init__(self, root: Path | None = None) -> None:
        self.root = ensure_lab_dirs(root)
        self.path = self.root / "lexicon" / "lexicon.json"
        self._entries: dict[str, PronunciationOverride] = {}
        self.load()

    @staticmethod
    def key_for(span: str, context: str | None) -> str:
        return sha256_json({"span": span.lower(), "context": (context or "").lower()})

    def load(self) -> None:
        if not self.path.is_file():
            self._entries = {}
            return
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self._entries = {}
        for item in payload.get("entries") or []:
            override = PronunciationOverride.from_dict(item)
            key = item.get("cache_key") or self.key_for(override.span, override.context)
            self._entries[key] = override

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "entries": [
                {"cache_key": key, **override.to_dict()}
                for key, override in sorted(self._entries.items())
            ]
        }
        self.path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def remember(self, override: PronunciationOverride) -> str:
        key = self.key_for(override.span, override.context)
        self._entries[key] = override
        self.save()
        return key

    def lookup(self, span: str, context: str | None = None) -> PronunciationOverride | None:
        return self._entries.get(self.key_for(span, context)) or self._entries.get(
            self.key_for(span, None)
        )

    def apply(self, packet: UtterancePacket, *, strategy: str = "respell") -> UtterancePacket:
        overrides = list(packet.pronunciation_overrides)
        merged: list[PronunciationOverride] = []
        seen: set[str] = set()
        for item in overrides:
            key = self.key_for(item.span, item.context)
            remembered = self._entries.get(key) or item
            merged.append(remembered)
            seen.add(key)
            if item.source in {"operator", "lexicon"}:
                self.remember(remembered)
        packet.pronunciation_overrides = merged
        packet.synthesis_text = compile_synthesis_text(
            packet.text, merged, strategy=strategy
        )
        hint = instruction_hint(merged)
        if strategy == "instruction" and hint:
            packet.steer = (packet.steer.rstrip() + " " + hint).strip()
        return packet
