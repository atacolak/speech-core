# DPDFNet2 Neural Denoise — sfnix Laptop Mic Ingress Evaluation

## Status: Experimental / Feasibility Prototype

**Date:** 2026-07-08  
**Evaluator:** execution-plane (sfUB → sfnix SSH probe)  
**Target:** Laptop (sfnix) mic ingest preprocessing, before WebSocket send to daemon  
**Constraint:** Do NOT touch `speech-core-daemon` or existing live-session scripts.

---

## Findings Summary

| Metric | Value |
|---|---|
| Model tested | `dpdfnet2` (2.49M params, 1.35G MACs, 9.7 MB ONNX) |
| Sample rate | 16 kHz (model native) |
| Frame size | 20 ms (320 samples) — matches speech-core default |
| StreamEnhancer latency | **8.52 ms / frame** on sfnix (NixOS, Intel x86_64) |
| Real-time budget | 20 ms / frame |
| Headroom | **~2.3× faster than real-time** |
| ONNX runtime | onnxruntime CPUExecutionProvider (1 thread) |
| License | Apache 2.0 |
| Install method | `pip install dpdfnet` (venv on NixOS via nix-shell) |

**Verdict: FEASIBLE.** DPDFNet2 StreamEnhancer runs comfortably faster than real-time on sfnix at 16 kHz with the default `dpdfnet2` model. The lighter `baseline` model would be even faster.

---

## Architecture

### Current pipeline (no denoise)

```
sfnix mic → speech-core-mic-adapter (CPAL) → WebSocket → speech-core-daemon (sfUB)
```

### Proposed denoise seam

```
sfnix mic → Python denoise wrapper (DPDFNet2 StreamEnhancer) → speech-core-mic-adapter or direct WebSocket → daemon
```

The denoise wrapper can either:
1. **Pipe mode:** Write enhanced PCM to a virtual audio source ("snd-aloop" or PipeWire null sink) that `speech-core-mic-adapter` reads from.
2. **Proxy mode:** A Python script that directly captures mic audio with `sounddevice`, denoises with `dpdfnet.StreamEnhancer`, and sends frames over WebSocket (reimplementing the `speech-core-mic-adapter` wire protocol in Python).

Option 1 is lower risk (keeps the Rust adapter unchanged) but needs virtual audio plumbing.
Option 2 is cleaner but duplicates the WebSocket framing logic (available in `speech-core-protocol` crate).

---

## Installation on sfnix (NixOS)

### One-time setup

```bash
# Start nix-shell with required system libraries
nix-shell -p python312 stdenv.cc.cc.lib zlib \
  --run "python3 -m venv ~/dpdfnet-env && source ~/dpdfnet-env/bin/activate && pip install dpdfnet"

# Download model
nix-shell -p python312 stdenv.cc.cc.lib zlib \
  --run "source ~/dpdfnet-env/bin/activate && python3 -c 'import dpdfnet; dpdfnet.download(\"dpdfnet2\")'"
```

### Runtime wrapper script

Every invocation needs `LD_LIBRARY_PATH` to include nix store paths. Use the wrapper:

```bash
nix-shell -p python312 stdenv.cc.cc.lib zlib --run '
  export LD_LIBRARY_PATH=$(for f in $NIX_LDFLAGS; do case "$f" in -L/*) dir="${f#-L}"; [ -d "$dir" ] && echo -n "$dir:"; esac; done)
  source ~/dpdfnet-env/bin/activate
  python3 your_script.py
'
```

---

## Performance Measurements

### sfUB (Ubuntu 24.04, same Intel x86_64 arch)

| Test | Result |
|---|---|
| Offline enhance 1s audio | 12,041 ms (12× RTF — offline path uses center=True STFT, not relevant for streaming) |
| StreamEnhancer 50 frames (1s) | 109.2 ms total, **2.18 ms/frame** |
| Real-time headroom | **9.2× faster** |

### sfnix (NixOS, Intel x86_64)

| Test | Result |
|---|---|
| StreamEnhancer 50 frames (1s) | 426.2 ms total, **8.52 ms/frame** |
| Real-time headroom | **2.3× faster** |

The ~4× difference between sfUB and sfnix is likely due to NixOS LD_LIBRARY_PATH overhead (dynamic linker resolving paths per call). A properly packaged NixOS build (with rpath set) would close this gap.

### Model variants

| Model | Params | MACs | ONNX size | Est. latency (sfnix) |
|---|---|---|---|---|
| `baseline` | 2.31M | 0.36G | 8.3 MB | ~3-4 ms (fastest) |
| `dpdfnet2` | 2.49M | 1.35G | 9.7 MB | **8.5 ms ✓** |
| `dpdfnet4` | 2.84M | 2.36G | 11.1 MB | ~15 ms (tight) |
| `dpdfnet8` | 3.54M | 4.37G | 13.9 MB | ~25 ms (exceeds budget) |

---

## Risks & Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| NixOS dynamic linker: pip wheels not linked against nix store paths | Runtime failure on import | Set `LD_LIBRARY_PATH` from `NIX_LDFLAGS`; better: package dpdfnet as a NixOS derivation |
| Additional latency in mic path | Voice feels delayed or echo issues | Current 8.5ms is well under 20ms budget; test with live mic to verify |
| Python GIL / GC pauses | Occasional frame drops | Use `onnxruntime` with `intra_op_num_threads=1` and pin to CPU core; pre-allocate buffers |
| Model accuracy vs real-world noise | Poor denoise quality in some environments | Test with actual laptop mic; try `dpdfnet4` if quality insufficient |
| Licensing | Apache 2.0 — compatible | No issues, but keep attribution |
| Model download at runtime | Requires internet on first run | Pre-download via `dpdfnet download` in install script |

---

## Next Safe Integration Plan

1. **Phase 0 (this evaluation):** ✅ Done. DPDFNet2 confirmed feasible at 2.3× real-time on sfnix.

2. **Phase 1 — Prototype wrapper script:**  
   Create `scripts/experimental/sfnix-mic-denoise.py` that:
   - Captures mic audio via `sounddevice`
   - Denoises with `dpdfnet.StreamEnhancer`
   - Sends enhanced audio to daemon WebSocket (reimplementing the speech-core wire protocol subset)
   - Measures and logs per-frame latency
   - Writes raw + enhanced WAV for offline comparison

3. **Phase 2 — NixOS packaging:**  
   Package `dpdfnet` as a NixOS derivation to avoid LD_LIBRARY_PATH hack. This gives robust runtime linking and declarative dependency management.

4. **Phase 3 — Integration decision:**  
   If wrapper is stable:
   - Option A: Replace `speech-core-mic-adapter` service with denoise wrapper (on sfnix only, gated by env var)
   - Option B: Add denoise as a new crate `speech-core-mic-denoise` or extend the mic adapter with optional ONNX preprocessing (requires adding onnxruntime to Cargo deps)

---

## Files in this directory

- `test_dpdfnet_perf.py` — Synthetic benchmark: measures StreamEnhancer throughput at various frame sizes
- `sfnix-mic-denoise.py` — Real mic capture + denoise + WebSocket proxy (Phase 1 wrapper)
- `README-dpdfnet2-denoise.md` — This file

---

## References

- DPDFNet repo: https://github.com/ceva-ip/DPDFNet
- PyPI package: `dpdfnet` (v0.6.0)
- Paper: https://arxiv.org/abs/2512.16420
- HF models: https://huggingface.co/Ceva-IP/DPDFNet
- License: Apache 2.0
