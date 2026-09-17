# core picture

this page is a short shared picture of the live system. see
[`current-state.md`](current-state.md) for exhaustive detail.

## 1. the ear

`ata-speech-core.service` on `:8765` takes 16 kHz mono PCM from voicecat,
transcribes on CPU with nemotron, and closes the turn with silero VAD plus
smart-turn v3. no GPU.

## 2. the mouth

`ata-speech-out.service` is the leftover daemon on websocket `:8788`.
it handles speak, append, and cancel. every unit becomes one hop
`POST http://10.77.67.147:7861/internal/leftover/v1/audio/speech`.
the hop is the mouth; nothing else speaks to the desk.

## 3. the lab

the TTS lab backend and web app sit behind `:7861`. `GENERATE` is the
long-document client of the same engine the mouth hops to.
`/api/synthesize` is for one-shot synthesis. `/api/generate/stream` is for
streamed s16le PCM.

## 4. one gpu occupant

the breeze-tts-2 E2 worker process holds the card, about 8720 MiB of a 12 GB
card. `ata-speech-tts.service` is parked qwentts.cpp on `:18091`; it is
inactive and disabled and must not start beside it. the lab reads a live-call
lease and answers 409 rather than taking a second occupant.

## 5. the enrolled voice reference

every synthesis request, live or lab, sends the same enrolled reference audio
and its transcript. breeze has one reference slot. generated audio is never
used as a reference.

## 6. stop vs cancel

Stop is the operator's word: segments that have not started never start, the
in-flight segment finishes or is cancelled, and the audio that was produced
is still saved as one take. cancel is the mechanism: SIGUSR1 to the worker
aborts the in-flight synthesis, and the parent drains the worker's remaining
frames and throws them away. leftover's cancel frame does the same for the
live mouth. nothing is generated after Stop.

the browser is still holding seconds of already-streamed audio when Stop is
pressed, so playback runs on after the server has stopped. a Stopped take's
saved wav ends at the quietest sample of whatever pcm landed in the take
buffer after Stop; when nothing landed, it keeps its last sample.

## 7. what a take is

a take is one row in runs, one wav artifact, and the request snapshot that
records the exact `produced_text` that was spoken. the voice's latest_take_id
points at the newest one, which is what the lab restores and what desk or
leftover ingest later.
