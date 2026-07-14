"""Barge-in cut aligners: map played assistant audio to intended text.

Backends implement forced alignment of *intended* LLM/TTS text onto *played*
PCM — not open-ended ASR. Nemotron B is not part of this package.
"""

from .backend import AlignCursor, align_played_clip, list_backends

__all__ = ["AlignCursor", "align_played_clip", "list_backends"]
