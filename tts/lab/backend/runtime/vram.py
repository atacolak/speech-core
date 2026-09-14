"""GPU VRAM probe. Prefer nvidia-smi process-global view over PyTorch."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from typing import Protocol


class VramProbe(Protocol):
    def free_bytes(self, gpu_index: int) -> int: ...
    def used_bytes(self, gpu_index: int) -> int: ...


@dataclass
class FixedVramProbe:
    """Test double. Does not touch a real GPU."""

    free: int
    used: int = 0

    def free_bytes(self, gpu_index: int) -> int:
        del gpu_index
        return int(self.free)

    def used_bytes(self, gpu_index: int) -> int:
        del gpu_index
        return int(self.used)


class NvidiaSmiVramProbe:
    """nvidia-smi memory.free / memory.used in bytes."""

    def __init__(self, binary: str | None = None) -> None:
        self.binary = binary or shutil.which("nvidia-smi") or "nvidia-smi"

    def _query(self, field: str, gpu_index: int) -> int:
        result = subprocess.run(
            [
                self.binary,
                f"--id={gpu_index}",
                f"--query-gpu={field}",
                "--format=csv,nounits,noheader",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or f"nvidia-smi {field} failed")
        mib = float(result.stdout.strip().splitlines()[0].strip())
        return int(mib * 1024 * 1024)

    def free_bytes(self, gpu_index: int) -> int:
        return self._query("memory.free", gpu_index)

    def used_bytes(self, gpu_index: int) -> int:
        return self._query("memory.used", gpu_index)
