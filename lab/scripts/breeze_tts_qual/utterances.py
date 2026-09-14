"""not on the voicecat path.

Fixed texts and direction set for sc-breeze-hybrid-81p.
"""

from __future__ import annotations

TINY = ("yeah.", "got it.", "one second.")
SHORT = ("I found the issue. The worker is holding the old session open.",)
MEDIUM = (
    "The worker kept the old session open after the handshake, so every later request reused stale credentials and looked like a hang. I closed that session, restarted the listener, and confirmed new requests land on a fresh worker. If it happens again, check the idle timeout before blaming the GPU path.",
)
LONG = (
    "This is a long-form continuity check for streaming speech. A voice agent still has to keep producing audio after the first chunk, without stalls that a listener would hear as dropouts, and without drifting off the cloned voice. I am going to keep talking through a few related points so the decoder stays in the loop: the leftover mouth is qwentts and must come back when this experiment releases the GPU; CosyVoice is not being pinned; ComfyUI is not a runtime; and the only question on the table is whether hybrid int8 plus selective CUDA graphs beats our own bf16 eager baseline on this 4070 with honest first-audio clocks. If the stream jitters, if VRAM walks into the ceiling, or if the clone quality falls apart on this paragraph, that configuration is not a candidate. Keep the cadence regular, keep the voice the same person, and finish the paragraph without inserting odd pauses or artifacts that the bf16 baseline does not have.",
)
DIRECTIONS = (
    "calm and matter-of-fact",
    "slightly amused",
    "urgent but controlled",
    "quiet / thoughtful",
)
