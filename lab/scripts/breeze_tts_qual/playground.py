#!/usr/bin/env python3
"""not on the voicecat path.

Lab Gradio playground over BreezeEngine. not a pin swap.
"""

from __future__ import annotations

import argparse
import atexit
import os
import signal
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path
from typing import Any, Callable

if __package__ is None:
    _scripts = Path(__file__).resolve().parent.parent
    if str(_scripts) not in sys.path:
        sys.path.insert(0, str(_scripts))

from breeze_tts_qual import park_leftover
from breeze_tts_qual.configs import CONFIGS, EngineConfig

_DEFAULT_QUAL_ROOT = (
    Path.home() / ".cache" / "speech-out" / "breeze-tts-qual-sc-breeze-hybrid-81p"
)

DEFAULT_PRESET = "E2"
SAFE_PRESETS = ("E2", "A", "C2", "E1", "B_depth")
KILLED_PRESETS = (
    "C3",
    "C4",
    "D",
    "E3",
    "E4",
    "E5",
    "B_backbone_prefill",
)


def _config_by_name() -> dict[str, EngineConfig]:
    return {c.name: c for c in CONFIGS}


def _default_engine_factory(config: EngineConfig, *, qual_root: Path) -> Any:
    from breeze_tts_qual.engine import BreezeEngine, OfficialBackend, checkpoint_for_precision

    ckpt_dir = checkpoint_for_precision(qual_root, config.precision)
    backend = OfficialBackend(
        config,
        device="cuda:0",
        qual_root=qual_root,
        ckpt_dir=ckpt_dir,
    )
    return BreezeEngine(config, ckpt_dir=ckpt_dir, backend=backend)


class PlaygroundSession:
    def __init__(
        self,
        qual_root: Path,
        *,
        engine_factory: Callable[..., Any] | None = None,
        default_preset: str = DEFAULT_PRESET,
    ) -> None:
        self.qual_root = Path(qual_root)
        self.engine_factory = engine_factory or _default_engine_factory
        self.default_preset = default_preset
        self.engine: Any | None = None
        self.loaded_preset: str | None = None
        self._parked = False
        self._lock = threading.RLock()

    def start(self, preset: str | None = None) -> None:
        name = preset or self.default_preset
        park_leftover.park()
        self._parked = True
        try:
            self._load(name)
        except Exception:
            self.close()
            raise

    def _load(self, name: str) -> None:
        config = _config_by_name()[name]
        self.engine = self.engine_factory(config, qual_root=self.qual_root)
        self.loaded_preset = name

    def ensure_preset(self, name: str) -> None:
        if self.engine is not None and self.loaded_preset == name:
            return
        engine = self.engine
        self.engine = None
        if engine is not None:
            closer = getattr(engine, "close", None)
            if closer is not None:
                closer()
        self._load(name)


    def resolve_preset(
        self,
        safe_preset: str,
        killed_preset: str | None,
        confirm_killed: bool,
    ) -> str:
        if killed_preset:
            if killed_preset not in KILLED_PRESETS:
                raise ValueError(f"unknown killed preset: {killed_preset}")
            if not confirm_killed:
                raise ValueError(
                    "killed/unsafe preset requires an explicit confirm checkbox"
                )
            return killed_preset
        return safe_preset

    def generate(
        self,
        *,
        text: str,
        instruction: str,
        cfg_scale: float,
        seed: int | float,
        clone_audio: Any,
        clone_text: str,
        safe_preset: str,
        killed_preset: str | None,
        confirm_killed: bool,
    ) -> tuple[tuple[int, Any], str]:
        import numpy as np

        from breeze_tts_qual.metrics import first_nonsilent_s

        with self._lock:
            preset = self.resolve_preset(safe_preset, killed_preset, confirm_killed)
            self.ensure_preset(preset)
            engine = self.engine
            if engine is None:
                raise RuntimeError("engine is not loaded")
            ref_audio = _clone_audio_path(clone_audio)
            ref_text = (clone_text or "").strip()
            if not ref_audio or not ref_text:
                raise ValueError("clone audio and transcript are required together")
            t0 = time.perf_counter()
            chunks = list(
                engine.synthesize(
                    text=text,
                    reference_audio=ref_audio,
                    reference_text=ref_text,
                    instruction=instruction,
                    seed=int(seed),
                    cfg_scale=float(cfg_scale),
                )
            )
            wall_s = time.perf_counter() - t0
            sample_rate = 24000
            if chunks:
                sample_rate = int(chunks[0].sample_rate or sample_rate)
            pcm = b"".join(chunk.pcm for chunk in chunks)
            first_pcm_s = None
            for chunk in chunks:
                if int(chunk.n_samples) > 0:
                    first_pcm_s = float(chunk.t_rel_s)
                    timing = dict(chunk.timing or {})
                    if timing.get("first_pcm") is not None:
                        first_pcm_s = float(timing["first_pcm"])
                    break
            first_nonsilent, _ = first_nonsilent_s(
                [(float(chunk.t_rel_s), chunk.pcm) for chunk in chunks],
                sample_rate=sample_rate,
            )
            audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32767.0
            status = (
                f"leftover parked; loaded {preset}; "
                f"first_pcm={_fmt_s(first_pcm_s)} "
                f"first_nonsilent={_fmt_s(first_nonsilent)} "
                f"wall={_fmt_s(wall_s)} "
                f"peak_vram={_peak_vram_status()} "
                f"gpu={_gpu_occupant()}"
            )
            return (sample_rate, audio), status



    def close(self) -> None:
        with self._lock:
            engine = self.engine
            self.engine = None
            self.loaded_preset = None
            try:
                if engine is not None:
                    closer = getattr(engine, "close", None)
                    if closer is not None:
                        closer()
            finally:
                if self._parked:
                    park_leftover.restore()
                    self._parked = False





def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Lab Breeze TTS Gradio playground (not on the voicecat path)."
    )
    parser.add_argument(
        "--qual-root",
        default=os.environ.get("QUAL_ROOT", str(_DEFAULT_QUAL_ROOT)),
        type=Path,
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7860)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Construct Gradio Blocks without parking leftover or loading the 3B.",
    )
    return parser.parse_args(argv)


def launch_kwargs(*, host: str, port: int) -> dict[str, Any]:
    return {
        "server_name": host,
        "server_port": port,
        "share": False,
        "inbrowser": False,
    }



def _default_clone(qual_root: Path) -> tuple[str | None, str]:
    wav = qual_root / "fixtures" / "ref.wav"
    txt = qual_root / "fixtures" / "ref.txt"
    transcript = ""
    if txt.is_file():
        transcript = txt.read_text(encoding="utf-8").strip()
    return (str(wav) if wav.is_file() else None, transcript)


def _clone_audio_path(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        path = value.get("path") or value.get("name") or ""
        return str(path)
    return str(value)


def _fmt_s(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):.3f}s"


def _peak_vram_status() -> str:
    try:
        import torch

        if not torch.cuda.is_available():
            return "n/a"
        peak = torch.cuda.max_memory_allocated() / (1024 ** 3)
        return f"{peak:.2f}GiB"
    except Exception:
        return "n/a"


def _gpu_occupant() -> str:
    try:
        import subprocess

        result = subprocess.run(
            ["systemctl", "--user", "is-active", park_leftover.UNIT],
            capture_output=True,
            text=True,
            check=False,
        )
        leftover = result.stdout.strip() or "unknown"
    except Exception:
        leftover = "unknown"
    return f"leftover={leftover}"



def build_interface(qual_root: Path, session: PlaygroundSession | None = None):
    import gradio as gr

    from breeze_tts_qual.utterances import DIRECTIONS, SHORT

    clone_wav, clone_txt = _default_clone(qual_root)

    with gr.Blocks(title="Breeze TTS lab playground") as demo:
        gr.Markdown(
            "Lab-only Breeze playground. not on the voicecat path. "
            "Live mouth stays leftover qwentts. not a pin swap."
        )
        safe = gr.Radio(
            choices=[
                ("E2 — SHELF winner (official bf16, fast=[depth,codec])", "E2"),
                ("A — bf16 eager baseline", "A"),
                ("C2 — hybrid, slower than A", "C2"),
                ("E1 — bf16 depth graph", "E1"),
                ("B_depth", "B_depth"),
            ],
            value="E2",
            label="Preset",
        )
        text = gr.Textbox(value=SHORT[0], lines=4, label="Text")
        instruction = gr.Textbox(
            value="Speak clearly and naturally.",
            lines=2,
            label="How it speaks",
        )
        with gr.Row():
            default_chip = gr.Button("Speak clearly and naturally.", size="sm")
            direction_chips = [gr.Button(phrase, size="sm") for phrase in DIRECTIONS]
        cfg = gr.Slider(
            minimum=1.0,
            maximum=8.0,
            value=1.0,
            step=0.5,
            label="cfg_scale",
            info="core bench used 1.0; direction test used 4.0",
        )
        seed = gr.Number(value=42, precision=0, label="seed")
        clone_audio = gr.Audio(
            value=clone_wav,
            type="filepath",
            sources=["upload"],
            label="Clone audio",
        )
        clone_text = gr.Textbox(
            value=clone_txt,
            lines=2,
            label="Clone transcript",
            info="Clone audio and transcript are required together.",
        )
        with gr.Accordion("Advanced — killed / unsafe presets", open=False):
            gr.Markdown(
                "Warning: these configs were killed or unsafe in the hybrid "
                "qualification. Generating on them is allowed only with the "
                "confirm checkbox below. Do not treat them as recommended."
            )
            killed = gr.Radio(
                choices=[
                    ("C3 — killed/unsafe", "C3"),
                    ("C4 — killed/unsafe", "C4"),
                    ("D — killed/unsafe", "D"),
                    ("E3 — killed/unsafe", "E3"),
                    ("E4 — killed/unsafe", "E4"),
                    ("E5 — killed/unsafe", "E5"),
                    ("B_backbone_prefill — killed/unsafe", "B_backbone_prefill"),
                ],
                value=None,
                label="Killed preset",
            )
            confirm = gr.Checkbox(
                value=False,
                label="I confirm generating on a killed/unsafe preset",
            )
        generate = gr.Button("Generate", variant="primary")
        audio_out = gr.Audio(
            label="Output",
            type="numpy",
            interactive=False,
            autoplay=True,
        )
        download = gr.File(label="Download wav")
        status_value = "dry-run; leftover not parked; 3B not loaded"
        if session is not None:
            parked = "parked" if session._parked else "not parked"
            loaded = session.loaded_preset or "none"
            status_value = (
                f"leftover {parked}; loaded {loaded}; gpu={_gpu_occupant()}"
            )
        status = gr.Textbox(value=status_value, label="Status", interactive=False)

        default_chip.click(
            lambda: "Speak clearly and naturally.",
            outputs=instruction,
        )
        for chip in direction_chips:
            chip.click(lambda phrase=chip.value: phrase, outputs=instruction)

        if session is not None:
            def _on_generate(
                text_value,
                instruction_value,
                cfg_value,
                seed_value,
                clone_audio_value,
                clone_text_value,
                safe_preset,
                killed_preset,
                confirm_killed,
            ):
                try:
                    audio, line = session.generate(
                        text=text_value,
                        instruction=instruction_value,
                        cfg_scale=cfg_value,
                        seed=seed_value,
                        clone_audio=clone_audio_value,
                        clone_text=clone_text_value,
                        safe_preset=safe_preset,
                        killed_preset=killed_preset,
                        confirm_killed=bool(confirm_killed),
                    )
                    return audio, _write_download_wav(audio), line
                except Exception as exc:
                    return None, None, f"error: {exc}"

            generate.click(
                _on_generate,
                inputs=[
                    text,
                    instruction,
                    cfg,
                    seed,
                    clone_audio,
                    clone_text,
                    safe,
                    killed,
                    confirm,
                ],
                outputs=[audio_out, download, status],
            )
    return demo


def _write_download_wav(audio: tuple[int, Any]) -> str:
    import numpy as np

    sample_rate, samples = audio
    pcm = (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
    handle = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    with handle:
        with wave.open(handle, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(int(sample_rate))
            wav_file.writeframes(pcm)
    return handle.name


def _serve(session: PlaygroundSession, demo: Any, *, host: str, port: int) -> int:
    def _shutdown(*_args: Any) -> None:
        session.close()

    atexit.register(_shutdown)
    signal.signal(signal.SIGINT, lambda *_: (_shutdown(), sys.exit(0)))
    signal.signal(signal.SIGTERM, lambda *_: (_shutdown(), sys.exit(0)))
    try:
        url = f"http://{host}:{port}"
        print(url, flush=True)
        demo.launch(**launch_kwargs(host=host, port=port))
        return 0
    except KeyboardInterrupt:
        return 0
    finally:
        _shutdown()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    qual_root = Path(args.qual_root)
    host = args.host
    if host not in {"127.0.0.1", "localhost", "::1"}:
        host = "127.0.0.1"
    if args.dry_run:
        build_interface(qual_root)
        return 0

    session = PlaygroundSession(qual_root)
    atexit.register(session.close)
    signal.signal(signal.SIGINT, lambda *_: (session.close(), sys.exit(0)))
    signal.signal(signal.SIGTERM, lambda *_: (session.close(), sys.exit(0)))
    try:
        session.start()
        demo = build_interface(qual_root, session=session)
        return _serve(session, demo, host=host, port=int(args.port))
    except KeyboardInterrupt:
        return 0
    finally:
        session.close()








if __name__ == "__main__":
    raise SystemExit(main())

