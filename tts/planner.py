"""not on the voicecat path.

Experimental delivery planner. Lab may call an LLM freely.
Live speech-core must not put this synchronously in front of every
voicecat utterance without measuring latency.

Conversation-history is out of the TTS suite for now. Persona exemplars
still inform the steer.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from typing import Any

from tts.packets import DeliveryAnnotations, PronunciationOverride, UtterancePacket
from tts.paths import TTS_ROOT
from tts.profiles import DeliveryProfile

PLANNER_VERSION = "tts-planner-v1"
DEFAULT_PLANNER_MODEL = os.environ.get("TTS_PLANNER_MODEL", "unset")


def flag_pronunciation_risks(text: str) -> list[str]:
    """Identify likely risks. Do not emit backend phoneme markup."""
    risks: list[str] = []
    for token in re.findall(r"\b[A-Z]{2,}\b", text or ""):
        risks.append(f"acronym-like:{token}")
    lowered = (text or "").lower()
    if re.search(r"\bminute\b", lowered):
        risks.append("ambiguous:minute")
    if re.search(r"\b[A-Z][a-z]+(?:[A-Z][a-z]+)+\b", text or ""):
        risks.append("camelcase-or-proper-noun")
    return risks


def load_steer_fixtures() -> list[dict[str, str]]:
    path = TTS_ROOT / "experiments" / "fixtures" / "steers.json"
    return json.loads(path.read_text(encoding="utf-8"))


def build_planner_prompt(
    *,
    text: str,
    delivery: DeliveryProfile | None,
    pronunciation_risks: list[str] | None = None,
) -> str:
    exemplars = []
    if delivery is not None:
        for item in delivery.exemplars[:6]:
            exemplars.append({"text": item.text, "steer": item.steer})
    payload = {
        "planner_version": PLANNER_VERSION,
        "instruction": (
            "Emit JSON with keys text, synthesis_text, steer, delivery, "
            "pronunciation_overrides. Steer is rich natural language for THIS "
            "utterance. Do not collapse to an emotion enum. Do not use "
            "conversation history. Flag pronunciation risks; do not invent "
            "backend phoneme markup."
        ),
        "persona": None
        if delivery is None
        else {
            "id": delivery.id,
            "name": delivery.name,
            "base_description": delivery.base_description,
            "priors": delivery.priors,
            "exemplars": exemplars,
        },
        "candidate_response": text,
        "pronunciation_risks": pronunciation_risks or [],
    }
    return json.dumps(payload, indent=2)


def _heuristic_plan(text: str, delivery: DeliveryProfile | None) -> UtterancePacket:
    steer = "Speak clearly and naturally, technically confident, not theatrical."
    if delivery is not None:
        if delivery.exemplars:
            steer = delivery.exemplars[-1].steer
        elif delivery.base_description:
            steer = delivery.base_description
    return UtterancePacket(
        text=text,
        synthesis_text=text,
        steer=steer,
        delivery=DeliveryAnnotations(intent="inform", energy="controlled"),
        delivery_profile_id=None if delivery is None else delivery.id,
        provenance={
            "planner": "heuristic",
            "planner_version": PLANNER_VERSION,
            "planner_model": "none",
        },
    )


def _parse_plan(payload: dict[str, Any], fallback_text: str) -> UtterancePacket:
    overrides = [
        PronunciationOverride.from_dict(item)
        for item in payload.get("pronunciation_overrides") or []
    ]
    return UtterancePacket(
        text=str(payload.get("text") or fallback_text),
        synthesis_text=payload.get("synthesis_text"),
        steer=str(payload.get("steer") or ""),
        delivery=DeliveryAnnotations.from_dict(payload.get("delivery")),
        pronunciation_overrides=overrides,
        provenance={
            "planner": "llm",
            "planner_version": PLANNER_VERSION,
        },
    )


def plan_utterance(
    *,
    text: str,
    delivery: DeliveryProfile | None = None,
    pronunciation_risks: list[str] | None = None,
) -> UtterancePacket:
    pronunciation_risks = list(pronunciation_risks or []) + flag_pronunciation_risks(text)
    prompt = build_planner_prompt(
        text=text,
        delivery=delivery,
        pronunciation_risks=pronunciation_risks,
    )
    url = os.environ.get("TTS_PLANNER_URL", "").strip()
    if not url:
        packet = _heuristic_plan(text, delivery)
        packet.provenance["planner_prompt_hash"] = str(len(prompt))
        packet.provenance["planner_prompt"] = prompt
        packet.provenance["pronunciation_risks"] = pronunciation_risks
        return packet
    body = json.dumps(
        {
            "model": DEFAULT_PLANNER_MODEL,
            "messages": [
                {"role": "system", "content": "Return only JSON for an utterance packet."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.4,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = json.loads(resp.read().decode("utf-8"))
    content = raw["choices"][0]["message"]["content"]
    parsed = json.loads(content)
    packet = _parse_plan(parsed, text)
    packet.provenance.update(
        {
            "planner_model": DEFAULT_PLANNER_MODEL,
            "planner_url": url,
        }
    )
    return packet
