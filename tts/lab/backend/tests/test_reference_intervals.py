#!/usr/bin/env python3
"""Canonical keep-interval math. not on the voicecat path."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO))

from tts.lab.backend.models import Interval
from tts.lab.backend.services.references import (
    exclude_interval,
    exclude_intervals,
    keep_duration_s,
    keep_only_interval,
    normalize_keep_intervals,
    slice_transcript,
)


def _pairs(intervals: list[Interval]) -> list[list[float]]:
    return [[iv.start_s, iv.end_s] for iv in intervals]


class KeepIntervalMath(unittest.TestCase):
    duration = 20.0

    def test_multiple_excludes_in_one_call(self) -> None:
        keep = normalize_keep_intervals(
            [Interval(start_s=0.0, end_s=20.0)], self.duration
        )
        keep = exclude_intervals(
            keep,
            [
                Interval(start_s=4.0, end_s=7.0),
                Interval(start_s=11.0, end_s=13.0),
            ],
        )
        self.assertEqual(_pairs(keep), [[0.0, 4.0], [7.0, 11.0], [13.0, 20.0]])

    def test_spec_exclude_then_keep_only(self) -> None:
        keep = normalize_keep_intervals(
            [Interval(start_s=0.0, end_s=20.0)], self.duration
        )
        self.assertEqual(_pairs(keep), [[0.0, 20.0]])

        keep = exclude_interval(keep, Interval(start_s=4.0, end_s=7.0))
        self.assertEqual(_pairs(keep), [[0.0, 4.0], [7.0, 20.0]])

        keep = exclude_interval(keep, Interval(start_s=11.0, end_s=13.0))
        self.assertEqual(_pairs(keep), [[0.0, 4.0], [7.0, 11.0], [13.0, 20.0]])

        keep = keep_only_interval(Interval(start_s=7.0, end_s=11.0), self.duration)
        self.assertEqual(_pairs(keep), [[7.0, 11.0]])

    def test_overlap_normalization(self) -> None:
        keep = normalize_keep_intervals(
            [
                Interval(start_s=0.0, end_s=10.0),
                Interval(start_s=8.0, end_s=15.0),
            ],
            self.duration,
        )
        self.assertEqual(_pairs(keep), [[0.0, 15.0]])

    def test_adjacent_intervals_merge(self) -> None:
        keep = normalize_keep_intervals(
            [
                Interval(start_s=0.0, end_s=4.0),
                Interval(start_s=4.0, end_s=7.0),
            ],
            self.duration,
        )
        self.assertEqual(_pairs(keep), [[0.0, 7.0]])

    def test_zero_length_rejected(self) -> None:
        with self.assertRaises(ValueError):
            Interval(start_s=5.0, end_s=5.0)
        with self.assertRaises(ValueError):
            normalize_keep_intervals(
                [Interval(start_s=0.0, end_s=20.0), Interval(start_s=9.0, end_s=9.0)],
                self.duration,
            )

    def test_negative_coordinate_rejected(self) -> None:
        with self.assertRaises(ValueError):
            Interval(start_s=-1.0, end_s=2.0)
        with self.assertRaises(ValueError):
            exclude_interval(
                [Interval(start_s=0.0, end_s=20.0)],
                Interval(start_s=-0.5, end_s=1.0),
            )

    def test_end_beyond_duration_rejected(self) -> None:
        with self.assertRaises(ValueError):
            normalize_keep_intervals(
                [Interval(start_s=18.0, end_s=22.0)], self.duration
            )
        with self.assertRaises(ValueError):
            keep_only_interval(Interval(start_s=19.0, end_s=21.0), self.duration)


class TranscriptSlice(unittest.TestCase):
    words = [
        {"text": "Can", "start_s": 0.0, "end_s": 4.0},
        {"text": "the", "start_s": 4.0, "end_s": 8.0},
        {"text": "Greys", "start_s": 8.0, "end_s": 12.0},
        {"text": "run", "start_s": 12.0, "end_s": 16.0},
        {"text": "the", "start_s": 16.0, "end_s": 20.0},
        {"text": "reactors?", "start_s": 20.0, "end_s": 24.0},
    ]

    def test_keep_only_middle_sentence(self) -> None:
        keep = keep_only_interval(Interval(start_s=8.0, end_s=16.0), 24.0)
        self.assertEqual(slice_transcript(self.words, keep), "Greys run")
        self.assertAlmostEqual(keep_duration_s(keep), 8.0)

    def test_exclude_middle_word(self) -> None:
        keep = exclude_interval(
            normalize_keep_intervals([Interval(start_s=0.0, end_s=24.0)], 24.0),
            Interval(start_s=8.0, end_s=16.0),
        )
        self.assertEqual(slice_transcript(self.words, keep), "Can the the reactors?")
        self.assertAlmostEqual(keep_duration_s(keep), 16.0)

    def test_empty_words_is_empty_slice(self) -> None:
        self.assertEqual(slice_transcript([], [Interval(start_s=0.0, end_s=4.0)]), "")


if __name__ == "__main__":
    unittest.main()
