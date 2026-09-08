# Speech-core TTS laboratory

**status:** lab-only. not on the voicecat path. live mouth stays leftover qwentts.

This directory is speech-core's synthesis / delivery laboratory. It is not
the production mouth. Artifacts here are intentionally reusable later via
stable ids (voice profiles, delivery profiles, utterance packets, audio
library records). Gradio temp paths are not the runtime contract.

## Selected Breeze runtime

**E2** — official bf16, `fast=[depth,codec]`.

See [`docs/architecture.md`](docs/architecture.md) for object boundaries
and [`breeze/docs/e2-selected.md`](breeze/docs/e2-selected.md) for why E2
won. Archived hybrid numbers live under
[`experiments/reports/`](experiments/reports/).

## Layout

```
tts/
  packets.py            utterance packet
  pronunciation.py      backend-independent overrides
  profiles.py           voice vs delivery persona
  store.py              audio library + run provenance
  generation.py         upstream Breeze defaults
  playground/app.py     Gradio laboratory
  breeze/runtime.py     E2 facade
  edits.py              non-destructive reference edit list
  reference.py          workbench: source + edits + stream.fm
  preprocess.py         optional stream.fm cache (internal only)
  planner.py            experimental delivery planner
  experiments/          fixtures, reports, runs
```

## Run the playground

Durable pin: `~/.local/share/speech-out/breeze-tts-2-e2` (official bf16 E2).
Saved lab artifacts: `~/.local/share/speech-out/tts-lab`.
`tts-lab/cache` and `tts-lab/tmp` are derived and may be deleted anytime.

```bash
export QUAL_ROOT="${QUAL_ROOT:-${XDG_DATA_HOME:-$HOME/.local/share}/speech-out/breeze-tts-2-e2}"
export PYTHONPATH=".:lab/scripts:${QUAL_ROOT}/src/breeze-tts"
"$QUAL_ROOT/venv/bin/python" tts/playground/app.py --host 127.0.0.1 --port 7860
```

`--dry-run` builds the UI without parking leftover or loading the 3B.

Transcribe uses `transcribe.cpp` / Parakeet TDT 0.6B v2 on CPU. MP3 uploads
are decoded with ffmpeg. Downloads are actual RIFF/WAV files.

## Future synthesizer shape

```text
synthesize(voice_profile_id=..., utterance_packet=...)
```

Do not put the delivery planner synchronously in front of every live voicecat
utterance without measuring latency.
