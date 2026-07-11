#!/usr/bin/env python3
"""CLI entry for sts-peek audio-only / headless sessions (Track L).

Examples:
  # Offline smoke (no daemons):
  python3 scripts/sts-peek/run_audio.py --mock-audio \\
    --intended-text "hello from assistant" \\
    --user-tts-text "wait stop" \\
    --barge-after-ms 200

  # Live (daemons up):
  SPEECH_OUT_WS_URL=ws://127.0.0.1:8788/ws/speech-out \\
  python3 scripts/sts-peek/run_audio.py \\
    --intended-text "..." --user-tts-text "wait stop" \\
    --barge-after-ms 1500

  # Human mode after barge (no user TTS):
  python3 scripts/sts-peek/run_audio.py --mock-audio --human \\
    --intended-text "..." --barge-after-ms 200

  # UI barge: omit --barge-after-ms and touch run_dir/control/barge.now
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import types
from pathlib import Path

# On-disk dir is hyphenated (scripts/sts-peek/); expose as sts_peek so relative
# imports in session.py / audio.py work (same pattern as barge-in-dual-asr.py).
_SCRIPT_DIR = Path(__file__).resolve().parent
_PKG_DIR = _SCRIPT_DIR
_PKG_NAME = "sts_peek"


def _ensure_package() -> None:
    if _PKG_NAME in sys.modules and getattr(sys.modules[_PKG_NAME], "__path__", None):
        return
    pkg = types.ModuleType(_PKG_NAME)
    pkg.__path__ = [str(_PKG_DIR)]  # type: ignore[attr-defined]
    pkg.__file__ = str(_PKG_DIR / "__init__.py")
    pkg.__package__ = _PKG_NAME
    sys.modules[_PKG_NAME] = pkg
    init_path = _PKG_DIR / "__init__.py"
    if init_path.is_file():
        code = compile(init_path.read_text(encoding="utf-8"), str(init_path), "exec")
        exec(code, pkg.__dict__)


def _load_pkg_module(name: str):
    _ensure_package()
    full_name = f"{_PKG_NAME}.{name}"
    if full_name in sys.modules:
        return sys.modules[full_name]
    path = _PKG_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(
        full_name,
        path,
        submodule_search_locations=[str(_PKG_DIR)],
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = _PKG_NAME
    sys.modules[full_name] = mod
    spec.loader.exec_module(mod)
    return mod


def main(argv: list[str] | None = None) -> int:
    session_mod = _load_pkg_module("session")
    audio_mod = _load_pkg_module("audio")
    Session = session_mod.Session
    SessionConfig = session_mod.SessionConfig
    default_run_base = session_mod.default_run_base
    AudioLayer = audio_mod.AudioLayer

    p = argparse.ArgumentParser(
        prog="sts-peek-audio",
        description="sts-peek Track L: live audio dual-TTS / human session layer",
    )
    p.add_argument(
        "--intended-text",
        default=os.environ.get(
            "STS_PEEK_INTENDED_TEXT",
            "This is the assistant speaking intended text for the sts-peek audio session.",
        ),
        help="Assistant TTS text (intended LLM utterance)",
    )
    p.add_argument(
        "--user-tts-text",
        default=os.environ.get("STS_PEEK_USER_TTS_TEXT", "wait stop"),
        help="User-role TTS text after barge (ignored with --human)",
    )
    p.add_argument(
        "--human",
        action="store_true",
        help="After barge, do not play user TTS; leave mic path for operator",
    )
    p.add_argument(
        "--mock-audio",
        action="store_true",
        help="Offline smoke: no speech-out daemon; synthetic play processes",
    )
    p.add_argument(
        "--barge-after-ms",
        type=int,
        default=None,
        help="Headless: auto-touch control/barge.now after N ms of assistant play",
    )
    p.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Explicit run_dir (default: $STS_PEEK_RUN_BASE/<session-id>)",
    )
    p.add_argument(
        "--run-base",
        type=Path,
        default=None,
        help="Base directory for auto session dirs",
    )
    p.add_argument(
        "--out-url",
        default=os.environ.get("SPEECH_OUT_WS_URL", "ws://127.0.0.1:8788/ws/speech-out"),
        help="speech-out websocket URL",
    )
    p.add_argument(
        "--core-url",
        default=os.environ.get("SPEECH_CORE_WS_URL", "ws://127.0.0.1:8765/ws/audio-ingress"),
        help="speech-core audio-ingress websocket URL",
    )
    p.add_argument(
        "--assistant-voice",
        default=os.environ.get("STS_PEEK_ASSISTANT_VOICE")
        or os.environ.get("SPEECH_OUT_VOICE", "M1"),
    )
    p.add_argument(
        "--user-voice",
        default=os.environ.get("STS_PEEK_USER_VOICE")
        or os.environ.get("SPEECH_OUT_USER_VOICE", "F1"),
        help="Distinct voice for user-role TTS when Supertonic supports it",
    )
    p.add_argument("--lang", default=os.environ.get("SPEECH_OUT_LANG", "en"))
    p.add_argument("--steps", type=int, default=int(os.environ.get("SPEECH_OUT_STEPS", "5")))
    p.add_argument(
        "--speed", type=float, default=float(os.environ.get("SPEECH_OUT_SPEED", "1.30"))
    )
    p.add_argument(
        "--play-command",
        default=os.environ.get("SPEECH_OUT_PLAY_COMMAND", "pw-play"),
        help="Local play command (pw-play); falls back to 'true' if missing",
    )
    p.add_argument(
        "--record-synthesis",
        action="store_true",
        help="Optional: launch record_client to capture speech-out WAV chunks",
    )
    p.add_argument(
        "--stream-session-id",
        default=None,
        help="Override stream_session_id (default: auto sts-peek-...)",
    )
    p.add_argument(
        "--probe-only",
        action="store_true",
        help="Create run_dir + probe readiness; do not speak",
    )
    p.add_argument(
        "--print-layout",
        action="store_true",
        help="Print run_dir contract JSON and exit (no audio)",
    )

    args = p.parse_args(argv)

    if args.print_layout:
        layout = {
            "run_dir_layout": {
                "params.env": "shell-sourceable knobs",
                "session.json": "ids + paths + mode",
                "assistant_intended.txt": "assistant TTS text",
                "user_text.txt": "user-role TTS text",
                "events.jsonl": "harness-local session events",
                "control/barge.now": "UI touches to barge",
                "pids/assistant_play.pid": "active assistant speech-out play PID",
                "pids/user_play.pid": "active user speech-out play PID",
                "speech_out/*.jsonl": "speech-out client event streams",
                "cancel/assistant_cancel.json": "cancel timestamp + reason",
                "record/": "optional synthesis capture",
                "mic/": "human-mode stub + mic launch helper",
            },
            "stream_ids": {
                "user": "laptop.live_mic (or SPEECH_CORE_STREAM_ID)",
                "assistant_self_asr": "assistant.self_asr",
            },
            "barge_contract": "touch run_dir/control/barge.now",
        }
        print(json.dumps(layout, indent=2))
        return 0

    config = SessionConfig(
        intended_text=args.intended_text,
        user_text=args.user_tts_text,
        human_mode=bool(args.human),
        mock_audio=bool(args.mock_audio),
        record_synthesis=bool(args.record_synthesis),
        barge_after_ms=args.barge_after_ms,
        core_ws=args.core_url,
        out_ws=args.out_url,
        assistant_voice=args.assistant_voice,
        user_voice=args.user_voice,
        lang=args.lang,
        steps=args.steps,
        speed=args.speed,
        play_command=args.play_command,
        stream_session_id=args.stream_session_id,
    )

    session = Session(
        config,
        run_dir=args.out_dir,
        run_base=args.run_base or default_run_base(),
    )
    paths = session.create()
    print(f"sts-peek audio run_dir: {paths.run_dir}", file=sys.stderr)
    print(f"stream_session_id: {session.stream_session_id}", file=sys.stderr)
    print(f"barge control: {paths.barge_now}", file=sys.stderr)

    audio = AudioLayer(session)

    if args.probe_only:
        probes = audio.probe()
        (paths.run_dir / "probes.json").write_text(
            json.dumps(probes, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps(probes, indent=2))
        if not probes.get("ready") and not config.mock_audio:
            print(
                "not ready (use --mock-audio or start speech-out daemon)",
                file=sys.stderr,
            )
            return 2
        return 0

    try:
        result = audio.run_sequence()
    except RuntimeError as exc:
        session.emit("audio_error", error=str(exc))
        print(f"error: {exc}", file=sys.stderr)
        print(
            f"(session artifacts still under {paths.run_dir})",
            file=sys.stderr,
        )
        return 2
    except KeyboardInterrupt:
        session.emit("interrupted")
        audio.cleanup()
        print("interrupted", file=sys.stderr)
        return 130
    finally:
        audio.cleanup()

    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
