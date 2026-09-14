#!/usr/bin/env python3
"""not on the voicecat path.

Serve the React lab shell + FastAPI on loopback :7860.
Netbird socat already forwards wt0:7860 -> 127.0.0.1:7860
(http://sfub.netbird.selfhosted:7860). Do not bind :8765 (speech-core daemon).

E2 starts UNLOADED. GPU residency is an explicit Load/Unload control.
--autoload is a developer flag only. not a pin swap. not a second 3B.
"""

from __future__ import annotations

import argparse
import atexit
import signal
import sys

import uvicorn

from tts.lab.backend.app import create_app
from tts.lab.backend.runtime.leftover import SystemdLeftover
from tts.lab.backend.runtime.manager import E2RuntimeManager
from tts.lab.backend.runtime.vram import NvidiaSmiVramProbe


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="TTS lab React+FastAPI on loopback")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7860)
    parser.add_argument(
        "--autoload",
        action="store_true",
        help="Load E2 at startup. Default is unloaded.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Deprecated alias for default unloaded startup.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    leftover = SystemdLeftover()
    runtime = E2RuntimeManager(
        leftover=leftover,
        vram=NvidiaSmiVramProbe(),
        autoload=bool(args.autoload) and not args.dry_run,
    )
    app = create_app(
        engine=None,
        leftover_parked=leftover.is_parked(),
        e2_runtime=runtime,
        seed_samples=True,
    )

    def _shutdown(*_args: object) -> None:
        try:
            runtime.unload(timeout=15)
        except Exception:
            pass

    atexit.register(_shutdown)
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    print(
        f"tts lab on {args.host}:{args.port}; E2 {runtime.status().state}"
        f"{' (autoload)' if args.autoload else ' (unloaded until Load)'}",
        flush=True,
    )
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
