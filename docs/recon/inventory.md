# Speech Core — Phase-1 inventory (brownfield reconstruction)

Branch `spec/recon-001`, base `e377491`. All findings tagged per brownfield-adoption.md
source-treatment table: **OBSERVED** (direct inspection), **INFERRED** (reasoned from
observations), **UNRESOLVED** (needs operator decision).

## 1. Crate set (workspace)

OBSERVED `Cargo.toml:2-10` — six workspace members; `vendor/parakeet-rs` excluded from the
workspace but linked as a path dependency (Cargo.toml:22 `parakeet-rs = { path = "vendor/parakeet-rs" }`).

| crate | rs LOC | role (OBSERVED from source) | entry point |
|---|---|---|---|
| speech-core-protocol | 648 | wire contract: SCF1 envelope, frame headers, control msgs, server events, timestamp provenance, latency math | `src/lib.rs` (library) |
| speech-core-daemon | 12,881 | speech-in server: ws audio ingress, session registry, ASR model worker, detector worker, turn policy, jsonl event log | `src/main.rs` |
| speech-core-mic-adapter | 1,337 | CPAL mic capture → SCF1 frames → ws (also synthetic/dry-run) | `src/main.rs` |
| speech-core-file-adapter | 479 | WAV replay → ws; `--follow-dir` live chunk feed | `src/main.rs` |
| speech-core-watch | 3,788 | daemon event subscriber; TUI/transcript/inject/jsonl modes | `src/main.rs` |
| speech-out | 2,838 | speech-out daemon: TTS (Supertonic HTTP) + playback, say/daemon/play subcommands | `src/main.rs` |

LOC method: `wc -l` over `*.rs` per crate (includes `build.rs` for daemon; excludes `native/*.cpp/.h`).
OBSERVED totals — crates 21,971; vendor/parakeet-rs 9,073; scripts/ 21,297 (30 files);
tests/ 4,711. Whole-tree ≈ 57k; without vendor ≈ 48k. The task's "~51k" is consistent with
a slightly different counting boundary (e.g. excluding some scripts). Not a discrepancy.

## 2. External interfaces (OBSERVED)

- **audio transport**: websocket. Daemon binds `127.0.0.1:8765` default (`main.rs:44`,
  `SPEECH_CORE_DAEMON_BIND`). Adapters default to `ws://127.0.0.1:8765/ws/audio-ingress`
  (mic-adapter main.rs:40, file-adapter main.rs:28, watch main.rs:46). Server-side:
  `tokio_tungstenite::accept_async` at `daemon/src/main.rs:839` performs the handshake
  WITHOUT inspecting the HTTP path — any path is accepted. `/ws/audio-ingress` is a
  client-side convention, not a server route. UNRESOLVED: is the path part of the contract
  or accidental?
- **wire format**: binary `SCF1` envelope (magic `SCF1`, version 1, JSON header +
  payload), `protocol/src/lib.rs:10-12,224-266`. Frame header fields:
  `protocol/src/lib.rs:163`. Control messages: `Hello`, `Ping`, `Drain`, `SubscribeEvents`
  (`lib.rs` `enum ControlMessage`). Server events: `StreamStart`, `HelloAck`,
  `AudioFrameIngested`, `AudioGap`, `AudioSampleGap`, `AudioDrop`, `Error`.
- **models**: Nemotron streaming ASR (GGUF, external `transcribe.cpp`); Silero VAD (ONNX,
  `vad-rs` dep); smart-turn-v3 (ONNX, `ort`); Parakeet realtime EOU (ONNX, disabled by
  default). Paths via env: `SPEECH_CORE_MODEL_PATH`, `SPEECH_CORE_VAD_MODEL_PATH`,
  `SPEECH_CORE_SMART_TURN_MODEL_PATH`, `SPEECH_CORE_EOU_MODEL_DIR`.
- **speech-out interface**: ws on `0.0.0.0:8788` default (`speech-out/src/main.rs:27-28`),
  path `/ws/speech-out` client-side convention. TTS backend HTTP Supertonic at
  `http://127.0.0.1:7788/v1/tts` (speech-out main.rs:26).

## 3. Load-bearing external build dependency

OBSERVED `build.rs:6-24` — the daemon compiles `native/transcribe_shim.cpp` and links the
`transcribe` static lib from `TRANSCRIBE_CPP_DIR`, default `~/workspace/external/transcribe.cpp`.
That checkout is NOT in this repo (vendor/ contains only parakeet-rs; no transcribe.cpp).
OBSERVED present at the default path on this host (daemon `cargo check` passed).
UNRESOLVED: is this external checkout pinned/versioned anywhere (nix, lockfile, script)?

## 4. Runtime topology — what runs when the system runs

OBSERVED `scripts/install-speech-core-daemon.sh:136-174` writes two systemd user units:

- `speech-core-daemon.service` → `~/.local/bin/speech-core-daemon`, env
  `~/.config/speech-core/daemon.env`
- `speech-out-daemon.service` → `~/.local/bin/speech-out daemon`, env
  `~/.config/speech-core/speech-out.env`

Steady-state server processes: `ata-speech-core` (ws 8765) + `ata-speech-out` (ws 8788) +
`ata-speech-tts` (qwentts `:18091`, GPU) + `ata-speech-align` (wav2vec2 CTC unix sock).
Supertonic in this recon snapshot is historical; live mouth is qwentts.

Laptop diagnostic: `scripts/speech-core-live-session.sh` runs mic-adapter + watch.
Dogfood: `scripts/speech-out-live-session.sh` (canned-reply + optional greying).
Parked: `lab/scripts/speech_talker_session.py` (old pi-profile Talker loop).
Live call: sibling voicecat.

Per-connection daemon wiring (OBSERVED main.rs:557-628 `start_session`, 833-1126
`handle_connection`): hello → registry insert (generation) → model worker session +
detector worker session → per-frame ingest → per-frame gap checks → events to
JsonlLogger. Model and detector workers are dedicated OS threads with bounded audio
queues (model.rs:516 `thread::spawn`, queue default 256 `SPEECH_CORE_MODEL_QUEUE_FRAMES`;
detectors/mod.rs:207, queue 512).

## 5. Speech-in pipeline (the mature seam)

OBSERVED main.rs + model.rs + detectors/: ws frame → `DaemonState::ingest` (main.rs:649)
→ gap detection/reset → `ModelIngress::ingest_frame` (ASR) and `DetectorIngress::ingest_frame`
(VAD/smart-turn) → `DetectorWorker` (mod.rs:346) funnels `DetectorSignal`s to `TurnManager`
(turn.rs:106) → close policy → `transcript_committed` (turn.rs:1434) → `turn_closed`
(turn.rs:1449) → JsonlLogger (durable) + broadcast (live subscribers, jsonl_logger.rs).

## 6. Test surface

OBSERVED:
- Rust unit tests: 161 `#[test]` fns — daemon 69, speech-out 42, watch 34, mic 9,
  protocol 7, file 0. `cargo check -p speech-core-protocol` and `-p speech-core-daemon`
  pass on this host.
- Shell integration tests `tests/`: test-speech-out-live-session-topology.sh (process
  teardown), test-barge-in-dual-asr.sh, test-assistant-self-asr-harness.sh,
  test-speech-out-live-session.sh, test-speech-out-playback-supervision.sh.
- Golden suite: `tests/golden/test_golden.py`, `test_operator.py` against
  `scripts/speech-core-golden.py` (133.9K) + `speech-core-golden-assert.py` (33.7K);
  spec at `docs/golden-suite-spec.md`. Synthetic/mocked; NOT live-mic (current-state.md).

## 7. Docs authority map (for conflict review)

- Charter (ratified 2026-07-30): product invariants 1-10 — binding intent, not code.
- ADR-001 ratified (backend-neutral reversible CosyVoice policy); ADR-002/003/004 proposed
  (Talker steering/async speech/multi-profile); ADR-005/006 accepted direction.
- `docs/evolution/02-08` + ACTIVE.md: accepted TARGET specs, "not proof of implementation"
  (each header states this; e.g. evolution/03 "schema not yet implemented").
- `docs/current-state.md`, `docs/seams.md`: maintained guides; seams.md is the closest
  existing map but is candidate-claim only (e.g. its model-worker seam references
  `native/transcribe_shim.cpp` — OBSERVED correct; but "one transcribe.cpp stream per
  stream_session_id" is OBSERVED true, model.rs `sessions: HashMap<String, ModelSession>`).

## 8. Boundaries and ownership (OBSERVED structure, INFERRED intent)

- protocol crate owns the wire contract; daemon owns sessions, ASR, detectors, turn
  policy, event log; adapters own capture/replay; watch owns presentation; speech-out
  owns synthesis/playback. Matches charter authority map (transport / speech input /
  synthesis / playback / event sinks) — INFERRED, not stated as code intent.
- No controller/session-control component exists in code yet (charter's "session control,
  when present"). Turn dispatch trigger `transcript_committed` is emitted but nothing in
  the workspace consumes it as a controller. UNRESOLVED: consumer = future work, per
  README "Later: Controller".
