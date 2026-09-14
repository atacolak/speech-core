"""Offline vibevoice speaker analysis + use-speaker keep. not on the voicecat path.

Diarization always reads the whole source artifact; keep intervals crop the
result afterwards. Overlap is not source separation, so overlapping slices are
marked unsafe and never reach automatic use-speaker keep.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Protocol

from tts.lab.backend.models import Interval
from tts.lab.backend.services.references import normalize_keep_intervals
from tts.lab.backend.services.resemble import ProcessorUnavailable

PROCESSOR = "vibevoice-asr"
LEASE_NAME = "vibevoice"
MODEL_ID = "Dubedo/VibeVoice-ASR-HF-NF4"
PROCESSOR_CONFIG: dict[str, Any] = {"model_id": MODEL_ID, "preprocess_version": "v1"}
VERSION = 1
DEVICE = "cuda"
_EPS = 1e-9

_START_KEYS = ("start_s", "start", "start_time")
_END_KEYS = ("end_s", "end", "end_time", "stop")


class AnalyzeFn(Protocol):
    """Source audio path in, parsed speaker analysis out."""

    def __call__(self, path: Path | str) -> dict[str, Any]: ...


def _folded(raw: dict[str, Any]) -> dict[str, Any]:
    """Case-insensitive lookup. Live NF4 decode uses Start/End/Speaker/Content."""
    return {str(key).lower(): value for key, value in raw.items()}


def _first_number(raw: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    folded = _folded(raw)
    for key in keys:
        value = folded.get(key.lower())
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _speaker_id(raw: dict[str, Any]) -> str:
    folded = _folded(raw)
    speaker = (
        folded.get("speaker_id")
        or folded.get("speaker")
        or folded.get("id")
        or folded.get("speaker_label")
    )
    if speaker is None or speaker == "":
        return "S1"
    if isinstance(speaker, bool):
        return str(speaker)
    if isinstance(speaker, (int, float)) and float(speaker).is_integer():
        return f"S{int(speaker) + 1}"
    text = str(speaker).strip()
    if text.isdigit():
        return f"S{int(text) + 1}"
    return text


def _segment(raw: Any) -> dict[str, Any] | None:
    """One vibevoice turn as {speaker_id, start_s, end_s, text, words, overlap}."""
    if not isinstance(raw, dict):
        return None
    start = _first_number(raw, _START_KEYS)
    end = _first_number(raw, _END_KEYS)
    if start is None or end is None or end - start <= _EPS:
        return None
    folded = _folded(raw)
    nested = folded.get("words")
    return {
        "speaker_id": _speaker_id(raw),
        "start_s": start,
        "end_s": end,
        "text": str(
            folded.get("text") or folded.get("content") or folded.get("transcript") or ""
        ).strip(),
        "words": [word for word in nested if isinstance(word, dict)] if isinstance(nested, list) else [],
        "overlap": bool(folded.get("overlap")),
    }


def _segments_from(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        raw_items: list[Any] = payload
        # NF4 decode often wraps the turn list once: [[{Start, End, Speaker, Content}, ...]]
        if len(raw_items) == 1 and isinstance(raw_items[0], list):
            raw_items = raw_items[0]
    elif isinstance(payload, dict):
        folded = _folded(payload)
        raw_items = []
        for key in ("segments", "turns", "speaker_turns"):
            value = folded.get(key)
            if isinstance(value, list):
                raw_items = value
                break
    else:
        raw_items = []
    return [seg for seg in (_segment(item) for item in raw_items) if seg is not None]


def _speaking_time_s(segments: list[dict[str, Any]], speaker_id: str) -> float:
    total = 0.0
    for seg in segments:
        if seg["speaker_id"] == speaker_id and not seg["overlap"]:
            total += max(0.0, float(seg["end_s"]) - float(seg["start_s"]))
    return round(total, 3)


def _speakers_from(payload: Any, segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    declared = _folded(payload).get("speakers") if isinstance(payload, dict) else None
    if isinstance(declared, list) and declared:
        speakers: list[dict[str, Any]] = []
        for index, raw in enumerate(declared):
            if isinstance(raw, dict):
                speaker_id = _speaker_id(raw)
                folded = _folded(raw)
                label = str(folded.get("label") or f"Speaker {index + 1}")
            else:
                speaker_id = _speaker_id({"speaker": raw})
                label = f"Speaker {index + 1}"
            duration = None
            if isinstance(raw, dict):
                duration = _first_number(raw, ("duration_s", "duration"))
            speakers.append(
                {
                    "id": speaker_id,
                    "label": label,
                    "duration_s": duration if duration else _speaking_time_s(segments, speaker_id),
                }
            )
        return speakers
    order: list[str] = []
    for seg in segments:
        if seg["speaker_id"] not in order:
            order.append(seg["speaker_id"])
    return [
        {"id": speaker_id, "label": f"Speaker {index + 1}", "duration_s": _speaking_time_s(segments, speaker_id)}
        for index, speaker_id in enumerate(order)
    ]


def mark_overlaps(
    segments: list[dict[str, Any]],
    declared_overlaps: list[Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Flag intersecting different-speaker slices and name the shared ranges.

    Two speakers at once is not separable here, so both slices become
    `overlap: true` and the shared range is reported. `declared_overlaps` from
    the parsed payload is merged in for models that report ranges only.
    """
    marked = [{**seg, "overlap": bool(seg.get("overlap"))} for seg in segments]
    ranges: list[dict[str, Any]] = []

    def _add(start: float, end: float, speakers: list[str]) -> None:
        key = (round(start, 6), round(end, 6), tuple(sorted(speakers)))
        for existing in ranges:
            if (round(existing["start_s"], 6), round(existing["end_s"], 6), tuple(sorted(existing["speakers"]))) == key:
                return
        ranges.append({"start_s": start, "end_s": end, "speakers": sorted(speakers)})

    for index, left in enumerate(marked):
        for right in marked[index + 1 :]:
            if left["speaker_id"] == right["speaker_id"]:
                continue
            start = max(float(left["start_s"]), float(right["start_s"]))
            end = min(float(left["end_s"]), float(right["end_s"]))
            if end - start <= _EPS:
                continue
            left["overlap"] = True
            right["overlap"] = True
            _add(start, end, [str(left["speaker_id"]), str(right["speaker_id"])])

    for raw in declared_overlaps or []:
        if not isinstance(raw, dict):
            continue
        start = _first_number(raw, _START_KEYS)
        end = _first_number(raw, _END_KEYS)
        speakers = [str(item) for item in (raw.get("speakers") or []) if item is not None]
        if start is None or end is None or not speakers or end - start <= _EPS:
            continue
        _add(start, end, speakers)
        for seg in marked:
            if seg["speaker_id"] not in speakers:
                continue
            if min(float(seg["end_s"]), end) - max(float(seg["start_s"]), start) > _EPS:
                seg["overlap"] = True

    ranges.sort(key=lambda item: (item["start_s"], item["end_s"]))
    return marked, ranges


def parse_analysis(payload: Any) -> dict[str, Any]:
    """Normalize vibevoice parsed output into speakers/segments/overlaps."""
    segments = _segments_from(payload)
    declared = payload.get("overlaps") if isinstance(payload, dict) else None
    marked, overlaps = mark_overlaps(segments, declared if isinstance(declared, list) else None)
    return {
        "version": VERSION,
        "speakers": _speakers_from(payload, marked),
        "segments": marked,
        "overlaps": overlaps,
    }


def keep_for_speaker(analysis: dict[str, Any], speaker_id: str, duration_s: float) -> list[Interval]:
    """Non-overlap spans for one speaker, clipped to the source and merged.

    Empty when the speaker only ever talks over somebody else.
    """
    known = {
        str(item.get("id"))
        for item in (analysis.get("speakers") or [])
        if isinstance(item, dict)
    }
    if known and speaker_id not in known:
        raise KeyError(speaker_id)
    if duration_s <= 0:
        raise ValueError("duration must be positive")
    spans: list[Interval] = []
    for seg in analysis.get("segments") or []:
        if str(seg.get("speaker_id")) != speaker_id or seg.get("overlap"):
            continue
        start = max(0.0, min(float(seg["start_s"]), duration_s))
        end = max(0.0, min(float(seg["end_s"]), duration_s))
        if end - start > _EPS:
            spans.append(Interval(start_s=start, end_s=end))
    if not spans:
        return []
    return normalize_keep_intervals(spans, duration_s)


def words_for_speaker(analysis: dict[str, Any], speaker_id: str) -> list[dict[str, Any]]:
    """Source-timeline words for one speaker's non-overlap slices."""
    words: list[dict[str, Any]] = []
    for seg in analysis.get("segments") or []:
        if str(seg.get("speaker_id")) != speaker_id or seg.get("overlap"):
            continue
        nested = [word for word in (seg.get("words") or []) if isinstance(word, dict)]
        if nested:
            words.extend(nested)
            continue
        text = str(seg.get("text") or "").strip()
        if text:
            words.append(
                {"text": text, "start_s": float(seg["start_s"]), "end_s": float(seg["end_s"])}
            )
    return words


def _vram_used_bytes() -> int | None:
    """Resident GB read after generation. Provenance only; None when unavailable."""
    try:
        proc = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        return int(proc.stdout.strip().splitlines()[0]) * 1024 * 1024
    except Exception:
        return None


def default_analyze(path: Path | str) -> dict[str, Any]:
    """`Dubedo/VibeVoice-ASR-HF-NF4` on the source file, parsed speaker turns out."""
    try:
        import torch
        from transformers import AutoProcessor, VibeVoiceAsrForConditionalGeneration
    except ImportError as exc:
        raise ProcessorUnavailable(f"VibeVoice ASR is not installed: {exc}") from exc
    processor = AutoProcessor.from_pretrained(MODEL_ID)
    model = VibeVoiceAsrForConditionalGeneration.from_pretrained(
        MODEL_ID,
        device_map=DEVICE,
        torch_dtype=torch.bfloat16,
    )
    inputs = processor.apply_transcription_request(audio=str(path)).to(
        model.device, model.dtype
    )
    with torch.inference_mode():
        output_ids = model.generate(**inputs)
    generated_ids = output_ids[:, inputs["input_ids"].shape[1] :]
    parsed = processor.decode(generated_ids, return_format="parsed")
    if isinstance(parsed, list):
        parsed = parsed[0] if parsed else {}
    analysis = parse_analysis(parsed)
    analysis["peak_vram_bytes"] = _vram_used_bytes()
    return analysis


# Module hook. Tests swap in a fake analyzer: no GPU, no weights.
analyze_hook: AnalyzeFn = default_analyze


def analyze_audio(path: Path | str, *, analyze_fn: AnalyzeFn | None = None) -> dict[str, Any]:
    """Analyze `path` (the source artifact) into a normalized analysis payload."""
    result = (analyze_fn or analyze_hook)(Path(path))
    analysis = parse_analysis(result)
    if isinstance(result, dict) and result.get("peak_vram_bytes") is not None:
        analysis["peak_vram_bytes"] = int(result["peak_vram_bytes"])
    return analysis
