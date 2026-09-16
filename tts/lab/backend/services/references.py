"""Canonical keep-interval math. not on the voicecat path.

Intervals are on the original source timeline. Exclude/keep-only never
shift later coordinates. Sequential EditSpec ops stay in tts.edits for
the Gradio rollback path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tts.lab.backend.models import Interval
from tts.lab.backend.store.cache import processor_cache_key

_EPS = 1e-9

# The origin a request's audio belongs to. `primary` is the voice's own source
# audio (and every legacy selection derived from it); `unknown` is audio no
# origin row of the voice owns, which therefore has no transcript of its own.
PRIMARY = "primary"
SOURCE = "source"
CLIP = "clip"
UNKNOWN = "unknown"

# Breeze conditions on `ref_text`: prose only. The analysis boundary strips
# diarization markup (`services/sources.py`); a stored top-level transcript may
# still carry SRT/VTT ranges, and those are dropped at composition, not in place.
#
# A range is the one thing that names a cue, so it is matched with the shape a
# cue has in either format: an optional bracket (`[00:01.23 --> 00:02.00]`), an
# optional leading cue number on its own line (SRT numbers every cue), and no
# bracket at all — a bare `00:00:00,000 --> 00:00:02,000` line is the common
# form. An arrow is the only separator that cannot be prose, so a bare range
# must be arrow-formed; `to` and the dashes stay inside a bracket, which is where
# hand-written cue markup lives (`[00:00 to 00:02]`). A lone clock token is not a
# range either: `_TIMESTAMP` only removes a bracketed one, and `_LONE_CLOCK`
# only a bare one carrying seconds or a fraction — `3:30 pm` survives.
_CLOCK = r"\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d{1,3})?"
_CUE_INDEX = r"(?:^[ \t]*\d{1,9}[ \t]*\r?\n[ \t]*)?"
_ARROW = r"(?:-->|->|\u2192)"
_BRACKET_SEPARATOR = r"(?:-->|->|\u2192|\u2014|\u2013|to)"
_SRT_RANGE = re.compile(
    _CUE_INDEX
    + r"(?:"
    + r"[\[(]?\s*" + _CLOCK + r"\s*" + _ARROW + r"\s*" + _CLOCK + r"\s*[\])]?"
    + r"|"
    + r"[\[(]\s*" + _CLOCK + r"\s*" + _BRACKET_SEPARATOR + r"\s*" + _CLOCK + r"\s*[\])]?"
    + r")",
    re.IGNORECASE | re.MULTILINE,
)
_TIMESTAMP = re.compile(r"[\[(]\s*" + _CLOCK + r"\s*[\])]")
# A clock with seconds or a fraction cannot be prose, so a bare one is a stray
# cue fragment. A bare `H:MM` can be a time of day (`4:15`, `3:30 pm`).
_LONE_CLOCK = re.compile(r"\b\d{1,2}:\d{2}:\d{2}(?:[.,]\d{1,3})?\b|\b\d{1,2}:\d{2}[.,]\d{1,3}\b")
# `SPEAKER_00:` anywhere, and the short `S0:` the decoder emits at a cue start.
# The id must be real: either a separator carries it (`SPEAKER_00:`,
# `SPEAKER 01:`, `Speaker A:`) or it cannot continue the word — digits
# (`speaker00:`) or a genuinely capitalised initial (`SpeakerA:`). A zero-width
# separator in front of a plain lowercase word is not markup: `speakers:` and
# `the speakerphone:` are the operator's sentence, and eating them showed a
# picker quote the reference never sent. The capital test is case-scoped so the
# flag above cannot collapse it into "any word". The short form is a whole token
# at the head of a line: `the s3: bucket policy` is prose, and the previous
# loose match ate it.
_SPEAKER_MARKUP = re.compile(
    r"(?:\bspeaker(?:[_\s-]+\w+|\d{1,3}|(?-i:[A-Z])\w*)|^[ \t]*s\d+)\s*:",
    re.IGNORECASE | re.MULTILINE,
)
# The WebVTT signature line. Only a header at the very start of the text is one,
# and a BOM or a leading blank line is still the start.
_WEBVTT_HEADER = re.compile(r"\A\ufeff?[ \t\r\n]*WEBVTT\b[^\n]*\r?\n?", re.IGNORECASE)


def _as_interval(value: Interval) -> Interval:
    return value if isinstance(value, Interval) else Interval.model_validate(value)


def normalize_keep_intervals(
    intervals: list[Interval],
    duration_s: float,
) -> list[Interval]:
    if duration_s <= 0:
        raise ValueError("duration must be positive")
    if not intervals:
        raise ValueError("keep intervals must not be empty")
    ordered: list[Interval] = []
    for raw in intervals:
        interval = _as_interval(raw)
        if interval.end_s > duration_s + _EPS:
            raise ValueError("end > duration")
        if interval.start_s > duration_s + _EPS:
            raise ValueError("end > duration")
        ordered.append(interval)
    ordered.sort(key=lambda iv: (iv.start_s, iv.end_s))
    merged: list[Interval] = [ordered[0]]
    for interval in ordered[1:]:
        prev = merged[-1]
        if interval.start_s <= prev.end_s + _EPS:
            merged[-1] = Interval(
                start_s=prev.start_s,
                end_s=max(prev.end_s, interval.end_s),
            )
        else:
            merged.append(interval)
    return merged


def exclude_interval(keep: list[Interval], excluded: Interval) -> list[Interval]:
    cut = _as_interval(excluded)
    if not keep:
        raise ValueError("keep intervals must not be empty")
    duration = max(max(iv.end_s for iv in keep), cut.end_s)
    pieces: list[Interval] = []
    for interval in keep:
        interval = _as_interval(interval)
        if cut.end_s <= interval.start_s + _EPS or cut.start_s >= interval.end_s - _EPS:
            pieces.append(interval)
            continue
        if interval.start_s < cut.start_s - _EPS:
            pieces.append(Interval(start_s=interval.start_s, end_s=cut.start_s))
        if interval.end_s > cut.end_s + _EPS:
            pieces.append(Interval(start_s=cut.end_s, end_s=interval.end_s))
    return normalize_keep_intervals(pieces, duration)


def keep_only_interval(selected: Interval, duration_s: float) -> list[Interval]:
    return normalize_keep_intervals([_as_interval(selected)], duration_s)


def exclude_intervals(keep: list[Interval], excluded: list[Interval]) -> list[Interval]:
    """Cut many sections. Each cut is on the original timeline; later cuts see earlier holes."""
    current = list(keep)
    for item in excluded:
        current = exclude_interval(current, item)
    return current


def materialize_keep_wav(
    source: Path | str,
    intervals: list[Interval],
    dest: Path | str,
) -> Path:
    from pathlib import Path as _Path

    import numpy as np

    from tts.wav import read_wav, write_wav

    src = _Path(source)
    out = _Path(dest)
    sr, samples = read_wav(src)
    pieces = []
    n = int(samples.size)
    for raw in normalize_keep_intervals(list(intervals), max(n / float(sr), 1e-3)):
        start = max(0, min(n, int(round(raw.start_s * sr))))
        end = max(0, min(n, int(round(raw.end_s * sr))))
        if end > start:
            pieces.append(samples[start:end])
    if not pieces:
        raise ValueError("effective reference is empty")
    write_wav(out, sr, np.concatenate(pieces))
    return out


def processed_variant_is_current(
    variant: dict[str, Any] | None,
    source_sha256: str,
    keep_intervals: list[Interval] | list[dict[str, Any]],
) -> bool:
    """True when a processed variant still matches the source bytes and keep.

    `original` is not a processed artifact: it reports False so callers
    materialize the keep crop from the source instead of cloning raw audio.
    """
    if not variant or variant.get("kind") in (None, "original"):
        return False
    stored = str(variant.get("processor_cache_key") or "")
    if not stored:
        return False
    expected = processor_cache_key(
        str(variant["kind"]),
        source_sha256,
        keep_intervals,
        variant.get("processor_config") or {},
    )
    return stored == expected


def keep_duration_s(intervals: list[Interval] | list[dict[str, float]]) -> float:
    total = 0.0
    for raw in intervals:
        interval = _as_interval(raw)
        total += max(0.0, interval.end_s - interval.start_s)
    return total


def _word_midpoint(word: dict[str, object]) -> float:
    start = float(word.get("start_s") or 0.0)
    end = float(word.get("end_s") or start)
    if end <= start:
        return start
    return 0.5 * (start + end)


def _word_in_keep(word: dict[str, object], keep: list[Interval]) -> bool:
    mid = _word_midpoint(word)
    for interval in keep:
        if interval.start_s - _EPS <= mid <= interval.end_s + _EPS:
            return True
    return False


def join_words(words: list[dict[str, object]]) -> str:
    parts: list[str] = []
    for word in words:
        text = str(word.get("text") or "")
        if not text:
            continue
        if parts and not text[:1].isspace() and not text.startswith(("'", ",", ".", "!", "?", ";", ":")):
            if not parts[-1].endswith((" ", "\n")):
                parts.append(" ")
        parts.append(text)
    return "".join(parts).strip()


def slice_transcript(
    words: list[dict[str, object]] | None,
    keep: list[Interval] | list[dict[str, float]],
) -> str:
    """Crop a cached source-aligned transcript to the keep windows.

    One ASR pass on the original file. Keep/exclude never re-runs Parakeet.
    """
    if not words:
        return ""
    windows = [_as_interval(item) for item in keep]
    kept = [word for word in words if _word_in_keep(word, windows)]
    return join_words(kept)


def reference_transcript(voice: dict[str, Any], reference_id: str | None) -> str:
    """The clean transcript of the selected reference, never another origin's.

    One rule for the voice picker and for `ref_text`: a selected artifact's
    `source_id` wins, then a clip's own clean transcript, then the primary
    transcript for a legacy variant or no selection at all.
    """
    source = _declared_source(voice, reference_id)
    if source is not None:
        return str(source.get("transcript") or "").strip()
    clip = _declared_clip(voice, reference_id)
    if clip is not None:
        return str(clip.get("clean_transcript") or "").strip()
    return str(voice.get("effective_transcript") or voice.get("source_transcript") or "").strip()


def _declared_source(voice: dict[str, Any], reference_id: str | None) -> dict[str, Any] | None:
    """The `voice_sources` row a selected artifact declares as its origin."""
    if not reference_id:
        return None
    target = next(
        (item for item in voice.get("artifacts") or [] if item["id"] == reference_id), None
    )
    if target is None or not target.get("source_id"):
        return None
    return next(
        (item for item in voice.get("sources") or [] if item["id"] == target["source_id"]),
        None,
    )


def _declared_clip(voice: dict[str, Any], reference_id: str | None) -> dict[str, Any] | None:
    """A clip the selection names by its own id or by its audio artifact."""
    if not reference_id:
        return None
    return next(
        (
            item
            for item in voice.get("clips") or []
            if reference_id in {item["id"], item["audio_artifact_id"]}
        ),
        None,
    )


def _audio_source(voice: dict[str, Any], audio_artifact_id: str) -> dict[str, Any] | None:
    """The `voice_sources` row whose own audio is `audio_artifact_id`."""
    return next(
        (
            item
            for item in voice.get("sources") or []
            if str(item.get("artifact_id") or "") == audio_artifact_id
        ),
        None,
    )


def _audio_clip(voice: dict[str, Any], audio_artifact_id: str) -> dict[str, Any] | None:
    """The clip whose own audio is `audio_artifact_id`."""
    return next(
        (
            item
            for item in voice.get("clips") or []
            if str(item.get("audio_artifact_id") or "") == audio_artifact_id
        ),
        None,
    )


@dataclass(frozen=True)
class ReferenceOrigin:
    """The origin the selected reference names: its audio and its own transcript.

    `kind` is `primary` for the voice's own source audio (and for a legacy
    variant or no selection at all), `source` for a `voice_sources` row, `clip`
    for a `clips` row, and `unknown` when the audio belongs to no origin row of
    this voice. An `unknown` origin carries no transcript on purpose: one
    origin's audio is never paired with another origin's text.
    """

    kind: str
    audio_artifact_id: str | None
    transcript: str = ""
    source_id: str | None = None
    clip_id: str | None = None

    @property
    def is_primary(self) -> bool:
        """True when this audio is the voice's own source audio or derived from it."""
        return self.kind == PRIMARY


def reference_origin(
    voice: dict[str, Any], reference_id: str | None, audio_artifact_id: str | None = None
) -> ReferenceOrigin:
    """The origin a request sends, and that origin's own transcript.

    `audio_artifact_id` is the audio the request actually sends. When it is the
    voice's own source audio, or a legacy variant / no selection at all, the
    origin is the primary and its transcript is `reference_transcript`'s ladder.
    Any other audio must find an origin row that owns it — the row a selected
    artifact declares, the clip it names, or the `voice_sources` / clip row whose
    own artifact it is — because one origin's audio is never given another
    origin's text.
    """
    audio = None if audio_artifact_id is None else str(audio_artifact_id)
    primary_audio = str(voice.get("source_audio_artifact_id") or "")
    if audio is None or audio == primary_audio:
        return ReferenceOrigin(
            PRIMARY,
            audio or primary_audio or None,
            reference_transcript(voice, reference_id),
        )
    source = _declared_source(voice, reference_id)
    if source is not None:
        return _source_origin(voice, source, audio)
    clip = _declared_clip(voice, reference_id)
    if clip is not None:
        return _clip_origin(clip, audio)
    owner = _audio_source(voice, audio)
    if owner is not None:
        return _source_origin(voice, owner, audio)
    owner_clip = _audio_clip(voice, audio)
    if owner_clip is not None:
        return _clip_origin(owner_clip, audio)
    return ReferenceOrigin(UNKNOWN, audio)


def _source_origin(
    voice: dict[str, Any], source: dict[str, Any], audio_artifact_id: str
) -> ReferenceOrigin:
    """A `voice_sources` origin. Its own audio is the primary origin's."""
    text = str(source.get("transcript") or "").strip()
    if str(source.get("artifact_id") or "") == str(voice.get("source_audio_artifact_id") or ""):
        return ReferenceOrigin(PRIMARY, audio_artifact_id, text)
    return ReferenceOrigin(SOURCE, audio_artifact_id, text, source_id=str(source["id"]))


def _clip_origin(clip: dict[str, Any], audio_artifact_id: str) -> ReferenceOrigin:
    return ReferenceOrigin(
        CLIP,
        audio_artifact_id,
        str(clip.get("clean_transcript") or "").strip(),
        clip_id=str(clip["id"]),
    )


def clean_ref_text(text: str) -> str:
    """Prose for Breeze: no timestamps, no SRT/VTT ranges, no diarization markup.

    Breeze conditions on `ref_text`, so the composition boundary strips what a
    stored transcript may carry: the WebVTT signature, every cue range with its
    cue number, a bracketed clock token, a bare clock carrying seconds or a
    fraction, and diarization markup. A bare `H:MM` stays because prose says
    `3:30 pm` and `we open from 9:00 to 5:00`. The stored row is never rewritten.
    """
    stripped = _WEBVTT_HEADER.sub(" ", text)
    stripped = _SRT_RANGE.sub(" ", stripped)
    stripped = _TIMESTAMP.sub(" ", stripped)
    stripped = _LONE_CLOCK.sub(" ", stripped)
    stripped = _SPEAKER_MARKUP.sub(" ", stripped)
    return " ".join(stripped.split())

