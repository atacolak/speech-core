#!/usr/bin/env python3
"""Offline tests for speech_talker_session helpers + barge history truncate."""

from __future__ import annotations

import json
import runpy
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent
MOD = runpy.run_path(str(ROOT / "speech_talker_session.py"))


class Helpers(unittest.TestCase):
    def test_speech_evidence(self) -> None:
        self.assertTrue(MOD["is_speech_evidence"]("hi"))
        self.assertTrue(MOD["is_speech_evidence"]("42"))
        self.assertFalse(MOD["is_speech_evidence"]("..."))
        self.assertFalse(MOD["is_speech_evidence"]("  "))

    def test_first_sentence(self) -> None:
        self.assertEqual(MOD["first_sentence"]("Hello there. More.", 120), "Hello there.")
        self.assertTrue(len(MOD["first_sentence"]("word " * 50, 40)) <= 40)

    def test_approx_cut(self) -> None:
        cut = MOD["approx_cut_by_played_ms"]("one two three four five", 1000, wps=2.0)
        self.assertEqual(cut, "one two")


class BargeTriple(unittest.TestCase):
    def test_truncate_rewrites_history(self) -> None:
        ns = argparse_namespace()
        sess = MOD["SpeechTalkerSession"](ns)
        sess.state.history = [
            {"role": "user", "text": "hi"},
            {"role": "assistant", "text": "one two three four five six seven"},
        ]
        sess.state.current = MOD["TurnState"](
            intended="one two three four five six seven",
            hear_start_ms=MOD["now_ms"]() - 900,
            utterance_id="u1",
        )
        sess.state.speaking = True
        prefix = sess.truncate_history_to_heard("test")
        self.assertTrue(prefix)
        self.assertEqual(sess.state.history[-1]["role"], "assistant")
        self.assertEqual(sess.state.history[-1].get("truncated"), "true")
        self.assertTrue(len(sess.state.history[-1]["text"].split()) <= 7)
        # events file got assistant_turn_truncated
        events = Path(ns.run_dir, "events.jsonl").read_text(encoding="utf-8").strip().splitlines()
        last = json.loads(events[-1])
        self.assertEqual(last["event"], "assistant_turn_truncated")
        self.assertEqual(last["primary_cut_source"], "approx_wallclock")


def argparse_namespace():
    import argparse

    run = tempfile.mkdtemp(prefix="talker-test-")
    # minimal namespace matching attributes used by SpeechTalkerSession.__init__
    return argparse.Namespace(
        run_dir=run,
        stream_session_id="test-session",
        pi_session="",
        print_events=False,
        core_ws_url="ws://127.0.0.1:8765/ws/audio-ingress",
        out_ws_url="ws://127.0.0.1:8788/ws/speech-out",
        stream_id="test",
        adapter_id="test",
        sample_rate_hz=16000,
        channels=1,
        format="pcm-s16-le",
        frame_ms=20,
        profile="talker",
        model="cpa/deepseek-v4-flash",
        thinking="low",
        pi_bin="/bin/true",
        voice="M1",
        lang="en",
        steps=5,
        speed=1.3,
        play_command="true",
        chunk_min_chars=8,
        chunk_max_chars=160,
        no_mic=True,
        replay_events="",
        record_wav="",
        talker_timeout_s=5,
        once_text="",
        watch_bin="/bin/true",
        speech_out_bin="/bin/true",
        mic_adapter="/bin/true",
    )


if __name__ == "__main__":
    unittest.main()
