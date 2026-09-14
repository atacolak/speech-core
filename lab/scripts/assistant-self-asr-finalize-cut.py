#!/usr/bin/env python3
"""Finalize assistant truncated cut for speech-out-live-session (real path).

Reads intended TTS text + optional drained ASR (from Nemotron B watch jsonl or
plain text). Optionally merges tee-captured played WAV chunks for B feed.

No mocks. Helper for speech-out-live-session.sh only.
"""
from __future__ import annotations

import argparse
import json
import sys
import wave
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
# Support both repo layout (scripts/ + scripts/barge-in-dual-asr/) and
# client install layout (~/.local/libexec/speech-core/ + .../barge-in-dual-asr/).
_DUAL_CANDIDATES = [
    Path(p)
    for p in (
        __import__("os").environ.get("SPEECH_CORE_DUAL_ASR_DIR"),
        _HERE / "barge-in-dual-asr",
        _REPO / "scripts" / "barge-in-dual-asr",
        _HERE.parent / "barge-in-dual-asr",
    )
    if p
]
_DUAL = next((p for p in _DUAL_CANDIDATES if (p / "cut.py").is_file()), _DUAL_CANDIDATES[0])
if str(_DUAL) not in sys.path:
    sys.path.insert(0, str(_DUAL))

from cut import production_cut  # type: ignore  # noqa: E402


def last_transcript_from_watch_jsonl(path: Path) -> str:
    if not path.is_file():
        return ""
    last = ""
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        ev = obj.get("event") or ""
        if ev in (
            "transcript_committed",
            "turn_transcript_committed",
            "transcript_update",
            "transcript_finalized",
        ):
            text = obj.get("text") or obj.get("committed_text") or ""
            if isinstance(text, str) and text.strip():
                last = text.strip()
        elif ev == "transcript_token_committed" and not last:
            tok = obj.get("text") or ""
            if isinstance(tok, str) and tok.strip():
                last = tok.strip()
    return last


def merge_chunk_wavs(chunk_dir: Path, out_wav: Path) -> int:
    chunks = sorted(chunk_dir.glob("chunk_*.wav"))
    if not chunks:
        return 0
    params = None
    frames: list[bytes] = []
    used = 0
    for c in chunks:
        try:
            with wave.open(str(c), "rb") as w:
                p = w.getparams()
                if params is None:
                    params = p
                elif (p.nchannels, p.sampwidth, p.framerate) != (
                    params.nchannels,
                    params.sampwidth,
                    params.framerate,
                ):
                    continue
                frames.append(w.readframes(w.getnframes()))
                used += 1
        except wave.Error:
            continue
    if not frames or params is None:
        return 0
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out_wav), "wb") as out:
        out.setnchannels(params.nchannels)
        out.setsampwidth(params.sampwidth)
        out.setframerate(params.framerate)
        for f in frames:
            out.writeframes(f)
    return used


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--intended-text", required=True)
    ap.add_argument("--out-dir", required=True, type=Path)
    ap.add_argument("--drained-text", default="")
    ap.add_argument("--watch-jsonl", type=Path, default=None)
    ap.add_argument("--last-pos-words", type=int, default=0)
    ap.add_argument("--pad-words", type=int, default=2)
    ap.add_argument("--drain-complete", action="store_true")
    ap.add_argument(
        "--played-chunks",
        type=int,
        default=0,
        help="Assistant WAV chunks actually played (tee capture count).",
    )
    ap.add_argument(
        "--merge-chunks-dir",
        type=Path,
        default=None,
        help="If set, merge chunk_*.wav into out-dir/assistant_played.wav",
    )
    ap.add_argument(
        "--merge-only",
        action="store_true",
        help="Only merge tee chunks to assistant_played.wav; do not cut.",
    )
    args = ap.parse_args()

    out = args.out_dir
    out.mkdir(parents=True, exist_ok=True)

    played_chunks = args.played_chunks
    if args.merge_chunks_dir is not None:
        n = merge_chunk_wavs(args.merge_chunks_dir, out / "assistant_played.wav")
        if n > 0:
            played_chunks = max(played_chunks, n)

    if args.merge_only:
        print(played_chunks)
        print(
            f"assistant-self-asr-finalize-cut: merge-only played_chunks={played_chunks}",
            file=sys.stderr,
        )
        return 0 if played_chunks > 0 else 1

    drained = (args.drained_text or "").strip()
    if not drained and args.watch_jsonl is not None:
        drained = last_transcript_from_watch_jsonl(args.watch_jsonl)

    last_pos = args.last_pos_words
    if last_pos <= 0 and played_chunks > 0:
        # Playback-progress fallback: ~4 words per played TTS text-chunk.
        last_pos = max(0, played_chunks * 4)

    decision = production_cut(
        args.intended_text,
        drained_asr_text=drained,
        last_pos_words=last_pos,
        pad_words=args.pad_words,
        drain_complete=bool(args.drain_complete or drained),
    )

    (out / "assistant_intended.txt").write_text(
        args.intended_text.strip() + "\n", encoding="utf-8"
    )
    (out / "drained_asr_text.txt").write_text(drained + "\n", encoding="utf-8")
    (out / "production_cut_text").write_text(
        decision.production_cut_text + "\n", encoding="utf-8"
    )
    metrics = {
        "label": "live_session",
        "primary_cut_source": decision.source,
        "production_cut_text": decision.production_cut_text,
        "intended_text": args.intended_text.strip(),
        "drained_asr_text": drained,
        "aligned_prefix": decision.aligned_prefix,
        "aligned_word_count": decision.aligned_word_count,
        "align_confidence": decision.align_confidence,
        "last_pos_words": decision.last_pos_words,
        "pad_words": decision.pad_words,
        "prefix_valid": decision.prefix_valid,
        "played_chunks": played_chunks,
        "drain_complete": decision.drain_complete,
    }
    (out / "metrics.json").write_text(
        json.dumps(metrics, indent=2) + "\n", encoding="utf-8"
    )
    commit = {
        "event": "assistant_turn_truncated",
        "label": "live_session",
        "immutable": True,
        "late_self_asr_revises": False,
        "primary_cut_source": decision.source,
        "production_cut_text": decision.production_cut_text,
    }
    (out / "commit.json").write_text(
        json.dumps(commit, indent=2) + "\n", encoding="utf-8"
    )
    print(decision.production_cut_text)
    print(
        f"assistant-self-asr-finalize-cut: source={decision.source} "
        f"cut={decision.production_cut_text!r} drained_words="
        f"{len(drained.split()) if drained else 0} played_chunks={played_chunks}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
