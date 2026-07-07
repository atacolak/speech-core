#!/usr/bin/env python3
"""Print a compact speech-core endpointing timeline for one stream session."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

SAMPLE_RATE = 16_000


def ms(samples: int | float | None) -> int:
    if samples is None:
        return 0
    return int(samples * 1000 // SAMPLE_RATE)


def field(event: dict[str, Any], name: str, default: Any = None) -> Any:
    value = event.get(name, default)
    return default if value is None else value


def time_key(event: dict[str, Any]) -> tuple[int, int]:
    # Sort by the acoustic sample first, then by decision/daemon time.
    for name in ("sample_start", "start_sample", "end_sample", "source_sample_start_estimate"):
        if name in event and event[name] is not None:
            return int(event[name]), int(field(event, "decision_sample", event.get("daemon_mono_ns", 0)))
    return int(field(event, "decision_sample", 0)), int(field(event, "daemon_mono_ns", 0))


def line_for(event: dict[str, Any]) -> str | None:
    kind = event.get("event")
    if kind == "vad_session_start":
        return (
            f"cfg  vad frame={field(event, 'frame_ms')}ms threshold={field(event, 'threshold')} "
            f"onset={field(event, 'onset_frames')} hangover={field(event, 'hangover_frames')} "
            f"emit_frames={field(event, 'emit_frames')}"
        )
    if kind == "smart_turn_session_start":
        return (
            f"cfg  smart_turn threshold={float(field(event, 'threshold', 0)):.2f} "
            f"budget={field(event, 'timeout_ms')}ms model={Path(str(field(event, 'model_path', 'unknown'))).name}"
        )
    if kind == "vad_state":
        start = int(field(event, "sample_start", 0))
        count = int(field(event, "sample_count", 0))
        return (
            f"{ms(start):>7}ms vad  state {ms(start):>7}-{ms(start + count):<7}ms "
            f"p={float(field(event, 'probability', 0)):.3f}/{float(field(event, 'threshold', 0)):.2f} "
            f"raw={'speech' if field(event, 'raw_is_speech', False) else 'silence':<7} "
            f"state={'in-speech' if field(event, 'smoothed_in_speech', False) else 'idle':<9} "
            f"silence={field(event, 'silence_counter', 0)}/{field(event, 'hangover_frames', 0)}"
        )
    if kind == "vad_speech_start":
        start = int(field(event, "start_sample", 0))
        decision = int(field(event, "decision_sample", 0))
        return (
            f"{ms(start):>7}ms vad  START start={ms(start)}ms decision={ms(decision)}ms "
            f"delay={ms(decision - start)}ms p={float(field(event, 'confidence', 0)):.3f}"
        )
    if kind == "vad_speech_end":
        start = int(field(event, "start_sample", 0))
        end = int(field(event, "end_sample", 0))
        decision = int(field(event, "decision_sample", 0))
        return (
            f"{ms(end):>7}ms vad  END   speech={ms(end - start)}ms "
            f"end={ms(end)}ms decision={ms(decision)}ms silence={ms(decision - end)}ms "
            f"p={float(field(event, 'confidence', 0)):.3f}"
        )
    if kind == "smart_turn_decision":
        end = int(field(event, "end_sample", 0))
        decision = int(field(event, "decision_sample", 0))
        label = "complete" if field(event, "complete", False) else "hold"
        return (
            f"{ms(decision):>7}ms st   {label:<8} end={ms(end)}ms decision={ms(decision)}ms "
            f"p={float(field(event, 'probability', 0)):.3f}/{float(field(event, 'threshold', 0)):.2f} "
            f"cost={float(field(event, 'inference_duration_ms', 0)):.0f}ms "
            f"feat={float(field(event, 'feature_duration_ms', 0)):.0f} onnx={float(field(event, 'model_duration_ms', 0)):.0f} "
            f"over_budget={field(event, 'timed_out', False)}"
        )
    if kind == "turn_eou_suppressed":
        end = int(field(event, "end_sample", 0))
        decision = int(field(event, "decision_sample", 0))
        return (
            f"{ms(decision):>7}ms turn no-eou source={field(event, 'source')} "
            f"reason={field(event, 'reason')} end={ms(end)}ms decision={ms(decision)}ms"
        )
    if kind == "turn_closed":
        end = int(field(event, "end_sample", 0))
        decision = int(field(event, "decision_sample", 0))
        return (
            f"{ms(decision):>7}ms turn EOU    source={field(event, 'source')} "
            f"reason={field(event, 'reason')} degraded={field(event, 'degraded')} "
            f"end={ms(end)}ms decision={ms(decision)}ms"
        )
    if kind == "transcript_token_committed":
        t0 = int(field(event, "source_sample_start_estimate", 0))
        t1 = int(field(event, "source_sample_end_estimate", t0))
        text = str(field(event, "text", "")).replace("\n", "\\n")
        return (
            f"{ms(t0):>7}ms asr  token {ms(t0):>7}-{ms(t1):<7}ms "
            f"text={text!r} p={float(field(event, 'probability', 0)):.2f} "
            f"committed={field(event, 'audio_committed_ms', 0)}ms"
        )
    return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("session_id")
    parser.add_argument(
        "--events",
        default=str(Path.home() / ".local/state/speech-core/logs/events.jsonl"),
        help="events.jsonl path",
    )
    parser.add_argument("--tokens", action="store_true", help="include ASR token commits")
    parser.add_argument("--all-vad-frames", action="store_true", help="include full vad_frame events too")
    args = parser.parse_args()

    wanted = {
        "vad_session_start",
        "smart_turn_session_start",
        "vad_state",
        "vad_speech_start",
        "vad_speech_end",
        "smart_turn_decision",
        "turn_eou_suppressed",
        "turn_closed",
    }
    if args.tokens:
        wanted.add("transcript_token_committed")
    if args.all_vad_frames:
        wanted.add("vad_frame")

    events: list[dict[str, Any]] = []
    with open(args.events, "r", encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if event.get("stream_session_id") != args.session_id:
                continue
            if event.get("event") in wanted:
                events.append(event)

    for event in sorted(events, key=time_key):
        text = line_for(event)
        if text:
            print(text)


if __name__ == "__main__":
    main()
