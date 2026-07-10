"""Exit codes, constants, and shared types for the golden assertion suite."""

from __future__ import annotations

import enum
from typing import Any, Dict, List, Optional

# ── exit codes (matching spec §13) ───────────────────────────────────────────
EXIT_PASS = 0
EXIT_ASSERTION_FAILED = 1
EXIT_MANIFEST_INVALID = 2
EXIT_QUALITY_FAILED = 3
EXIT_CONSENT_REQUIRED = 4
EXIT_DEPENDENCY_MISSING = 5
EXIT_DAEMON_UNREACHABLE = 6
EXIT_MODEL_UNAVAILABLE = 7
EXIT_CAPTURE_TIMEOUT = 8
EXIT_TERMINAL_MARKER_MISSING = 9
EXIT_ARTIFACT_HASH_MISMATCH = 10
EXIT_PRIVACY_POLICY_VIOLATION = 11
EXIT_CONFIG_MISMATCH = 12
EXIT_UNSUPPORTED_PROFILE = 13
EXIT_SCENARIO_NOT_FOUND = 14
EXIT_RECORDER_ABORTED = 15
EXIT_SYNTH_GENERATION_FAILED = 16
EXIT_WAV_FORMAT_INVALID = 17
EXIT_EVENT_SCHEMA_INVALID = 18
EXIT_BASELINE_REQUIRES_REVIEW = 19
EXIT_INTERNAL_ERROR = 20
EXIT_CAPTURE_INCOMPLETE = 21

EXIT_NAMES: Dict[int, str] = {
    0: "PASS",
    1: "ASSERTION_FAILED",
    2: "MANIFEST_INVALID",
    3: "QUALITY_FAILED",
    4: "CONSENT_REQUIRED",
    5: "DEPENDENCY_MISSING",
    6: "DAEMON_UNREACHABLE",
    7: "MODEL_UNAVAILABLE",
    8: "CAPTURE_TIMEOUT",
    9: "TERMINAL_MARKER_MISSING",
    10: "ARTIFACT_HASH_MISMATCH",
    11: "PRIVACY_POLICY_VIOLATION",
    12: "CONFIG_MISMATCH",
    13: "UNSUPPORTED_PROFILE",
    14: "SCENARIO_NOT_FOUND",
    15: "RECORDER_ABORTED",
    16: "SYNTH_GENERATION_FAILED",
    17: "WAV_FORMAT_INVALID",
    18: "EVENT_SCHEMA_INVALID",
    19: "BASELINE_REQUIRES_REVIEW",
    20: "INTERNAL_ERROR",
    21: "CAPTURE_INCOMPLETE",
}

# ── timing constants at 16 kHz ───────────────────────────────────────────────
SAMPLE_RATE = 16_000
TRANSPORT_FRAME_SAMPLES = 320  # 20 ms
VAD_NATIVE_FRAME_SAMPLES = 512  # 32 ms
VAD_ONSET_DEFAULT_SAMPLES = 1024  # 2 frames
VAD_HANGOVER_DEFAULT_SAMPLES = 1536  # 3 frames
TURN_MIN_VAD_SPEECH_SAMPLES = 6400  # 400 ms
INSTALLED_FALLBACK_SAMPLES = 40000  # 2500 ms
CODE_DEFAULT_FALLBACK_SAMPLES = 56000  # 3500 ms
HUMAN_HOLD_SAMPLES = 120000  # 7500 ms
TRANSCRIPT_SILENCE_SAMPLES = 11200  # 700 ms
SMART_TURN_RECHECK_SAMPLES = [1536, 3072, 6144, 12288, 24576]  # 96/192/384/768/1536 ms


def ms_to_samples(ms: int) -> int:
    """Convert milliseconds to 16 kHz sample count."""
    return ms * SAMPLE_RATE // 1000


def samples_to_ms(samples: int) -> int:
    """Convert samples to milliseconds at 16 kHz."""
    return int(samples * 1000 // SAMPLE_RATE)


# ── terminal markers for golden-mvp profile ──────────────────────────────────
DEFAULT_TERMINAL_MARKERS = [
    {"event": "vad_session_end"},
    {"event": "turn_session_end"},
    {"event": "smart_turn_session_end"},
    {"event": "model_chunk_processed", "where": {"is_final": True}},
]

# ── unstable field blacklist ─────────────────────────────────────────────────
UNSTABLE_FIELD_PATTERNS = [
    "daemon_mono_ns",
    "_mono_ns",
    "source_capture_mono_ns",
    "adapter_send_mono_ns",
    "ingress_receive_mono_ns",
    "ingress_queue_enter_mono_ns",
    "ingress_queue_exit_mono_ns",
    "stream_session_id",
    "turn_id",
    "adapter_id",
    "stream_id",
    "adapter_clock_id",
    "uuid",
    "nonce",
    "mono_ns",
]

UNSTABLE_WHOLE_FIELD_NAMES = {
    "inference_duration_ms",
    "feature_duration_ms",
    "model_duration_ms",
    "open_duration_ms",
    "queue_depth",
    "ingress_queue_depth_frames",
    "adapter_hello_send_mono_ns",
}


def is_unstable_field(field_name: str) -> bool:
    """Check if a field name matches the unstable blacklist."""
    if field_name in UNSTABLE_WHOLE_FIELD_NAMES:
        return True
    for pattern in UNSTABLE_FIELD_PATTERNS:
        if pattern in field_name:
            return True
    return False


# ── allowed turn close sources ───────────────────────────────────────────────
ALLOWED_CLOSE_SOURCES = {
    "smart_turn",
    "vad",
    "vad_acoustic_fallback",
    "human_hold",
    "transcript_silence",
    "model_eou",
}

FORBIDDEN_NORMAL_CLOSE_SOURCES = {
    "session_end",
}

# ── event type helpers ───────────────────────────────────────────────────────
TERMINAL_EVENT_TYPES = {
    "vad_session_end",
    "turn_session_end",
    "smart_turn_session_end",
    "model_chunk_processed",
}

SESSION_START_EVENT_TYPES = {
    "stream_start",
    "vad_session_start",
    "smart_turn_session_start",
    "turn_session_start",
}

# ── typed aliases ────────────────────────────────────────────────────────────
Event = Dict[str, Any]
EventStream = List[Event]
Selector = Dict[str, Any]


class CaptureValidity(enum.Enum):
    VALID = "valid"
    INCOMPLETE = "incomplete"
    SCHEMA_INVALID = "schema_invalid"
    WRONG_SESSION = "wrong_session"
    STALE = "stale"
    EMPTY = "empty"
    MALFORMED = "malformed"
