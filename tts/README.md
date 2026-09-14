# Speech-core TTS laboratory

**status:** lab-only. not on the voicecat path. live mouth stays the leftover hop.

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
  reference.py          workbench: source + edits
  preprocess.py         optional preprocess cache (internal only, not installed)
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
"$QUAL_ROOT/venv/bin/python" -m tts.lab.backend.serve --host 127.0.0.1 --port 7861
```

Public URL: `https://voice.net.colak.sh/lab` (Caddy `/lab` → loopback `:7861`).
`:7860` is voicecat desk. `:8765` is the speech-core daemon, not this lab API.
Gradio stays rollback (`tts/playground/app.py`) and is not bound to 7860/7861
while the React shell is up.

`--dry-run` on the Gradio app builds that UI without parking leftover or loading the 3B.

Lab web instrument source: `tts/lab/web` (`pnpm --dir tts/lab/web dev` is local Vite only).

Transcribe uses `transcribe.cpp` / Parakeet TDT 0.6B v2 on CPU. MP3 uploads
are decoded with ffmpeg. Downloads are actual RIFF/WAV files.

## Parked: AuK reference preparation

AuK was evaluated here for reference preparation and editing — denoise, repair,
perform, synthesize — with the operator driving a task chip and an instruction
box. It was not retained: the results did not justify the extra GPU occupant
beside Breeze, and the cookbook promised more than it delivered. The code is
parked, not deleted: `tts/auk/`, the `auk_*` columns and `/api/auk/*` remain, and
the lab chrome no longer loads, generates with, or names it. References are
prepared from the source keep crop instead.

## Future synthesizer shape

```text
synthesize(voice_profile_id=..., utterance_packet=...)
```

Do not put the delivery planner synchronously in front of every live voicecat
utterance without measuring latency.
