"""Live dual-Nemotron barge-in wiring (fails closed with clear steps).

Does not change speech-core-protocol or daemon event vocabulary.
Prefers real wiring when speech-out + speech-core are reachable; otherwise
emits a checklist and exits non-zero unless --allow-live-stub.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


DEFAULT_CORE_WS = os.environ.get(
    "SPEECH_CORE_WS_URL", "ws://127.0.0.1:8765/ws/audio-ingress"
)
DEFAULT_OUT_WS = os.environ.get(
    "SPEECH_OUT_WS_URL", "ws://127.0.0.1:8788/ws/speech-out"
)
ASSISTANT_STREAM_ID = "assistant.self_asr"


@dataclass
class LiveProbe:
    name: str
    ok: bool
    detail: str


def _host_port_from_ws(url: str) -> tuple[str, int] | None:
    try:
        parsed = urlparse(url)
        host = parsed.hostname or "127.0.0.1"
        if parsed.port is not None:
            port = parsed.port
        elif parsed.scheme in ("wss", "https"):
            port = 443
        else:
            port = 80
        return host, port
    except Exception:
        return None


def probe_tcp(url: str, timeout: float = 0.4) -> LiveProbe:
    hp = _host_port_from_ws(url)
    if hp is None:
        return LiveProbe("tcp", False, f"unparseable url: {url}")
    host, port = hp
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return LiveProbe("tcp", True, f"{host}:{port} open")
    except OSError as exc:
        return LiveProbe("tcp", False, f"{host}:{port} closed ({exc})")


def which_bin(name: str, extra_dirs: list[Path] | None = None) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    for d in extra_dirs or []:
        cand = d / name
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return None


def repo_bin_dirs(repo_root: Path) -> list[Path]:
    return [
        repo_root / "target" / "debug",
        repo_root / "target" / "release",
        Path.home() / ".local" / "bin",
    ]


def probe_environment(
    repo_root: Path,
    *,
    core_ws: str = DEFAULT_CORE_WS,
    out_ws: str = DEFAULT_OUT_WS,
) -> dict[str, Any]:
    bins = repo_bin_dirs(repo_root)
    probes: dict[str, Any] = {
        "core_ws": core_ws,
        "out_ws": out_ws,
        "assistant_stream_id": ASSISTANT_STREAM_ID,
        "binaries": {},
        "reachability": {},
        "ready_for_live": False,
    }

    for name in (
        "speech-out",
        "speech-core-file-adapter",
        "speech-core-watch",
        "speech-core-mic-adapter",
    ):
        path = which_bin(name, bins)
        probes["binaries"][name] = {"path": path, "ok": path is not None}

    core_tcp = probe_tcp(core_ws)
    out_tcp = probe_tcp(out_ws)
    probes["reachability"]["speech_core"] = {
        "ok": core_tcp.ok,
        "detail": core_tcp.detail,
        "url": core_ws,
    }
    probes["reachability"]["speech_out"] = {
        "ok": out_tcp.ok,
        "detail": out_tcp.detail,
        "url": out_ws,
    }

    have_out = probes["binaries"]["speech-out"]["ok"]
    have_file = probes["binaries"]["speech-core-file-adapter"]["ok"]
    probes["ready_for_live"] = bool(
        have_out and have_file and core_tcp.ok and out_tcp.ok
    )
    return probes


def live_wiring_checklist(probes: dict[str, Any]) -> dict[str, Any]:
    return {
        "label": "eval_only",
        "mode": "live",
        "status": "ready" if probes.get("ready_for_live") else "not_ready",
        "topology": {
            "nemotron_a": {
                "role": "user_mic",
                "stream_id_example": "laptop.live_mic",
                "path": "existing speech-in (do not break)",
                "instance": "Nemotron A (separate)",
            },
            "nemotron_b": {
                "role": "assistant_self_asr",
                "stream_id": ASSISTANT_STREAM_ID,
                "path": (
                    "record-only speech-out WS client → PCM WAV → "
                    "speech-core-file-adapter → second speech-core session"
                ),
                "instance": "Nemotron B (separate process/stream)",
            },
            "shared_worker": False,
            "contract_rev": 4,
        },
        "sequence": [
            "Speak / play assistant text via speech-out (server PCM chunks on WS).",
            "Record-only client captures binary WAV chunks + text events (no play).",
            "On first user alnum token (Nemotron A): pause/cancel playback immediately.",
            "Feed already-emitted PCM into Nemotron B (file-adapter, stream_id=assistant.self_asr).",
            "Drain B during user speech window (hidden inside user turn).",
            "At user transcript_committed: force-align B text to intended; primary cut.",
            "If drain incomplete: fallback last_pos + pad_words(~2) from intended.",
            "Commit truncated assistant message once (harness log / commit.json).",
        ],
        "probes": probes,
        "wiring_steps": [
            {
                "id": "start_speech_core",
                "cmd": "scripts/start-speech-core-daemon.sh",
                "expect": f"TCP open on {probes.get('core_ws')}",
            },
            {
                "id": "start_speech_out",
                "cmd": (
                    "cargo run -p speech-out -- daemon --bind 0.0.0.0:8788 "
                    "# or installed speech-out daemon"
                ),
                "expect": f"TCP open on {probes.get('out_ws')}",
            },
            {
                "id": "build_adapters",
                "cmd": (
                    "cargo build -p speech-out -p speech-core-file-adapter "
                    "-p speech-core-watch -p speech-core-mic-adapter"
                ),
            },
            {
                "id": "record_only_client",
                "desc": (
                    "Second WS client on speech-out: subscribe, write binary WAV "
                    "chunks under run_dir/assistant_pcm/ while speak is active. "
                    "Implemented as scripts/barge-in-dual-asr/record_client.py "
                    "(stdlib websockets optional; see live mode)."
                ),
            },
            {
                "id": "feed_nemotron_b",
                "cmd": (
                    "speech-core-file-adapter --url $SPEECH_CORE_WS_URL "
                    f"--stream-id {ASSISTANT_STREAM_ID} "
                    "--stream-session-id <session> --adapter-id assistant.self_asr "
                    "--frame-ms 20 --realtime <captured.wav>"
                ),
            },
            {
                "id": "user_path_unchanged",
                "desc": (
                    "Keep Nemotron A on existing mic path "
                    "(scripts/speech-out-live-session.sh / speech-core-mic-adapter). "
                    "Do not share one serialized transcribe worker with B."
                ),
            },
            {
                "id": "coordinate_cut",
                "desc": (
                    "Coordinator watches user events for first alnum → pause; "
                    "on transcript_committed reads B committed/partial text; "
                    "production_cut() → commit.json."
                ),
            },
        ],
        "forbidden": [
            "speech-core-protocol shared event vocabulary changes",
            "speech-core-daemon shared event vocabulary changes",
            "collapsing A and B onto one Nemotron worker (v1)",
            "revising commit after late self-ASR",
        ],
        "fallback": {
            "when": "drain incomplete / lag / dual-stream starve",
            "rule": "intended prefix at last known alignment pos + pad_words(~2)",
        },
    }


def try_record_client_import() -> bool:
    """True if a websocket client library is available for live record mode."""
    try:
        import websockets  # type: ignore  # noqa: F401

        return True
    except Exception:
        pass
    try:
        import websocket  # type: ignore  # noqa: F401

        return True
    except Exception:
        return False


def write_live_artifacts(
    out_dir: Path,
    *,
    probes: dict[str, Any],
    allow_stub: bool,
) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    checklist = live_wiring_checklist(probes)
    checklist["websocket_client_available"] = try_record_client_import()
    checklist["allow_live_stub"] = allow_stub
    checklist["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")

    (out_dir / "live_wiring.json").write_text(
        json.dumps(checklist, indent=2) + "\n", encoding="utf-8"
    )
    (out_dir / "probes.json").write_text(
        json.dumps(probes, indent=2) + "\n", encoding="utf-8"
    )

    readme = f"""barge-in dual-Nemotron LIVE wiring
=================================

status: {checklist['status']}
ready_for_live: {probes.get('ready_for_live')}
out_dir: {out_dir}

speech-core: {probes['reachability']['speech_core']}
speech-out:  {probes['reachability']['speech_out']}

Binaries:
{json.dumps(probes['binaries'], indent=2)}

If not ready: start daemons (see live_wiring.json wiring_steps), rebuild
adapters, re-run:

  python3 scripts/barge-in-dual-asr.py --mode live --out-dir {out_dir}

Dry-run (always works offline):

  python3 scripts/barge-in-dual-asr.py --mode dry-run

See docs/barge-in-dual-asr.md
"""
    (out_dir / "README-run.txt").write_text(readme, encoding="utf-8")
    return checklist


def run_live_coordinator_stub(
    repo_root: Path,
    out_dir: Path,
    *,
    core_ws: str,
    out_ws: str,
    allow_stub: bool,
    intended_text: str,
) -> tuple[int, dict[str, Any]]:
    """Probe live environment; fail closed unless ready or allow_stub.

    When ready_for_live, still writes an executable wiring plan rather than
    silently inventing a partial dual-daemon session (no protocol changes).
    Full end-to-end live loop remains operator-driven via the checklist +
    record_client/file-adapter commands.
    """
    probes = probe_environment(repo_root, core_ws=core_ws, out_ws=out_ws)
    checklist = write_live_artifacts(out_dir, probes=probes, allow_stub=allow_stub)

    # Always persist intended text for operator convenience.
    (out_dir / "assistant_intended.txt").write_text(
        (intended_text.rstrip() + "\n") if intended_text else "\n",
        encoding="utf-8",
    )

    if probes.get("ready_for_live"):
        # Emit a concrete operator runbook when daemons are up.
        runbook = {
            "label": "eval_only",
            "status": "daemons_reachable_operator_runbook",
            "note": (
                "Daemons reachable. Full automatic dual-session orchestration is "
                "intentionally harness-driven: use record_client + file-adapter "
                "with stream_id=assistant.self_asr while user path stays on mic."
            ),
            "assistant_stream_id": ASSISTANT_STREAM_ID,
            "steps": checklist["sequence"],
            "core_ws": core_ws,
            "out_ws": out_ws,
        }
        (out_dir / "live_runbook.json").write_text(
            json.dumps(runbook, indent=2) + "\n", encoding="utf-8"
        )
        return 0, checklist

    if allow_stub:
        checklist["status"] = "live_stub_not_ready"
        (out_dir / "live_wiring.json").write_text(
            json.dumps(checklist, indent=2) + "\n", encoding="utf-8"
        )
        return 0, checklist

    return 2, checklist
