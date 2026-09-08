"""not on the voicecat path.

Pronunciation test harness for Breeze. Compare strategies; do not assume
bracketed phonemes work because some other TTS accepted them.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from tts.packets import PronunciationOverride, UtterancePacket
from tts.paths import TTS_ROOT
from tts.pronunciation import compile_synthesis_text, instruction_hint

STRATEGIES = ("ordinary", "respell", "instruction", "punctuation")


def load_cases() -> list[dict[str, Any]]:
    path = TTS_ROOT / "experiments" / "fixtures" / "pronunciation.json"
    return json.loads(path.read_text(encoding="utf-8"))


def apply_strategy(text: str, overrides: list[PronunciationOverride], strategy: str) -> dict[str, str]:
    if strategy not in STRATEGIES:
        raise ValueError(strategy)
    if strategy == "ordinary":
        return {"synthesis_text": text, "steer_extra": ""}
    if strategy == "punctuation":
        compiled = text
        for item in overrides:
            compiled = compiled.replace(item.span, f", {item.span},", 1)
        return {"synthesis_text": compiled, "steer_extra": ""}
    if strategy == "instruction":
        return {"synthesis_text": text, "steer_extra": instruction_hint(overrides)}
    return {
        "synthesis_text": compile_synthesis_text(text, overrides, strategy="respell"),
        "steer_extra": "",
    }


def plan_case(case: dict[str, Any]) -> dict[str, Any]:
    overrides = [PronunciationOverride.from_dict(item) for item in case.get("overrides") or []]
    arms = {}
    for strategy in STRATEGIES:
        arms[strategy] = apply_strategy(case["text"], overrides, strategy)
    return {
        "id": case["id"],
        "text": case["text"],
        "overrides": [item.to_dict() for item in overrides],
        "arms": arms,
        "native_ipa_advertised": False,
        "note": (
            "Breeze documented interface does not advertise phoneme/IPA/lexicon "
            "input. Winning mechanism is empirical; default adapter compiles "
            "respell into synthesis_text and keeps display text canonical."
        ),
    }


def run_harness(
    synthesize: Callable[[UtterancePacket, str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    rows = []
    for case in load_cases():
        planned = plan_case(case)
        if synthesize is not None:
            for strategy, arm in planned["arms"].items():
                packet = UtterancePacket(
                    text=case["text"],
                    synthesis_text=arm["synthesis_text"],
                    steer=arm["steer_extra"] or "Speak clearly.",
                )
                planned["arms"][strategy]["result"] = synthesize(packet, strategy)
        rows.append(planned)
    return {
        "backend": "breeze",
        "native_pronunciation_control": "undocumented / not advertised",
        "default_adapter_strategy": "respell-into-synthesis_text",
        "cases": rows,
    }
