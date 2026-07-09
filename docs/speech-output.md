# speech-out seam

`speech-out` is the local text-to-speech / utterance side of this repo. It is intentionally separate from the existing `speech-core-daemon` speech-in path: no microphone ingress, ASR, VAD, smart-turn, or turn-taking code is changed by this seam.

## current MVP

Binary:

```bash
cargo run -p speech-out -- say "hello from speech-out"
```

Default `say` behavior is the safe `mock` backend. It accepts text and prints a JSON utterance event, which is enough for pi profiles/processes to wire a stable command without requiring model artifacts or audio devices:

```json
{"event":"speech_out_utterance","backend":"mock","text":"hello from speech-out","voice":"M1","lang":"en","output":null}
```

The `say` CLI also supports stdin:

```bash
printf 'hello from stdin' | cargo run -p speech-out -- say --stdin
```

Installed/process-facing name should be `speech-out`.

The interactive MVP is websocket based:

```text
sfub/desktop: speech-out daemon --bind 0.0.0.0:8788
laptop/client: speech-out play --url ws://sfub:8788/ws/speech-out "heard you."
```

Inference stays on sfub/desktop. Client devices connect as playback adapters and receive evented websocket text messages plus binary WAV byte chunks. The daemon synthesizes text chunks sequentially: one Supertonic HTTP request per text chunk. The client playback adapter also plays completed WAV chunks sequentially via a single playback worker. Do not spawn one player per chunk concurrently; that creates overlapping "chorus" playback and can kill short utterances when the client process exits.

## backend contract

`speech-out` owns the process seam:

```text
text -> SpeechOut backend -> optional wav/output/playback
```

Backends available now:

- `mock`: deterministic JSON only. No audio dependencies.
- `command`: runs an external command. By default the utterance text is appended as the final argv; use `--command-stdin` to pipe text to stdin instead.
- `supertonic-http`: uses `curl` to call a local Supertonic HTTP server and writes/plays a WAV.

Common arguments:

```bash
speech-out say --backend mock "text"
speech-out say --backend command --command espeak-ng --command-arg -v --command-arg en "text"
speech-out say --backend supertonic-http --steps 5 --speed 1.30 --output /tmp/out.wav "text"
```

Environment variables mirror the CLI for process profiles:

```text
SPEECH_OUT_BACKEND=mock|command|supertonic-http
SPEECH_OUT_COMMAND=...
SPEECH_OUT_COMMAND_ARGS=...
SPEECH_OUT_COMMAND_STDIN=false
SPEECH_OUT_SUPERTONIC_URL=http://127.0.0.1:7788/v1/tts
SPEECH_OUT_VOICE=M1
SPEECH_OUT_LANG=en
SPEECH_OUT_OUTPUT=/tmp/out.wav
SPEECH_OUT_TIMEOUT_SECS=60
SPEECH_OUT_PLAY_COMMAND=pw-play
SPEECH_OUT_WS_URL=ws://100.68.60.39:8788/ws/speech-out
SPEECH_OUT_DAEMON_BIND=0.0.0.0:8788
SPEECH_OUT_WARM_TTL_SECS=1200
SPEECH_OUT_SUPERTONIC_STARTUP_GRACE_MS=5000
```

## websocket daemon / playback adapter

Run on sfub/desktop:

```bash
cargo run -p speech-out -- daemon \
  --bind 0.0.0.0:8788 \
  --supertonic-url http://127.0.0.1:7788/v1/tts
```

By default the daemon starts `supertonic serve --host 127.0.0.1 --port 7788` on first request, keeps it warm for `SPEECH_OUT_WARM_TTL_SECS=1200` (20 minutes) after the last request, then kills the managed child. If Supertonic is managed elsewhere, set `SPEECH_OUT_EXTERNAL_SUPERTONIC=true` or pass `--external-supertonic`.

Send a request and play it on a client/laptop:

```bash
speech-out play \
  --url ws://100.68.60.39:8788/ws/speech-out \
  --steps 5 --speed 1.30 --voice M1 --lang en \
  "heard you."
```

Client request JSON:

```json
{"type":"speak","utterance_id":"optional-id","text":"heard you.","voice":"M1","lang":"en","steps":5,"speed":1.30,"reference":null,"style":null}
```

Daemon text events intentionally mirror speech-in observability style:

```text
speech_out_request_received
speech_out_synthesis_started
speech_out_audio_chunk      # followed by one websocket binary chunk containing WAV bytes
speech_out_text_chunk_completed
speech_out_completed
speech_out_failed
speech_out_pong
speech_out_playback_started    # client-side event from speech-out play
speech_out_playback_completed  # client-side event from speech-out play
speech_out_playback_failed     # client-side event from speech-out play
```

`streaming_mode=text_chunked_http_responses` means the daemon splits text into pragmatic chunks and sends one Supertonic `/v1/tts` request per text chunk. Within a single text chunk, Supertonic still buffers internally until it can return WAV bytes; first-audio latency is therefore bounded by the first text chunk, not the entire paragraph. The client queues completed chunk WAVs and plays them in order.

## developer live-session harness

`scripts/speech-out-live-session.sh` reuses the speech-core live microphone/session pattern. It streams mic audio through `speech-core-mic-adapter`, feeds speech-in events plus speech-out events through the same `speech-core-watch --mode debug` TUI, watches for speech-in `turn_closed`, and triggers/appends a short speech-out response (default `heard you.`) through the speech-out websocket daemon:

```bash
SPEECH_CORE_WS_URL=ws://100.68.60.39:8765/ws/audio-ingress \
SPEECH_OUT_WS_URL=ws://100.68.60.39:8788/ws/speech-out \
./scripts/speech-out-live-session.sh --steps 5 --speed 1.30 --voice M1 --style calm
```

Knobs: `--steps`, `--speed`, `--voice`, `--lang`, `--reference`, `--style`, `--response-text`, `--core-url`, `--out-url`, `--play-command`, and `--device`.

## verified Supertonic assumptions

Network checks on 2026-07-07 found:

- Official GitHub repo: `https://github.com/supertone-inc/supertonic`.
- Official Python package docs are linked from that repo as `supertonic-py`.
- PyPI package `supertonic` was reachable; metadata reported version `1.3.1`, `requires_python >=3.9`, and summary `High-quality Text-to-Speech synthesis with ONNX Runtime`.
- Official Hugging Face model: `Supertone/supertonic-3`.
- HF metadata reports `library_name: supertonic`, `pipeline_tag: text-to-speech`, `license: openrail`, non-gated, ONNX files, preset voice JSONs, and about 411 MB of storage.
- The repo README says Supertonic 3 is ONNX Runtime based, local/on-device, 44.1 kHz WAV output, 31 languages, and that `pip install supertonic` downloads assets automatically on first run.
- The repo README says the Python SDK provides `supertonic serve --host 127.0.0.1 --port 7788` with native `POST /v1/tts` and OpenAI-compatible `POST /v1/audio/speech` endpoints.

The MVP now targets native `/v1/tts` so Supertonic controls are explicit:

```json
{"text":"hello","voice":"M1","lang":"en","steps":5,"speed":1.30,"response_format":"wav"}
```

Unverified locally: the `supertonic` Python package is not installed in this worktree, and the 411 MB model artifact was not downloaded. The exact handling of optional `reference` / `style` should be confirmed against `supertonic serve --help` / OpenAPI docs after installation.

## making Supertonic real

Use a separate Python environment; do not vendor the model into this repo:

```bash
python3 -m venv /tmp/speech-out-supertonic-venv
/tmp/speech-out-supertonic-venv/bin/pip install 'supertonic[serve]'
/tmp/speech-out-supertonic-venv/bin/supertonic serve --host 127.0.0.1 --port 7788
```

Then, in another shell:

```bash
cargo run -p speech-out -- say \
  --backend supertonic-http --steps 5 --speed 1.30 \
  --output /tmp/speech-out.wav \
  "Supertonic is the first real speech-out target."
```

If local audio playback is desired and `pw-play` is available:

```bash
cargo run -p speech-out -- say \
  --backend supertonic-http --steps 5 --speed 1.30 \
  "Say this and play it."
```

## cancellation and barge-in design

Implemented today:

- bounded one-shot calls: `--timeout-secs` terminates command/http/playback child processes when they overrun.
- external supervisors can stop the top-level `speech-out` process, but full signal-aware child cleanup is left for `speech-out-daemon`.

Daemon design, not implemented yet:

```text
POST /v1/utterances
  {"text":"...","voice":"M1","priority":"normal","barge_in_group":"agent-main"}
  -> {"utterance_id":"...","state":"queued"}

DELETE /v1/utterances/{utterance_id}
  cancels queued work or stops current playback

POST /v1/barge-in
  {"group":"agent-main","reason":"user_speech_start"}
  cancels current playback and clears lower-priority queued utterances in that group

GET /v1/events
  emits queued|started|audio_ready|playback_started|cancelled|completed|failed
```

Speech-in integration should be event-level, not a dependency from `speech-core-daemon`: a future coordinator can subscribe to `vad_speech_start` / `turn_closed` events and call the speech-out daemon's cancel/barge-in endpoint. This keeps speech-in turn-taking owned by `speech-core-daemon` and speech-out playback owned by `speech-out-daemon`.

## future `speech-out-daemon`

A future `speech-out-daemon` should keep Supertonic warm, serialize or prioritize utterances, expose cancellation/barge-in, and choose the actual playback device. The current CLI is deliberately small so profiles can use it now while that daemon API is designed.


## operator testing surface

`speech-out-live-session` is an end-to-end speech loop harness, not a raw output-only smoke test. It intentionally mirrors the `speech-core-live-session --debug-tui` diagnostic surface: speech-in VAD/smart-turn glyphs and horizontal VAD gauges remain visible, then the speech-out response is appended under the closed input turn as:

```text
#01
  ◖ "user utterance" ◗ ①.79 ◆
  > "heard you."
```

For future developers: the useful manual testing mode is the debug tui, not the minimal default transcript view. The minimal view is good for normal conversation; the debug tui is the canonical substrate-contact view while tuning speech-in/speech-out timing.

## relation to patterns speech-to-speech docs

The local `~/workspace/patterns` repo documents `delayed-streams-modeling` as the speech-to-speech ideal: a single decoder-only model over time-aligned streams, avoiding the STT -> LLM -> TTS cascade. `speech-core` is not that architecture today. It is deliberately still a cascade:

```text
speech-in: mic -> vad/asr/smart-turn -> turn_closed
router:    future coordinator / model response
speech-out: text chunks -> Supertonic -> ordered playback
```

So the useful pattern import is not "pretend we are full duplex DSM". The useful import is the stream discipline: every seam needs typed events, sequence numbers, timing, cancellation/barge-in, and replayable logs. Current speech-out events expose request/chunk/synthesis/playback timings so the cascade's latency and ordering failures are visible instead of vibes-only.

## current barge-in harness

The developer live-session harness now implements local barge-in at the client supervisor layer:

```text
user speech starts or committed transcript token arrives
  -> cancel active speech-out play process tree
  -> emit speech_out_barge_in
  -> allow speech-in to keep ingesting the new turn
```

This is not yet the final daemon-level cancellation API. It is the correct next substrate step because playback currently lives on the laptop client. Later, the router/speech-out daemon should own cancellation by utterance id and expose explicit `cancel` / `barge_in` control messages over the websocket.

The harness also skips the canned response for empty transcript turns, emitting `speech_out_skipped` with `reason=empty_transcript`, so noise-only or detector-only closures do not say `heard you.`

TUI output lines use speech-out glyphs directly, without a prompt marker:

```text
⇢ ✂ ⌁ ▣ ✓ "heard you. ✂ long second chunk..."
```

The `✂` separators inside the quoted text show the actual text chunks sent to Supertonic.
