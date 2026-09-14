"""Measured E2 VRAM guard. not a mathematical guarantee.

Values come from a clean-GPU residency observation on this machine
(Tesla T4 12282 MiB): E2 process ~8698 MiB after CUDA graphs, GPU
memory.used ~8837 MiB with leftover parked. Fail closed with an
explicit margin for fragmentation and other allocators.
"""

from __future__ import annotations

MIB = 1024 * 1024

# Process residency after a successful load + graph capture.
E2_REQUIRED_VRAM_BYTES = 8698 * MIB
E2_VRAM_MARGIN_BYTES = 768 * MIB
E2_GPU_INDEX = 0
E2_LOAD_TIMEOUT_S = 300.0
E2_SYNTH_TIMEOUT_S = 180.0
E2_UNLOAD_TIMEOUT_S = 8.0
# Hop safety: leftover hop open/finally refreshes this. Not call presence.
E2_LIVE_CALL_LEASE_S = 120.0
# VoiceCat session heartbeat default. POST /api/runtime/live-call without ttl_s.
E2_LIVE_CALL_SESSION_LEASE_S = 90.0

# AuK: bf16 diffusion (3.1 GB) + w4a8 Qwen-Omni encoder (3.2 GB) + fp32 VAE
# (0.6 GB) resident at once. Measured residency lives in the bead; the guard is
# deliberately the whole-card-plus-margin ceiling because AuK is loaded alone.
AUK_GPU_INDEX = 0
AUK_REQUIRED_VRAM_BYTES = 9216 * MIB
AUK_VRAM_MARGIN_BYTES = 1024 * MIB
AUK_LOAD_TIMEOUT_S = 900.0
AUK_GENERATE_TIMEOUT_S = 600.0
AUK_UNLOAD_TIMEOUT_S = 8.0
