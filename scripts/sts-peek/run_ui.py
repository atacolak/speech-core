#!/usr/bin/env python3
"""Entry point for sts-peek observability UI (Track U).

Usage examples:

  # Offline mock (no daemons) — CI / local self-check
  python3 scripts/sts-peek/run_ui.py --mock --key-script 'b,sleep:0.2,q'

  # Attach to a Track L run_dir (writes control/barge.now on key barge)
  python3 scripts/sts-peek/run_ui.py --run-dir /path/to/session

  # Live watch with stream filters
  python3 scripts/sts-peek/run_ui.py \\
    --run-dir "$RUN_DIR" \\
    --stream-id laptop.live_mic \\
    --watch-mode debug

  # Replay a recorded watch.jsonl through speech-core-watch
  python3 scripts/sts-peek/run_ui.py \\
    --run-dir "$RUN_DIR" \\
    --replay-events "$RUN_DIR/watch.jsonl"
"""
from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from ui import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
