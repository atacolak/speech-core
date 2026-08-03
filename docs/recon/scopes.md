# Speech Core — proposed living-spec scopes (recon phase 1)

Selection criteria from brownfield-adoption.md §2: small set of externally observable
scenarios, identifiable state owners, executable tests/probes, bounded dependency
surface, architectural consequence. Existing code = observed reality, never automatic
intent; every scope answers a concrete operator question.

## Scope A — speech-in turn lifecycle (DRAFTED)

The TurnManager state machine: how detector evidence (VAD / smart-turn / EOU) becomes an
immutable `transcript_committed` + `turn_closed`, in what event order, with what
guarantees (per-turn text slicing, ghost-turn guard, human-hold, close-time model drain
finalize).

- operator question: "When the daemon closes a turn, exactly what did it commit, in what
  order, and what can a controller rely on?"
- owners: TurnManager (turn.rs), ModelProgressMap (model.rs), DetectorWorker (mod.rs).
- why bounded: charter invariants 1-2 and 9 are directly testable here; 69 unit tests in
  daemon plus `tests/test-barge-in-dual-asr.sh` touch this seam.
- why now: this is the product's authoritative snapshot boundary; every future controller
  depends on its exact ordering.

## Scope B — audio ingress transport contract (DRAFTED)

The SCF1 wire contract between adapters and daemon: envelope, hello/session lifecycle,
per-frame metadata validation, sequence/sample gap handling and reset propagation,
latency provenance rules.

- operator question: "What must an adapter (or test harness) conform to, and what does
  the daemon promise in return — including on gaps?"
- owners: speech-core-protocol (lib.rs), DaemonState (main.rs), adapters.
- why bounded: pure wire contract, fully deterministic, no ML; 7 protocol unit tests +
  daemon ws integration tests; replayable with file-adapter.

## Scope C — speech-out utterance contract (proposed, NOT drafted)

Speech-out ws protocol: Speak/NestedSpeak/Cancel/PlaybackReady/Ping, text chunking
bounds, Supertonic HTTP backend lifecycle (managed/external, warm TTL), playback gate,
retained WAV for barge align, cancellation semantics.

- operator question: "What does the speech-out seam promise, and what parts are
  dogfood-transient while ADR-001 (reversible backend qualification) is in flight?"
- why deferred: ADR-001 explicitly says current Supertonic behavior does not define the
  replacement wire contract; the doc-confessed limits (per-chunk buffering, ~0.5s synth
  floor) make much of the current behavior transient. Drafting now would enshrine
  transitional behavior. Revisit when a backend-neutral contract lands.

## Scope D — event/observability surface (proposed, NOT drafted)

JsonlLogger duality: broadcast-to-subscribers vs durable-jsonl (filtered: `vad_meter`,
`turn_hold`, `audio_frame_ingested`), rotation policy, event naming, watch modes.

- operator question: "Which events are live-only, which are durable, and what is the
  schema versioning policy?" (`runtime_provenance`/`EVENT_SCHEMA_VERSION`, provenance.rs).
- why deferred: orthogonal to the two commitment questions; cheap to add once A/B accepted.

## Scope E — runtime deployment & external deps (proposed, NOT drafted)

systemd units, daemon.env/speech-out.env generation, external transcribe.cpp + Supertonic
presence/versioning, install/rollback path.

- operator question: "What is the reproducible deployment unit, and which dependencies
  are outside the repo?"
- why deferred: inventory.md §3 records the transcribe.cpp finding as the key
  UNRESOLVED; a spec here needs a versioning decision first.

## Scope F — dogfood loop (barge-in + greying + Talker) (proposed, NOT drafted)

`speech-out-live-session.sh` harness, barge cut (provisional wall-clock → CTC refine),
TUI greying, `speech_talker_session.py`.

- operator question: "What is the current live loop's contract so barge-in work can be
  evaluated against a moving spec?"
- why deferred: Python/bash harness, few deterministic tests, and ADR-002/003/004
  (proposed) may change Talker routing assumptions underneath it.

## Recommended order

A and B first (both drafted here, both accepted-evidence-backed). Then C once ADR-001's
backend-neutral decision crystallizes, D alongside A (same event stream), E when a
transcribe.cpp pin exists, F last or on explicit operator request.

## What got drafts

A `docs/recon/specs/speech-in-turn-lifecycle.md`
B `docs/recon/specs/audio-ingress-transport.md`
