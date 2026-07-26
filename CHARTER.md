# Speech Core charter

**status:** draft — binding only after explicit operator ratification

## Purpose

Speech Core exists to make spoken human-agent interaction real-time, interruptible, observable, testable, and causally legible.

Its product domain includes:

- speech input, transcription evidence, and user-turn commitment;
- routing explicit assistant speech toward synthesis and playback;
- synthesis, playback, cancellation, and interruption coordination;
- adapters to transports and reasoning runtimes;
- structured events, traces, replay, evaluation, and operator diagnostics.

## Product authority boundaries

Speech Core integrates several authorities without flattening them:

- **transport** owns connection, channel, and transport-delivery state;
- **reasoning runtime** owns model runs, tool execution, prompt or steer acceptance, and reasoner lifecycle;
- **speech input** owns audio ingress, ASR, VAD, turn evidence, and committed user-turn state;
- **synthesis** owns model loading, synthesis progress, emitted audio, cancellation acknowledgement, and backend provenance;
- **playback** owns device submission, buffering, stop/flush behavior, and qualified cursor or audibility evidence;
- **session control**, when present, owns conversation/run coordination, interruption policy, and conversational-history mutation;
- **event and audit sinks** preserve structured evidence but are not the real-time control path by default.

A component may declare only state it can directly establish. Observing another authority does not transfer ownership.

## Product invariants

1. `transcript_committed` is the authoritative committed user-turn snapshot. Later diagnostic finalization does not silently revise it.
2. Speech input and speech output remain separable processes and failure domains.
3. Every cross-component operation has bounded resources or queues, explicit backpressure behavior, and one terminal outcome.
4. Timing claims identify comparable monotonic clock domains; audio-sensitive claims identify sample domains and ranges.
5. Audible and heard states are qualified estimates or classifications with method, evidence, and uncertainty, never unqualified facts.
6. Dependency or target loss is explicit. Speech Core does not silently retarget, create a fallback reasoner, resume cancelled output, or substitute independent state.
7. Only explicit user-directed assistant text may become speech. Hidden reasoning, tool names, calls, arguments, results, and lifecycle events are not automatically narrated.
8. Conversational history may be mutated only by its explicitly designated owner.
9. Physical playback stop does not wait for retrospective alignment or history classification.
10. A deployed path remains available until its replacement passes accepted product gates and rollback has been demonstrated.

## Prohibited compromises

Speech Core must not:

- let an adapter become authoritative merely because it observes another component;
- infer hidden runtime state from logs, process output, timing guesses, or UI presentation when a structured contract is required;
- introduce a second steering queue or silently replace the reasoning runtime's prompt, steer, run, or tool authority;
- claim that submitted audio was heard without qualified playback evidence;
- treat JSONL or another audit sink as the real-time transport by default;
- convert hidden reasoning or tool activity into speech;
- allow cancelled audio to resume silently;
- report partial interruption success as complete coordinated success.

## Amendment authority

The operator is the ratifying authority for this charter and for material decisions that change product scope, authority boundaries, external contracts, production synthesis backend, transport commitments, reasoning-runtime integration, or safety and rollback gates.

An amendment becomes active only after explicit operator approval of the exact diff and a committed change. Draft decisions have no authority. Supersession is explicit and prior records remain available through Git.

Emergency stop, isolation, or rollback does not silently amend this charter.
