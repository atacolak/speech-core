"""CTC forced aligner (torchaudio MMS_FA) on played assistant audio.

Uses *both* waveform and intended text:
  - audio → frame-level CTC emissions (acoustic evidence)
  - intended words → token ids
  - Viterbi forced alignment → per-word time spans on the *full* teed clip
  - cut = words whose end_ms <= played_ms

Important: we do NOT trim the waveform to played_ms before alignment.
Forced alignment always places the whole transcript into whatever audio you
give it — trimming would squash every word into the short prefix and always
look "fully spoken." Instead align on the full TTS wav, then gate by time.
"""

from __future__ import annotations

import math
import re
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

from .backend import AlignCursor, approx_wallclock

_NON_ALIGN_RE = re.compile(r"[^a-z']+")


def _normalize_word(w: str) -> str:
    w = w.lower().strip()
    w = _NON_ALIGN_RE.sub("", w)
    return w


def _load_wav_mono_16k(path: Path):
    import torchaudio

    wav, sr = torchaudio.load(str(path))
    if wav.dim() == 2 and wav.size(0) > 1:
        wav = wav.mean(dim=0, keepdim=True)
    if sr != 16000:
        wav = torchaudio.functional.resample(wav, sr, 16000)
        sr = 16000
    return wav.squeeze(0), sr


@lru_cache(maxsize=1)
def _load_mms_fa():
    """Lazy singleton: model + tokenizer + aligner (CPU)."""
    import torch
    from torchaudio.pipelines import MMS_FA

    device = torch.device("cpu")
    model = MMS_FA.get_model()
    model.eval()
    model.to(device)
    tokenizer = MMS_FA.get_tokenizer()
    aligner = MMS_FA.get_aligner()
    return {
        "model": model,
        "tokenizer": tokenizer,
        "aligner": aligner,
        "sample_rate": int(MMS_FA.sample_rate),
        "device": device,
        "bundle": MMS_FA,
    }


def preload() -> dict[str, Any]:
    """Warm weights into memory (call at session start)."""
    return _load_mms_fa()


def align_ctc_forced(
    *,
    intended_text: str,
    played_ms: int,
    wav_path: str | Path | None = None,
    speed: float = 1.0,
    tail_slack_ms: int = 40,
    **_kwargs: Any,
) -> AlignCursor:
    """Force-align intended text onto full teed audio; cut by played_ms."""
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
        pack = _load_mms_fa()
        import torch

        wav, sr = _load_wav_mono_16k(path)
        if wav.numel() < int(0.05 * sr):
            fb = approx_wallclock(intended_text, played_ms, speed=speed)
            fb.detail = {**(fb.detail or {}), "fallback_reason": "audio_too_short"}
            return fb

        audio_ms = int(1000.0 * float(wav.numel()) / float(sr))
        use_played = played_ms if played_ms > 0 else audio_ms
        # Cap to audio length (+slack); can't have spoken past the file.
        use_played = min(use_played, audio_ms + tail_slack_ms)

        waveform = wav.unsqueeze(0)
        if getattr(pack["bundle"], "_normalize_waveform", False):
            waveform = torch.nn.functional.layer_norm(waveform, waveform.shape)

        with torch.inference_mode():
            emission, _ = pack["model"](waveform.to(pack["device"]))
            if emission.dim() == 3:
                emission = emission[0]
            emission = emission.cpu()

        norm_words: list[str] = []
        orig_index: list[int] = []
        for i, w in enumerate(words):
            nw = _normalize_word(w)
            if not nw:
                continue
            if any(c not in pack["tokenizer"].dictionary for c in nw):
                continue
            norm_words.append(nw)
            orig_index.append(i)

        if not norm_words:
            fb = approx_wallclock(intended_text, use_played, speed=speed)
            fb.detail = {**(fb.detail or {}), "fallback_reason": "no_alignable_words"}
            return fb

        token_batches = pack["tokenizer"](norm_words)
        word_spans = pack["aligner"](emission, token_batches)

        n_frames = int(emission.size(0))
        audio_s = float(wav.numel()) / float(sr)
        frame_hz = (n_frames / audio_s) if audio_s > 0 else 50.0

        word_ends_ms: list[float] = []
        word_scores: list[float] = []
        for spans in word_spans:
            if not spans:
                word_ends_ms.append(float("inf"))
                word_scores.append(0.0)
                continue
            end_f = int(spans[-1].end)
            try:
                score = sum(float(s.score) for s in spans) / max(1, len(spans))
            except Exception:
                score = float(getattr(spans[-1], "score", 0.0))
            word_ends_ms.append(1000.0 * end_f / frame_hz)
            word_scores.append(score)

        # How many *normalized* words fully ended by played time?
        cutoff = float(use_played + tail_slack_ms)
        spoken_norm = 0
        for end_ms in word_ends_ms:
            if end_ms <= cutoff:
                spoken_norm += 1
            else:
                break

        if spoken_norm <= 0:
            # Very early barge — at most first word if any audio played
            if use_played >= 80 and words:
                prefix = words[0]
                last_orig = 1
            else:
                prefix, last_orig = "", 0
            ms = (time.perf_counter() - t0) * 1000.0
            return AlignCursor(
                spoken_prefix=prefix,
                word_index=last_orig,
                intended_text=intended_text,
                backend_id="ctc_forced",
                confidence=0.25,
                align_latency_ms=ms,
                played_ms=use_played,
                audio_path=str(path),
                detail={
                    "n_frames": n_frames,
                    "frame_hz": frame_hz,
                    "audio_ms": audio_ms,
                    "cutoff_ms": cutoff,
                    "aligned_norm_words": 0,
                    "note": "before_first_word_end",
                    "model": "torchaudio.pipelines.MMS_FA",
                },
            )

        last_orig = orig_index[spoken_norm - 1] + 1
        last_orig = max(1, min(len(words), last_orig))
        prefix = " ".join(words[:last_orig])

        scores_used = word_scores[:spoken_norm]
        conf = 0.5
        if scores_used:
            conf = max(
                0.15,
                min(0.99, 1.0 / (1.0 + math.exp(-sum(scores_used) / len(scores_used)))),
            )

        ms = (time.perf_counter() - t0) * 1000.0
        return AlignCursor(
            spoken_prefix=prefix,
            word_index=last_orig,
            intended_text=intended_text,
            backend_id="ctc_forced",
            confidence=float(conf),
            align_latency_ms=ms,
            played_ms=use_played,
            audio_path=str(path),
            detail={
                "n_frames": n_frames,
                "frame_hz": frame_hz,
                "audio_ms": audio_ms,
                "cutoff_ms": cutoff,
                "aligned_norm_words": spoken_norm,
                "norm_vocab_words": len(norm_words),
                "word_ends_ms": [round(x, 1) for x in word_ends_ms],
                "model": "torchaudio.pipelines.MMS_FA",
            },
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
