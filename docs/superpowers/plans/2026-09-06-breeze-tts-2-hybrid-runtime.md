# Breeze TTS 2 Hybrid Runtime Implementation Plan

**Technical Spec:** `$HOME/workspace/speech-core/docs/superpowers/specs/2026-09-06-breeze-tts-2-hybrid-runtime.md`
**Design Brief:** `$HOME/worlds/personal/designs/speech-core/breeze-tts-2-hybrid-runtime.md` (via spec; do not bypass)
**Bead:** `sc-breeze-hybrid-81p`

> **For the project lead:** execute task-by-task with isolated `builder` workers. Do not implement these tasks inline. Persist progress in this file's checkboxes, not only in conversation.

**Goal:** Ship a lab-only Breeze TTS 2 hybrid-int8 + selective CUDA-graph streaming runtime and a 4070 benchmark report (A, B, C0–C4, cheap D, E1–E5) under a **9 GB peak VRAM hard ceiling** that answers whether warm first-non-silent PCM is SHELF-quality vs this experiment's own bf16 eager baseline, and whether best ≤9 GB E beats best ≤9 GB hybrid. Terminal state is characterize → record → park; do not integrate.
**Architecture:** Isolated `lab/scripts/breeze_tts_qual/` wraps official `FastBreezeStreamingRuntime` behind `BreezeEngine.synthesize(...)`. Hybrid/full-int8 loads published ConvRot safetensors via extracted `ConvRotInt8Linear` (no ComfyUI). E is a pruned/narrow official BF16 ladder with a VoiceCat-specific warmup profile (not stock `fast.json`). Protocol parks leftover for exclusive VRAM, stops adding stages when peak would exceed 9.0 GiB without running the overflowing stage, measures 30-run short-class TTFA/RTF/VRAM/jitter, dumps wavs, runs a direction test, writes `lab/docs/qualification/`, then restores `ata-speech-tts.service`. After restore, the lead comments measurements on `sc-breeze-hybrid-81p` and parks the bead; builders do not close it.
**Tech stack:** Python 3.10+, stdlib `unittest`, official `breeze-tts` pin `43e2ea1595297c4059477e2e4a300653761c759b` (`torch==2.9.1`, `torchaudio==2.9.1`, `transformers==4.57.3`, `qwen-tts==0.1.1`), `safetensors`, `comfy-kitchen` kernel or vendored equivalent, `soundfile`. No pytest. No crates/voicecat/systemd unit edits.

---

## File map

| Path | Responsibility |
|---|---|
| `lab/scripts/breeze_tts_qual/__init__.py` | Package + tombstone |
| `lab/scripts/breeze_tts_qual/configs.py` | `EngineConfig` + A/B/C/D/E1–E5 table |
| `lab/scripts/breeze_tts_qual/metrics.py` | first-non-silent, percentiles, RTF, jitter, VRAM |
| `lab/scripts/breeze_tts_qual/engine.py` | `BreezeEngine` / `PcmChunk` |
| `lab/scripts/breeze_tts_qual/int8_convrot.py` | ConvRot scan/swap |
| `lab/scripts/breeze_tts_qual/protocol.py` | warmup, n=30, graph-stop |
| `lab/scripts/breeze_tts_qual/utterances.py` | fixed texts + direction set |
| `lab/scripts/breeze_tts_qual/report.py` | compact table + recommendation |
| `lab/scripts/breeze_tts_qual/park_leftover.py` | stop/start `ata-speech-tts.service` only |
| `lab/scripts/breeze_tts_qual/run_benchmark.py` | CLI harness |
| `lab/scripts/breeze_tts_qual/install_pin.sh` | venv + clone + weight fetch |
| `lab/scripts/test_breeze_tts_qual.py` | CPU unittest |
| `lab/README.md` | inventory row |
| `lab/docs/qualification/breeze-tts-2-hybrid-sc-breeze-hybrid-81p.md` | compact report (written by harness) |
| `lab/docs/qualification/breeze-tts-2-hybrid-sc-breeze-hybrid-81p-report.json` | machine twin |

Do not create or edit: `crates/**`, `spec/**`, systemd unit files, leftover env, voicecat, `CHARTER.md`.

Tombstone required as the first line of the module docstring in every created `lab/scripts/breeze_tts_qual/*.py`:

```text
not on the voicecat path.
```

Default QUAL_ROOT: `$HOME/.cache/speech-out/breeze-tts-qual-sc-breeze-hybrid-81p` (never commit `/home/sf` paths).

CPU tests (Tasks 1–7, 2E) must not start leftover and must not load the 3B model.

---

### Task 1: Config table and tombstone package

**Owner:** builder
**Files:**
- Create: `lab/scripts/breeze_tts_qual/__init__.py`
- Create: `lab/scripts/breeze_tts_qual/configs.py`
- Create: `lab/scripts/test_breeze_tts_qual.py`

**Verification (anti-gameable):** `python3 -m unittest lab/scripts/test_breeze_tts_qual.py` prints `OK` and a grep of created `*.py` files contains `not on the voicecat path.`

- [x] **Step 1: Write the failing test**

Append to `lab/scripts/test_breeze_tts_qual.py`:

```python
#!/usr/bin/env python3
"""CPU tests for lab/scripts/breeze_tts_qual. not on the voicecat path."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent / "breeze_tts_qual"
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT))


class Tombstone(unittest.TestCase):
    def test_runtime_files_carry_lab_tombstone(self) -> None:
        py_files = sorted(ROOT.glob("*.py"))
        self.assertTrue(py_files, "expected breeze_tts_qual python files")
        for path in py_files:
            text = path.read_text(encoding="utf-8")
            self.assertIn(
                "not on the voicecat path.",
                text,
                f"{path} missing lab tombstone",
            )


class ConfigTable(unittest.TestCase):
    def test_brief_configs_exist(self) -> None:
        from breeze_tts_qual.configs import CONFIGS, EngineConfig

        names = {c.name for c in CONFIGS}
        self.assertGreaterEqual(
            names,
            {
                "A",
                "B_depth",
                "B_codec",
                "B_backbone_decode",
                "B_backbone_prefill",
                "C0",
                "C1",
                "C2",
                "C3",
                "C4",
                "D",
            },
        )
        by = {c.name: c for c in CONFIGS}
        self.assertEqual(by["A"].precision, "bf16")
        self.assertFalse(by["A"].fast_depth_decoder)
        self.assertFalse(by["A"].fast_codec)
        self.assertFalse(by["A"].fast_backbone_decode)
        self.assertFalse(by["A"].fast_backbone_prefill)
        self.assertTrue(by["B_depth"].fast_depth_decoder)
        self.assertFalse(by["B_depth"].fast_codec)
        self.assertEqual(by["C0"].precision, "hybrid_int8")
        self.assertFalse(by["C0"].fast_depth_decoder)
        self.assertTrue(by["C1"].fast_depth_decoder)
        self.assertTrue(by["C2"].fast_codec)
        self.assertTrue(by["C3"].fast_backbone_decode)
        self.assertTrue(by["C4"].fast_backbone_prefill)
        self.assertEqual(by["D"].precision, "full_int8")
        for cfg in CONFIGS:
            self.assertIsInstance(cfg, EngineConfig)
            self.assertFalse(
                getattr(cfg, "fast_text_encoder", False),
                f"{cfg.name} must not enable text-encoder graphs",
            )


if __name__ == "__main__":
    unittest.main()
```

- [x] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest lab/scripts/test_breeze_tts_qual.py`

Expected: FAIL with `ModuleNotFoundError` or `expected breeze_tts_qual python files` / cannot import `CONFIGS`.

- [x] **Step 3: Write minimal implementation**

`lab/scripts/breeze_tts_qual/__init__.py`:

```python
"""Lab Breeze TTS 2 hybrid runtime. not on the voicecat path."""
```

`lab/scripts/breeze_tts_qual/configs.py`:

```python
"""EngineConfig table for sc-breeze-hybrid-81p. not on the voicecat path."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EngineConfig:
    name: str
    precision: str
    fast_depth_decoder: bool = False
    fast_codec: bool = False
    fast_backbone_prefill: bool = False
    fast_backbone_decode: bool = False
    fast_text_encoder: bool = False


def _cfg(
    name: str,
    precision: str,
    *,
    depth: bool = False,
    codec: bool = False,
    backbone_decode: bool = False,
    backbone_prefill: bool = False,
) -> EngineConfig:
    return EngineConfig(
        name=name,
        precision=precision,
        fast_depth_decoder=depth,
        fast_codec=codec,
        fast_backbone_decode=backbone_decode,
        fast_backbone_prefill=backbone_prefill,
        fast_text_encoder=False,
    )


CONFIGS: tuple[EngineConfig, ...] = (
    _cfg("A", "bf16"),
    _cfg("B_depth", "bf16", depth=True),
    _cfg("B_codec", "bf16", codec=True),
    _cfg("B_backbone_decode", "bf16", backbone_decode=True),
    _cfg("B_backbone_prefill", "bf16", backbone_prefill=True),
    _cfg("C0", "hybrid_int8"),
    _cfg("C1", "hybrid_int8", depth=True),
    _cfg("C2", "hybrid_int8", depth=True, codec=True),
    _cfg("C3", "hybrid_int8", depth=True, codec=True, backbone_decode=True),
    _cfg("C4", "hybrid_int8", depth=True, codec=True, backbone_decode=True, backbone_prefill=True),
    _cfg("D", "full_int8"),
)
```

If `from breeze_tts_qual.configs import ...` fails because `lab/scripts` is not a package, the test already inserts `ROOT.parent` on `sys.path`. Add empty `lab/scripts/__init__.py` **only if** import still fails; do not create a production package.

- [x] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest lab/scripts/test_breeze_tts_qual.py`

Expected: `OK` (2 tests).

- [x] **Step 5: Commit**

```bash
git add lab/scripts/breeze_tts_qual/__init__.py lab/scripts/breeze_tts_qual/configs.py lab/scripts/test_breeze_tts_qual.py
git commit -m "lab: breeze hybrid config table (sc-breeze-hybrid-81p)"
```

---

### Task 2: First-non-silent PCM, percentiles, RTF, jitter, VRAM helpers

**Owner:** builder
**Files:**
- Create: `lab/scripts/breeze_tts_qual/metrics.py`
- Modify: `lab/scripts/test_breeze_tts_qual.py`

**Verification (anti-gameable):** unittest asserts the leftover energy gate `|s16| >= 512` and p50/p95 on a 30-sample series.

- [x] **Step 1: Write the failing test**

Add to `lab/scripts/test_breeze_tts_qual.py`:

```python
import struct


class Metrics(unittest.TestCase):
    def test_first_nonsilent_skips_subthreshold(self) -> None:
        from breeze_tts_qual.metrics import SILENCE_ABS_S16, first_nonsilent_s

        self.assertEqual(SILENCE_ABS_S16, 512)
        silent = struct.pack("<" + "h" * 8, *([0] * 7 + [511]))
        t, idx = first_nonsilent_s(
            [(0.010, silent)],
            sample_rate=24000,
        )
        self.assertIsNone(t)
        self.assertIsNone(idx)

    def test_first_nonsilent_uses_chunk_clock_plus_offset(self) -> None:
        from breeze_tts_qual.metrics import first_nonsilent_s

        # 4 silent samples, then 600, at 24 kHz → 4/24000 s after chunk start
        pcm = struct.pack("<hhhhh", 0, 0, 0, 0, 600)
        t, idx = first_nonsilent_s([(0.100, pcm)], sample_rate=24000)
        self.assertEqual(idx, 4)
        self.assertAlmostEqual(t, 0.100 + 4 / 24000.0, places=9)

    def test_percentiles_rtf_jitter(self) -> None:
        from breeze_tts_qual.metrics import inter_chunk_gaps, percentile, rtf

        xs = [float(i) for i in range(1, 31)]
        self.assertEqual(len(xs), 30)
        self.assertAlmostEqual(percentile(xs, 0.50), 15.5, places=5)
        self.assertAlmostEqual(percentile(xs, 0.95), 29.55, places=5)
        self.assertAlmostEqual(percentile(xs, 0.99), 29.91, places=5)
        self.assertAlmostEqual(rtf(wall_s=0.5, audio_s=1.0), 0.5)
        gaps = inter_chunk_gaps(
            [
                {"t_rel_s": 0.10, "duration_s": 0.04},
                {"t_rel_s": 0.20, "duration_s": 0.04},
                {"t_rel_s": 0.40, "duration_s": 0.04},
            ]
        )
        # gap = t[i] - (t[i-1] + duration[i-1])
        self.assertAlmostEqual(gaps[0], 0.06, places=9)
        self.assertAlmostEqual(gaps[1], 0.16, places=9)
```

- [x] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest lab/scripts/test_breeze_tts_qual.py Metrics`

Expected: FAIL with `ModuleNotFoundError: breeze_tts_qual.metrics` or `cannot import name`.

- [x] **Step 3: Write minimal implementation**

`lab/scripts/breeze_tts_qual/metrics.py`:

```python
"""Benchmark clocks for sc-breeze-hybrid-81p. not on the voicecat path."""

from __future__ import annotations

import struct
from typing import Iterable, Optional

SILENCE_ABS_S16 = 512


def percentile(values: list[float], q: float) -> Optional[float]:
    if not values:
        return None
    xs = sorted(float(v) for v in values)
    if len(xs) == 1:
        return xs[0]
    k = (len(xs) - 1) * q
    f = int(k)
    c = min(f + 1, len(xs) - 1)
    return xs[f] * (c - k) + xs[c] * (k - f)


def rtf(*, wall_s: float, audio_s: float) -> Optional[float]:
    if audio_s <= 0:
        return None
    return wall_s / audio_s


def inter_chunk_gaps(chunks: list[dict]) -> list[float]:
    gaps: list[float] = []
    for prev, cur in zip(chunks, chunks[1:]):
        gaps.append(float(cur["t_rel_s"]) - (float(prev["t_rel_s"]) + float(prev["duration_s"])))
    return gaps


def first_nonsilent_s(
    chunks: Iterable[tuple[float, bytes]],
    *,
    sample_rate: int,
) -> tuple[Optional[float], Optional[int]]:
    """Return (time_s, global_sample_index) of first |s16| >= 512."""
    sample_index = 0
    for t_rel_s, pcm in chunks:
        n = len(pcm) // 2
        samples = struct.unpack("<" + "h" * n, pcm[: n * 2])
        for i, s in enumerate(samples):
            if abs(s) >= SILENCE_ABS_S16:
                return t_rel_s + (i / float(sample_rate)), sample_index + i
        sample_index += n
    return None, None


def vram_mb() -> dict[str, float]:
    try:
        import torch
    except ImportError:
        return {"available": 0.0}
    if not torch.cuda.is_available():
        return {"available": 0.0}
    free, total = torch.cuda.mem_get_info()
    return {
        "free_mb": free / (1024 * 1024),
        "total_mb": total / (1024 * 1024),
        "allocated_mb": torch.cuda.memory_allocated() / (1024 * 1024),
        "reserved_mb": torch.cuda.memory_reserved() / (1024 * 1024),
        "peak_allocated_mb": torch.cuda.max_memory_allocated() / (1024 * 1024),
        "peak_reserved_mb": torch.cuda.max_memory_reserved() / (1024 * 1024),
        "used_mb": (total - free) / (1024 * 1024),
    }


def bytes_to_gib(n_bytes: float) -> float:
    return float(n_bytes) / (1024.0 ** 3)
```

- [x] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest lab/scripts/test_breeze_tts_qual.py Metrics Tombstone ConfigTable`

Expected: `OK`

- [x] **Step 5: Commit**

```bash
git add lab/scripts/breeze_tts_qual/metrics.py lab/scripts/test_breeze_tts_qual.py
git commit -m "lab: breeze hybrid TTFA/RTF/jitter metrics (sc-breeze-hybrid-81p)"
```

---

### Task 2E: Extend CONFIGS with E1–E5 (pruned official BF16 ladder)

**Owner:** builder
**Files:**
- Modify: `lab/scripts/breeze_tts_qual/configs.py`
- Modify: `lab/scripts/test_breeze_tts_qual.py`

**Verification (anti-gameable):** `python3 -m unittest lab/scripts/test_breeze_tts_qual.py ConfigTableE Tombstone` prints `OK`. E names exist; E1 is depth-only; E5 `fast_text_encoder` is True; A/B/C still False. Tombstone glob already required by `Tombstone`.

- [x] **Step 1: Write the failing test**

Add to `lab/scripts/test_breeze_tts_qual.py`. Do not rewrite Task 1's `ConfigTable.test_brief_configs_exist` except the blanket `fast_text_encoder` loop, which Step 3 must narrow so E5 does not fail the landed Task 1 test:

```python
class ConfigTableE(unittest.TestCase):
    def test_e_ladder_exists(self) -> None:
        from breeze_tts_qual.configs import CONFIGS, EngineConfig

        names = {c.name for c in CONFIGS}
        self.assertGreaterEqual(names, {"E1", "E2", "E3", "E4", "E5"})
        by = {c.name: c for c in CONFIGS}
        for name in ("E1", "E2", "E3", "E4", "E5"):
            self.assertEqual(by[name].precision, "bf16")
            self.assertIsInstance(by[name], EngineConfig)

        self.assertTrue(by["E1"].fast_depth_decoder)
        self.assertFalse(by["E1"].fast_codec)
        self.assertFalse(by["E1"].fast_backbone_decode)
        self.assertFalse(by["E1"].fast_backbone_prefill)
        self.assertFalse(by["E1"].fast_text_encoder)

        self.assertTrue(by["E2"].fast_depth_decoder)
        self.assertTrue(by["E2"].fast_codec)
        self.assertFalse(by["E2"].fast_backbone_decode)
        self.assertFalse(by["E2"].fast_text_encoder)

        self.assertTrue(by["E3"].fast_backbone_decode)
        self.assertFalse(by["E3"].fast_backbone_prefill)

        self.assertTrue(by["E4"].fast_backbone_prefill)
        self.assertFalse(by["E4"].fast_text_encoder)

        self.assertTrue(by["E5"].fast_depth_decoder)
        self.assertTrue(by["E5"].fast_codec)
        self.assertTrue(by["E5"].fast_backbone_decode)
        self.assertTrue(by["E5"].fast_backbone_prefill)
        self.assertTrue(by["E5"].fast_text_encoder)

        for cfg in CONFIGS:
            if cfg.name.startswith("E"):
                continue
            self.assertFalse(
                cfg.fast_text_encoder,
                f"{cfg.name} must not enable text-encoder graphs",
            )
```

- [x] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest lab/scripts/test_breeze_tts_qual.py ConfigTableE`

Expected: FAIL (`E1` missing from `CONFIGS` / assertion on names).

- [x] **Step 3: Write minimal implementation**

In `lab/scripts/breeze_tts_qual/configs.py`, extend `_cfg` with `text_encoder: bool = False` passed through to `EngineConfig.fast_text_encoder` (A/B/C/D keep the default False), then append to `CONFIGS`:

```python
    _cfg("E1", "bf16", depth=True),
    _cfg("E2", "bf16", depth=True, codec=True),
    _cfg("E3", "bf16", depth=True, codec=True, backbone_decode=True),
    _cfg("E4", "bf16", depth=True, codec=True, backbone_decode=True, backbone_prefill=True),
    _cfg("E5", "bf16", depth=True, codec=True, backbone_decode=True, backbone_prefill=True, text_encoder=True),
```

Narrow the Task 1 blanket loop in `ConfigTable.test_brief_configs_exist` so it skips names starting with `"E"` (A/B/C/D remain False). Do not otherwise rewrite that test.

- [x] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest lab/scripts/test_breeze_tts_qual.py ConfigTable ConfigTableE Tombstone`

Expected: `OK`

- [x] **Step 5: Commit**

```bash
git add lab/scripts/breeze_tts_qual/configs.py lab/scripts/test_breeze_tts_qual.py
git commit -m "lab: breeze E1-E5 pruned official BF16 config ladder"
```

---

### Task 3: Streaming `BreezeEngine` contract (fake backend)

**Owner:** builder
**Files:**
- Create: `lab/scripts/breeze_tts_qual/engine.py`
- Modify: `lab/scripts/test_breeze_tts_qual.py`

**Verification (anti-gameable):** fake engine yields ≥2 PCM chunks before `is_final`; harness records first-bytes and first-non-silent as different times.

- [x] **Step 1: Write the failing test**

```python
class EngineContract(unittest.TestCase):
    def test_synthesize_yields_incremental_pcm(self) -> None:
        from breeze_tts_qual.configs import CONFIGS
        from breeze_tts_qual.engine import BreezeEngine, PcmChunk, FakeBackend
        from breeze_tts_qual.metrics import first_nonsilent_s

        cfg = next(c for c in CONFIGS if c.name == "A")
        engine = BreezeEngine(cfg, ckpt_dir=".", backend=FakeBackend())
        chunks = list(
            engine.synthesize(
                text="yeah.",
                reference_audio="ref.wav",
                reference_text="ref",
            )
        )
        self.assertGreaterEqual(len(chunks), 2)
        self.assertTrue(all(isinstance(c, PcmChunk) for c in chunks))
        self.assertTrue(chunks[-1].is_final)
        self.assertFalse(chunks[0].is_final)
        self.assertGreater(chunks[0].n_samples, 0)
        first_bytes = next(c.t_rel_s for c in chunks if c.n_samples > 0)
        t_ns, _ = first_nonsilent_s(
            [(c.t_rel_s, c.pcm) for c in chunks],
            sample_rate=chunks[0].sample_rate,
        )
        self.assertIsNotNone(t_ns)
        self.assertGreaterEqual(t_ns, first_bytes)
        engine.close()
```

- [x] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest lab/scripts/test_breeze_tts_qual.py EngineContract`

Expected: FAIL import/`FakeBackend` missing.

- [x] **Step 3: Write minimal implementation**

`lab/scripts/breeze_tts_qual/engine.py`:

```python
"""BreezeEngine facade. not on the voicecat path."""

from __future__ import annotations

import struct
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from configs import EngineConfig


@dataclass(frozen=True)
class PcmChunk:
    pcm: bytes
    sample_rate: int
    n_samples: int
    is_final: bool
    t_rel_s: float
    timing: dict = field(default_factory=dict)


class FakeBackend:
    sample_rate = 24000

    def synthesize(self, **kwargs: Any) -> Iterator[PcmChunk]:
        silent = struct.pack("<" + "h" * 200, *([0] * 200))
        audible = struct.pack("<" + "h" * 200, *([1000] * 200))
        yield PcmChunk(silent, 24000, 200, False, 0.050, {})
        yield PcmChunk(audible, 24000, 200, True, 0.120, {})

    def close(self) -> None:
        return None


class BreezeEngine:
    def __init__(
        self,
        config: EngineConfig,
        *,
        ckpt_dir: Path | str,
        device: str = "cuda:0",
        backend: Any | None = None,
    ) -> None:
        self.config = config
        self.ckpt_dir = Path(ckpt_dir)
        self.device = device
        self.backend = backend or FakeBackend()

    def synthesize(
        self,
        *,
        text: str,
        reference_audio: Path | str,
        reference_text: str,
        instruction: str = "Speak clearly and naturally.",
        seed: int = 42,
    ) -> Iterator[PcmChunk]:
        yield from self.backend.synthesize(
            text=text,
            reference_audio=reference_audio,
            reference_text=reference_text,
            instruction=instruction,
            seed=seed,
        )

    def close(self) -> None:
        close = getattr(self.backend, "close", None)
        if close is not None:
            close()
```

If `from configs import EngineConfig` fails when imported as `breeze_tts_qual.engine`, use `from .configs import EngineConfig`. FakeBackend remains the default until Task 9 wires OfficialBackend.

- [x] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest lab/scripts/test_breeze_tts_qual.py EngineContract`

Expected: `OK`

- [x] **Step 5: Commit**

```bash
git add lab/scripts/breeze_tts_qual/engine.py lab/scripts/test_breeze_tts_qual.py
git commit -m "lab: breeze hybrid streaming engine contract (sc-breeze-hybrid-81p)"
```

---

### Task 4: Graph-stop helper and 30-run protocol stats

**Owner:** builder
**Files:**
- Create: `lab/scripts/breeze_tts_qual/protocol.py`
- Create: `lab/scripts/breeze_tts_qual/utterances.py`
- Modify: `lab/scripts/test_breeze_tts_qual.py`

**Verification (anti-gameable):** unittest encodes Brief stop rules and 30-run short-class counts.

- [x] **Step 1: Write the failing test**

```python
class Protocol(unittest.TestCase):
    def test_stop_adding_graphs(self) -> None:
        from breeze_tts_qual.protocol import (
            should_stop_adding_graphs,
            would_exceed_hard_ceiling,
            UNSAFE_PEAK_GIB,
        )

        self.assertAlmostEqual(UNSAFE_PEAK_GIB, 9.0)
        prev = {"p50_ttfa_s": 0.20, "gap_p95_s": 0.05, "startup_s": 10.0}
        unsafe = dict(prev, p50_ttfa_s=0.18, peak_allocated_gib=9.1)
        stop, reason = should_stop_adding_graphs(prev, unsafe)
        self.assertTrue(stop)
        self.assertEqual(reason, "unsafe_vram")
        no_win = dict(prev, p50_ttfa_s=0.21, peak_allocated_gib=8.0)
        stop, reason = should_stop_adding_graphs(prev, no_win)
        self.assertTrue(stop)
        self.assertEqual(reason, "latency_no_improve")
        jitter = dict(prev, p50_ttfa_s=0.10, gap_p95_s=0.06, peak_allocated_gib=8.0)
        stop, reason = should_stop_adding_graphs(prev, jitter)
        self.assertTrue(stop)
        self.assertEqual(reason, "jitter_worse")
        bad = dict(prev, p50_ttfa_s=0.10, peak_allocated_gib=8.0, correctness_changed=True)
        stop, reason = should_stop_adding_graphs(prev, bad)
        self.assertTrue(stop)
        self.assertEqual(reason, "correctness_changed")
        init = dict(prev, p50_ttfa_s=0.10, peak_allocated_gib=8.0, init_unreasonable=True)
        stop, reason = should_stop_adding_graphs(prev, init)
        self.assertTrue(stop)
        self.assertEqual(reason, "init_unreasonable")
        ok = dict(prev, p50_ttfa_s=0.10, gap_p95_s=0.04, peak_allocated_gib=8.0)
        stop, reason = should_stop_adding_graphs(prev, ok)
        self.assertFalse(stop)
        self.assertIsNone(reason)
        at_ceiling = dict(prev, p50_ttfa_s=0.10, gap_p95_s=0.04, peak_allocated_gib=9.0)
        stop, reason = should_stop_adding_graphs(prev, at_ceiling)
        self.assertFalse(stop)
        self.assertIsNone(reason)
        self.assertTrue(would_exceed_hard_ceiling(9.1))
        self.assertFalse(would_exceed_hard_ceiling(9.0))

    def test_utterance_classes_and_run_counts(self) -> None:
        from breeze_tts_qual.protocol import measured_plan
        from breeze_tts_qual.utterances import DIRECTIONS, LONG, MEDIUM, SHORT, TINY

        self.assertEqual(TINY, ("yeah.", "got it.", "one second."))
        self.assertEqual(
            SHORT,
            ("I found the issue. The worker is holding the old session open.",),
        )
        self.assertGreaterEqual(len(MEDIUM[0].split()), 40)
        self.assertLessEqual(len(MEDIUM[0].split()), 70)
        self.assertGreaterEqual(len(LONG[0].split()), 150)
        self.assertLessEqual(len(LONG[0].split()), 250)
        self.assertEqual(
            list(DIRECTIONS),
            [
                "calm and matter-of-fact",
                "slightly amused",
                "urgent but controlled",
                "quiet / thoughtful",
            ],
        )
        plan = measured_plan()
        self.assertEqual(plan["tiny"], 30)
        self.assertEqual(plan["short"], 30)
        self.assertEqual(plan["medium"], 3)
        self.assertEqual(plan["long"], 3)
        self.assertEqual(plan["warmup"], 3)
```

Use the **exact** medium and long strings from the spec.

- [x] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest lab/scripts/test_breeze_tts_qual.py Protocol`

Expected: FAIL import.

- [x] **Step 3: Write minimal implementation**

`utterances.py`: copy the spec strings exactly.

`protocol.py`:

```python
UNSAFE_PEAK_GIB = 9.0  # hard ceiling; in-envelope is peak <= 9.0; overflow is peak > 9.0


def would_exceed_hard_ceiling(peak_allocated_gib: float) -> bool:
    return float(peak_allocated_gib) > UNSAFE_PEAK_GIB


def should_run_measured_protocol(peak_allocated_gib: float) -> bool:
    """Do not run or optimize a stage whose peak would exceed 9 GB."""
    return not would_exceed_hard_ceiling(peak_allocated_gib)


def should_stop_adding_graphs(prev: dict, curr: dict) -> tuple[bool, str | None]:
    if curr.get("init_unreasonable"):
        return True, "init_unreasonable"
    if curr.get("correctness_changed"):
        return True, "correctness_changed"
    if would_exceed_hard_ceiling(curr.get("peak_allocated_gib", 0.0)):
        return True, "unsafe_vram"
    if float(curr["p50_ttfa_s"]) >= float(prev["p50_ttfa_s"]):
        return True, "latency_no_improve"
    if float(curr["gap_p95_s"]) > float(prev["gap_p95_s"]):
        return True, "jitter_worse"
    return False, None

def measured_plan() -> dict[str, int]:
    return {"warmup": 3, "tiny": 30, "short": 30, "medium": 3, "long": 3}

def tiny_rotation() -> list[str]:
    # 10 of each tiny text, fixed order, 30 total
    from breeze_tts_qual.utterances import TINY
    return [TINY[i % 3] for i in range(30)]
```

Check stop conditions in this order: init_unreasonable, correctness_changed, unsafe_vram, latency_no_improve, jitter_worse — matching the test.

Before measuring the next cumulative stage, if load/warmup peak `would_exceed_hard_ceiling`, mark that stage and later ones `not_run` + `unsafe_vram`. Do not run the 30-run protocol. Do not optimize the overflowing stage. 9.0 GiB exactly is in-envelope.

- [x] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest lab/scripts/test_breeze_tts_qual.py Protocol`

Expected: `OK`

- [x] **Step 5: Commit**

```bash
git add lab/scripts/breeze_tts_qual/protocol.py lab/scripts/breeze_tts_qual/utterances.py lab/scripts/test_breeze_tts_qual.py
git commit -m "lab: breeze hybrid 30-run protocol and graph-stop rules"
```

---

### Task 5: Extract ConvRot int8 linear swap (no ComfyUI)

**Owner:** builder
**Files:**
- Create: `lab/scripts/breeze_tts_qual/int8_convrot.py`
- Modify: `lab/scripts/test_breeze_tts_qual.py`

**Verification (anti-gameable):** unittest swaps a named `nn.Linear` and errors on a missing module; file must not import `comfy` / ComfyUI node modules. `rg 'import comfy|from comfy' lab/scripts/breeze_tts_qual/int8_convrot.py` exits 1.

- [x] **Step 1: Write the failing test**

```python
class Int8Convrot(unittest.TestCase):
    def test_replace_named_linear(self) -> None:
        import torch
        from torch import nn
        from breeze_tts_qual.int8_convrot import (
            ConvRotInt8Linear,
            QuantLayerInfo,
            replace_quantized_linears,
        )

        class Toy(nn.Module):
            def __init__(self) -> None:
                super().__init__()
                self.backbone = nn.Linear(16, 8, bias=True)
                self.depth = nn.Linear(16, 8, bias=True)

        model = Toy()
        qmap = {
            "backbone": QuantLayerInfo(prefix="backbone", group_size=16, in_features=16, out_features=8, has_bias=True)
        }
        replaced = replace_quantized_linears(model, qmap)
        self.assertEqual(replaced, ["backbone"])
        self.assertIsInstance(model.backbone, ConvRotInt8Linear)
        self.assertIsInstance(model.depth, nn.Linear)

    def test_missing_target_raises(self) -> None:
        import torch
        from torch import nn
        from breeze_tts_qual.int8_convrot import QuantLayerInfo, replace_quantized_linears

        class Toy(nn.Module):
            def __init__(self) -> None:
                super().__init__()
                self.other = nn.Linear(16, 8)

        with self.assertRaises(RuntimeError):
            replace_quantized_linears(
                Toy(),
                {"backbone": QuantLayerInfo(prefix="backbone", group_size=16, in_features=16, out_features=8)},
            )

    def test_no_comfyui_import(self) -> None:
        text = (ROOT / "int8_convrot.py").read_text(encoding="utf-8")
        self.assertNotIn("import comfy\n", text)
        self.assertNotIn("from comfy", text)
        self.assertNotIn("nodes.py", text)
```

If `torch` is missing in the default python, run tests with whatever python the CosyVoice lab venv provides **only after** documenting it. Prefer the upcoming QUAL venv once Task 8 exists; until then skip this task's GPU kernels — `replace_quantized_linears` itself only needs `torch.nn`. If host python has no torch, create a tiny `unittest.mock` double for `nn.Module` **is not allowed**; install torch CPU in a local throwaway venv under QUAL_ROOT without loading weights.

- [x] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest lab/scripts/test_breeze_tts_qual.py Int8Convrot`

Expected: FAIL import.

- [x] **Step 3: Write minimal implementation**

Port `QuantLayerInfo`, `validate_group_size`, `replace_quantized_linears`, `ConvRotInt8Linear` from `https://raw.githubusercontent.com/Saganaki22/ComfyUI-Breeze-TTS-2/main/int8.py` (Apache-2.0). Keep `scan_checkpoint_quantization`. `ConvRotInt8Linear.forward` may `import comfy_kitchen` **inside forward only**. Do not import ComfyUI. Copy the tombstone into the module docstring and attribute the extraction in a comment: `extracted from ComfyUI-Breeze-TTS-2 int8.py; ComfyUI is not a runtime dep.`

- [x] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest lab/scripts/test_breeze_tts_qual.py Int8Convrot`

Expected: `OK`

Also run: `rg -n 'import comfy|from comfy' lab/scripts/breeze_tts_qual/int8_convrot.py || true`

Expected: no `import comfy` / `from comfy` matches (`comfy_kitchen` inside `forward` is allowed).

- [x] **Step 5: Commit**

```bash
git add lab/scripts/breeze_tts_qual/int8_convrot.py lab/scripts/test_breeze_tts_qual.py
git commit -m "lab: extract ConvRot int8 linear swap without ComfyUI"
```

---

### Task 6: Compact report writer

**Owner:** builder
**Files:**
- Create: `lab/scripts/breeze_tts_qual/report.py`
- Modify: `lab/scripts/test_breeze_tts_qual.py`

**Verification (anti-gameable):** writer output contains the spec columns and only `SHELF` / `PROMISING — NEEDS ONE MORE EXPERIMENT` / `REJECT`. Paths in JSON use `$HOME` or `~`, never `/home/sf`.

- [x] **Step 1: Write the failing test**

```python
class Report(unittest.TestCase):
    def test_compact_table_and_recommendation(self) -> None:
        from breeze_tts_qual.report import RECOMMENDATIONS, render_report, sanitize_path

        self.assertEqual(
            set(RECOMMENDATIONS),
            {"SHELF", "PROMISING — NEEDS ONE MORE EXPERIMENT", "REJECT"},
        )
        rows = [
            {
                "configuration": "A",
                "peak_vram_gib": 7.7,
                "p50_ttfa_s": 0.30,
                "p95_ttfa_s": 0.40,
                "rtf": 0.8,
                "stream_jitter_p95_s": 0.02,
                "quality_notes": "baseline",
            }
        ]
        md, payload = render_report(
            rows=rows,
            recommendation="REJECT",
            bead="sc-breeze-hybrid-81p",
            qual_root="$HOME/.cache/speech-out/breeze-tts-qual-sc-breeze-hybrid-81p",
        )
        self.assertIn("configuration", md)
        self.assertIn("peak VRAM", md)
        self.assertIn("p50 TTFA", md)
        self.assertIn("p95 TTFA", md)
        self.assertIn("RTF", md)
        self.assertIn("stream jitter", md)
        self.assertIn("quality notes", md)
        self.assertIn("REJECT", md)
        self.assertEqual(payload["recommendation"], "REJECT")
        self.assertNotIn("/home/sf", md)
        self.assertNotIn("/home/sf", json.dumps(payload))
        self.assertTrue(sanitize_path("$HOME/.cache/x").startswith("~") or "$HOME" in sanitize_path("$HOME/.cache/x"))

    def test_invalid_recommendation_rejected(self) -> None:
        from breeze_tts_qual.report import render_report

        with self.assertRaises(ValueError):
            render_report(rows=[], recommendation="MAYBE", bead="sc-breeze-hybrid-81p", qual_root="~")
```

Need `import json` at top of the test file if not present.

- [x] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest lab/scripts/test_breeze_tts_qual.py Report`

Expected: FAIL import.

- [x] **Step 3: Write minimal implementation**

`sanitize_path` replaces `str(Path.home())` with `~`. `RECOMMENDATIONS` is exactly `("SHELF", "PROMISING — NEEDS ONE MORE EXPERIMENT", "REJECT")`. `render_report` raises if recommendation is not in that set. Markdown table columns match the spec names. Do not emit any other recommendation token.

- [x] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest lab/scripts/test_breeze_tts_qual.py Report`

Expected: `OK`

- [x] **Step 5: Commit**

```bash
git add lab/scripts/breeze_tts_qual/report.py lab/scripts/test_breeze_tts_qual.py
git commit -m "lab: breeze hybrid compact report writer"
```

---

### Task 7: Leftover park/restore helpers (mocked; do not start leftover)

**Owner:** builder
**Files:**
- Create: `lab/scripts/breeze_tts_qual/park_leftover.py`
- Modify: `lab/scripts/test_breeze_tts_qual.py`

**Verification (anti-gameable):** unittest mocks `systemctl`; live `systemctl --user is-active ata-speech-tts.service` is **not** changed by this task. `ps` of the test process must not invoke `systemctl start`.

- [x] **Step 1: Write the failing test**

```python
class ParkLeftover(unittest.TestCase):
    def test_park_stops_only_ata_speech_tts(self) -> None:
        from unittest.mock import patch
        from breeze_tts_qual.park_leftover import UNIT, park, restore

        self.assertEqual(UNIT, "ata-speech-tts.service")
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            from types import SimpleNamespace
            if argv[-2:] == ["is-active", UNIT]:
                return SimpleNamespace(returncode=0, stdout="active\n", stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=fake_run):
            park()
            restore()
        self.assertEqual(
            calls,
            [
                ["systemctl", "--user", "is-active", UNIT],
                ["systemctl", "--user", "stop", UNIT],
                ["systemctl", "--user", "start", UNIT],
            ],
        )
        joined = " ".join(" ".join(c) for c in calls)
        self.assertNotIn("qwentts-tts-server.service", joined)
        self.assertNotIn("edit", joined)
```

- [x] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest lab/scripts/test_breeze_tts_qual.py ParkLeftover`

Expected: FAIL import.

- [x] **Step 3: Write minimal implementation**

`park()`: `is-active`; if `active`, `stop`. If already inactive, do not `stop` (adjust the test if you implement skip-stop: then expected calls are `is-active` only when inactive). Prefer: always `stop` if active, no-op if inactive. Match the test above by making the mock report `active`.

`restore()`: `systemctl --user start ata-speech-tts.service` only. Do not rewrite unit files. Do not `daemon-reload`.

- [x] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest lab/scripts/test_breeze_tts_qual.py ParkLeftover`

Expected: `OK`

Then run: `systemctl --user is-active ata-speech-tts.service || true`

Expected: same state as before this task (do **not** start it here).

- [x] **Step 5: Commit**

```bash
git add lab/scripts/breeze_tts_qual/park_leftover.py lab/scripts/test_breeze_tts_qual.py
git commit -m "lab: leftover park/restore helpers for ata-speech-tts.service"
```

---

### Task 8: Isolated pin install (clone + venv + weights; no inference)

**Owner:** builder
**Files:**
- Create: `lab/scripts/breeze_tts_qual/install_pin.sh`

**Verification (anti-gameable):** `git -C $QUAL_ROOT/src/breeze-tts rev-parse HEAD` equals `43e2ea1595297c4059477e2e4a300653761c759b`; `$QUAL_ROOT/venv/bin/python -c 'import torch,transformers,safetensors'` succeeds; no `systemctl start`.

- [x] **Step 1: Write the failing test**

No unittest. The failing probe is:

Run:

```bash
QUAL_ROOT="${QUAL_ROOT:-$HOME/.cache/speech-out/breeze-tts-qual-sc-breeze-hybrid-81p}"
test -x "$QUAL_ROOT/venv/bin/python" && git -C "$QUAL_ROOT/src/breeze-tts" rev-parse HEAD
```

Expected: FAIL (`test -x` non-zero) before the script exists/runs.

- [x] **Step 2: Confirm the probe fails**

Same command. Expected: non-zero exit.

- [x] **Step 3: Write `install_pin.sh`**

```bash
#!/usr/bin/env bash
# not on the voicecat path.
set -euo pipefail
CODE_COMMIT="43e2ea1595297c4059477e2e4a300653761c759b"
QUAL_ROOT="${QUAL_ROOT:-$HOME/.cache/speech-out/breeze-tts-qual-sc-breeze-hybrid-81p}"
SRC="$QUAL_ROOT/src/breeze-tts"
VENV="$QUAL_ROOT/venv"
MODELS="$QUAL_ROOT/models"
mkdir -p "$QUAL_ROOT"/{logs,models,runs,fixtures,src}
if [[ ! -d "$SRC/.git" ]]; then
  git clone https://github.com/breezeblue-ai/breeze-tts.git "$SRC"
fi
git -C "$SRC" fetch --all --tags
git -C "$SRC" checkout "$CODE_COMMIT"
if [[ ! -x "$VENV/bin/python" ]]; then
  uv venv --python 3.10 "$VENV" || python3 -m venv "$VENV"
fi
# Official requirements from breeze-tts/requirements.txt at the pin.
uv pip install --python "$VENV/bin/python" -r "$SRC/requirements.txt" || \
  "$VENV/bin/pip" install -r "$SRC/requirements.txt"
# Kernel for ConvRot; must not install ComfyUI.
uv pip install --python "$VENV/bin/python" 'comfy-kitchen' 'safetensors' 'huggingface_hub' || \
  "$VENV/bin/pip" install 'comfy-kitchen' 'safetensors' 'huggingface_hub'
# Weights: official bf16 + hybrid + full-int8 + shared audio_tokenizer.
"$VENV/bin/python" - <<'PY'
from huggingface_hub import snapshot_download
import os
root = os.environ["QUAL_ROOT"]
snapshot_download("BreezeBlue/Breeze-TTS-2", local_dir=f"{root}/models/official")
snapshot_download(
    "drbaph/Breeze-TTS-2-comfyui",
    local_dir=f"{root}/models/comfyui-deriv",
    allow_patterns=[
        "Breeze-TTS-2-bf16.safetensors",
        "Breeze-TTS-2-int8-hybrid.safetensors",
        "Breeze-TTS-2-int8-convrot.safetensors",
        "config.json",
        "generation_config.json",
        "tokenizer.json",
        "tokenizer_config.json",
        "special_tokens_map.json",
        "audio_tokenizer/*",
    ],
)
print("ok")
PY
echo "HEAD=$(git -C "$SRC" rev-parse HEAD)"
```

Export `QUAL_ROOT` in the heredoc environment (`QUAL_ROOT="$QUAL_ROOT"` before python). If `comfy-kitchen` requires ComfyUI, stop installing ComfyUI; vendor only the `int8_linear` kernel into `lab/scripts/breeze_tts_qual/int8_kernel.py` and point `ConvRotInt8Linear.forward` at it.

Place a 5–15 s English clone wav + exact transcript at `$QUAL_ROOT/fixtures/ref.wav` and `ref.txt`. Record SHA256 with `sha256sum`. Do not commit the wav. Do not use a path that requires `/home/sf` in repo files.

- [x] **Step 4: Run install and verify pin**

Run:

```bash
chmod +x lab/scripts/breeze_tts_qual/install_pin.sh
QUAL_ROOT="${QUAL_ROOT:-$HOME/.cache/speech-out/breeze-tts-qual-sc-breeze-hybrid-81p}"
export QUAL_ROOT
./lab/scripts/breeze_tts_qual/install_pin.sh
git -C "$QUAL_ROOT/src/breeze-tts" rev-parse HEAD
test -f "$QUAL_ROOT/models/comfyui-deriv/Breeze-TTS-2-int8-hybrid.safetensors"
"$QUAL_ROOT/venv/bin/python" -c 'import torch,transformers,safetensors; print("ok")'
```

Expected: HEAD `43e2ea1595297c4059477e2e4a300653761c759b`; hybrid safetensors present; `ok`.

- [x] **Step 5: Commit**

```bash
git add lab/scripts/breeze_tts_qual/install_pin.sh
git commit -m "lab: breeze-tts pin install for hybrid qual"
```

Do not commit downloaded weights.

---

### Task 9: Official backend behind `BreezeEngine` (config A load path)

**Owner:** builder
**Files:**
- Modify: `lab/scripts/breeze_tts_qual/engine.py`
- Modify: `lab/scripts/test_breeze_tts_qual.py`

**Verification (anti-gameable):** CPU unittest still uses `FakeBackend`. GPU smoke in Task 11. This task: `OfficialBackend` maps `EngineConfig` flags onto `FastStreamingConfig` and selects checkpoint dir by `precision`.

- [x] **Step 1: Write the failing test**

```python
class OfficialMapping(unittest.TestCase):
    def test_fast_flags_map(self) -> None:
        from breeze_tts_qual.configs import CONFIGS
        from breeze_tts_qual.engine import fast_streaming_kwargs

        a = next(c for c in CONFIGS if c.name == "A")
        kw = fast_streaming_kwargs(a)
        self.assertFalse(kw["fast_depth_decoder"])
        self.assertFalse(kw["fast_codec"])
        self.assertFalse(kw["fast_backbone_decode"])
        self.assertFalse(kw["fast_backbone_prefill"])
        self.assertFalse(kw.get("fast_text_encoder", False))
        self.assertFalse(kw.get("fast_all", False) not in (None, False) and kw.get("fast_all"))
        c2 = next(c for c in CONFIGS if c.name == "C2")
        kw2 = fast_streaming_kwargs(c2)
        self.assertTrue(kw2["fast_depth_decoder"])
        self.assertTrue(kw2["fast_codec"])
        self.assertFalse(kw2["fast_backbone_decode"])
        self.assertFalse(kw2.get("fast_text_encoder", False))
        e5 = next(c for c in CONFIGS if c.name == "E5")
        kw5 = fast_streaming_kwargs(e5)
        self.assertTrue(kw5["fast_text_encoder"])
        e1 = next(c for c in CONFIGS if c.name == "E1")
        self.assertFalse(fast_streaming_kwargs(e1).get("fast_text_encoder", False))

    def test_checkpoint_for_precision(self) -> None:
        from pathlib import Path
        from breeze_tts_qual.engine import checkpoint_for_precision

        root = Path("/tmp/qual")
        self.assertIn("int8-hybrid", str(checkpoint_for_precision(root, "hybrid_int8")))
        self.assertIn("int8-convrot", str(checkpoint_for_precision(root, "full_int8")))
```

- [x] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest lab/scripts/test_breeze_tts_qual.py OfficialMapping`

Expected: FAIL missing `fast_streaming_kwargs`.

- [x] **Step 3: Write minimal implementation**

Add `fast_streaming_kwargs(config) -> dict` passing the four flags, `fast_all=None`, `fast_text_encoder=config.fast_text_encoder` (False for A/B/C/D; True for E5), `collect_timing=True`.

`checkpoint_for_precision(qual_root, precision)`:

- `bf16` → `$QUAL_ROOT/models/official` if it contains shards, else `$QUAL_ROOT/models/comfyui-deriv` with `Breeze-TTS-2-bf16.safetensors` + tokenizer/config/audio_tokenizer
- `hybrid_int8` → deriv dir using `Breeze-TTS-2-int8-hybrid.safetensors`
- `full_int8` → deriv dir using `Breeze-TTS-2-int8-convrot.safetensors`

Implement `OfficialBackend` that:

1. Inserts `$QUAL_ROOT/src/breeze-tts` on `sys.path`
2. `load_runtime(ckpt, device=..., attn_implementation="eager")`
3. For int8 precisions: `scan_checkpoint_quantization` + `replace_quantized_linears` **before** weight assign as `int8.py` requires (match ComfyUI loader order; if `from_pretrained` already assigned weights, replace then load state dict from the safetensors file)
4. Builds `FastBreezeStreamingRuntime` with mapped flags
5. If any fast flag:
   - if `config.name` starts with `E`: do **not** load `src/configs/fast.json` (VoiceCat profile is Task 14E; skip stock warmup until then or call `voicecat_warmup_profile_dict` if already present)
   - else: `warmup_from_profile(load_warmup_profile(src/configs/fast.json))`
6. `synthesize`: `prepare_inputs` + `iter_audio_chunks`; convert numpy float audio to s16le `PcmChunk`; stamp `t_rel_s` from `time.perf_counter() - t0`

Keep `FakeBackend` working.

- [x] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest lab/scripts/test_breeze_tts_qual.py OfficialMapping EngineContract`

Expected: `OK`

- [x] **Step 5: Commit**

```bash
git add lab/scripts/breeze_tts_qual/engine.py lab/scripts/test_breeze_tts_qual.py
git commit -m "lab: map EngineConfig onto official FastBreezeStreamingRuntime"
```

---

### Task 10: Park leftover (real stop, before exclusive VRAM)

**Owner:** builder
**Files:**
- Modify: none required in repo if Task 7 exists
- Use: `lab/scripts/breeze_tts_qual/park_leftover.py`

**Verification (anti-gameable):** `systemctl --user is-active ata-speech-tts.service` is `inactive` after park. Do **not** start leftover in this task.

- [x] **Step 1: Write the failing probe**

If leftover is already inactive, the probe already passes; still run park (no-op) and record the state in the run log. If leftover is active, the pre-park probe `is-active` equals `active` and post-park must be `inactive`.

- [x] **Step 2: Observe current state**

Run: `systemctl --user is-active ata-speech-tts.service || true`

Expected: `active` or `inactive` printed.

- [x] **Step 3: Park**

Run:

```bash
QUAL_ROOT="${QUAL_ROOT:-$HOME/.cache/speech-out/breeze-tts-qual-sc-breeze-hybrid-81p}"
"$QUAL_ROOT/venv/bin/python" -c 'from breeze_tts_qual.park_leftover import park; park()'
```

(or `PYTHONPATH=lab/scripts` as needed)

Do not edit the unit file.

- [x] **Step 4: Verify GPU occupant gone**

Run:

```bash
systemctl --user is-active ata-speech-tts.service
# torch.cuda used_mb after empty cache should not include qwentts ~2.4 GiB
```

Expected: `inactive`.

- [x] **Step 5: Commit**

No unit file changes. If a tiny run-log markdown was added under `lab/docs/qualification/`, commit that only:

```bash
git add lab/docs/qualification/breeze-tts-2-hybrid-park-log.md || true
git commit -m "lab: park ata-speech-tts.service for breeze hybrid GPU exclusive" || true
```

Skip the commit if nothing in the repo changed.

---

### Task 11: Configuration A — bf16 eager streaming smoke

**Owner:** builder
**Files:**
- Create: `lab/scripts/breeze_tts_qual/run_benchmark.py` (minimal `--configs A --smoke`)
- Modify: `lab/scripts/breeze_tts_qual/engine.py` if OfficialBackend still stubbed

**Verification (anti-gameable):** smoke JSON shows `n_chunks >= 2` on short text, `first_pcm_s` and `first_nonsilent_s` both present, `sample_rate` 24000, wav written.

- [x] **Step 1: Write the failing probe**

Run:

```bash
test -f "$HOME/.cache/speech-out/breeze-tts-qual-sc-breeze-hybrid-81p/runs/smoke-A/metrics.json"
```

Expected: FAIL file missing.

- [x] **Step 2: Confirm fail**

Same command. Expected: non-zero.

- [x] **Step 3: Implement `--smoke` path**

`run_benchmark.py` CLI:

- loads config A
- records `startup_s`
- 1 warmup (unmeasured)
- 1 measured short utterance
- writes wav + metrics: `first_model_output_s` (from official timing if present), `first_codec_frame_s`, `first_pcm_s`, `first_nonsilent_s`, `rtf`, `peak_allocated_mb`, `peak_reserved_mb`, `n_chunks`, `sample_rate`
- `n_chunks >= 2` or exit 2 (`streaming contract failed`)

Do not CUDA-synchronize inside the yield loop except official `collect_timing` events.

- [x] **Step 4: Run smoke**

```bash
export QUAL_ROOT="${QUAL_ROOT:-$HOME/.cache/speech-out/breeze-tts-qual-sc-breeze-hybrid-81p}"
export PYTHONPATH="lab/scripts:${QUAL_ROOT}/src/breeze-tts${PYTHONPATH:+:$PYTHONPATH}"
"$QUAL_ROOT/venv/bin/python" lab/scripts/breeze_tts_qual/run_benchmark.py \
  --configs A --smoke --run-id smoke-A \
  --ref-audio "$QUAL_ROOT/fixtures/ref.wav" \
  --ref-text "$(cat "$QUAL_ROOT/fixtures/ref.txt")"
python3 - <<'PY'
import json, os
from pathlib import Path
p = Path(os.environ["QUAL_ROOT"])/"runs"/"smoke-A"/"metrics.json"
d = json.loads(p.read_text())
assert d["configs"]["A"]["n_chunks"] >= 2
assert d["configs"]["A"]["first_nonsilent_s"] is not None
assert d["configs"]["A"]["first_pcm_s"] is not None
assert d["configs"]["A"]["sample_rate"] == 24000
print("A smoke ok", d["configs"]["A"]["first_nonsilent_s"], d["configs"]["A"]["peak_allocated_mb"])
PY
```

Expected: `A smoke ok` and a wav under the run dir.

- [x] **Step 5: Commit**

```bash
git add lab/scripts/breeze_tts_qual/run_benchmark.py lab/scripts/breeze_tts_qual/engine.py
git commit -m "lab: breeze config A bf16 eager streaming smoke"
```

---

### Task 12: Configuration B — independent official fast stages

**Owner:** builder
**Files:**
- Modify: `lab/scripts/breeze_tts_qual/run_benchmark.py`
- Modify: `lab/scripts/breeze_tts_qual/protocol.py` (B compose helper)

**Verification (anti-gameable):** metrics JSON contains `B_depth`, `B_codec`, `B_backbone_decode`, `B_backbone_prefill`, each with exactly one fast flag, plus `B_winner` with `peak_allocated_gib <= 9.0`. Arms that would exceed 9.0 GiB are not run/optimized and are excluded from the winner.

- [x] **Step 1: Write the failing test**

```python
class ComposeB(unittest.TestCase):
    def test_compose_independent_winners(self) -> None:
        from breeze_tts_qual.protocol import compose_b_winner

        arms = {
            "A": {"p50_ttfa_s": 0.30, "gap_p95_s": 0.05, "peak_allocated_gib": 7.7, "fast": []},
            "B_depth": {"p50_ttfa_s": 0.22, "gap_p95_s": 0.04, "peak_allocated_gib": 8.0, "fast": ["depth"]},
            "B_codec": {"p50_ttfa_s": 0.28, "gap_p95_s": 0.04, "peak_allocated_gib": 8.2, "fast": ["codec"]},
            "B_backbone_decode": {"p50_ttfa_s": 0.31, "gap_p95_s": 0.05, "peak_allocated_gib": 8.5, "fast": ["backbone_decode"]},
            "B_backbone_prefill": {"p50_ttfa_s": 0.29, "gap_p95_s": 0.06, "peak_allocated_gib": 8.1, "fast": ["backbone_prefill"]},
        }
        winner = compose_b_winner(arms)
        # depth improved; codec improved; backbone_decode did not; prefill jitter worse vs current
        self.assertIn("depth", winner["fast"])
        self.assertIn("codec", winner["fast"])
        self.assertNotIn("backbone_decode", winner["fast"])
```

`compose_b_winner` starts from A, tries flags in order depth, codec, backbone_decode, backbone_prefill, using independent arm metrics as the proposed `curr` vs running composed `prev`. Prefill in the fixture has worse jitter (0.06 > 0.05) so it must not be added.

- [x] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest lab/scripts/test_breeze_tts_qual.py ComposeB`

Expected: FAIL missing `compose_b_winner`.

- [x] **Step 3: Implement compose + B CLI**

Implement `compose_b_winner`. Exclude any arm with `would_exceed_hard_ceiling(peak)`. Extend `run_benchmark.py --configs B` to run A (reuse if present) + four independent arms with `--n 5` allowed only for debug; default measured counts remain spec (30 short-class) when `--full` is passed. For this task run **short class n=5** only if GPU time is gated by `--n`; spec full 30 is Task 16. Record peak VRAM. If an arm OOMs or warmup peak would exceed 9.0 GiB, mark `unsafe_vram`, do not optimize it, and exclude it.

- [x] **Step 4: Run B independents**

```bash
export QUAL_ROOT="${QUAL_ROOT:-$HOME/.cache/speech-out/breeze-tts-qual-sc-breeze-hybrid-81p}"
export PYTHONPATH="lab/scripts:${QUAL_ROOT}/src/breeze-tts"
"$QUAL_ROOT/venv/bin/python" lab/scripts/breeze_tts_qual/run_benchmark.py \
  --configs B --n 5 --run-id b-indep \
  --ref-audio "$QUAL_ROOT/fixtures/ref.wav" \
  --ref-text "$(cat "$QUAL_ROOT/fixtures/ref.txt")"
python3 - <<'PY'
import json, os
from pathlib import Path
d = json.loads((Path(os.environ["QUAL_ROOT"])/"runs"/"b-indep"/"metrics.json").read_text())
for name in ["B_depth","B_codec","B_backbone_decode","B_backbone_prefill"]:
    assert name in d["configs"], name
assert d["configs"]["B_winner"]["peak_allocated_gib"] <= 9.0
print("B ok", d["configs"]["B_winner"])
PY
```

Expected: `B ok` with a winner payload.

- [x] **Step 5: Commit**

```bash
git add lab/scripts/breeze_tts_qual/protocol.py lab/scripts/breeze_tts_qual/run_benchmark.py lab/scripts/test_breeze_tts_qual.py
git commit -m "lab: breeze config B independent fast-path sweep"
```

---

### Task 13: Configuration C0 — hybrid int8 eager

**Owner:** builder
**Files:**
- Modify: `lab/scripts/breeze_tts_qual/engine.py` (int8 load)

**Verification (anti-gameable):** C0 metrics JSON `precision=hybrid_int8`, `n_chunks>=2`, peak allocated GiB **lower** than A on the same machine, wav exists. `nvidia-smi` not required.

- [x] **Step 1: Write the failing probe**

```bash
test -f "$HOME/.cache/speech-out/breeze-tts-qual-sc-breeze-hybrid-81p/runs/smoke-C0/metrics.json"
```

Expected: missing file.

- [x] **Step 2: Confirm fail**

Same command.

- [x] **Step 3: Implement hybrid load**

Load `Breeze-TTS-2-int8-hybrid.safetensors` via `scan_checkpoint_quantization` + `replace_quantized_linears` before assigning those weights. Depth decoder remains bf16 (not in quant map). Codec = bundled `audio_tokenizer/`. Smoke C0 like A.

- [x] **Step 4: Run C0 smoke**

```bash
export QUAL_ROOT="${QUAL_ROOT:-$HOME/.cache/speech-out/breeze-tts-qual-sc-breeze-hybrid-81p}"
export PYTHONPATH="lab/scripts:${QUAL_ROOT}/src/breeze-tts"
"$QUAL_ROOT/venv/bin/python" lab/scripts/breeze_tts_qual/run_benchmark.py \
  --configs C0 --smoke --run-id smoke-C0 \
  --ref-audio "$QUAL_ROOT/fixtures/ref.wav" \
  --ref-text "$(cat "$QUAL_ROOT/fixtures/ref.txt")"
python3 - <<'PY'
import json, os
from pathlib import Path
d = json.loads((Path(os.environ["QUAL_ROOT"])/"runs"/"smoke-C0"/"metrics.json").read_text())
c0 = d["configs"]["C0"]
assert c0["n_chunks"] >= 2
assert c0["first_nonsilent_s"] is not None
print("C0 ok", c0["first_nonsilent_s"], c0["peak_allocated_mb"])
PY
```

Expected: `C0 ok`. If ConvRot×official runtime crashes, record `correctness_changed` / traceback in metrics and **do not** silently fall back to bf16.

- [x] **Step 5: Commit**

```bash
git add lab/scripts/breeze_tts_qual/engine.py lab/scripts/breeze_tts_qual/run_benchmark.py
git commit -m "lab: breeze C0 hybrid int8 eager load"
```

---

### Task 14: Configurations C1–C4 — incremental graphs + stop rules

**Owner:** builder
**Files:**
- Modify: `lab/scripts/breeze_tts_qual/run_benchmark.py`

**Verification (anti-gameable):** metrics JSON has C0..C4 keys; any skipped row has `not_run` + `stop_reason` in `{unsafe_vram,latency_no_improve,jitter_worse,init_unreasonable,correctness_changed}`.

- [x] **Step 1: Write the failing probe**

```bash
python3 - <<'PY'
import json, os
from pathlib import Path
p = Path(os.environ.get("QUAL_ROOT", str(Path.home()/".cache/speech-out/breeze-tts-qual-sc-breeze-hybrid-81p")))/"runs"/"c-incr"/"metrics.json"
assert p.exists()
PY
```

Expected: `AssertionError` before the run.

- [x] **Step 2: Confirm fail**

Same snippet.

- [x] **Step 3: Implement cumulative C sweep**

Run C0 then C1…C4. After load/warmup of the next Ck, if `would_exceed_hard_ceiling`, do **not** run the measured protocol or optimize that overflowing stage; mark it and later Ck as `{"not_run": true, "stop_reason": "unsafe_vram"}`. After each executed stage, call `should_stop_adding_graphs(prev, curr)`. On stop, write remaining configs as `{"not_run": true, "stop_reason": reason}`. Do not enable `fast_text_encoder`. Warmup graphs when any fast flag is true. Ceiling is 9.0 GiB (`>` is overflow).

- [x] **Step 4: Run C sweep**

```bash
export QUAL_ROOT="${QUAL_ROOT:-$HOME/.cache/speech-out/breeze-tts-qual-sc-breeze-hybrid-81p}"
export PYTHONPATH="lab/scripts:${QUAL_ROOT}/src/breeze-tts"
"$QUAL_ROOT/venv/bin/python" lab/scripts/breeze_tts_qual/run_benchmark.py \
  --configs C0,C1,C2,C3,C4 --n 5 --run-id c-incr \
  --ref-audio "$QUAL_ROOT/fixtures/ref.wav" \
  --ref-text "$(cat "$QUAL_ROOT/fixtures/ref.txt")"
python3 - <<'PY'
import json, os
from pathlib import Path
d = json.loads((Path(os.environ["QUAL_ROOT"])/"runs"/"c-incr"/"metrics.json").read_text())
for name in ["C0","C1","C2","C3","C4"]:
    assert name in d["configs"], name
    row = d["configs"][name]
    if row.get("not_run"):
        assert row["stop_reason"] in {
            "unsafe_vram","latency_no_improve","jitter_worse","init_unreasonable","correctness_changed"
        }
print("C sweep ok")
PY
```

Expected: `C sweep ok`.

- [x] **Step 5: Commit**

```bash
git add lab/scripts/breeze_tts_qual/run_benchmark.py
git commit -m "lab: breeze C1-C4 incremental CUDA graph sweep"
```

---

### Task 14E: Configuration E1–E5 — VoiceCat warmup + 9 GB stop

**Owner:** builder
**Files:**
- Modify: `lab/scripts/breeze_tts_qual/protocol.py`
- Modify: `lab/scripts/breeze_tts_qual/engine.py`
- Modify: `lab/scripts/breeze_tts_qual/run_benchmark.py`
- Modify: `lab/scripts/test_breeze_tts_qual.py`

**Verification (anti-gameable):** metrics JSON has E1..E5 keys; overflowing stages are `not_run` + `unsafe_vram` and were not measured; peak VRAM recorded after each executed E stage; E warmup does not load stock `fast.json`; executed E peaks are `<= 9.0`. If E cannot compete under 9 GB, `e_rejected_for_our_purposes` is true.

- [x] **Step 1: Write the failing CPU test**

```python
class VoiceCatWarmup(unittest.TestCase):
    def test_profile_uses_utterance_buckets_not_fast_json(self) -> None:
        import json
        from breeze_tts_qual.protocol import voicecat_warmup_profile_dict
        from breeze_tts_qual.utterances import LONG, MEDIUM, SHORT, TINY

        payload = voicecat_warmup_profile_dict()
        blob = json.dumps(payload)
        self.assertNotIn("fast.json", blob)
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(sorted(payload["service"]["cfg_scales"]), [1.0, 4.0])
        self.assertEqual(payload["service"]["concurrency"], 1)
        self.assertIn("yeah.", payload["warmup_request"]["text"])
        self.assertNotIn(LONG[0], blob)
        self.assertTrue(payload["stages"]["text_encoder"]["graphs"])
        self.assertTrue(payload["stages"]["backbone_prefill"]["graphs"])
        for graph in payload["stages"]["text_encoder"]["graphs"]:
            self.assertEqual(graph["token_length"] % 32, 0)
        for graph in payload["stages"]["backbone_prefill"]["graphs"]:
            self.assertEqual(graph["sequence_length"] % 32, 0)
        self.assertEqual(
            set(g["branch_batch_size"] for g in payload["stages"]["backbone_decode"]["graphs"]),
            {1, 2},
        )
```

Tiny/short/medium from `utterances.py` define the prompt-length buckets (round token lengths up to the official 32). CFG 1.0 is core clone (`no_cfg`); CFG 4.0 is direction (`single_cfg`). Do not load stock broad `fast.json`.

- [x] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest lab/scripts/test_breeze_tts_qual.py VoiceCatWarmup`

Expected: FAIL import/`voicecat_warmup_profile_dict` missing.

- [x] **Step 3: Implement VoiceCat profile + E sweep**

Add `voicecat_warmup_profile_dict()` in `protocol.py` returning a JSON-compatible `FastStreamingWarmupProfile` dict (`schema_version` 1):

- `name`: `voicecat-tiny-short-medium`
- `service.cfg_scales`: `[1.0, 4.0]`
- `service.concurrency`: 1
- text_encoder / backbone_prefill graphs: only buckets covering tiny/short/medium (and the clone prompt) rounded up to multiples of 32; include `branch_batch_size` 1 (cfg 1.0) and 2 (cfg 4.0)
- backbone_decode branch batches `{1, 2}`; depth_decoder batch sizes matching; codec `num_lanes=1`, `chunk_frames=1` when the config has `fast_codec` else `2`
- `warmup_request.template` = `ref_edit_tata`; text = `yeah.`; instruction = `Speak clearly and naturally.`; seed 42
- **never** call `load_warmup_profile` on `configs/fast.json` for E configs

`OfficialBackend`: if `config.name.startswith("E")`, `warmup_from_profile(parse_warmup_profile(voicecat_warmup_profile_dict(), source="voicecat"))`. Do not load stock `fast.json` for E.

E ladder CLI `--configs E1,E2,E3,E4,E5`:

1. Run E1…E5 in Brief priority order.
2. After load + unmeasured warmup, record peak allocated GiB.
3. If `would_exceed_hard_ceiling(peak)`: mark this and later E rows `{"not_run": true, "stop_reason": "unsafe_vram"}`. Do **not** run the measured protocol. Do **not** optimize the overflowing stage.
4. Else measure (this task: short-class `--n 5`; full 30 is Task 16).
5. Then `should_stop_adding_graphs(prev, curr)` for other stop reasons.
6. Compose `E_winner` as the executed in-envelope E row with lowest p50 TTFA (omit `E_winner` if none in-envelope).
7. If no E row is in-envelope (`peak_vram_gib <= 9.0`) or E cannot compete under 9 GB, set `e_rejected_for_our_purposes: true`. Scratch E rather than spending time above 9 GB.

- [x] **Step 4: Run E sweep**

```bash
export QUAL_ROOT="${QUAL_ROOT:-$HOME/.cache/speech-out/breeze-tts-qual-sc-breeze-hybrid-81p}"
export PYTHONPATH="lab/scripts:${QUAL_ROOT}/src/breeze-tts"
"$QUAL_ROOT/venv/bin/python" lab/scripts/breeze_tts_qual/run_benchmark.py \
  --configs E1,E2,E3,E4,E5 --n 5 --run-id e-incr \
  --ref-audio "$QUAL_ROOT/fixtures/ref.wav" \
  --ref-text "$(cat "$QUAL_ROOT/fixtures/ref.txt")"
python3 - <<'PY'
import json, os
from pathlib import Path
d = json.loads((Path(os.environ["QUAL_ROOT"])/"runs"/"e-incr"/"metrics.json").read_text())
for name in ["E1","E2","E3","E4","E5"]:
    assert name in d["configs"], name
    row = d["configs"][name]
    if row.get("not_run"):
        assert row["stop_reason"] in {
            "unsafe_vram","latency_no_improve","jitter_worse","init_unreasonable","correctness_changed"
        }
        continue
    peak = row.get("peak_allocated_gib")
    if peak is None:
        peak = row["peak_allocated_mb"] / 1024.0
    assert peak <= 9.0, (name, peak)
print("E sweep ok", {k: d["configs"][k].get("peak_allocated_gib") or d["configs"][k].get("not_run") for k in ["E1","E2","E3","E4","E5"]})
PY
```

Expected: `E sweep ok`. Overflowing stages are `not_run`, not optimized.

- [x] **Step 5: Commit**

```bash
git add lab/scripts/breeze_tts_qual/protocol.py lab/scripts/breeze_tts_qual/engine.py lab/scripts/breeze_tts_qual/run_benchmark.py lab/scripts/test_breeze_tts_qual.py
git commit -m "lab: breeze E1-E5 VoiceCat warmup ladder under 9GB"
```

---

### Task 15: Configuration D — cheap full-int8 control

**Owner:** builder
**Files:**
- Modify: `lab/scripts/breeze_tts_qual/run_benchmark.py` (enable D)

**Verification (anti-gameable):** D metrics exist with `precision=full_int8`. No D graph sweep files. If D p50 TTFA ≥ C0 p50, `d_killed: true`.

- [x] **Step 1: Write the failing probe**

```bash
python3 - <<'PY'
from pathlib import Path
import os
p = Path(os.environ.get("QUAL_ROOT", str(Path.home()/".cache/speech-out/breeze-tts-qual-sc-breeze-hybrid-81p")))/"runs"/"smoke-D"/"metrics.json"
assert p.exists()
PY
```

Expected: assert fail.

- [x] **Step 2: Confirm fail**

Same snippet.

- [x] **Step 3: Implement D**

Load `Breeze-TTS-2-int8-convrot.safetensors`, eager only. Short-class n=5 for this task (full 30 in Task 16). Compare to C0; set `d_killed` if slower. Do not add D graph stages. Scratch D immediately if peak VRAM exceeds 9.0 GiB (`not_run` / `unsafe_vram`); do not optimize it.

- [x] **Step 4: Run D**

```bash
export QUAL_ROOT="${QUAL_ROOT:-$HOME/.cache/speech-out/breeze-tts-qual-sc-breeze-hybrid-81p}"
export PYTHONPATH="lab/scripts:${QUAL_ROOT}/src/breeze-tts"
"$QUAL_ROOT/venv/bin/python" lab/scripts/breeze_tts_qual/run_benchmark.py \
  --configs D --n 5 --run-id smoke-D \
  --ref-audio "$QUAL_ROOT/fixtures/ref.wav" \
  --ref-text "$(cat "$QUAL_ROOT/fixtures/ref.txt")"
python3 - <<'PY'
import json, os
from pathlib import Path
d = json.loads((Path(os.environ["QUAL_ROOT"])/"runs"/"smoke-D"/"metrics.json").read_text())
assert d["configs"]["D"]["precision"] == "full_int8"
assert not d["configs"]["D"].get("fast_depth_decoder")
print("D ok", d["configs"]["D"])
PY
```

Expected: `D ok`.

- [x] **Step 5: Commit**

```bash
git add lab/scripts/breeze_tts_qual/run_benchmark.py lab/scripts/breeze_tts_qual/engine.py
git commit -m "lab: breeze D full-int8 cheap control"
```

---

### Task 16: Full 30-run short-class protocol, wav dumps, RTF/VRAM/jitter

**Owner:** builder
**Files:**
- Modify: `lab/scripts/breeze_tts_qual/run_benchmark.py`

**Verification (anti-gameable):** for every executed config (A, each B independent, B_winner, each executed Ck, each executed Ek, D if not killed), `classes.tiny.n_measured==30` and `classes.short.n_measured==30`; wavs exist; peak allocated **and** reserved recorded; inter-chunk p50/p95 and max stall recorded. Overflowing stages stay `not_run` and must not have n_measured==30.

- [x] **Step 1: Write the failing probe**

```bash
export QUAL_ROOT="${QUAL_ROOT:-$HOME/.cache/speech-out/breeze-tts-qual-sc-breeze-hybrid-81p}"
python3 - <<'PY'
import json, os
from pathlib import Path
p = Path(os.environ["QUAL_ROOT"])/"runs"/"full-30"/"metrics.json"
assert p.exists(), p
d = json.loads(p.read_text())
row = d["configs"]["A"]["classes"]["short"]
assert row["n_measured"] == 30
PY
```

Expected: file missing / n_measured != 30.

- [x] **Step 2: Confirm fail**

Same snippet.

- [x] **Step 3: Implement full protocol**

For every config actually run:

1. initialize once
2. 3 unmeasured warmups
3. 30 tiny (rotation) + 30 short
4. 3 medium + 3 long
5. identical ref + texts; seed 42
6. `time.perf_counter` only; no extra CUDA sync in the stream
7. save wavs: `{config}_{class}_{idx}.wav` for idx 0 of each class plus all measured if cheap; **at least** one wav per config×class
8. record every spec metric including GPU util/power via `nvidia-smi --query-gpu=utilization.gpu,power.draw --format=csv,noheader,nounits` once per config; if it fails, store null

`--full --run-id full-30 --configs A,B,C0,C1,C2,C3,C4,D,E1,E2,E3,E4,E5`

Reuse compose/stop so B independents still run (they are required configs). Every executed config, including each B independent arm, must measure 30 tiny + 30 short. Do not drop a Brief config or a short-input class to save time.

- [x] **Step 4: Run full protocol**

```bash
export QUAL_ROOT="${QUAL_ROOT:-$HOME/.cache/speech-out/breeze-tts-qual-sc-breeze-hybrid-81p}"
export PYTHONPATH="lab/scripts:${QUAL_ROOT}/src/breeze-tts"
"$QUAL_ROOT/venv/bin/python" lab/scripts/breeze_tts_qual/run_benchmark.py \
  --full --run-id full-30 \
  --ref-audio "$QUAL_ROOT/fixtures/ref.wav" \
  --ref-text "$(cat "$QUAL_ROOT/fixtures/ref.txt")"
python3 - <<'PY'
import json, os
from pathlib import Path
root = Path(os.environ["QUAL_ROOT"])/"runs"/"full-30"
d = json.loads((root/"metrics.json").read_text())
required = ["A","B_depth","B_codec","B_backbone_decode","B_backbone_prefill","C0","E1"]
for name in required:
    row = d["configs"][name]
    if row.get("not_run"):
        continue
    assert row["classes"]["short"]["n_measured"] == 30, name
    assert row["classes"]["tiny"]["n_measured"] == 30, name
    assert "peak_allocated_mb" in row and "peak_reserved_mb" in row
    assert (root/"wavs"/f"{name}_short_0.wav").exists()
print("full-30 ok")
PY
```

Expected: `full-30 ok`.

- [x] **Step 5: Commit**

```bash
git add lab/scripts/breeze_tts_qual/run_benchmark.py
git commit -m "lab: breeze hybrid 30-run short-class protocol and wav dumps"
```

Do not commit `$HOME/.cache` wavs. Copy representative wavs into `lab/docs/qualification/breeze-wavs/` only if they are small enough and rights-cleared; otherwise leave them in QUAL_ROOT and link via `~` paths in the report.

---

### Task 17: Direction test vs bf16

**Owner:** builder
**Files:**
- Modify: `lab/scripts/breeze_tts_qual/run_benchmark.py`
- Modify: `lab/scripts/breeze_tts_qual/utterances.py` (already has DIRECTIONS)

**Verification (anti-gameable):** four instruction wavs exist for A and for the surviving hybrid candidate; report `direction_notes` filled (listen vs bf16). No prompt tuning.

- [x] **Step 1: Write the failing probe**

```bash
python3 - <<'PY'
from pathlib import Path
import os
root = Path(os.environ.get("QUAL_ROOT", str(Path.home()/".cache/speech-out/breeze-tts-qual-sc-breeze-hybrid-81p")))/"runs"/"full-30"/"wavs"
for inst in ["calm","amused","urgent","quiet"]:
    assert list(root.glob(f"A_dir_{inst}*.wav")), inst
PY
```

Expected: assert fail before direction dumps exist.

- [x] **Step 2: Confirm fail**

Same snippet.

- [x] **Step 3: Implement direction pass**

After core bench, for config A, hybrid candidate (last executed in-envelope Ck, else C0), and surviving in-envelope E candidate (last executed Ek / `E_winner`) if any:

- same ref audio/text
- text = short utterance
- `instruction` exactly the four Brief phrases
- `cfg_scale=4`
- seed 42
- save wavs
- do not edit the instruction strings

- [x] **Step 4: Run direction**

```bash
export QUAL_ROOT="${QUAL_ROOT:-$HOME/.cache/speech-out/breeze-tts-qual-sc-breeze-hybrid-81p}"
export PYTHONPATH="lab/scripts:${QUAL_ROOT}/src/breeze-tts"
"$QUAL_ROOT/venv/bin/python" lab/scripts/breeze_tts_qual/run_benchmark.py \
  --direction-only --run-id full-30 \
  --ref-audio "$QUAL_ROOT/fixtures/ref.wav" \
  --ref-text "$(cat "$QUAL_ROOT/fixtures/ref.txt")"
ls "$QUAL_ROOT/runs/full-30/wavs" | rg 'dir_'
```

Expected: four wavs per executed candidate (A, hybrid, and E if in-envelope). Builder listens and writes `direction_notes` into metrics JSON (`weakened` / `comparable` / `stronger`) without retuning prompts.

- [x] **Step 5: Commit**

```bash
git add lab/scripts/breeze_tts_qual/run_benchmark.py
git commit -m "lab: breeze hybrid voice-direction test vs bf16"
```

---

### Task 18: Compact report + recommendation + first-audio trace

**Owner:** builder
**Files:**
- Create: `lab/docs/qualification/breeze-tts-2-hybrid-sc-breeze-hybrid-81p.md`
- Create: `lab/docs/qualification/breeze-tts-2-hybrid-sc-breeze-hybrid-81p-report.json`
- Modify: `lab/scripts/breeze_tts_qual/report.py` / `run_benchmark.py` to emit them

**Verification (anti-gameable):** markdown table has A, B (independents + winner), C0–C4 (or stop_reason), E1–E5 (or stop_reason), D; report names best ≤9 GB E vs best ≤9 GB hybrid; recommendation is one of `SHELF` / `PROMISING — NEEDS ONE MORE EXPERIMENT` / `REJECT`; `choose_recommendation` yields `SHELF` when a C0–C4, `B_winner`, or in-envelope E_winner / E1–E5 row meets the TTFA band vs A, RTF<1, `peak_vram_gib <= 9.0`, no quality regression; no `/home/sf` in either file; stage trace present or `trace_unavailable` with reason; leftover comparison is commentary only.

- [x] **Step 1: Write the failing probe**

```bash
test -f lab/docs/qualification/breeze-tts-2-hybrid-sc-breeze-hybrid-81p.md
```

Expected: missing file.

- [x] **Step 2: Confirm fail**

Same command.

- [x] **Step 3: Emit report from full-30 metrics**

Use `render_report`. Add `choose_recommendation` to `lab/scripts/breeze_tts_qual/report.py`. Recommendation rules (not leftover-relative; bands are vs this experiment's own A, not leftover, and do not mean integrate):

- `SHELF` if a hybrid (C0–C4), `B_winner`, or in-envelope E (`E1`–`E5` / `E_winner`) config has warm first-non-silent in the Brief acceptable band vs **this experiment's A** (`p50_ttfa_s < 0.250`), RTF<1, VRAM in-envelope (`peak_vram_gib <= 9.0`), no obvious quality regression. **<11.5 GB is no longer acceptable.**
- `REJECT` if no executed non-A config has RTF<1 **or** obvious quality/direction regression vs A **or** no material TTFA win vs A with safe VRAM
- else `PROMISING — NEEDS ONE MORE EXPERIMENT`

`choose_recommendation` yields `SHELF` when a C0–C4, `B_winner`, or in-envelope E row meets that TTFA band vs A, RTF<1, `peak_vram_gib <= 9.0`, and no quality regression. SHELF text in the report must say the best config is characterized and the bead is parked; it is not permission to integrate, propose a pin swap, or spawn a follow-on wiring bead. The markdown/JSON must also name `best_e_le_9gib` and `best_hybrid_le_9gib` and compare them on the spec metrics. If no E row is in-envelope, set `e_rejected_for_our_purposes`.

Include leftover ~70 ms / ~2.4 GB only as commentary. A win vs leftover still parks the bead.

```python
TTFA_ACCEPTABLE_S = 0.250
RTF_REQUIRED = 1.0
VRAM_ACCEPTABLE_GIB = 9.0
SHELF_CANDIDATE_NAMES = (
    "C0", "C1", "C2", "C3", "C4", "B_winner",
    "E1", "E2", "E3", "E4", "E5", "E_winner",
)


def choose_recommendation(rows: list[dict], *, quality_ok: bool) -> str:
    by = {r["configuration"]: r for r in rows if not r.get("not_run")}
    a = by["A"]

    def shelf_quality(row: dict) -> bool:
        return (
            quality_ok
            and float(row["p50_ttfa_s"]) < TTFA_ACCEPTABLE_S
            and float(row["rtf"]) < RTF_REQUIRED
            and float(row["peak_vram_gib"]) <= VRAM_ACCEPTABLE_GIB
        )

    for name in SHELF_CANDIDATE_NAMES:
        row = by.get(name)
        if row is not None and shelf_quality(row):
            return "SHELF"

    non_a = [r for name, r in by.items() if name != "A"]
    rtf_ok = any(float(r["rtf"]) < RTF_REQUIRED for r in non_a)
    material_win = any(
        float(r["p50_ttfa_s"]) < float(a["p50_ttfa_s"])
        and float(r["peak_vram_gib"]) <= VRAM_ACCEPTABLE_GIB
        for r in non_a
    )
    if (not non_a) or (not rtf_ok) or (not quality_ok) or (not material_win):
        return "REJECT"
    return "PROMISING — NEEDS ONE MORE EXPERIMENT"
```

If `collect_timing` produced stage times on a short run, include the flame/timing section (text encoder, backbone prefill, first backbone decode, depth decode, codec, pcm emission).

- [x] **Step 4: Generate and grep**

```bash
export QUAL_ROOT="${QUAL_ROOT:-$HOME/.cache/speech-out/breeze-tts-qual-sc-breeze-hybrid-81p}"
export PYTHONPATH="lab/scripts:${QUAL_ROOT}/src/breeze-tts"
"$QUAL_ROOT/venv/bin/python" lab/scripts/breeze_tts_qual/run_benchmark.py \
  --report-only --run-id full-30 \
  --out-md lab/docs/qualification/breeze-tts-2-hybrid-sc-breeze-hybrid-81p.md \
  --out-json lab/docs/qualification/breeze-tts-2-hybrid-sc-breeze-hybrid-81p-report.json
rg -n '/home/sf' lab/docs/qualification/breeze-tts-2-hybrid-sc-breeze-hybrid-81p.md lab/docs/qualification/breeze-tts-2-hybrid-sc-breeze-hybrid-81p-report.json && exit 1 || true
rg -n 'SHELF|PROMISING|REJECT' lab/docs/qualification/breeze-tts-2-hybrid-sc-breeze-hybrid-81p.md
python3 - <<'PY'
from breeze_tts_qual.report import choose_recommendation
rows = [
    {"configuration": "A", "p50_ttfa_s": 0.40, "rtf": 0.8, "peak_vram_gib": 7.7},
    {"configuration": "C1", "p50_ttfa_s": 0.20, "rtf": 0.6, "peak_vram_gib": 8.5},
    {"configuration": "B_winner", "p50_ttfa_s": 0.22, "rtf": 0.7, "peak_vram_gib": 9.0},
]
assert choose_recommendation(rows, quality_ok=True) == "SHELF"
rows_e = [
    {"configuration": "A", "p50_ttfa_s": 0.40, "rtf": 0.8, "peak_vram_gib": 7.7},
    {"configuration": "C1", "p50_ttfa_s": 0.18, "rtf": 0.6, "peak_vram_gib": 9.1},
    {"configuration": "E1", "p50_ttfa_s": 0.19, "rtf": 0.65, "peak_vram_gib": 8.8},
]
assert choose_recommendation(rows_e, quality_ok=True) == "SHELF"
rows_over = [
    {"configuration": "A", "p50_ttfa_s": 0.40, "rtf": 0.8, "peak_vram_gib": 7.7},
    {"configuration": "C1", "p50_ttfa_s": 0.18, "rtf": 0.6, "peak_vram_gib": 9.1},
]
assert choose_recommendation(rows_over, quality_ok=True) != "SHELF"
print("choose_recommendation SHELF ok")
PY
```

Expected: report files exist; no `/home/sf`; one recommendation token from `SHELF` / `PROMISING — NEEDS ONE MORE EXPERIMENT` / `REJECT`; `choose_recommendation SHELF ok`.

- [x] **Step 5: Commit**

```bash
git add lab/docs/qualification/breeze-tts-2-hybrid-sc-breeze-hybrid-81p.md lab/docs/qualification/breeze-tts-2-hybrid-sc-breeze-hybrid-81p-report.json lab/scripts/breeze_tts_qual/run_benchmark.py lab/scripts/breeze_tts_qual/report.py
git commit -m "lab: breeze hybrid compact recommendation report"
```

---

### Task 19: lab/README inventory row

**Owner:** builder
**Files:**
- Modify: `lab/README.md`

**Verification (anti-gameable):** `rg breeze_tts_qual lab/README.md` shows the tombstone inventory row.

- [x] **Step 1: Write the failing test**

```bash
rg -n 'breeze_tts_qual' lab/README.md
```

Expected: no matches.

- [x] **Step 2: Confirm fail**

Same command. Expected: exit 1.

- [x] **Step 3: Add the row**

In the `what is here` table add:

```markdown
| `scripts/breeze_tts_qual/` | Breeze TTS 2 hybrid-int8 + CUDA-graph 4070 qual (sc-breeze-hybrid-81p) | not on the voicecat path; live mouth stays leftover qwentts |
```

Do not claim it is production.

- [x] **Step 4: Verify**

Run: `rg -n 'breeze_tts_qual' lab/README.md`

Expected: one table row containing `not on the voicecat path`.

- [x] **Step 5: Commit**

```bash
git add lab/README.md
git commit -m "lab: inventory breeze_tts_qual hybrid runtime experiment"
```

---

### Task 20: Restore leftover after GPU is free (FINAL)

**Owner:** builder
**Files:** none in repo (do not rewrite the unit)

**Verification (anti-gameable):** `systemctl --user is-active ata-speech-tts.service` prints `active`. `nvidia-smi` / torch may show qwentts VRAM again (~2.4 GiB). This task is last even on SHELF or REJECT.

- [x] **Step 1: Write the failing probe (if still parked)**

Run: `systemctl --user is-active ata-speech-tts.service`

Expected before restore: `inactive` (if Task 10 parked it). If already active because no GPU tasks ran, still execute restore (`start` is idempotent enough; do not edit the unit).

- [x] **Step 2: Confirm GPU is free of the Breeze process**

Run:

```bash
# no remaining breeze python occupying CUDA0
pgrep -af 'breeze_tts_qual|FastBreeze' || true
```

Expected: no live benchmark process. If one remains, terminate it **before** start. Do not `systemctl start` while Breeze still holds the 4070.

- [x] **Step 3: Restore (do not rewrite the unit)**

```bash
systemctl --user start ata-speech-tts.service
```

Do not `daemon-reload`. Do not edit `~/.config/systemd/user/ata-speech-tts.service`. Do not swap CosyVoice.

- [x] **Step 4: Verify**

```bash
systemctl --user is-active ata-speech-tts.service
systemctl --user show ata-speech-tts.service -p ActiveState -p SubState -p MainPID
ss -ltnp | rg 18091 || true
```

Expected: `active`; MainPID non-zero; listen on `127.0.0.1:18091`.

- [x] **Step 5: Commit**

No unit file commit. If the report needs a restore receipt line, amend `lab/docs/qualification/breeze-tts-2-hybrid-sc-breeze-hybrid-81p.md` with `leftover_restored: true` using `~` paths:

```bash
git add lab/docs/qualification/breeze-tts-2-hybrid-sc-breeze-hybrid-81p.md lab/docs/qualification/breeze-tts-2-hybrid-sc-breeze-hybrid-81p-report.json
git commit -m "lab: note leftover ata-speech-tts restore after breeze hybrid GPU"
```

---

## Lead-owned parking (not a builder product task)

After Task 20 leftover restore, the **project lead** comments the measurements on bead `sc-breeze-hybrid-81p` and **parks the bead**. Builders must not `br close`.

Record on the bead: exact runtime/config, upstream commit/revision, quantization layout / graph configuration, peak and steady-state VRAM, warm TTFA p50/p95, first PCM vs first audible PCM, sustained RTF, streaming chunk cadence/jitter, startup/warmup cost, clone/voice-direction quality notes, 4070 surprises, and whether hybrid or pruned/narrow official BF16 wins. If a pruned/narrow official BF16 candidate is tested, it stays in this same bead (config B).

SHELF (or any other recommendation) is not permission to integrate, propose a pin-swap, or spawn a follow-on wiring bead.

---

## Notes for builders

- CPU unittest command for Tasks 1–7, 2E, 9, 12, 14E: `python3 -m unittest lab/scripts/test_breeze_tts_qual.py`
- Never `systemctl start ata-speech-tts.service` before Task 20.
- Never install or import ComfyUI. `comfy_kitchen` kernel only.
- Never edit leftover env, voicecat, crates, CHARTER, or `spec/`.
