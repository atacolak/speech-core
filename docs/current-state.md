# speech-core current state

this is the reality as of the current committed runtime. if another doc disagrees, trust this file and the repo code first.

## one-line summary

`speech-core` is a rust speech runtime: laptop mic audio streams to the sfub daemon, nemotron produces low-latency transcript text, and silero vad marks turn boundaries.

## current live path

```text
sfnix laptop
  speech-core-mic-adapter
    captures cpal mic audio as 16khz mono pcm_s16le
    sends websocket audio frames
      ↓
sfub
  speech-core-daemon
    validates frame/session metadata
    writes jsonl event log
    feeds nemotron streaming asr
    feeds silero vad
    turn manager promotes vad speech_end into turn_closed
      ↓
sfnix or sfub
  speech-core-watch / speech-core-live-session
    prints transcript text
    prints <EOU> when turn_closed arrives
```

## current defaults

installed daemon defaults:

```text
SPEECH_CORE_STREAM_CHUNK_MS=160
SPEECH_CORE_ATT_CONTEXT_RIGHT=1
SPEECH_CORE_VAD_THRESHOLD=0.3
SPEECH_CORE_VAD_ONSET_FRAMES=2
SPEECH_CORE_VAD_HANGOVER_FRAMES=12
SPEECH_CORE_TURN_MIN_VAD_SPEECH_MS=700
SPEECH_CORE_TURN_VAD_CLOSE_ENABLED=true
SPEECH_CORE_EOU_MODEL_DIR=
SPEECH_CORE_TURN_MODEL_EOU_CLOSE_ENABLED=false
```

important translation:

- nemotron runs every ~160ms of audio with ~80ms right-context.
- silero vad uses 30ms frames.
- vad starts speech after 2 speech frames, so roughly 60ms of above-threshold speech.
- vad ends speech after 12 non-speech frames, so roughly 360ms of below-threshold audio.
- turn manager ignores vad segments whose total speech duration is under 700ms.
- parakeet realtime eou is disabled by default.

## what `<EOU>` means right now

`<EOU>` in the watcher is not a token from the speech model.

it means:

```text
silero vad emitted vad_speech_end
and the detected speech segment lasted at least 700ms
and turn manager emitted turn_closed source=vad
```

so if you pause mid-sentence for about 360ms or more after a segment that has already lasted at least 700ms, the watcher may print `<EOU>` even though you intended to continue. this is expected under the current simple vad-only policy. it is tunable, not magic.

## what “short vad segments under 700ms are suppressed” means

this is not about pause length. it is about the length of the whole speech segment.

example that gets suppressed:

```text
noise/click/short syllable starts vad
vad segment lasts 270ms
vad says speech ended
turn manager says: too short, do not close a turn
watcher prints no <EOU>
```

example that still closes:

```text
you speak for 2 seconds
then pause for 360ms
vad says speech ended
the segment was longer than 700ms
turn manager closes the turn
watcher prints <EOU>
```

so the 700ms gate filters tiny false starts; it does not prevent eou after short pauses inside longer speech.

## why parakeet realtime eou is retired

recent live evidence was poor:

- one ~59s laptop session had one meaningful spoken region in the transcript.
- parakeet realtime eou emitted 51 raw eou tokens.
- 50 were suppressed by the turn manager.
- model eou often arrived later than silero speech_end.
- using it for turn closure made the system worse, not smarter.

it remains in-tree for experiments only. to re-enable intentionally:

```bash
SPEECH_CORE_EOU_MODEL_DIR=/home/sf/workspace/external/parakeet-eou/realtime_eou_120m-v1-onnx \
SPEECH_CORE_TURN_MODEL_EOU_CLOSE_ENABLED=true \
./scripts/install-sfub-daemon.sh
```

## what works

- websocket audio transport from sfnix to sfub.
- native nixos build/install path for the laptop client.
- sfub systemd user service for the daemon.
- nemotron streaming transcript, fast enough for interactive use.
- silero vad turn boundaries.
- clean live watcher output: transcript plus `<EOU>`.
- jsonl event log for debugging.

## what is still rough

- vad-only eou is acoustics-only. it does not know syntax or intent.
- mid-sentence pauses can close a turn.
- no parakeet unified gguf artifact is currently local.
- parakeet unified has not been benchmarked as a delayed correction/finalizer.
- cross-host capture latency is preserved but not calibrated.
- docs under `/home/sf/workspace/docs/speech-core` contain older planning/spec history; they are useful archaeology but not the current runtime source of truth.

## useful commands

sfub daemon:

```bash
systemctl --user status speech-core-daemon.service
systemctl --user restart speech-core-daemon.service
journalctl --user -u speech-core-daemon.service -f
cat ~/.config/speech-core/daemon.env
```

sfnix laptop:

```bash
speech-core-live-session
cat ~/.config/speech-core/client.env
systemctl --user status speech-core-mic-adapter.service
```

repo:

```bash
cargo test --workspace
./scripts/install-sfub-daemon.sh
./scripts/sfnix-sync-build-adapter.sh
```
