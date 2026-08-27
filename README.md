# Speech Core

Real-time speech substrate for human-agent interaction.

## Start here

This file is the front door. It tells you what exists and how to run it.

| Need | Read |
|---|---|
| **Accepted behavior truth — how every mechanism works, and why** | [`spec/README.md`](spec/README.md) |
| Enduring Speech Core purpose, authority boundaries, and invariants | [`CHARTER.md`](CHARTER.md) |
| Product documentation map (reference & evidence) | [`docs/README.md`](docs/README.md) |
| Current implementation, installed defaults, and honest limits | [`docs/current-state.md`](docs/current-state.md) |
| Accepted Speech Core architectural directions | [`docs/decisions/`](docs/decisions/) |
| Active Speech Core target and delivery sequence | [`docs/evolution/ACTIVE.md`](docs/evolution/ACTIVE.md) |
| City-standard mapping and work namespace | [`rig.toml`](rig.toml) |
| Authorized work and status | the city `sc` work ledger |

This repository contains Speech Core product truth and a small city binding. No planning document proves implementation, and no memory projection proves current state.

```text
speech-in   → microphone audio → transcript + turn events
speech-out  → text → audible speech  (leftover: speak then append)
agent loop  → decides what to do with a completed turn
```

The mature seam is **speech-in**. It runs a separate **speech-core-daemon** that ingests timestamped PCM, transcribes with Nemotron, detects voice activity with Silero, semantically endpoints with smart-turn v3, and emits immutable per-turn transcripts.

**speech-out** is a separate TTS/playback daemon. live pin is **qwentts.cpp Q8 CustomVoice** (`ata-speech-tts.service` on `:18091`, daemon on `:8788`). CosyVoice is rollback only. Isolated CosyVoice3 RL playground: `~/workspace/cosyvoice_playground` (not live). Input and output do not share a process because their failure modes differ.

**the live call** is not this repo. sibling `voicecat` is the phone line (desk `/desk` or phone `/ws-phone`). headed omp in `~/worlds/talker` is the brain. this repo stays ear + mouth. see `../voicecat/README.md`.

## Components

```text
speech-in
  speech-core-daemon      ASR + VAD + turn detection
  speech-core-mic-adapter CPAL mic → websocket
  speech-core-file-adapter WAV replay → websocket
  speech-core-watch       transcript/event subscriber + TUI
  speech-core-protocol    shared messages

speech-out
  speech-out              TTS + playback (qwentts progressive PCM over websocket)

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
- Human-hold degraded close: see [`spec/specs/speech-in-turn-lifecycle.md`](spec/specs/speech-in-turn-lifecycle.md) (do not restate the threshold here).
- RMS energy gating is available server-side as an onset veto. It is currently a fixed-threshold gate and is intentionally conservative.
- Barge-in (dogfood): pause playback on the first alphanumeric user ASR token; provisional cut from wall-clock playback; async CTC refine when the warm align worker is up. Greying updates the same assistant line (dim spoken / white unsaid).

## Environment

| variable | purpose |
|---|---|
| `SPEECH_CORE_WS_URL` | `ws://host:8765/ws/audio-ingress` |
| `SPEECH_OUT_WS_URL` | `ws://host:8788/ws/speech-out` |
| `SPEECH_CORE_MODEL_PATH` | Nemotron GGUF |
| `SPEECH_CORE_VAD_MODEL_PATH` | Silero VAD ONNX |
| `SPEECH_CORE_SMART_TURN_MODEL_PATH` | smart-turn-v3 ONNX |
| `SPEECH_OUT_QWENTTS_VOICE` | live named speaker (default **ryan**; `default` aliases here) |
| `SPEECH_OUT_ASSISTANT_SELF_ASR` | dual-Nemotron self-ASR (`0` default — off) |
| `SPEECH_OUT_CUPE_LIVE` | experimental live position tracker (`0` default — off) |
| `SPEECH_OUT_ALIGN_BACKEND` | barge refine backend (`ctc_forced` when align stack present) |

Install scripts write core URLs/paths to `~/.config/speech-core/daemon.env` and `client.env`.

The full installed-default table lives in [`docs/current-state.md`](docs/current-state.md). Defaults originate in code and installed configuration; documentation must not become an independent configuration source.

## Run

Server:

```bash
./scripts/install-speech-core-daemon.sh
systemctl --user restart ata-speech-core
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

Use [`docs/README.md`](docs/README.md) as the canonical map. The most common references are:

- [`docs/current-state.md`](docs/current-state.md) — what works right now;
- [`docs/seams.md`](docs/seams.md) — current component boundaries and contracts;
- [`docs/turn-detection.md`](docs/turn-detection.md) — exact EOU triggers and tuning knobs;
- [`docs/speech-output.md`](docs/speech-output.md) — speech-out protocol and cancellation;
- [`docs/evolution/ACTIVE.md`](docs/evolution/ACTIVE.md) — accepted target, current gap, and delivery sequence.

## Now vs later

**On this branch (dogfood):**

- Barge-in stop on first alphanumeric user token; provisional wall-clock cut; CTC refine via warm TCP worker when available
- TUI greying on the original assistant line (no orphan cut line)
- Deterministic turn finalize / ghost-turn guards on the speech-in path

**Honest limits:**

- speech-out pcm **out** streams (~70 ms first usable). text **in** is one complete `speak`. CosyVoice is not live.
- GPU TTS is qwentts.cpp on the 4070 (~2.4 GB), not CPU Supertonic
- CUPE live and dual-Nemotron self-ASR are off by default

**Later:**

- Controller: consume `transcript_committed`, dispatch agent turns, manage assistant/user alternation
- call-side first-clause flush (voicecat SENTENCE → clause). not CosyVoice token-bistream
- Mic-open empty first turn / adaptive energy gate during TTS
- Monolith cleanup (`turn.rs`, watch TUI, golden scripts) once the controller contract is stable
