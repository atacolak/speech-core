#!/usr/bin/env python3
"""Where a Stopped take should end. CPU only; never touches CUDA."""

from __future__ import annotations

import unittest

import numpy as np

from tts.lab.backend.services.stop_trim import find_min_energy_cut

SAMPLE_RATE = 24000
FRAME = 120  # 5 ms at 24 kHz: one comparison frame
# The Brief's own example, scaled to int16: the quietest frame is index 7.
BRIEF = [5000, 5500, 6000, 7000, 5500, 4000, 2000, 1000, 3000, 5000]
QUIETEST = 7


def shaped(levels: list[int]) -> np.ndarray:
    """One constant-amplitude comparison frame per level."""
    return np.concatenate([np.full(FRAME, level, dtype="<i2") for level in levels])


def cut(samples: np.ndarray, search_from: int) -> int | None:
    return find_min_energy_cut(
        samples, sample_rate=SAMPLE_RATE, search_from=search_from
    )


class FindMinEnergyCutTest(unittest.TestCase):
    def test_cuts_at_the_quietest_sample_of_the_flush_window(self) -> None:
        heard = shaped([6000] * 20)
        samples = np.concatenate([heard, shaped(BRIEF)])
        self.assertEqual(cut(samples, heard.size), heard.size + QUIETEST * FRAME + 1)

    def test_never_cuts_back_before_the_stop_frontier(self) -> None:
        heard = np.concatenate([shaped([6000] * 10), shaped([0] * 10)])
        samples = np.concatenate([heard, shaped(BRIEF)])
        index = cut(samples, heard.size)
        self.assertEqual(index, heard.size + QUIETEST * FRAME + 1)
        self.assertGreater(index, heard.size)

    def test_empty_window_has_no_cut(self) -> None:
        samples = shaped([6000] * 10)
        self.assertIsNone(cut(samples, samples.size))

    def test_window_shorter_than_two_frames_has_no_cut(self) -> None:
        heard = shaped([6000] * 10)
        samples = np.concatenate([heard, np.full(200, 1000, dtype="<i2")])
        self.assertIsNone(cut(samples, heard.size))

    def test_energy_flat_window_has_no_cut(self) -> None:
        heard = shaped([6000] * 10)
        samples = np.concatenate([heard, shaped([4000] * 10)])
        self.assertIsNone(cut(samples, heard.size))

    def test_digital_silence_window_has_no_cut(self) -> None:
        heard = shaped([6000] * 10)
        samples = np.concatenate([heard, shaped([0] * 10)])
        self.assertIsNone(cut(samples, heard.size))

    def test_ties_take_the_earliest_quiet_frame(self) -> None:
        samples = shaped([5000, 1000, 5000, 1000])
        self.assertEqual(cut(samples, 0), FRAME + 1)

    def test_trailing_partial_frame_is_not_searched(self) -> None:
        samples = np.concatenate(
            [shaped([5000, 1000, 5000]), np.full(60, 5, dtype="<i2")]
        )
        self.assertEqual(cut(samples, 0), FRAME + 1)


if __name__ == "__main__":
    unittest.main()
