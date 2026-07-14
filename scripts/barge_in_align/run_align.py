#!/usr/bin/env python3
"""CLI entry for live-session: align played clip → spoken_prefix JSON on stdout."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow `python run_align.py` from install dir without package install.
_HERE = Path(__file__).resolve().parent
_SCRIPTS = _HERE.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from barge_in_align.backend import align_played_clip, list_backends  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--backend", default="ctc_forced", choices=list_backends())
    p.add_argument("--wav", type=Path, required=False)
    p.add_argument("--intended", default="")
    p.add_argument("--played-ms", type=int, default=0)
    p.add_argument("--speed", type=float, default=1.0)
    p.add_argument("--preload", action="store_true", help="only warm CTC weights")
    p.add_argument("--out", type=Path, default=None)
    args = p.parse_args(argv)

    if args.preload:
        from barge_in_align.ctc_forced import preload

        preload()
        print(json.dumps({"ok": True, "preloaded": "ctc_forced"}))
        return 0

    if not args.intended:
        p.error("--intended is required unless --preload")

    cursor = align_played_clip(
        backend=args.backend,
        intended_text=args.intended,
        played_ms=args.played_ms,
        wav_path=args.wav,
        speed=args.speed,
    )
    payload = cursor.to_json()
    line = json.dumps(payload, ensure_ascii=False)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    # single-line for shell capture
    print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
