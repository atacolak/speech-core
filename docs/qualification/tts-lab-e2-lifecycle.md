# TTS lab E2 lifecycle — 2026-09-09

Not a pin swap. Lab FastAPI stays up; E2 is an explicit worker process.

## Unloaded baseline (after this pass)

- GPU: Tesla T4 12282 MiB
- compute apps: leftover `ata-speech-tts` / qwen3-tts 2240 MiB only
- nvidia-smi: used 2691 MiB · free 9163 MiB
- `GET /api/runtime/e2`: `state=unloaded`, `worker_pid=null`
- leftover parked: false (live mouth restored)
- E2 required residency (measured earlier this session): 8698 MiB process / ~8837 MiB GPU used after graphs
- fail-closed threshold: 8698 MiB + 768 MiB margin = 9925820416 bytes

A full Load → one synthesis → Unload GPU cycle was not run at the end of this pass because the operator needed the GPU. Use the top-right **Load** / **Unload** control for that check; do not leave E2 loaded afterward.
