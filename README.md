# speech-core

Real-time speech substrate for human/agent interaction.

> **Branch `feature/assistant-self-asr`:** live dogfood loop with barge-in cut (provisional wall-clock → async CTC) and TUI greying. Not merged to `main` yet.

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
  speech-out              TTS + playback (HTTP TTS → websocket audio frames)

dogfood (laptop)
  speech-out-live-session[-dogfood]   mic + TUI + TTS + barge-in cut (canned reply)
  speech-talker-session               mic → Talker profile → TTS + interrupt triple
  scripts/barge_in_align/             host warm CTC worker (optional refine)
```

## Core invariants

- A turn, once closed, is immutable. Late punctuation or finalization events do not revise operator-visible state.
- `transcript_committed` is the authoritative per-turn snapshot. It is emitted after model drain and before `turn_closed`. Controllers dispatch on it.
- `transcript_finalized` is diagnostic-only.
- VAD proposes boundaries; smart-turn checks semantic completion; a 2500 ms acoustic fallback prevents hangs.
- Sustained speech-like audio without ASR tokens for 7500 ms emits `turn_human_hold` and forces a degraded close.
- RMS energy gating is available server-side as an onset veto. It is currently a fixed-threshold gate and is intentionally conservative.
- Barge-in (dogfood): pause playback on the first alphanumeric user ASR token; provisional cut from wall-clock playback; async CTC refine when the warm align worker is up. Greying updates the same assistant line (dim spoken / white unsaid).

## Environment

| variable | purpose |
|----------|---------|
| `SPEECH_CORE_WS_URL` | `ws://host:8765/ws/audio-ingress` |
| `SPEECH_OUT_WS_URL` | `ws://host:8788/ws/speech-out` |
| `SPEECH_CORE_MODEL_PATH` | Nemotron GGUF |
| `SPEECH_CORE_VAD_MODEL_PATH` | Silero VAD ONNX |
| `SPEECH_CORE_SMART_TURN_MODEL_PATH` | smart-turn-v3 ONNX |
| `SPEECH_OUT_STEPS` | Supertonic quality steps (dogfood default **5**; lower is faster, worse) |
| `SPEECH_OUT_ASSISTANT_SELF_ASR` | dual-Nemotron self-ASR (`0` default — off) |
| `SPEECH_OUT_CUPE_LIVE` | experimental live position tracker (`0` default — off) |
| `SPEECH_OUT_ALIGN_BACKEND` | barge refine backend (`ctc_forced` when align stack present) |

Install scripts write core URLs/paths to `~/.config/speech-core/daemon.env` and `client.env`.

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
```

### Dogfood (barge-in + greying)

On the laptop, after client install. Prefer an absolute path if `~/.local/bin` is not on `PATH`:

```bash
SPEECH_OUT_CUPE_LIVE=0 \
SPEECH_OUT_ASSISTANT_SELF_ASR=0 \
~/.local/bin/speech-out-live-session-dogfood
```

Or from `~/.local/bin`: `./speech-out-live-session-dogfood`.

**Talker voice loop (MVP B)** — real `pi --profile talker` answers (not canned text), reasoner tools stubbed:

```bash
# synthetic one turn (no mic)
./scripts/speech-talker-session.sh --no-mic --once-text "what are you?"

# live mic → Talker → Supertonic (uses client.env WS URLs)
./scripts/speech-talker-session.sh
```

Interrupt triple on barge: stop playout, cancel Talker gen, truncate assistant history to heard prefix.

Mid-phrase barge → playback stops; assistant line greys (dim spoken / white unsaid). Session artifacts:

```text
~/.local/state/speech-core/session/speech-out-<id>/
  mic.wav  trigger.log  watch.jsonl  ui-events.jsonl
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
- `docs/barge-in-dual-asr.md` — dual-Nemotron path notes (historical; not the default)

## Now vs later

**On this branch (dogfood):**

- Barge-in stop on first alphanumeric user token; provisional wall-clock cut; CTC refine via warm TCP worker when available
- TUI greying on the original assistant line (no orphan cut line)
- Deterministic turn finalize / ghost-turn guards on the speech-in path

**Honest limits:**

- Supertonic at steps ≥ 5 is ~0.5 s synth floor on the current host (full WAV, not progressive PCM stream)
- GPU ONNX path was tried on GTX 1060 and failed (cuDNN); production TTS stays CPU
- CUPE live and dual-Nemotron self-ASR are off by default

**Later:**

- Controller: consume `transcript_committed`, dispatch agent turns, manage assistant/user alternation
- Streaming TTS or a working accelerator without dropping quality below steps 5
- Mic-open empty first turn / adaptive energy gate during TTS
- Monolith cleanup (`turn.rs`, watch TUI, golden scripts) once the controller contract is stable
