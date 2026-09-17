"""Where a Stopped GENERATE take should end. not on the voicecat path.

Stop already has a flush: the pcm that landed in the take buffer between the
moment Stop ran and the last appended sample. The saved wav is cut at the
quietest sample of that window. Nothing is generated after Stop, nothing is
appended, and nothing before the window is ever removed. An empty, too-short,
or energy-flat window keeps the take's last sample.
"""

from __future__ import annotations

from typing import Any

import numpy as np

FRAME_MS = 5  # 120 samples at 24 kHz: the energy comparison frame


def find_min_energy_cut(
    samples: Any,
    *,
    sample_rate: int,
    search_from: int,
) -> int | None:
    """One past the quietest sample of the quietest frame after `search_from`.

    None when the window is empty, holds fewer than two whole frames, or every
    frame carries the same energy; the caller then keeps the last sample.
    """
    arr = np.asarray(samples)
    total = int(arr.size)
    start = max(0, int(search_from))
    frame = max(1, int(sample_rate * FRAME_MS / 1000))
    count = max(0, total - start) // frame
    if count < 2:
        return None
    window = arr[start : start + count * frame].astype(np.float64).reshape(count, frame)
    energy = np.mean(np.square(window), axis=1)
    if float(energy.max()) == float(energy.min()):
        return None
    quietest = int(np.argmin(energy))
    inside = int(np.argmin(np.abs(window[quietest])))
    return start + quietest * frame + inside + 1
