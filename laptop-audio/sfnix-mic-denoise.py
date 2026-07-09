#!/usr/bin/env python3
"""
EXPERIMENTAL — DPDFNet2 denoise wrapper for sfnix mic ingest.

Captures microphone audio via sounddevice, denoises with dpdfnet.StreamEnhancer,
and forwards enhanced PCM frames to speech-core-daemon via WebSocket.

This is a PROXY that replaces speech-core-mic-adapter for testing.
It implements a minimal subset of the speech-core WebSocket protocol.

Usage:
  # On sfnix (NixOS):
  nix-shell -p python312 stdenv.cc.cc.lib zlib portaudio --run '
    export LD_LIBRARY_PATH=$(for f in $NIX_LDFLAGS; do case "$f" in -L/*) dir="${f#-L}"; [ -d "$dir" ] && echo -n "$dir:"; esac; done)
    source ~/dpdfnet-env/bin/activate
    python3 scripts/experimental/sfnix-mic-denoise.py [options]
  '

Options:
  --model NAME         DPDFNet model name (default: dpdfnet2)
  --device NAME        Input device name substring
  --list-devices       List audio input devices and exit
  --url URL            WebSocket URL (default: ws://100.68.60.39:8765/ws/audio-ingress)
  --stream-id ID       Stream identifier (default: sfnix.denoised_mic)
  --dry-run            Don't connect to WebSocket, just save to WAV
  --save-wav PATH      Save raw+denoised WAV for comparison

Requires: pip install dpdfnet sounddevice
"""
import argparse
import json
import os
import sys
import time
import struct
import uuid
from pathlib import Path

import numpy as np

# ── Constants ────────────────────────────────────────────────────────────────
DEFAULT_URL = "ws://100.68.60.39:8765/ws/audio-ingress"
DEFAULT_STREAM_ID = "sfnix.denoised_mic"
DEFAULT_MODEL = "dpdfnet2"
SR = 16000
FRAME_MS = 20
FRAME_SAMPLES = int(SR * FRAME_MS / 1000)  # 320
CHANNELS = 1
FORMAT = "pcm_s16le"


def patch_sounddevice_portaudio():
    """Help python-sounddevice find PortAudio on NixOS.

    sounddevice uses ctypes.util.find_library(), which often ignores
    LD_LIBRARY_PATH on NixOS even when libportaudio is present. Patch it before
    importing sounddevice so it can load the exact library path.
    """
    import ctypes.util
    paths = []
    env = os.environ.get("SPEECH_CORE_PORTAUDIO_LIB")
    if env:
        paths.append(env)
    for directory in os.environ.get("LD_LIBRARY_PATH", "").split(":"):
        if not directory:
            continue
        for name in ("libportaudio.so", "libportaudio.so.2"):
            candidate = os.path.join(directory, name)
            if os.path.exists(candidate):
                paths.append(candidate)
    if not paths:
        return
    original = ctypes.util.find_library
    selected = paths[0]
    def find_library(name):
        if name in ("portaudio", "libportaudio", "libportaudio.so", "libportaudio.so.2"):
            return selected
        return original(name)
    ctypes.util.find_library = find_library

# ─────────────────────────────────────────────────────────────────────────────


def list_devices():
    """List available audio input devices."""
    patch_sounddevice_portaudio()
    import sounddevice as sd
    print(f"{'Index':>6}  {'Name':<50}  {'Channels':>8}  {'SR':>8}")
    print("-" * 80)
    for i, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] > 0:
            print(
                f"{i:>6}  {dev['name']:<50}  {dev['max_input_channels']:>8}  {int(dev['default_samplerate']):>8}"
            )


def parse_args():
    parser = argparse.ArgumentParser(
        description="DPDFNet2 denoise wrapper for sfnix mic ingest (EXPERIMENTAL)"
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Model name (default: {DEFAULT_MODEL})")
    parser.add_argument("--device", type=int, default=None, help="Input device index")
    parser.add_argument("--list-devices", action="store_true", help="List input devices and exit")
    parser.add_argument("--url", default=DEFAULT_URL, help=f"WebSocket URL (default: {DEFAULT_URL})")
    parser.add_argument("--stream-id", default=DEFAULT_STREAM_ID, help=f"Stream ID (default: {DEFAULT_STREAM_ID})")
    parser.add_argument("--stream-session-id", default=None, help="Stream session id (default: UUID)")
    parser.add_argument("--adapter-id", default=None, help="Adapter id (default: generated sfnix.dpdfnet.*)")
    parser.add_argument("--dry-run", action="store_true", help="No WebSocket, just save WAV")
    parser.add_argument("--save-wav", type=Path, default=None, help="Save raw+denoised WAV pair")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")
    return parser.parse_args()


def build_hello(stream_id: str, adapter_id: str, session_id: str) -> str:
    return json.dumps({
        "type": "hello",
        "adapter_id": adapter_id,
        "stream_id": stream_id,
        "stream_session_id": session_id,
        "source_kind": "mic",
        "sample_rate_hz": SR,
        "channels": CHANNELS,
        "format": "pcm_s16le",
        "timestamp_provenance": {
            "adapter_clock_id": f"host:{os.uname().nodename}:monotonic",
            "adapter_clock_domain": "host_monotonic",
            "timestamp_quality": "unknown",
            "timestamp_semantics": "first_sample",
            "clock_comparability": "uncalibrated",
            "estimated_daemon_offset_ns": None,
            "estimated_offset_uncertainty_ns": None,
        },
        "adapter_hello_send_mono_ns": time.time_ns(),
    })


def encode_audio_frame(
    stream_id: str,
    session_id: str,
    adapter_id: str,
    seq: int,
    samples: np.ndarray,
    source_sample_start: int,
) -> bytes:
    """Encode one AudioFrame in speech-core binary protocol."""
    ns = time.time_ns()
    sample_count = len(samples)
    payload = struct.pack(f"<{sample_count}h", *(samples * 32767).astype(np.int16).tolist())

    header = {
        "AudioFrameHeader": {
            "stream_id": stream_id,
            "stream_session_id": session_id,
            "adapter_id": adapter_id,
            "source_kind": "mic",
            "seq": seq,
            "format": "pcm_s16le",
            "sample_rate_hz": SR,
            "channels": 1,
            "source_sample_start": source_sample_start,
            "sample_count": sample_count,
            "source_capture_mono_ns": ns,
            "adapter_send_mono_ns": ns,
            "timestamp_provenance": {
                "adapter_clock_id": f"host:{os.uname().nodename}:monotonic",
                "adapter_clock_domain": "host_monotonic",
                "timestamp_quality": "unknown",
                "timestamp_semantics": "first_sample",
                "clock_comparability": "uncalibrated",
                "estimated_daemon_offset_ns": None,
                "estimated_offset_uncertainty_ns": None,
            },
        }
    }

    # Encode as JSON header + binary payload
    header_json = json.dumps(header, separators=(",", ":")).encode("utf-8")
    body = struct.pack("<I", len(header_json)) + header_json + payload
    return body


class WavSaver:
    """Simple WAV writer for raw/denoised comparison."""

    def __init__(self, path: Path, label: str, sr: int = SR):
        self.path = path.parent / f"{path.stem}_{label}{path.suffix}"
        self.sr = sr
        self.file = None
        self.data_bytes = 0

    def open(self):
        self.file = open(self.path, "wb")
        # Write placeholder header
        self.file.write(b"RIFF\x00\x00\x00\x00WAVEfmt ")
        self.file.write(struct.pack("<IHHIIHH", 16, 1, 1, self.sr, self.sr * 2, 2, 16))
        self.file.write(b"data\x00\x00\x00\x00")

    def write(self, samples: np.ndarray):
        if self.file is None:
            return
        data = struct.pack(f"<{len(samples)}h", *(samples * 32767).astype(np.int16).tolist())
        self.file.write(data)
        self.data_bytes += len(data)

    def close(self):
        if self.file is None:
            return
        # Fix RIFF and data sizes
        self.file.seek(4)
        self.file.write(struct.pack("<I", 36 + self.data_bytes))
        self.file.seek(40)
        self.file.write(struct.pack("<I", self.data_bytes))
        self.file.close()

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *args):
        self.close()
        print(f"  Saved: {self.path}")


def main():
    args = parse_args()

    if args.list_devices:
        patch_sounddevice_portaudio()
        list_devices()
        return

    patch_sounddevice_portaudio()
    import sounddevice as sd
    import dpdfnet

    adapter_id = args.adapter_id or f"sfnix.dpdfnet.{uuid.uuid4().hex[:8]}"
    session_id = args.stream_session_id or str(uuid.uuid4())
    stream_id = args.stream_id

    # Initialize enhancer
    print(f"Loading DPDFNet model '{args.model}'...")
    t0 = time.time()
    enhancer = dpdfnet.StreamEnhancer(model=args.model, verbose=args.verbose)
    print(f"  Loaded in {time.time() - t0:.2f}s")

    # Open audio input stream
    device = args.device
    if device is not None:
        device_info = sd.query_devices(device)
        sr_device = int(device_info["default_samplerate"])
        print(f"Using device [{device}]: {device_info['name']} @ {sr_device} Hz")
    else:
        device_info = sd.query_devices(kind="input")
        device = device_info["index"]
        sr_device = int(device_info["default_samplerate"])
        print(f"Using default input device [{device}]: {device_info['name']} @ {sr_device} Hz")

    if sr_device != SR:
        print(f"  Note: {sr_device} Hz -> will be resampled to {SR} Hz by StreamEnhancer")

    # Initialize WebSocket (or dry-run)
    ws = None
    if not args.dry_run:
        try:
            from websockets.sync.client import connect as ws_connect
            print(f"Connecting to {args.url}...")
            ws = ws_connect(args.url)
            # Send Hello
            hello = build_hello(stream_id, adapter_id, session_id)
            ws.send(hello)
            print(f"  Connected. stream_id={stream_id}, session_id={session_id}")
        except ImportError:
            print("  Warning: websockets not installed. Run: pip install websockets")
            print("  Falling back to dry-run mode (no WebSocket)")
            args.dry_run = True
        except Exception as e:
            print(f"  Warning: WebSocket connection failed: {e}")
            print("  Falling back to dry-run mode")
            args.dry_run = True

    # Open WAV savers
    raw_wav = None
    denoised_wav = None
    if args.save_wav:
        raw_wav = WavSaver(args.save_wav, "raw")
        denoised_wav = WavSaver(args.save_wav, "denoised")
        raw_wav.open()
        denoised_wav.open()

    # ── Capture loop ────────────────────────────────────────────────────
    print(f"\nCapturing mic — press Ctrl+C to stop")
    print(f"  Frame size: {FRAME_MS}ms ({FRAME_SAMPLES} samples @ {SR} Hz)")
    print(f"  Model: {args.model}")
    if args.dry_run:
        print(f"  Mode: DRY-RUN (no WebSocket)")
    else:
        print(f"  Mode: LIVE (WebSocket to {args.url})")

    seq = 0
    source_sample_start = 0
    frame_times = []
    frame_count = 0

    def audio_callback(indata, frames, callback_time, status):
        nonlocal seq, source_sample_start, frame_count

        if status:
            print(f"  Audio status: {status}", file=sys.stderr)

        mono = indata[:, 0] if indata.ndim > 1 else indata.ravel()
        if raw_wav:
            raw_wav.write(mono)

        # Denoise
        t0 = time.perf_counter()
        enhanced = enhancer.process(mono, sample_rate=sr_device)
        elapsed = time.perf_counter() - t0
        frame_times.append(elapsed)
        frame_count += 1

        if len(enhanced) == 0:
            return

        if denoised_wav:
            denoised_wav.write(enhanced)

        # Send binary AudioFrame over WebSocket
        if ws:
            frame = encode_audio_frame(
                stream_id, session_id, adapter_id,
                seq, enhanced, source_sample_start
            )
            ws.send(frame)
            seq += 1
            source_sample_start += len(enhanced)

        if args.verbose and frame_count % 10 == 0:
            avg_ms = (sum(frame_times[-50:]) / min(len(frame_times[-50:]), 50)) * 1000
            print(f"  [{frame_count}] avg frame: {avg_ms:.2f}ms  (budget: {FRAME_MS}ms)  {'✓' if avg_ms < FRAME_MS else '✗'}")

    try:
        # Use device-rate blocksize for ~FRAME_MS windows
        device_blocksize = int(sr_device * FRAME_MS / 1000)
        with sd.InputStream(
            samplerate=sr_device,
            blocksize=device_blocksize,
            channels=1,
            dtype="float32",
            callback=audio_callback,
            device=device,
        ):
            print("  Capturing... press Ctrl+C to stop")
            while True:
                time.sleep(0.1)
    except KeyboardInterrupt:
        print("\n  Stopping...")
    finally:
        if ws:
            ws.close()

        # Drain final enhanced audio
        tail = enhancer.flush()
        if denoised_wav and len(tail) > 0:
            denoised_wav.write(tail)

        if raw_wav:
            raw_wav.close()
        if denoised_wav:
            denoised_wav.close()

        # Report timing stats
        if frame_times:
            import statistics
            times_ms = [t * 1000 for t in frame_times]
            print(f"\n--- Timing stats ({len(times_ms)} frames) ---")
            print(f"  Mean:   {statistics.mean(times_ms):.3f} ms")
            print(f"  Median: {statistics.median(times_ms):.3f} ms")
            print(f"  P95:    {sorted(times_ms)[int(len(times_ms)*0.95)]:.3f} ms")
            print(f"  Min:    {min(times_ms):.3f} ms")
            print(f"  Max:    {max(times_ms):.3f} ms")
            print(f"  Budget: {FRAME_MS} ms")
            ratio = FRAME_MS / statistics.mean(times_ms) if statistics.mean(times_ms) > 0 else float("inf")
            print(f"  Headroom: {ratio:.1f}× faster than real-time")
            print(f"  Status:  {'✓ FEASIBLE' if ratio >= 1 else '✗ INFEASIBLE'}")


if __name__ == "__main__":
    main()
