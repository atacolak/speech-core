# speech-core

Real-time speech substrate for human/agent interaction.

```text
speech-in   → microphone audio → transcript + turn events
speech-out  → text → audible speech
agent loop  → decides what to do with a completed turn
```

The mature seam is **speech-in**. It runs a separate **speech-core-daemon** that ingests timestamped PCM, transcribes with Nemotron, detects voice activity with Silero, semantically endpoints with smart-turn v3, and emits immutable per-turn transcripts.

**speech-out** is a separate TTS/playback daemon. Input and output do not share a process because their failure modes differ: speech-in must stay low-latency; speech-out owns model warmup, queues, and interruption.

## Components

```text
speech-in
  speech-core-daemon      ASR + VAD + turn detection
  speech-core-mic-adapter CPAL mic → websocket
  speech-core-file-adapter WAV replay → websocket
  speech-core-watch       transcript/event subscriber + TUI
  speech-core-protocol    shared messages

speech-out
  speech-out              TTS + playback streaming
```

## Core invariants

- A turn, once closed, is immutable. Late punctuation or finalization events do not revise operator-visible state.
- `transcript_committed` is the authoritative per-turn snapshot. It is emitted after model drain and before `turn_closed`. Controllers dispatch on it.
- `transcript_finalized` is diagnostic-only.
- VAD proposes boundaries; smart-turn checks semantic completion; a 2500 ms acoustic fallback prevents hangs.
- Sustained speech-like audio without ASR tokens for 7500 ms emits `turn_human_hold` and forces a degraded close.
- RMS energy gating is available server-side as an onset veto. It is currently a fixed-threshold gate and is intentionally conservative.

## Environment

| variable | purpose |
|----------|---------|
| `SPEECH_CORE_WS_URL` | `ws://host:8765/ws/audio-ingress` |
| `SPEECH_OUT_WS_URL` | `ws://host:8788/ws/speech-out` |
| `SPEECH_CORE_MODEL_PATH` | Nemotron GGUF |
| `SPEECH_CORE_VAD_MODEL_PATH` | Silero VAD ONNX |
| `SPEECH_CORE_SMART_TURN_MODEL_PATH` | smart-turn-v3 ONNX |

Install scripts write these to `~/.config/speech-core/daemon.env` and `client.env`.

## Run

Server:

```bash
./scripts/install-speech-core-daemon.sh
systemctl --user restart speech-core-daemon
```

Laptop (NixOS — build natively):

```bash
./scripts/speech-core-sync-build-adapter.sh
speech-core-live-session
speech-out-live-session --response-text "hello"
```

Inspect events:

```bash
tail -f ~/.local/state/speech-core/logs/events.jsonl
```

## Documentation

- `docs/current-state.md` — what works right now
- `docs/turn-detection.md` — exact EOU triggers and tuning knobs
- `docs/seams.md` — component boundaries and contracts
- `docs/speech-output.md` — speech-out protocol and cancellation

## What we are doing next

- Controller skeleton: consume `transcript_committed`, dispatch agent turns, and manage assistant/user turn alternation.
- Barge-in alignment: when the user interrupts assistant playback, cut the assistant transcript at the sample where user speech began.
- Adaptive RMS gate: replace the fixed threshold with a noise-floor-relative onset veto, especially during TTS playback.
- Monolith cleanup: split `turn.rs`, `speech-core-watch/src/main.rs`, and `speech-core-golden.py` into smaller modules once the controller contract is stable.
