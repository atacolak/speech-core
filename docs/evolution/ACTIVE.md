# Active Speech Core evolution

**status date:** 2026-07-26
**selected direction:** build the low-latency interruptible Speech Core target described in this directory
**authorization:** only city work items authorize implementation

## Current reality

**2026-08-18 live mouth is qwentts.cpp Q8 CustomVoice**, not CosyVoice and not Supertonic. see [`../current-state.md`](../current-state.md). CosyVoice in this file is a *selected 2026-07 target*, not the installed pin.

The live branch contains a mature speech-input path and a dogfood speech-output path:

- Nemotron streaming ASR, Silero VAD, smart-turn v3 endpointing, and immutable `transcript_committed`;
- a separate speech-out daemon (`:8788`) whose inference is `qwentts-tts-server` (`:18091`);
- voicecat as the call (SmallWebRTC / leftover Discord), still a `:8788` client;
- CosyVoice3 TRT `aa86b67` kept as rollback only.

This directory is not the accepted final production architecture. It remains direction archive until the operator opens a speech-out spec change.

## Accepted decisions

The operator has selected these directions, recorded under [`../decisions/`](../decisions/):

- pinned CosyVoice progressive PCM for the replacement production path;
- native Pi prompt/steer routing without a second steering queue;
- speech of explicit intermediate and final assistant messages;
- sticky shadowing of one exact Pi session with fail-loud target loss;
- qualified audibility and heard-state semantics;
- Discord as a first-class acceptance interface.

Acceptance selects direction. It does not claim implementation.

## Target architecture

The target introduces explicit seams for:

- canonical events and identities;
- persistent Pi/Talker binding;
- session control and history ownership;
- progressive speech egress;
- bounded synthesis and cancellation;
- a separate player with cursor and audibility evidence;
- asynchronous BFA refinement;
- shared reducer/evaluation surfaces;
- Discord transport integration.

See the numbered specifications in this directory.

## Immediate delivery sequence

The candidate dependency order is:

1. **baseline evidence:** capture reproducible current service topology, commands, test results, representative traces, and known defects;
2. **CosyVoice qualification:** pin repository, model, runtime, voice assets, dependencies, GPU/VRAM behavior, first-PCM latency, cancellation granularity, and rollback without touching deployed paths;
3. **canonical event foundation:** implement the event envelope, identities, terminal outcomes, fixtures, and reducer skeleton;
4. **progressive PCM contract:** implement and test speak/cancel/frame/backpressure schemas with fake source and sink;
5. **persistent Pi adapter:** prove exact-session binding, native prompt/steer, event translation, and fail-loud loss;
6. **bounded egress and player:** implement fake-sink semantics before real PipeWire and CosyVoice integration;
7. **session control and segmentation:** join explicit assistant text, immutable segments, scheduling, and history authority;
8. **coordinated interruption:** implement stop/cancel/steer/history transaction and provisional/BFA refinement;
9. **Discord and cutover evidence:** pass deterministic, pinned-model, physical, and Discord gates before operator-approved cutover.

Only the currently authorized subset belongs in the work ledger; this sequence is not a pre-created work graph.

## Known blockers and required operator decisions

- exact CosyVoice repository/model/runtime/voice pin;
- exact supported Pi RPC/SDK interface and version;
- Discord adapter repository ownership if work crosses repositories;
- measurable audibility calibration and BFA model choice;
- cutover approval after end-to-end dogfood and rollback evidence.

## Evidence

Completed slices link the relevant work item, commits, tests or traces, review evidence when required by that work item, and remaining product gaps. A material direction changes only through an operator-ratified decision.
