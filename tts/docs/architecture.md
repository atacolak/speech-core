# TTS laboratory architecture

not on the voicecat path. Lab-only. Live mouth stays leftover qwentts.

This is a boundary document, not a framework manifesto. The objects below
exist so an utterance can be reconstructed later: why it sounded the way
it did, which voice it cloned, which persona directed it, and which
backend settings produced the wav.

## Voice profile

Acoustic speaker identity. Stable id (`vp_…`). Holds original reference
audio (lossless wav), exact transcript, selected region, hashes, derived
preprocessed variants, preferred variant, optional default Breeze config,
and notes.

The future synthesizer should be able to say:

    synthesize(voice_profile_id=..., utterance_packet=...)

Gradio upload paths are not the runtime contract. Saving a voice copies
bytes into the managed library.

## Delivery profile / persona

How this conversational entity tends to speak. Separate object (`dp_…`).
Contains a base description, priors, exemplar utterance+steer pairs,
observations, default vocal events, and pronunciation preferences.

There is no requirement that a cloned voice imitate the behaviour of the
person who produced the reference. Voice A + persona X and voice A +
persona Y are first-class.

Exemplars are the behavioural model. A one-line "sound sarcastic" prompt
is not.

## Utterance packet

Separates WHAT is said from HOW it is rendered.

- `text` — canonical semantic / display text
- `synthesis_text` — backend text actually handed to TTS (may contain
  respellings)
- `steer` — rich natural-language direction; authoritative input to Breeze
- `delivery` — optional structured annotations (intent, affect, energy,
  pace, emphasis, pauses, trajectory, vocal events). Never required.
- `pronunciation_overrides` — backend-independent
- `voice_profile_id` / `delivery_profile_id`
- provenance

The steer is not replaced by the schema. An excellent free-form steer may
contain distinctions the schema has not anticipated.

## Pronunciation resolver

Looks up a saved lexicon, persona overrides, and operator overrides.
Compiles a display-preserving `synthesis_text`. Does not invent Breeze
phoneme markup. Cache key is lexical context, not "whatever the last
model typed".

## Backend adapter

Converts a packet + voice profile into whatever the selected engine
accepts. For Breeze E2 that is currently: engine text, steer as
instruction, reference wav + transcript, cfg/seed/generation settings.
Dual-cfg is an experimental adapter path, not the E2 default.

## Experiment run

Durable JSON under the lab store (`runs/`). Includes packet, voice and
delivery ids, reference path/transcript/region, runtime
implementation/commit/E2 config, cfg mode and scales, seed, non-default
generation settings, planner prompt when used, output audio
id/hash, latency, RTF, optional operator rating.

This is a dataset, not log detritus.

## Audio / reference artifact

Named, tagged, hashed, playable library item. Original vs processed
variants. Explicit deletion. Prefer wav/flac internally. Do not
repeatedly MP3-encode clone references.


## Reference edits

The clone reference is an ordered edit list on immutable source audio:

    source audio + [exclude | trim_start | trim_end]* -> effective reference
                  -> optional stream.fm
                  -> effective synthesis reference

Never overwrite the original. Multiple excluded ranges are first-class.
The transcript handed to Breeze is the *effective* transcript. If cuts
remove speech, mark the transcript stale and retranscribe or edit it.

## Cache, save/promote, download

Durable lab store: `~/.local/share/speech-out/tts-lab`.

- **cache** (`tts-lab/cache`, plus `tmp`) — automatic, derived, rebuildable (stream.fm, effective wavs). Safe to delete.
- **save/promote** — durable library / voice profile identity (`voices/`, `library.json`, `generations/`, `runs/`)
- **download/export** — explicit standalone file for the operator

The selected Breeze E2 pin lives at `~/.local/share/speech-out/breeze-tts-2-e2`, not under `~/.cache`.

stream.fm writes only into the lab cache. It must not drop files next to
the source or in Downloads merely because the operator auditioned a pass.

## Production boundary

The lab may run an explicit planner pass freely. Live speech-core must
not put a heavyweight LLM call synchronously in front of every voicecat
utterance without measuring latency. Planner interface exists now;
wiring it into the mouth is a later, measured decision.
