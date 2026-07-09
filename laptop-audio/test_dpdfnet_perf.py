#!/usr/bin/env python3
"""
DPDFNet2 performance benchmark for sfnix mic ingest.
Measures StreamEnhancer throughput at various frame sizes.

Usage:
  # On sfnix (NixOS):
  nix-shell -p python312 stdenv.cc.cc.lib zlib --run '
    export LD_LIBRARY_PATH=$(for f in $NIX_LDFLAGS; do case "$f" in -L/*) dir="${f#-L}"; [ -d "$dir" ] && echo -n "$dir:"; esac; done)
    source ~/dpdfnet-env/bin/activate
    python3 scripts/experimental/test_dpdfnet_perf.py
  '

  # On Ubuntu/Debian:
  source ~/dpdfnet-test-venv/bin/activate
  python3 scripts/experimental/test_dpdfnet_perf.py

Requires: dpdfnet (pip install dpdfnet)
"""
import sys
import os
import time
import numpy as np

# ── Config ──────────────────────────────────────────────────────────────────
MODEL_NAME = os.environ.get("DPDFNET_MODEL", "dpdfnet2")
DURATION_SECS = float(os.environ.get("DPDFNET_TEST_DURATION", "5"))
FRAME_MS_VALUES = [10, 20, 30, 40, 50]  # frame sizes to test
SR = 16000
ATTN_LIMIT_DB = 12  # mild attenuation limit
# ────────────────────────────────────────────────────────────────────────────

import dpdfnet


def format_ns(ns: float) -> str:
    if ns < 1000:
        return f"{ns:.1f} ns"
    us = ns / 1000
    if us < 1000:
        return f"{us:.1f} µs"
    ms = us / 1000
    if ms < 1000:
        return f"{ms:.2f} ms"
    return f"{ms / 1000:.3f} s"


def main():
    print("=" * 70)
    print(f"DPDFNet2 Performance Benchmark — model={MODEL_NAME}")
    print(f"Sample rate: {SR} Hz,  Test duration: {DURATION_SECS}s per frame size")
    print("=" * 70)

    # Print system info
    print(f"\nSystem: {os.uname().sysname} {os.uname().release} {os.uname().machine}")
    print(f"Python: {sys.version}")
    print(f"NumPy: {np.__version__}")
    import onnxruntime as ort
    print(f"ONNX Runtime: {ort.__version__}, providers: {ort.get_available_providers()}")

    # Download / resolve model
    print(f"\nResolving model '{MODEL_NAME}'...")
    t0 = time.time()
    dpdfnet.download(MODEL_NAME, verbose=False)
    models = dpdfnet.available_models()
    model_info = next((m for m in models if m["name"] == MODEL_NAME), None)
    if model_info and model_info.get("onnx_path"):
        print(f"  ONNX path: {model_info['onnx_path']}")
        print(f"  Model ready: {model_info['ready']}")
    print(f"  Resolve time: {time.time() - t0:.2f}s")

    # Generate noisy test audio
    rng = np.random.default_rng(42)
    total_samples = int(SR * DURATION_SECS)
    # Clean speech-like signal (modulated tones)
    t = np.arange(total_samples, dtype=np.float32) / SR
    clean = (
        0.3 * np.sin(2 * np.pi * 220 * t)
        + 0.2 * np.sin(2 * np.pi * 440 * t)
        + 0.1 * np.sin(2 * np.pi * 880 * t)
    )
    # Modulate to simulate speech activity
    clean *= 0.5 + 0.5 * np.sin(2 * np.pi * 0.5 * t)
    # Add noise
    noise = rng.normal(0, 0.05, total_samples).astype(np.float32)
    noisy = (clean + noise).astype(np.float32)

    print(f"\nTest signal: {DURATION_SECS}s synthetic speech + noise")
    print(f"  RMS noisy: {np.sqrt(np.mean(noisy**2)):.4f}")

    # ── Per-frame-size benchmark ──────────────────────────────────────────
    print(f"\n{'Frame Size':>12} {'Frames':>8} {'Total Time':>12} {'Per Frame':>12} {'Budget':>12} {'Ratio':>10}")
    print("-" * 70)

    results = {}
    for frame_ms in FRAME_MS_VALUES:
        frame_samples = int(SR * frame_ms / 1000)
        n_frames = total_samples // frame_samples

        enhancer = dpdfnet.StreamEnhancer(model=MODEL_NAME, verbose=False)

        # Measure with a loop matching real streaming use
        t0 = time.perf_counter()
        output_samples = 0
        for i in range(n_frames):
            chunk = noisy[i * frame_samples : (i + 1) * frame_samples]
            out = enhancer.process(chunk, sample_rate=SR)
            output_samples += len(out)
        tail = enhancer.flush()
        output_samples += len(tail)
        elapsed = time.perf_counter() - t0

        per_frame_us = (elapsed / n_frames) * 1_000_000
        budget_us = frame_ms * 1000
        ratio = budget_us / per_frame_us if per_frame_us > 0 else float("inf")

        results[frame_ms] = {
            "frame_samples": frame_samples,
            "n_frames": n_frames,
            "elapsed_s": elapsed,
            "per_frame_us": per_frame_us,
            "budget_us": budget_us,
            "ratio": ratio,
            "output_samples": output_samples,
        }

        status = "✓" if ratio >= 1.0 else "✗"
        print(
            f"{status} {frame_ms:>3} ms  "
            f"{n_frames:>8}  "
            f"{elapsed*1000:>9.2f} ms  "
            f"{per_frame_us:>9.2f} µs  "
            f"{budget_us:>9.2f} µs  "
            f"{ratio:>7.2f}×"
        )

    # ── Summary ───────────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    feasible = [ms for ms, r in results.items() if r["ratio"] >= 1.0]
    infeasible = [ms for ms, r in results.items() if r["ratio"] < 1.0]
    if feasible:
        print(f"✓ Feasible frame sizes: {feasible} ms")
    if infeasible:
        print(f"✗ Infeasible frame sizes: {infeasible} ms (exceeds real-time budget)")
    print(f"\nRecommended: {feasible[-1] if feasible else 'N/A'} ms (best quality that fits budget)")
    print("=" * 70)


if __name__ == "__main__":
    main()
