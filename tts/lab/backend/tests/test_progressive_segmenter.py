from __future__ import annotations

import unittest

from tts.lab.backend.services.segmenter import (
    SEGMENT_MAX_CHARS,
    SEGMENT_MIN_CHARS,
    segment_text,
)


class ProgressiveSegmenterTest(unittest.TestCase):
    def test_prefers_paragraph_then_sentence_boundaries(self) -> None:
        paragraph = (("Alpha " * 30) + "ends here.").strip()
        second = (("Beta " * 60) + "continues safely!").strip()
        parts = segment_text(f"{paragraph}\n\n{second}")
        self.assertEqual(SEGMENT_MIN_CHARS, 120)
        self.assertEqual(SEGMENT_MAX_CHARS, 420)
        self.assertEqual(parts[0], paragraph)
        self.assertTrue(parts[-1].endswith("!"))

    def test_keeps_decimal_abbreviation_and_cjk_words(self) -> None:
        prefix = "Steady context " * 10
        tail = " More context follows here." * 4
        text = prefix + "uses 3.14 and e.g. this value. 下一句结束。 Tail." + tail
        parts = segment_text(text, min_chars=80, max_chars=200)
        self.assertNotEqual(parts[0][-2:], "3.")
        self.assertNotEqual(parts[0][-3:], "e.g")
        self.assertTrue(parts[0].endswith("."))
        self.assertTrue(all(len(part) <= 200 for part in parts))
        self.assertEqual(" ".join(" ".join(parts).split()), " ".join(text.split()))

    def test_run_on_sentence_never_exceeds_hard_cap(self) -> None:
        text = ("alpha beta gamma delta " * 300) + "end."
        parts = segment_text(text)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(part) <= SEGMENT_MAX_CHARS for part in parts))
        self.assertEqual(
            " ".join(" ".join(parts).split()),
            " ".join(text.split()),
        )

    def test_falls_back_to_whitespace_then_hard_cut(self) -> None:
        words = "word " * 100
        parts = segment_text(words, min_chars=120, max_chars=420)
        self.assertTrue(all(len(part) <= 420 for part in parts))
        self.assertTrue(all(not part.endswith("wor") for part in parts))
        token = "x" * 421
        self.assertEqual(segment_text(token), ["x" * 420, "x"])

    def test_empty_text_has_no_segments(self) -> None:
        self.assertEqual(segment_text(" \n\t "), [])


if __name__ == "__main__":
    unittest.main()
