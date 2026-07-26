# 07 — Evaluation and release gates

**status:** accepted evaluation specification; implementation partial
**authority:** subordinate to `CHARTER.md`; not proof of implementation

## Evaluation tiers

### L0 — deterministic event/reducer replay

Canonical fixtures drive reducers, TUI panels and summaries. This tier validates ordering, duplicates, gaps, unknown events, lifecycle state and terminal outcomes. It does not rerun models.

### L1 — deterministic component simulation

Fake Pi streams, fake CosyVoice PCM and a fake player validate segmentation, scheduling, cancellation, cursor accounting, interruption transactions and history policy without devices or models.

### L2 — pinned-model replay

Pinned audio, model revisions, seeds where meaningful and configuration locks rerun speech-in, CosyVoice and BFA with threshold-based assertions. Bit-identical model output is not assumed.

### L3 — local physical/loopback route

PipeWire virtual loopback or calibrated real hardware measures first audible estimate, device buffering, stop latency and cursor error.

### L4 — Discord end-to-end

Discord is a primary acceptance path, not an optional late benchmark. It validates the actual operator transport and voice-channel lifecycle.

## Required scenario families

- no interruption;
- real spoken interruption;
- false VAD from noise/wind;
- interruption during a vowel and near a word boundary;
- hesitation/abandoned interruption;
- user speech while Pi is running a tool;
- explicit intermediate assistant message before a tool;
- multiple intermediate messages followed by final response;
- final speech superseding queued intermediate speech;
- repeated rapid native Pi steering;
- steer/run-completion race;
- shadow target process death and exact-session loss;
- reconnect to the same durable session;
- prohibited silent fallback attempt;
- CosyVoice cancel before/after first PCM;
- player underrun, disconnect and duplicate cancellation;
- BFA timeout and stale late result.

## Discord acceptance path

At minimum automate or operator-script:

```text
join/connect
-> select exact agent/session
-> committed spoken request
-> early intermediate assistant speech
-> tool work
-> spoken steer during active run
-> continued agent work
-> streamed final speech
-> audible barge
-> disconnect
```

Also validate:

- voice-channel reconnect;
- ordering between Discord text/status and speech;
- target-loss notification;
- no accidental standalone Talker answer;
- buffering and latency attributable to Discord transport.

## Stage metrics

Measure independently:

- committed user EOU -> Pi prompt/steer submission;
- committed user EOU -> Pi acceptance;
- user EOU -> first assistant text delta;
- first delta -> first committed speakable segment;
- segment commit -> TTS request;
- TTS request -> first PCM;
- first PCM -> player acceptance;
- player acceptance -> first audible estimate;
- total EOU -> first audible assistant speech;
- barge confirmation -> duck;
- barge confirmation -> final submitted sample;
- barge confirmation -> audible-silence estimate;
- BFA warm latency;
- steer acceptance and next-agent-response latency.

Report p50, p95, sample count, clock domains, uncertainty and backend/configuration.

## Correctness gates

### Event gate

- every operation has one terminal outcome;
- duplicate/out-of-order fixtures reduce deterministically;
- unknown events remain visible;
- no cross-clock latency claim lacks comparability metadata.

### Shadow-session gate

- direct Pi steer is used during active runs;
- active tools are not killed merely for steering;
- target loss is explicit;
- no silent new session, label-based substitution or independent answer;
- connected-mode tool and prompt shrink is proven.

### Speech semantics gate

- only explicit user-directed assistant text is speakable;
- tool lifecycle events never generate speech;
- intermediate/final classification is causally correct;
- raw model tokens are not sent individually to TTS.

### Audio gate

- production configuration cannot activate Supertonic or arbitrary command synthesis;
- CosyVoice provenance is emitted;
- cancellation is bounded and emits no post-cancel PCM;
- player cursor is monotonic and bounded by submitted samples;
- audible estimates carry calibration/uncertainty.

### Interruption gate

- playback stop does not await BFA;
- no cancelled audio resumes;
- all transaction legs share `interruption_id`;
- partial failure cannot report total success;
- late BFA cannot rewrite a newer turn.

### Privacy and operations gate

- audio/transcript/trace retention classes and TTLs are explicit;
- worker/control endpoints bind locally by default or are authenticated;
- retain/delete actions are auditable;
- disabling audio retention leaves no retained mic or TTS artifact unless required by an explicit test fixture.

## Baseline defects

The existing topology test failure and Talker file-resource warnings are recorded separately. A new-path gate cannot be weakened to hide them; equally, they are not attributed to a planning-only change.

## Cutover gate

Cutover requires:

1. package gates green;
2. L0/L1 on every change and L2/L3/L4 evidence for release;
3. exact pinned artifacts and reproducible environment setup;
4. current service preservation and tested rollback;
5. operator approval after Discord dogfood;
6. no automatic fallback from the candidate to Supertonic.

After cutover acceptance, remove Supertonic production configuration, process management and documentation in explicit removal slices.
