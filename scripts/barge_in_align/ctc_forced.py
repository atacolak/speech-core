"""CTC forced aligner on played assistant audio.

Uses *both* waveform and intended text (not text-only):
  audio → CTC emissions, intended chars → targets, Viterbi → word end times,
  cut = words with end_ms <= played_ms.

Default model: torchaudio WAV2VEC2_ASR_BASE_960H (~94M, English chars).
Override with SPEECH_OUT_CTC_MODEL=mms_fa|wav2vec2_base (default wav2vec2_base).

Windowing: by default only the last ALIGN_WINDOW_MS of audio up to played_ms is
encoded (env SPEECH_OUT_ALIGN_WINDOW_MS, default 2000). Words before the window
are assumed fully spoken so we do not re-encode the whole reply on every barge.
"""

from __future__ import annotations

import math
import os
import re
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

from .backend import AlignCursor, approx_wallclock

_NON_LETTER_RE = re.compile(r"[^a-z']+")

# Only re-align the recent prefix of played audio (ms). Prior words assumed spoken.
_DEFAULT_ALIGN_WINDOW_MS = 2000
_WINDOW_PAD_MS = 80  # small right pad past played_ms for frame edge


def _normalize_word_en(w: str) -> str:
    return _NON_LETTER_RE.sub("", w.lower().strip())


def _load_wav_mono_16k(path: Path):
    import torchaudio

    wav, sr = torchaudio.load(str(path))
    if wav.dim() == 2 and wav.size(0) > 1:
        wav = wav.mean(dim=0, keepdim=True)
    if sr != 16000:
        wav = torchaudio.functional.resample(wav, sr, 16000)
        sr = 16000
    return wav.squeeze(0), sr


def _model_choice() -> str:
    return (os.environ.get("SPEECH_OUT_CTC_MODEL") or "wav2vec2_base").strip().lower()


@lru_cache(maxsize=2)
def _load_pack(model_id: str):
    import torch
    import torchaudio
    from torchaudio.pipelines import MMS_FA, WAV2VEC2_ASR_BASE_960H

    device = torch.device("cpu")
    if model_id in ("mms_fa", "mms", "fa"):
        bundle = MMS_FA
        model = bundle.get_model()
        model.eval().to(device)
        return {
            "id": "mms_fa",
            "kind": "mms_fa",
            "model": model,
            "tokenizer": bundle.get_tokenizer(),
            "aligner": bundle.get_aligner(),
            "sample_rate": int(bundle.sample_rate),
            "device": device,
            "bundle": bundle,
            "labels": list(bundle.get_labels()),
        }

    # Default: English wav2vec2 base ASR (smaller than MMS_FA).
    bundle = WAV2VEC2_ASR_BASE_960H
    model = bundle.get_model()
    model.eval().to(device)
    labels = list(bundle.get_labels())  # ('-', '|', 'E', 'T', ...)
    # blank is index 0 ('-') in torchaudio ASR bundles
    blank = 0
    char_to_idx = {c: i for i, c in enumerate(labels)}
    return {
        "id": "wav2vec2_base",
        "kind": "wav2vec2_base",
        "model": model,
        "sample_rate": int(bundle.sample_rate),
        "device": device,
        "bundle": bundle,
        "labels": labels,
        "blank": blank,
        "char_to_idx": char_to_idx,
        "normalize_waveform": bool(getattr(bundle, "_normalize_waveform", False)),
    }


def preload() -> dict[str, Any]:
    return _load_pack(_model_choice())


def _align_window_ms() -> int:
    raw = (os.environ.get("SPEECH_OUT_ALIGN_WINDOW_MS") or str(_DEFAULT_ALIGN_WINDOW_MS)).strip()
    try:
        v = int(raw)
    except ValueError:
        v = _DEFAULT_ALIGN_WINDOW_MS
    return max(0, v)  # 0 = full clip (legacy)


def _window_waveform(waveform, sample_rate: int, played_ms: int) -> tuple[Any, int, dict[str, Any]]:
    """Trim to last window ending at played_ms. Returns (wave[1,T], offset_ms, meta)."""
    import torch

    if waveform.dim() == 1:
        waveform = waveform.unsqueeze(0)
    total_samples = int(waveform.size(-1))
    audio_ms = int(1000.0 * total_samples / float(sample_rate))
    use_played = min(max(0, int(played_ms)), audio_ms + _WINDOW_PAD_MS)
    window_ms = _align_window_ms()
    meta: dict[str, Any] = {
        "window_ms_cfg": window_ms,
        "audio_ms_full": audio_ms,
        "played_ms_used": use_played,
    }
    if window_ms <= 0 or use_played <= window_ms:
        # Full prefix up to played (+pad) — short clips or window disabled.
        end_s = min(total_samples, int((use_played + _WINDOW_PAD_MS) * sample_rate / 1000.0))
        end_s = max(end_s, min(total_samples, int(0.05 * sample_rate)))
        clipped = waveform[..., :end_s]
        meta.update({"window_mode": "prefix", "offset_ms": 0, "window_audio_ms": int(1000.0 * end_s / sample_rate)})
        return clipped, 0, meta

    end_ms = min(audio_ms, use_played + _WINDOW_PAD_MS)
    start_ms = max(0, end_ms - window_ms)
    start_s = int(start_ms * sample_rate / 1000.0)
    end_s = min(total_samples, int(end_ms * sample_rate / 1000.0))
    if end_s <= start_s:
        end_s = min(total_samples, start_s + int(0.05 * sample_rate))
    clipped = waveform[..., start_s:end_s]
    meta.update(
        {
            "window_mode": "tail",
            "offset_ms": start_ms,
            "window_audio_ms": int(1000.0 * max(0, end_s - start_s) / sample_rate),
            "window_start_ms": start_ms,
            "window_end_ms": int(1000.0 * end_s / sample_rate),
        }
    )
    return clipped, start_ms, meta


def _assumed_spoken_words(words: list[str], offset_ms: int, played_ms: int, speed: float) -> int:
    """How many leading words are assumed spoken before the alignment window."""
    if offset_ms <= 0 or not words:
        return 0
    # Same ~2.5 wps prior used by approx_wallclock; only for pre-window assumption.
    wps = max(0.8, 2.5 * max(0.5, float(speed)))
    n = int(round((max(0, offset_ms) / 1000.0) * wps))
    # Never assume past what wall-clock would claim for full played_ms.
    n_cap = int(round((max(0, played_ms) / 1000.0) * wps))
    n = max(0, min(len(words) - 1, n, n_cap))  # leave ≥1 word for window align
    return n


def _align_wav2vec2_base(pack, waveform, words: list[str], played_ms: int, path: Path, t0: float, speed: float) -> AlignCursor:
    import torch
    import torchaudio.functional as F

    device = pack["device"]
    with torch.inference_mode():
        emissions, _ = pack["model"](waveform.to(device))
        # emissions: [batch, time, n_class]
        if emissions.dim() == 3:
            emission = emissions[0]
        else:
            emission = emissions
        emission = emission.cpu()
        log_probs = torch.log_softmax(emission, dim=-1)

    # Build target char sequence with word-boundary '|' like LibriSpeech CTC.
    # Map original word index → whether it contributed chars.
    char_to_idx = pack["char_to_idx"]
    targets: list[int] = []
    # For each original word, the index of its last target token (or None if skipped)
    word_last_target_index: list[int | None] = []
    for i, w in enumerate(words):
        nw = _normalize_word_en(w)
        if not nw:
            word_last_target_index.append(None)
            continue
        # uppercase letters as in labels
        chars = list(nw.upper())
        if any(c not in char_to_idx for c in chars):
            word_last_target_index.append(None)
            continue
        if targets and targets[-1] != char_to_idx.get("|", -1):
            # word separator
            if "|" in char_to_idx:
                targets.append(char_to_idx["|"])
        for c in chars:
            targets.append(char_to_idx[c])
        word_last_target_index.append(len(targets) - 1)

    if not targets:
        fb = approx_wallclock(" ".join(words), played_ms, speed=speed)
        fb.detail = {**(fb.detail or {}), "fallback_reason": "no_alignable_words", "model": pack["id"]}
        return fb

    targets_t = torch.tensor(targets, dtype=torch.int32)
    # forced_align expects [batch, time, n_class] log_probs and [batch, target_len]
    try:
        aligned_tokens, scores = F.forced_align(
            log_probs.unsqueeze(0),
            targets_t.unsqueeze(0),
            blank=pack["blank"],
        )
    except Exception as exc:
        fb = approx_wallclock(" ".join(words), played_ms, speed=speed)
        fb.detail = {
            **(fb.detail or {}),
            "fallback_reason": "forced_align_failed",
            "error": f"{type(exc).__name__}: {exc}",
            "model": pack["id"],
        }
        fb.align_latency_ms = (time.perf_counter() - t0) * 1000.0
        return fb

    # aligned_tokens: [batch, time] token ids per frame (blank-filled)
    aligned = aligned_tokens[0].tolist()
    n_frames = len(aligned)
    audio_s = float(waveform.size(-1)) / float(pack["sample_rate"])
    # waveform was [1, samples]
    frame_hz = (n_frames / audio_s) if audio_s > 0 else 50.0

    # For each non-blank target token index, find last frame where it appears
    # after accounting for CTC path. Simpler: use merge_tokens if available.
    try:
        spans = F.merge_tokens(aligned_tokens[0], scores[0])
        # spans: list of TokenSpan for non-blank runs in order of targets
        # Map target position → end frame
        target_end_frame: list[int] = []
        for sp in spans:
            target_end_frame.append(int(sp.end))
    except Exception:
        # Fallback: walk frames
        target_end_frame = []
        ti = 0
        last_f = 0
        for f, tok in enumerate(aligned):
            if tok == pack["blank"]:
                continue
            if ti < len(targets) and tok == targets[ti]:
                last_f = f
                # peek if next nonblank continues same? CTC can repeat
                # record end when token changes or end
                target_end_frame.append(f)
                # advance only when next different target needed — crude
                ti += 1
        # This fallback is imperfect; prefer merge_tokens path.

    # word end = end frame of last char of that word
    word_ends_ms: list[float] = []
    spoken = 0
    cutoff = float(played_ms + 40)
    for wi, last_ti in enumerate(word_last_target_index):
        if last_ti is None:
            # unalignable punctuation word: inherit previous timing decision
            if spoken == wi and wi > 0:
                # count as spoken if previous was
                spoken = wi + 1
            word_ends_ms.append(word_ends_ms[-1] if word_ends_ms else 0.0)
            continue
        if last_ti >= len(target_end_frame):
            break
        end_f = target_end_frame[last_ti]
        end_ms = 1000.0 * end_f / frame_hz
        word_ends_ms.append(end_ms)
        if end_ms <= cutoff:
            spoken = wi + 1
        else:
            break

    if spoken <= 0:
        if played_ms >= 80 and words:
            prefix, spoken = words[0], 1
        else:
            prefix, spoken = "", 0
    else:
        prefix = " ".join(words[:spoken])

    ms = (time.perf_counter() - t0) * 1000.0
    return AlignCursor(
        spoken_prefix=prefix,
        word_index=spoken,
        intended_text=" ".join(words),
        backend_id="ctc_forced",
        confidence=0.7 if spoken else 0.2,
        align_latency_ms=ms,
        played_ms=played_ms,
        audio_path=str(path),
        detail={
            "model": pack["id"],
            "n_frames": n_frames,
            "frame_hz": frame_hz,
            "audio_ms": int(audio_s * 1000),
            "cutoff_ms": cutoff,
            "word_ends_ms": [round(x, 1) for x in word_ends_ms],
            "n_targets": len(targets),
        },
    )


def _align_mms_fa(pack, waveform, words: list[str], played_ms: int, path: Path, t0: float, speed: float) -> AlignCursor:
    import torch

    with torch.inference_mode():
        emission, _ = pack["model"](waveform.to(pack["device"]))
        if emission.dim() == 3:
            emission = emission[0]
        emission = emission.cpu()

    norm_words: list[str] = []
    orig_index: list[int] = []
    for i, w in enumerate(words):
        nw = _normalize_word_en(w)
        if not nw:
            continue
        if any(c not in pack["tokenizer"].dictionary for c in nw):
            continue
        norm_words.append(nw)
        orig_index.append(i)
    if not norm_words:
        fb = approx_wallclock(" ".join(words), played_ms, speed=speed)
        fb.detail = {**(fb.detail or {}), "fallback_reason": "no_alignable_words", "model": pack["id"]}
        return fb

    token_batches = pack["tokenizer"](norm_words)
    word_spans = pack["aligner"](emission, token_batches)
    n_frames = int(emission.size(0))
    audio_s = float(waveform.size(-1)) / float(pack["sample_rate"])
    frame_hz = (n_frames / audio_s) if audio_s > 0 else 50.0
    cutoff = float(played_ms + 40)
    spoken_norm = 0
    word_ends_ms: list[float] = []
    for spans in word_spans:
        if not spans:
            break
        end_f = int(spans[-1].end)
        end_ms = 1000.0 * end_f / frame_hz
        word_ends_ms.append(end_ms)
        if end_ms <= cutoff:
            spoken_norm += 1
        else:
            break
    if spoken_norm <= 0:
        if played_ms >= 80 and words:
            last_orig, prefix = 1, words[0]
        else:
            last_orig, prefix = 0, ""
    else:
        last_orig = orig_index[spoken_norm - 1] + 1
        prefix = " ".join(words[:last_orig])
    ms = (time.perf_counter() - t0) * 1000.0
    return AlignCursor(
        spoken_prefix=prefix,
        word_index=last_orig if spoken_norm else (1 if prefix else 0),
        intended_text=" ".join(words),
        backend_id="ctc_forced",
        confidence=0.7 if prefix else 0.2,
        align_latency_ms=ms,
        played_ms=played_ms,
        audio_path=str(path),
        detail={
            "model": pack["id"],
            "n_frames": n_frames,
            "frame_hz": frame_hz,
            "audio_ms": int(audio_s * 1000),
            "cutoff_ms": cutoff,
            "word_ends_ms": [round(x, 1) for x in word_ends_ms],
        },
    )


def align_ctc_forced(
    *,
    intended_text: str,
    played_ms: int,
    wav_path: str | Path | None = None,
    speed: float = 1.0,
    **_kwargs: Any,
) -> AlignCursor:
    t0 = time.perf_counter()
    words = intended_text.split()
    if not words:
        return AlignCursor(
            spoken_prefix="",
            word_index=0,
            intended_text=intended_text,
            backend_id="ctc_forced",
            confidence=0.0,
            align_latency_ms=0.0,
            played_ms=played_ms,
            detail={"error": "empty_intended"},
        )

    if wav_path is None or not Path(wav_path).is_file():
        fb = approx_wallclock(intended_text, played_ms, speed=speed)
        fb.detail = {**(fb.detail or {}), "fallback_reason": "missing_wav"}
        return fb

    path = Path(wav_path)
    try:
        pack = _load_pack(_model_choice())
        import torch

        wav, sr = _load_wav_mono_16k(path)
        if wav.numel() < int(0.05 * sr):
            fb = approx_wallclock(intended_text, played_ms, speed=speed)
            fb.detail = {**(fb.detail or {}), "fallback_reason": "audio_too_short"}
            return fb

        audio_ms = int(1000.0 * float(wav.numel()) / float(sr))
        use_played = played_ms if played_ms > 0 else audio_ms
        use_played = min(use_played, audio_ms + 40)

        full = wav.unsqueeze(0)
        windowed, offset_ms, win_meta = _window_waveform(full, sr, use_played)
        assumed = _assumed_spoken_words(words, offset_ms, use_played, speed)
        window_words = words[assumed:]
        # played_ms relative to window start for the sub-aligner
        local_played = max(0, use_played - offset_ms)

        waveform = windowed
        if pack.get("normalize_waveform") or (
            pack["kind"] == "mms_fa" and getattr(pack["bundle"], "_normalize_waveform", False)
        ):
            waveform = torch.nn.functional.layer_norm(waveform, waveform.shape)

        if not window_words:
            # everything assumed spoken
            ms = (time.perf_counter() - t0) * 1000.0
            prefix = " ".join(words)
            return AlignCursor(
                spoken_prefix=prefix,
                word_index=len(words),
                intended_text=intended_text,
                backend_id="ctc_forced",
                confidence=0.55,
                align_latency_ms=ms,
                played_ms=played_ms,
                audio_path=str(path),
                detail={**win_meta, "assumed_words": assumed, "model": pack["id"]},
            )

        if pack["kind"] == "mms_fa":
            cur = _align_mms_fa(pack, waveform, window_words, local_played, path, t0, speed)
        else:
            cur = _align_wav2vec2_base(pack, waveform, window_words, local_played, path, t0, speed)

        # Stitch: assumed prefix + window-aligned suffix.
        local_idx = int(cur.word_index or 0)
        total_idx = min(len(words), assumed + local_idx)
        if total_idx <= 0:
            prefix = ""
        else:
            prefix = " ".join(words[:total_idx])
        detail = dict(cur.detail or {})
        detail.update(win_meta)
        detail["assumed_words"] = assumed
        detail["window_word_index"] = local_idx
        detail["stitched_word_index"] = total_idx
        return AlignCursor(
            spoken_prefix=prefix,
            word_index=total_idx,
            intended_text=intended_text,
            backend_id=cur.backend_id or "ctc_forced",
            confidence=cur.confidence,
            align_latency_ms=(time.perf_counter() - t0) * 1000.0,
            played_ms=played_ms,
            audio_path=str(path),
            detail=detail,
        )
    except Exception as exc:
        fb = approx_wallclock(intended_text, played_ms, speed=speed)
        fb.detail = {
            **(fb.detail or {}),
            "fallback_reason": "ctc_exception",
            "error": f"{type(exc).__name__}: {exc}",
        }
        fb.align_latency_ms = (time.perf_counter() - t0) * 1000.0
        return fb
