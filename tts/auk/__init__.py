"""AuK: the isolated audio-editing model, ported without ComfyUI.

Nothing in this package is imported by the lab process. The lab only reads
``tts.auk.pin`` (torch-free) and talks JSON lines to the worker that runs
``tts.auk.runtime`` inside the AuK venv.
"""
