# 05 — Progressive audio contract

**status:** accepted target specification; implementation pending qualification
**authority:** subordinate to `CHARTER.md`; not proof of implementation

## Production policy

The replacement production synthesis path is CosyVoice-only. The exact repository, model revision, runtime, voice/reference assets and configuration must be pinned before integration. Deterministic fake PCM remains available for tests.

## Process boundaries

```text
session control
  -> speech-egress command
  -> CosyVoice synthesis worker
  -> progressive PCM frames
  -> internal player
  -> playback/cursor events
```

Egress owns synthesis. Player owns the device. They share a versioned protocol and do not import one another's implementations.

## Speak command

A speak request must include at least:

- session/turn/run/response/message/segment/utterance identities;
- utterance class: intermediate or final;
- immutable source text span;
- voice and language/prosody controls;
- priority and supersession group;
- requested output format;
- request monotonic timestamp;
- idempotency key.

## PCM frame

Each frame carries:

- `utterance_id` and `audio_chunk_id`;
- producer sequence;
- PCM format, sample rate and channels;
- cumulative sample start/end in a named utterance output domain;
- backend-native acoustic token range where available;
- generation timestamp/clock provenance;
- final/non-final marker;
- discontinuity/gap indication;
- backend/model/revision/voice/config provenance.

No contract depends on WAV containers.

## Backpressure and bounds

Define explicitly:

- maximum queued utterances;
- maximum queued PCM samples/milliseconds;
- per-conversation serialization;
- slow-player behavior;
- whether synthesis pauses, blocks or cancels under pressure;
- disconnect behavior;
- drop policy, if any, and mandatory drop events;
- how cancel/supersede overtakes queued frames.

Silent unbounded buffering is prohibited.

## Cooperative cancellation

Cancellation is an operation with an idempotency key and one terminal event. Required cases:

- before first acoustic token;
- after tokens but before first PCM;
- between PCM chunks;
- during flow/vocoder execution;
- duplicate cancellation;
- client disconnect;
- timeout while joining worker activity;
- worker/process crash.

Acceptance requires:

- no post-cancel PCM attributed to the cancelled operation;
- bounded cleanup time;
- reclaimed GPU resources;
- no false synthesis completion;
- player cancellation/flush treated as a separate correlated operation.

Stopping consumption of an upstream Python generator is not, by itself, proof of synthesis cancellation.

## Speakable-unit segmenter

Do not send individual language-model tokens to TTS. Commit immutable speakable segments using:

- stable punctuation/clause boundaries;
- minimum and maximum text bounds;
- timeout flush for slow streams;
- protection for numbers, abbreviations, names, code fragments and phoneme-inpaint spans;
- explicit cancellation and supersession.

The segmenter is independent of CosyVoice and preserves source text spans.

## Internal player contract

The player provides:

- ordered non-overlapping playback;
- bounded PCM ring buffer;
- submitted sample cursor;
- device-consumed/audible estimate with uncertainty and calibration ID;
- buffer depth;
- underrun/overrun events;
- duck, fade, stop-at-sample and flush;
- exact retained PCM submitted before stop;
- deterministic fake sink.

The player does not expose an unqualified `heard_sample_cursor`.

## CosyVoice qualification gate

Before production implementation is accepted, pin and measure:

- repository commit and model hash;
- Python and dependency lock;
- native PyTorch versus TensorRT/vLLM route;
- voice/reference/style assets and hashes;
- text normalization path;
- output sample rate;
- cold and warm startup;
- first-PCM latency and real-time factor;
- VRAM under concurrent speech-in/alignment/TTS load;
- cancellation granularity;
- failure isolation and rollback procedure.
