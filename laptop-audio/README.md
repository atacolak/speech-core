# laptop-audio

Local OS-level microphone configuration tools for the sfnix laptop. These are **not** part of the speech-core daemon or speech seams.

## Why this is separate

speech-core handles two things: **speech-in** (microphone audio → transcript + turn events) and **speech-out** (text → audible speech). Both seams are transport and model logic — they don't care what microphone you're using, how it's routed, or whether echo cancellation is on.

But real laptops are messy. The microphone picks up speaker playback. Fan noise, keyboard clatter, street sounds bleed in. PipeWire routing needs to be told which source to use. These are OS-level audio hygiene problems, not speech perception problems.

This directory holds the tools that make the laptop's microphone signal clean before speech-core ever sees it:

- **AEC** (acoustic echo cancellation) — PipeWire/Pulse WebRTC echo cancel module so the mic doesn't feed speaker output back into ASR
- **DPDFNet** (experimental) — neural noise suppression for background noise
- **Sway keybinds** — keyboard shortcuts for toggling AEC during a session

## What's here

| Script | Purpose |
|--------|---------|
| `aec-toggle.sh` | Toggle PipeWire WebRTC echo cancellation on/off. Creates virtual source/sink, routes defaults. |
| `aec-diagnose.sh` | Print current PipeWire/Pulse source/sink/module state. |
| `install-sway-bind.sh` | Install Mod4+F9 as AEC toggle keybind in Sway. |
| `dpdfnet-toggle.sh` | Start/stop DPDFNet neural noise suppression proxy. |
| `dpdfnet-mic.sh` | DPDFNet mic proxy wrapper (called by systemd service). |
| `dpdfnet-test.sh` | Record N seconds through DPDFNet and save raw + denoised WAVs. |
| `sfnix-mic-denoise.py` | Python DPDFNet denoising proxy — captures via sounddevice, denoises, sends to speech-core WebSocket. |
| `test_dpdfnet_perf.py` | DPDFNet performance benchmark. |

## Relationship to speech-core

```
laptop-audio/                  speech-core/
┌──────────────────┐          ┌──────────────────────────┐
│ AEC toggle       │          │ speech-in  (STT/ASR)     │
│ DPDFNet denoise   │ ──clean──►  turn detection          │
│ device selection │  source  │                          │
│ PipeWire routing │          │ speech-out (TTS/playback) │
└──────────────────┘          └──────────────────────────┘
```

speech-core opens the OS default audio input via CPAL. If you've configured AEC, the default source is the echo-cancelled virtual device, and speech-core gets clean audio with no code changes. Neither side needs to import the other.

## Status

- **AEC** — functional. `aec-toggle.sh --on` creates a `speech_core_echo_cancel` virtual source with WebRTC AEC. This is the primary path for clean laptop audio.
- **DPDFNet** — experimental and unproven. The test harness produces 0-byte WAVs under investigation. It is noise suppression, not echo cancellation, and is not a system input device — it proxies audio through Python.
- **Keybinds** — Mod4+F9 wired to AEC toggle. Sway-only.
