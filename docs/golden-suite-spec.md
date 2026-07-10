# Golden Suite Specification

Status: implementation-ready specification

Scope: speech-core endpointing, transport/session lifecycle, golden fixture capture, and semantic comparison

Out of scope unless explicitly marked optional: ASR phoneme/minimal-pair diagnostics

## 1. Goals, non-goals, and trust model

### Goals

The golden suite must make speech-core turn-taking regressions observable, repeatable, and reviewable without depending on raw JSONL line diffs. It must:

1. Verify endpoint behavior across VAD, Smart Turn, acoustic fallback, human-hold, transcript-silence, and session lifecycle paths.
2. Preserve enough provenance to explain why a run passed or failed: WAV identity, cue timing, observed speech timing, event stream, effective daemon config, build commit, model artifact identity, and privacy/consent state.
3. Separate deterministic synthetic cases from human-recorded acoustic cases so exact millisecond expectations are only used where the stimulus is generated or scheduled.
4. Replace current `tail events.jsonl && sleep` capture patterns with direct event subscription and terminal-session synchronization.
5. Compare behavior semantically: event presence, partial order, field predicates, sample/timing tolerances, turn pairing, and accepted close sources.
6. Define an MVP gate small enough to implement first, and an exhaustive gate broad enough to prevent policy regressions before release.
7. Keep optional ASR phoneme/minimal-pair diagnostics separate and nonblocking.

### Non-goals

1. This suite is not a benchmark for absolute ASR word accuracy. Transcript checks are allowed only where endpointing needs transcript evidence or where text is deliberately coarse/tolerant.
2. This suite is not a training corpus, model-evaluation corpus, or multilingual corpus.
3. This suite does not require exact `daemon_mono_ns`, floating probabilities, generated IDs, or total event order equality.
4. This suite does not require exact human reaction timing. Human cue timelines use coarse bands and post-capture observed timing.
5. This suite does not validate speech-out/TTS behavior except where future tests explicitly cover barge-in at the client layer.

### Trust model

The suite trusts:

- 16 kHz mono PCM WAV bytes identified by SHA-256.
- Declarative scenario manifests checked into the repo.
- Generated/synthetic fixture construction code when it records generator version, seed, exact segment durations, and WAV hash.
- Direct websocket event subscription filtered by `stream_session_id`, with fail-closed validity checks before any assertion result can pass.
- Terminal session markers emitted by daemon components for the same session.
- Effective config snapshots emitted during the run, plus captured environment/build/model provenance.

The suite does **not** trust:

- Global event-log tailing by line number.
- Fixed sleeps after adapter exit.
- Raw JSONL line order across all events.
- Session IDs, turn IDs, process timestamps, queue depths, inference durations, or exact floating-point probabilities.
- Human operators to hit exact millisecond cues.
- Existing golden baselines whose daemon config, build commit, model hashes, runtime profile, fixture hashes, consent state, and capture validity were not recorded.

## 2. Definitions

- **Fixture**: A WAV plus metadata and expected assertions for one scenario.
- **Scenario**: Declarative test intent and construction/capture instructions.
- **Take**: One recorded attempt for a scenario.
- **Cue timeline**: Instructions shown to a recorder, with scheduled visual/audio cues.
- **Observed speech timing**: Post-capture measured speech/activity intervals from the WAV and event stream.
- **Terminal marker**: A session-specific event indicating no more events for a component/session are expected.
- **Effective config**: Final daemon runtime config after defaults, env, flags, profile overlays, and installed overrides are resolved.

## 3. Named config profiles and contradictory defaults

The repo currently has contradictory defaults in code/docs/installed configs. Golden runs must never infer intended thresholds from documentation alone. They must select a named profile and persist the observed effective config.

### Profiles

#### `code-defaults`

The daemon's CLI/env defaults from source:

```yaml
profile: code-defaults
sample_rate_hz: 16000
stream_chunk_ms: 160
att_context_right: 1
vad:
  frame_samples: 512
  threshold: 0.5
  onset_frames: 2
  hangover_frames: 3
  pre_speech_frames: 5
  smoothing_alpha: 0.1
  stop_threshold: 0.2
  configured_fallback_threshold: 0.1
  effective_fallback_threshold: 0.2  # max(configured_fallback_threshold, stop_threshold)
  acoustic_fallback_silence_ms: 3500
  energy_enabled: false
turn:
  vad_close_enabled: false
  semantic_gate_enabled: false
  semantic_gate_close_enabled: false
  min_vad_speech_ms: 400
  min_model_eou_speech_ms: 300
  model_eou_refractory_ms: 700
  model_alignment_timeout_ms: 3000
  human_hold_silence_ms: 7000
  transcript_silence_close_ms: 700
smart_turn:
  threshold: 0.5
  timeout_ms: 250
  cpu_count: 1
  max_audio_secs: 8
  pre_speech_ms: 500
  recheck_offsets_ms: [96, 192, 384, 768, 1536]
  legacy_recheck_interval_ms: 0
  legacy_recheck_max_attempts: 0
model_eou:
  close_enabled: false
```

#### `installed-live`

The current installed/live profile described by `docs/current-state.md` and observed baselines:

```yaml
profile: installed-live
inherits: code-defaults
overrides:
  vad.acoustic_fallback_silence_ms: 1700
  turn.vad_close_enabled: true
  turn.semantic_gate_enabled: true
  turn.semantic_gate_close_enabled: true
```

#### `golden-mvp`

The required initial golden profile. It is explicit, not inherited from host config:

```yaml
profile: golden-mvp
inherits: installed-live
required_models:
  asr: nemotron-speech-streaming-en-0.6b-Q4_K_M.gguf
  vad: silero_vad_v4.onnx
  smart_turn: smart-turn-v3.2-cpu.onnx
capture:
  frame_ms: 20
  append_silence_ms: scenario_defined
  hold_open_ms: until_terminal_markers
  realtime: false_for_synthetic_true_for_human_replay_smoke
```

#### `golden-exhaustive`

Same as `golden-mvp`, plus lifecycle/gap/drop/revision/disconnect cases, live diagnostic event subscription, and the full human-recorded scenario catalog.

### Required effective config artifact

Every run writes `effective-config.json` with:

- `profile_name` and profile file hash.
- All resolved daemon flags/env vars listed above.
- Model paths, file sizes, and SHA-256 hashes when available.
- Build provenance: git commit, dirty flag, target triple, binary path, binary SHA-256, cargo profile.
- Runtime provenance: hostname, OS, CPU architecture, start timestamp, command line with secrets redacted.
- Event-emitted snapshots: `vad_session_start`, `turn_session_start`, `smart_turn_session_start`, `model_session_start` fields.

A run fails with exit code `12 CONFIG_MISMATCH` if requested profile values differ from event-emitted effective values, except for explicitly allowed path normalization.

## 4. Artifact layout

All artifacts for one capture live under one immutable run directory. The directory is write-once for a completed run: corrections create a new run directory or a new take, never an in-place mutation without a new manifest hash.

```text
tests/golden-runs/<run_id>/
├── run.json                         # run id, start/end time, operator/tool version
├── effective-config.json            # resolved config/build/model provenance
├── manifest.lock.json               # exact expanded scenario manifest used
├── consent.json                     # consent/privacy state for human recordings
├── scenarios/
│   └── <scenario_id>/
│       ├── scenario.json            # expanded scenario entry
│       ├── takes/
│       │   └── take-001/
│       │       ├── audio.wav
│       │       ├── audio.sha256
│       │       ├── cue-timeline.json
│       │       ├── observed-speech-timing.json
│       │       ├── event-stream.jsonl
│       │       ├── filtered-live-diagnostics.jsonl
│       │       ├── assertions.json
│       │       ├── result.json
│       │       ├── recorder-quality.json
│       │       └── privacy.json
│       └── accepted-take.json
└── report.json
```

Baseline fixtures promoted to repo live separately from run output:

```text
tests/golden/
├── manifest.yaml
├── profiles/
│   ├── golden-mvp.yaml
│   └── golden-exhaustive.yaml
├── fixtures/
│   └── <scenario_id>/
│       ├── audio.wav
│       ├── audio.sha256
│       ├── cue-timeline.json
│       ├── observed-speech-timing.json
│       ├── assertions.yaml
│       ├── provenance.json
│       └── privacy.json
└── baselines/
    └── <scenario_id>.expected.json
```

### Required per-fixture provenance fields

`provenance.json` must include:

```json
{
  "wav_sha256": "...",
  "wav_sample_rate_hz": 16000,
  "wav_channels": 1,
  "wav_format": "pcm_s16le",
  "wav_duration_ms": 0,
  "construction": {
    "kind": "synthetic|generated_tts|human_recorded|legacy_import",
    "generator": "golden-synth-v1|espeak-ng|guided-recorder|unknown",
    "seed": 0,
    "source_prompt": "..."
  },
  "cue_timeline_path": "cue-timeline.json",
  "observed_speech_timing_path": "observed-speech-timing.json",
  "effective_config_hash": "...",
  "build_git_commit": "...",
  "model_hashes": {
    "asr": "...",
    "vad": "...",
    "smart_turn": "..."
  },
  "consent_id": "local-consent-...",
  "retention_policy": "local-dev-ttl-90d"
}
```

### Immutable provenance and chain of custody

Every promoted fixture and every run result must bind five immutable identities by name:

1. **Build provenance**: source git commit, dirty flag, target triple, cargo profile, daemon binary path, daemon binary SHA-256, adapter/runner binary SHA-256, and golden tool version.
2. **Effective-config provenance**: selected profile name, profile file SHA-256, resolved CLI/env/default values, event-emitted component config snapshots, and the comparison result between requested and observed values.
3. **Model provenance**: ASR, VAD, Smart Turn, and optional model-EOU artifact paths, byte sizes, SHA-256 hashes, runtime provider, and model load status. Mutable filesystem paths alone are not provenance.
4. **Runtime provenance**: hostname or pseudonymous host id, OS, kernel, CPU architecture, ONNX/runtime provider, start/end timestamps, command line with secrets redacted, and clock/timestamp provenance.
5. **Fixture provenance**: scenario manifest hash, fixture WAV hash, fixture sidecar hashes, replay parameters, construction kind, generator version/seed or recorder build, capture device id, and consent/retention class.

Human and generated audio require an explicit chain-of-custody record:

```json
{
  "capture_time_raw_sha256": "sha256 of bytes written at capture stop before edits or trimming",
  "post_quality_sha256": "sha256 after allowed deterministic normalization, if any",
  "accepted_fixture_sha256": "sha256 promoted to tests/golden/fixtures",
  "normalization_steps": ["trim_leading_silence_to_manifest_bound"],
  "review": {
    "reviewer": "local pseudonym or tool identity",
    "reviewed_at": "ISO-8601",
    "decision": "accepted|rejected|candidate",
    "heard_expected_prompt": true,
    "cue_contamination_absent": true,
    "quality_override": false,
    "notes": "structured free text"
  }
}
```

The accepted fixture hash is the only WAV identity used for release assertions. If `capture_time_raw_sha256` is missing for a new human/generated take, promotion fails with `19 BASELINE_REQUIRES_REVIEW`. If any recorded hash changes without a new take id and review entry, comparison fails with `10 ARTIFACT_HASH_MISMATCH`.

### Fixture disposition states

Every fixture has exactly one disposition in its scenario or provenance metadata:

- `release_gate`: blocking fixture with complete provenance, passing quality, consent, and accepted assertions.
- `candidate`: usable for local investigation but not release blocking; must state missing evidence.
- `diagnostic`: nonblocking smoke/analysis fixture; may fail without release impact.
- `quarantined`: retained only to explain legacy behavior; never used as expected behavior.
- `delete_after_run`: temporary human artifact scheduled for deletion after report generation.
- `rejected_take`: failed quality, consent, prompt, or review; not promoted.

A fixture without a disposition is invalid. Legacy fixture disposition is listed in Section 11.

## 5. Versioned declarative scenario manifest schema

The manifest is versioned. Version `1` is YAML or JSON and must validate before capture.

### Schema outline

```yaml
schema_version: 1
suite_id: string
suite_version: semver
profiles:
  - profile_name: string
    path: string
scenarios:
  - id: string                         # stable kebab-case id
    priority: mvp|exhaustive|diagnostic
    class: natural_endpoint|threshold_boundary|smart_turn|fallback|human_hold|transcript_silence|lifecycle|asr_diagnostic
    construction: synthetic|generated_tts|human_recorded|legacy_import
    human_audio_required: boolean
    deterministic_synthetic: boolean
    profile: golden-mvp|golden-exhaustive|custom
    prompt:
      display_text: string
      spoken_text: string|null
      acceptable_transcripts: [string]
      forbidden_transcripts: [string]
    cue_timeline:
      timebase: sample|ms|human_band
      cues:
        - at_ms: integer|null           # exact only for synthetic/generated
          band_ms: [integer, integer]|null # human-recorded coarse window
          label: READY|SPEAK|PAUSE|RESUME|STOP|HOLD|NOISE|DISCONNECT
          visual: string
          audio_cue: none|beep|haptic
          noncontaminating: boolean
          operator_action: string
    audio_plan:
      sample_rate_hz: 16000
      leading_silence_ms: integer|null
      segments:
        - kind: silence|tone|noise|speech_tts|human_speech|hum|frame_gap|disconnect
          duration_ms: integer|null
          band_ms: [integer, integer]|null
          text: string|null
          amplitude_dbfs: number|null
          vad_probability_target: string|null
      trailing_silence_ms: integer|null
    replay:
      frame_ms: 20
      realtime: boolean
      append_silence_ms: integer
      terminal_markers:
        required_events: [string]
        timeout_ms: integer
    assertions:
      dsl_version: 1
      file: string|null
      inline: object|null
    quality:
      min_peak_dbfs: number|null
      max_peak_dbfs: number|null
      max_clipped_samples: integer
      max_noise_floor_dbfs: number|null
      min_duration_ms: integer
      max_duration_ms: integer
    privacy:
      contains_human_voice: boolean
      consent_required: boolean
      retention: local-dev-ttl-90d|repo-fixture-explicit|delete-after-run
```

### Example scenario entry

```yaml
schema_version: 1
suite_id: speech-core-golden-endpointing
suite_version: 0.1.0
profiles:
  - profile_name: golden-mvp
    path: tests/golden/profiles/golden-mvp.yaml
scenarios:
  - id: synthetic-min-vad-speech-below
    priority: mvp
    class: threshold_boundary
    construction: synthetic
    human_audio_required: false
    deterministic_synthetic: true
    profile: golden-mvp
    prompt:
      display_text: "Synthetic 384 ms speech island, below 400 ms min VAD speech."
      spoken_text: null
      acceptable_transcripts: []
      forbidden_transcripts: []
    cue_timeline:
      timebase: ms
      cues:
        - {at_ms: 0, label: READY, visual: "generate silence", audio_cue: none, noncontaminating: true, operator_action: "none"}
        - {at_ms: 500, label: SPEAK, visual: "synthetic speech/noise starts", audio_cue: none, noncontaminating: true, operator_action: "none"}
        - {at_ms: 884, label: STOP, visual: "synthetic speech/noise stops", audio_cue: none, noncontaminating: true, operator_action: "none"}
    audio_plan:
      sample_rate_hz: 16000
      leading_silence_ms: 500
      segments:
        - {kind: silence, duration_ms: 500, text: null, amplitude_dbfs: null, vad_probability_target: "below_stop"}
        - {kind: speech_tts, duration_ms: 384, text: "uh", amplitude_dbfs: -18, vad_probability_target: "above_threshold"}
      trailing_silence_ms: 2500
    replay:
      frame_ms: 20
      realtime: false
      append_silence_ms: 0
      terminal_markers:
        required_events: [vad_session_end, turn_session_end]
        timeout_ms: 10000
    assertions:
      dsl_version: 1
      inline:
        forbid:
          - event: turn_closed
        require:
          - event: smart_turn_skipped
            where: {reason: "vad_too_short"}
        order:
          - [vad_speech_start, vad_speech_end, smart_turn_skipped]
    quality:
      min_peak_dbfs: -30
      max_peak_dbfs: -1
      max_clipped_samples: 0
      max_noise_floor_dbfs: -55
      min_duration_ms: 3000
      max_duration_ms: 4000
    privacy:
      contains_human_voice: false
      consent_required: false
      retention: repo-fixture-explicit
```

## 6. Guided recorder UX requirements

The guided recorder is required for human-recorded fixtures. It must not rely on bare `pw-record` prompts.

### Live recording controls and cues

The recorder UI must provide:

1. A monotonic live millisecond timer starting at take start, displayed as `mm:ss.mmm` and backed by a monotonic clock.
2. Countdown: `3`, `2`, `1`, `READY`, then `SPEAK`.
3. State prompts: `SPEAK`, `PAUSE`, `RESUME`, `HOLD`, `STOP`.
4. Visual cue by default. Optional audio cue must be noncontaminating: played through headphones or emitted before the recorded speech window with enough separation to trim/ignore it. Speaker playback into the microphone is prohibited for accepted takes.
5. Practice mode: runs cue timeline without saving.
6. Take mode: records and saves a numbered take.
7. Retry flow: reject take, keep artifact in rejected-takes if privacy permits, and record another.
8. Playback: local-only playback of the take before accept/reject.
9. Accept flow: stores hash, cue timeline, quality metrics, consent state, and operator notes.
10. Accessibility mode: high-contrast text, screen-reader-friendly cue labels, keyboard-only controls, optional haptic/visual cues, no mandatory audio cues.

### Noncontaminating cue and review rules

Cue mechanisms must not become test stimulus unless the scenario explicitly declares a cue-audio contamination test. Accepted human takes must satisfy all of the following:

1. Visual or haptic cues are preferred. Audible cues are allowed only through headphones or a physically separate cue channel not captured by the mono test channel.
2. Speaker playback of beeps, prompts, metronomes, TTS prompt previews, or countdown sounds into the microphone is prohibited for accepted endpoint fixtures.
3. Keyboard, mouse, device-click, chair, and monitor sounds during speech/pause windows are rejection reasons unless the scenario is a declared noise fixture.
4. If an audio cue is used before the speech window, the manifest must define a cue-exclusion region and a minimum quiet separation before speech. Automated analysis must verify no cue energy in the accepted speech/pause regions.
5. Playback review must use headphones and must not occur while recording the accepted channel.
6. Structured human review must confirm that the spoken prompt, pause/hold instructions, and absence of cue contamination match the scenario before promotion.

### Meters and automated checks

The recorder must show and persist:

- Calibration noise floor before each scenario or batch.
- Live RMS meter and peak meter in dBFS.
- Clipping count and clipping warning.
- Speech/activity detection overlay with coarse timing.
- Device name, sample rate, channels, and format.

Automated quality checks fail a take unless overridden with an explicit structured review note. Release-gate fixtures may not override wrong format, undecodable audio, missing consent, missing hashes, or cue contamination in the accepted speech region.

Required audio format and quality checks:

- RIFF/WAV decodes fully and has nonzero sample count.
- Format is exactly 16,000 Hz mono PCM16 for release fixtures; PCM F32 is allowed only for intermediate generated artifacts that are deterministically converted before promotion.
- Duration and exact sample count match manifest bounds.
- Clipping count and clipping fraction are within scenario `max_clipped_samples` and review policy.
- Peak is not below `min_peak_dbfs` or above `max_peak_dbfs`.
- RMS/noise floor is within scenario bounds for leading silence, trailing silence, and declared pause windows.
- DC offset and discontinuity/duplicate-content screens do not indicate broken capture or editing mistakes.
- Expected active regions are present in observed speech/activity bands.
- Expected silence/pause/hold regions do not contain forbidden speech or cue audio.
- Device id, sample rate, channels, sample format, and recorder version are persisted.
- Capture-time raw hash, accepted-fixture hash, and review decision are persisted.

### Repetitions, randomization, and operator bias

- Human scenarios requiring robustness must specify repetitions. MVP may use one accepted take per scenario; exhaustive requires at least three takes from at least two recording sessions for human-natural cases.
- The recorder supports randomized scenario order within a block to reduce operator learning effects.
- The manifest records the actual order shown.
- Practice takes are not promoted to baselines.

## 7. Event capture contract

Golden capture must subscribe directly to daemon events and wait for terminal markers. It must not tail a global log and must not sleep for a fixed duration as completion logic.

### Required flow

1. Generate a globally unique `stream_session_id` for the scenario run.
2. Open a websocket event subscription using `ControlMessage::SubscribeEvents` with that `stream_session_id`. If the implementation uses the same `/ws/audio-ingress` endpoint, send the subscription control message before replay. A separate `/ws/events` endpoint is acceptable if it preserves the same filter semantics.
3. Start the replay/adapter connection with the same `stream_session_id`.
4. Persist every subscribed event in receive order to `event-stream.jsonl`.
5. Continue reading until all required terminal markers for enabled components have been observed for the session, or until `timeout_ms` expires.
6. Only then close the subscription and run assertions.

### Fail-closed capture validity contract

A capture result can be `pass` only after a machine-readable validity record is written with `valid=true`. Invalid evidence fails before semantic assertions and exits nonzero. The runner must mark `valid=false` and exit with the listed code for any of these conditions:

| Invalid condition | Required behavior |
| --- | --- |
| Missing, empty, stale, or pre-existing `event-stream.jsonl` reused for a new take | `21 CAPTURE_INCOMPLETE` or `8 CAPTURE_TIMEOUT`; never pass from zero events |
| Event stream contains malformed JSON, unknown required schema version, wrong `stream_session_id`, or unparseable event discriminators | `18 EVENT_SCHEMA_INVALID` |
| WAV missing, undecodable, zero-sample, wrong sample rate/channel/format, or sample count outside manifest bounds | `17 WAV_FORMAT_INVALID` |
| Fixture hash, scenario hash, profile hash, model hash, binary hash, or accepted-take hash differs from lockfile | `10 ARTIFACT_HASH_MISMATCH` or `12 CONFIG_MISMATCH` |
| Adapter, daemon, recorder, synth tool, or assertion command exits nonzero | Propagate nonzero as `20 INTERNAL_ERROR` unless a more specific code applies |
| Daemon websocket closes before required terminal markers or final model chunk | `21 CAPTURE_INCOMPLETE`; close frame alone is not terminal evidence |
| Required terminal marker missing by timeout | `9 TERMINAL_MARKER_MISSING` |
| Capture timeout before terminal markers | `8 CAPTURE_TIMEOUT` |
| Protocol, model, VAD, Smart Turn, assertion-engine, or privacy error event not declared/expected by scenario | `1 ASSERTION_FAILED` or specific dependency/config code |
| No `stream_start`, no component session start, missing `turn_session_end`, or impossible frame/sample coverage | `21 CAPTURE_INCOMPLETE` |
| Audio frame sequence/sample coverage is noncontiguous without declared gaps, or contains unexpected duplicate/out-of-order frames | `1 ASSERTION_FAILED` |
| Human take lacks consent, retention class, access policy, structured review, or required hashes | `4 CONSENT_REQUIRED`, `11 PRIVACY_POLICY_VIOLATION`, or `19 BASELINE_REQUIRES_REVIEW` |

`event_count`, `turn_closed_count`, and raw JSONL diffs are diagnostics only. They are never sufficient pass criteria. The validity record must include the daemon/adapter exit statuses, capture start/end timestamps, session id, expected terminal markers, observed terminal markers, frame/sample coverage, artifact hashes, and a reason for any invalid disposition.

### Terminal markers

For the `golden-mvp` profile with ASR, VAD, Smart Turn, and turn manager enabled, the required terminal marker set is:

```yaml
required_terminal_markers:
  - event: vad_session_end
  - event: turn_session_end
  - event: smart_turn_session_end
  - event: model_chunk_processed
    where: {is_final: true}
```

If a component is disabled by effective config, its terminal marker is omitted and the omission is recorded in `effective-config.json`.

The runner must also treat websocket `Close` from the daemon plus missing terminal markers as a failure with exit code `21 CAPTURE_INCOMPLETE`, not as success.

### Turn close ownership and terminal-source contract

For every normal endpointing scenario, the suite enforces turn ownership rather than only counting close events:

1. Every `turn_started` for a normal started turn must have exactly one terminal `turn_closed` with the same turn ownership key.
2. A normal accepted close may use only a scenario-allowed source such as `smart_turn`, `vad`, `vad_acoustic_fallback`, `human_hold`, `transcript_silence`, or `model_eou`. The allowed source set is explicit per scenario.
3. `turn_closed source=session_end` is forbidden in normal endpointing fixtures and is valid only in scenarios whose class is `session_end`, `disconnect`, `queue_full`, or another explicit lifecycle/error class.
4. After a terminal close for a turn, no later event may mutate that turn's accepted boundary, reopen the same turn id, or assign late transcript tokens to a new turn unless the scenario explicitly tests late revision handling and asserts the revision is quarantined.
5. `session_end`, disconnect-before/after-hello, disconnect-with-open-turn, model/detector queue-full, daemon shutdown, and subscriber lag/log-delay are separate lifecycle scenarios. Their expected degraded close or error source must not be copied into normal fixture expectations.
6. A normal accepted boundary immediately rearms the turn manager for a later independent onset. A forced close while input remains continuously high must not resurrect a new turn until an observed low-release condition occurs, as specified in Section 17.

### Filtered live diagnostic events

High-frequency live diagnostics that are filtered from daemon JSONL must be capturable via direct subscription for diagnostic runs:

- `vad_meter`
- `turn_hold`
- `audio_frame_ingested` when JSONL filtering is enabled
- optionally `vad_frame` when `SPEECH_CORE_VAD_EMIT_FRAMES=true`

They are written to `filtered-live-diagnostics.jsonl`. Assertions may use them only when the scenario declares `requires_live_diagnostics: true`; normal MVP pass/fail must not depend on per-frame diagnostics.

## 8. Semantic assertion DSL

The assertion DSL compares meaning, not raw event files.

### Principles

Allowed assertions:

- Event presence/absence/count by type and field predicates.
- Partial order between named event patterns.
- Pairing invariants, such as every `turn_started` has exactly one matching `turn_closed`.
- Field equality for stable enum/string/bool fields (`source`, `degraded`, `reason`, `complete`, `timed_out`).
- Numeric ranges/tolerances for sample and timing fields.
- Transcript normalized inclusion for scenarios where transcript matters.

Forbidden assertions:

- Exact `daemon_mono_ns`, `*_mono_ns`, wall-clock timestamps, inference durations, open durations, queue depths.
- Exact floating-point probabilities/confidences. Use thresholds/ranges such as `probability >= threshold` only when semantically required.
- Exact generated `stream_session_id`, `turn_id`, token IDs, or whole-event raw JSON equality.
- Total ordering of unrelated high-frequency events.

### DSL v1 shape

```yaml
dsl_version: 1
selectors:
  close: {event: turn_closed}
  smart_complete: {event: smart_turn_decision, where: {complete: true}}
require:
  - event: turn_closed
    count: 1
    where:
      source: smart_turn
      degraded: false
  - event: turn_eou
    count: 1
forbid:
  - event: turn_closed
    where: {source: session_end}
order:
  - [vad_speech_start, turn_started]
  - [vad_speech_end, smart_turn_candidate, smart_turn_decision]
  - [turn_semantic_decision, turn_eou_candidate, turn_eou, turn_closed]
partial_order:
  - before: {event: turn_semantic_decision}
    after: {event: turn_closed}
    match: same_session
numeric:
  - event: vad_speech_end
    field: decision_sample_minus_end_sample
    expected_samples: 1536
    tolerance_samples: 512
  - event: turn_closed
    field: decision_sample
    relation: ">="
    value_from: event(vad_speech_end).decision_sample
transcript:
  normalize: lowercase_strip_punctuation_whitespace
  require_any:
    - "the weather looks great today"
invariants:
  - balanced_turns
  - monotonic_audio_seq
  - contiguous_source_samples_except_declared_gaps
```

### Partial-order, turn-ownership, and sample-window operators

The DSL must support these operators before MVP release:

```yaml
ownership:
  key: turn_id_or_session_scoped_index
  require_exactly_one_close_per_started_turn: true
  forbid_late_mutation_after_close: true
partial_order:
  - before: {event: vad_speech_end, bind: vad_end}
    after: {event: turn_closed, bind: close}
    match: same_turn
sample_window:
  - name: close_after_acoustic_end
    event: turn_closed
    field: decision_sample
    min_from: event(vad_speech_end).end_sample
    max_from: event(vad_speech_end).end_sample + samples(2500ms)
  - name: no_close_before_resume
    forbid_event: turn_closed
    window: [event(first_vad_end).decision_sample, event(resume_vad_start).start_sample]
gaps:
  expected: none|declared_only|scenario_defined
late_revisions:
  policy: forbid_after_close|allow_quarantined_diagnostic
```

Assertions may bind events by selector and then compare fields within the same session, same turn, same VAD segment, or same sample window. Total order is required only for causally related selectors listed by the scenario. Unrelated high-frequency diagnostics remain partially ordered or ignored.

### Unstable field blacklist

Assertion files are invalid if they require equality on these unstable fields, unless a scenario explicitly declares a diagnostic-only field check:

- `daemon_mono_ns`, `*_mono_ns`, wall-clock timestamps, scheduler durations, and model/VAD/Smart Turn inference durations.
- Generated `stream_session_id`, `turn_id`, token ids, UUIDs, nonce-bearing paths, host-specific absolute paths, and process ids.
- Queue depths, thread timing, log line numbers, raw JSONL byte order, whole-event equality, and total ordering of unrelated events.
- Exact floating probabilities/confidences from real ONNX/model runs. Deterministic stub tests may assert threshold relations, not platform-specific real-model floats.
- Raw transcript punctuation/case/whitespace unless the scenario is an ASR diagnostic and declares normalization.

### Tolerances

Default tolerances:

```yaml
tolerances:
  synthetic:
    cue_ms: 0
    source_sample: 0
    vad_boundary_samples: 512       # one native VAD frame
    turn_decision_samples: 1536      # three native VAD frames, unless scenario exact
    transcript_text: normalized
  generated_tts:
    cue_ms: 0
    source_sample: 0
    vad_boundary_samples: 1024
    turn_decision_samples: 1536
  human_recorded:
    cue_band_ms: scenario_defined
    observed_speech_band_ms: scenario_defined
    vad_boundary_samples: 2048      # 128 ms
    turn_decision_samples: 4096     # 256 ms
```

Scenario-specific threshold-boundary tests may tighten tolerances when construction is deterministic and fixture WAV hash is fixed.

## 9. Prioritized scenario catalog

The catalog below is the implementation target. MVP scenarios are required first; exhaustive scenarios expand coverage.

### Timing constants for synthetic boundary scenarios

At 16 kHz:

- Transport frame: 20 ms = 320 samples.
- VAD native frame: 32 ms = 512 samples.
- VAD onset default: 2 frames = 64 ms = 1024 samples.
- VAD hangover default: 3 frames = 96 ms = 1536 samples.
- Turn min VAD speech default: 400 ms = 6400 samples.
- Smart Turn rechecks: 96/192/384/768/1536 ms = 1536/3072/6144/12288/24576 samples.
- Installed acoustic fallback: 1700 ms = 27200 samples.
- Code-default acoustic fallback: 3500 ms = 56000 samples.
- Human hold: 7000 ms = 112000 samples.
- Transcript silence close: 700 ms = 11200 samples.

### Synthetic exact threshold triplets

Human recordings use coarse cue bands and observed speech timing. Exact threshold gates use synthetic sample-addressed stimuli or deterministic stubs. At 16 kHz, one sample is 0.0625 ms and one integer millisecond is exactly 16 samples. Tables below give sample counts as the normative value; millisecond labels are explanatory. VAD native decisions are quantized to 512-sample frames, so VAD-frame triplets are expressed in frames. Scheduler/recheck assertions compare sample targets and then allow only the scenario-declared processing quantization, usually zero in a stub harness and one VAD frame for real-audio integration.

| Policy / boundary | Exact below / at / above triplet | Required assertion domain |
| --- | ---: | --- |
| VAD onset frames | 1 / 2 / 3 qualifying 512-sample frames = 512 / 1024 / 1536 samples | Stubbed VAD probabilities; no `vad_speech_start` below, exactly one start at/above, no duplicate start |
| VAD hangover | 2 / 3 / 4 low 512-sample frames = 1024 / 1536 / 2048 samples | Stubbed VAD probabilities; no end before 1536 low samples, one end at/after, no duplicate end |
| Minimum VAD speech | 399 / 400 / 401 ms = 6384 / 6400 / 6416 samples | No close candidate below; eligible at/above after start/end quantization is accounted for |
| VAD start probability | 0.499999 / 0.500000 / 0.500001 | Deterministic VAD stub only; below does not count as speech, at/above counts |
| VAD stop probability | 0.199999 / 0.200000 / 0.200001 | Deterministic VAD stub only; below is low/silence, at/above remains speech |
| Configured fallback probability | 0.099999 / 0.100000 / 0.100001 | Config parser/stub verifies configured value; must not be described as effective when stop threshold is higher |
| Effective fallback probability | 0.199999 / 0.200000 / 0.200001 | VAD stub verifies effective threshold is `max(configured_fallback_threshold, stop_threshold)`; default effective value is 0.2 |
| Installed fallback silence | 1699 / 1700 / 1701 ms = 27184 / 27200 / 27216 samples | No fallback before threshold; exactly one fallback close at/after; profile hash records installed value |
| Code-default fallback silence | 3499 / 3500 / 3501 ms = 55984 / 56000 / 56016 samples | Same assertion under `code-defaults` profile or explicit profile-derived override |
| Smart Turn completion probability | 0.499999 / 0.500000 / 0.500001 | Deterministic semantic-decision injection; below suppresses/incomplete, at/above completes/closes when close enabled |
| Smart Turn timeout | 249 / 250 / 251 ms = 3984 / 4000 / 4016 sample-equivalent harness ticks | Controllable inference clock; no timeout classification below, boundary behavior explicitly defined by implementation, timeout/fail-open after boundary |
| Smart Turn rechecks | 95/96/97, 191/192/193, 383/384/385, 767/768/769, 1535/1536/1537 ms = 1520/1536/1552, 3056/3072/3088, 6128/6144/6160, 12272/12288/12304, 24560/24576/24592 samples after acoustic end | Probe no earlier than target; duplicate/obsolete probes filtered; cancellation on resumed speech; no probe after close |
| Human hold | 6999 / 7000 / 7001 ms = 111984 / 112000 / 112016 samples of continuous speech-like/no-token input | No hold close before; exactly one `turn_human_hold` and `turn_closed source=human_hold` at/after threshold; continuous high cannot reopen until low release |
| Transcript silence close | 699 / 700 / 701 ms = 11184 / 11200 / 11216 samples | Transcript-backed/no-VAD path closes at/after; VAD-backed turns unaffected |
| Model EOU minimum speech | 299 / 300 / 301 ms = 4784 / 4800 / 4816 samples | EOU suppressed below; eligible at/above if not otherwise refractory/in-speech |
| Model EOU refractory | 699 / 700 / 701 ms = 11184 / 11200 / 11216 samples | Second EOU suppressed inside refractory; one acceptance at/after boundary as implementation defines equality |
| Model alignment deadline | 2999 / 3000 / 3001 ms = 47984 / 48000 / 48016 sample-equivalent harness ticks | Close does not hang; `turn_close_alignment` records aligned vs timed-out; transcript quiescence is bounded rather than requiring token end >= decision sample |

Exact triplets belong in Rust unit/integration tests or deterministic harness scenarios first. Real human takes may cover the same behavior qualitatively but cannot pass or fail these exact boundaries.

### MVP natural endpoint classes

#### `human-clean-complete`

- Priority: MVP
- Construction: human-recorded
- Human audio required: yes
- Prompt: “The weather looks great today. I think I will go outside.”
- Cue timeline: `READY` 0-3000 ms, `SPEAK` 3000-9000 ms, `STOP` after speech plus a comfortable 1000-2000 ms silence. Exact speech timing is not asserted.
- Assertions: exactly one `turn_closed`, expected preferred source `smart_turn`, `degraded=false`; if current model cannot achieve this, fixture remains `candidate` and cannot be MVP release gate until re-recorded.
- Purpose: ordinary complete utterance.

#### `human-trailing-off`

- Priority: MVP
- Construction: human-recorded
- Human audio required: yes
- Prompt: “I was going to say something about the… actually, never mind.” Speak with trailing uncertainty near the ellipsis.
- Cue timeline: `SPEAK` band 3000-12000 ms, optional trailing low-volume words, `STOP` after 1000-2000 ms silence.
- Assertions: one `turn_closed`; allow `smart_turn` if complete or `vad_acoustic_fallback` degraded if semantic stays incomplete. Must include a `smart_turn_decision complete=false` or a complete close with clear rationale.
- Purpose: trailing-off endpointing and conservative fallback.

#### `human-pause-resume-incomplete`

- Priority: MVP
- Construction: human-recorded
- Human audio required: yes
- Prompt: “I need to check…” `PAUSE`, then “one more thing before I answer.”
- Cue timeline: `SPEAK` 3000-5000 ms, `PAUSE` 5000-6200 ms coarse band, `RESUME` 6200-10000 ms. Human band: pause must be 800-1600 ms by observed speech timing.
- Assertions: no `turn_closed` before resumed speech starts; at least one `smart_turn_decision complete=false` or `turn_eou_suppressed reason=semantic_incomplete`; final close after resumed speech.
- Purpose: Smart Turn recheck cancellation and no premature turn split.

#### `human-rapid-question`

- Priority: MVP
- Construction: human-recorded
- Human audio required: yes
- Prompt: “What time is it right now?” spoken naturally and quickly.
- Cue timeline: `SPEAK` 3000-5500 ms; `STOP` after 1000-2000 ms silence.
- Assertions: one clean close, preferred `smart_turn`, no session-end extra close.
- Purpose: short complete question.

### Exact threshold boundary triplets

Each threshold has below/at/above triplets. Construction must be deterministic synthetic unless stated otherwise. Use generated voiced/noise segments that are verified to cross VAD threshold in calibration; if pure synthetic cannot reliably drive Silero, use checked-in generated TTS WAVs and lock WAV hashes.

#### VAD onset triplet: 2 frames / 64 ms

1. `synthetic-vad-onset-below-32ms`
   - Audio: 500 ms silence, 32 ms speech-like segment, 1000 ms silence.
   - Expected: no `vad_speech_start`, no `turn_started`.
2. `synthetic-vad-onset-at-64ms`
   - Audio: 500 ms silence, 64 ms speech-like segment, 1500 ms silence.
   - Expected: `vad_speech_start` may occur only after second frame; if VAD emits start, segment may be suppressed later by min speech. No `turn_closed` unless min speech also satisfied.
3. `synthetic-vad-onset-above-96ms`
   - Audio: 500 ms silence, 96 ms speech-like segment, 1500 ms silence.
   - Expected: `vad_speech_start` present; later close suppressed by `vad_too_short` or no close.

#### VAD hangover triplet: 3 frames / 96 ms

1. `synthetic-vad-hangover-below-64ms-silence`
   - Audio: 500 ms silence, 600 ms speech-like, 64 ms low silence, 400 ms speech-like, 1500 ms silence.
   - Expected: one VAD segment; no `vad_speech_end` in the 64 ms internal gap.
2. `synthetic-vad-hangover-at-96ms-silence`
   - Audio: 500 ms silence, 600 ms speech-like, 96 ms low silence, 400 ms speech-like, 1500 ms silence.
   - Expected: VAD may end at the boundary; assert `vad_speech_end.decision_sample - end_sample` within 1536 ± 512 samples.
3. `synthetic-vad-hangover-above-128ms-silence`
   - Audio: 500 ms silence, 600 ms speech-like, 128 ms low silence, 400 ms speech-like, 1500 ms silence.
   - Expected: two VAD segments or one end/start pair around the gap; exact turn close depends on semantic gate and min speech.

#### Turn min VAD speech triplet: 400 ms

The normative min-speech boundary is asserted in the event/harness sample domain with exact 16 kHz counts. Real-WAV VAD start/end quantization may add a separate scenario-declared boundary tolerance, but the threshold comparison itself uses the triplet below.

1. `synthetic-min-vad-speech-below-399ms`
   - Harness/audio: 500 ms silence, 6384 samples (399 ms) speech-like or injected VAD segment, 2500 ms silence.
   - Expected: `smart_turn_skipped reason=vad_too_short` or `turn_eou_suppressed reason=vad_too_short`; no `turn_closed` from VAD/smart_turn.
2. `synthetic-min-vad-speech-at-400ms`
   - Harness/audio: 500 ms silence, 6400 samples (400 ms) speech-like or injected VAD segment, 2500 ms silence.
   - Expected: eligible boundary; Smart Turn candidate may be emitted. Assertion checks eligibility, not completion probability.
3. `synthetic-min-vad-speech-above-401ms`
   - Harness/audio: 500 ms silence, 6416 samples (401 ms) speech-like or injected VAD segment, 2500 ms silence.
   - Expected: eligible boundary; no `vad_too_short` suppression.

### Smart Turn recheck scenarios

#### `synthetic-smart-recheck-schedule`

- Priority: MVP
- Construction: synthetic/generated TTS with forced incomplete semantic decision, or mock Smart Turn in harness.
- Exact timeline: speech segment ends at sample `S`; initial decision at `S + 1536` from VAD hangover; scheduled recheck decision samples must be greater than initial and drawn from `S + {3072, 6144, 12288, 24576}` after duplicate filtering.
- Assertions: `smart_turn_recheck_scheduled` pending samples match expected offsets within 0 samples in mock/synthetic harness, or within 512 samples in real-audio runner; `smart_turn_recheck_exhausted` appears if no completion.

#### `human-smart-recheck-cancel-on-resume`

- Priority: exhaustive
- Construction: human-recorded
- Prompt: “I need to think…” pause 800-1600 ms, resume “actually continue here.”
- Assertions: at least one `smart_turn_recheck_scheduled`, then `smart_turn_recheck_cancelled reason=speech_resumed|new_speech` before final close.

### Acoustic fallback scenarios

#### `synthetic-acoustic-fallback-installed-1700`

- Priority: MVP
- Profile: `installed-live` or `golden-mvp`
- Audio: eligible speech-like segment ending at sample `E`, semantic incomplete/unavailable forced if needed, then low silence for 1900 ms.
- Expected: `vad_acoustic_fallback` after at least 1700 ms low silence; `turn_closed source=vad_acoustic_fallback degraded=true`.
- Timing assertion: `vad_acoustic_fallback.decision_sample - vad_speech_end.end_sample >= 27200`, tolerance +1024 samples.

#### `synthetic-acoustic-fallback-code-default-3500`

- Priority: exhaustive
- Profile: `code-defaults` with VAD close/semantic enabled for test
- Audio: same as above with 3700 ms silence.
- Expected: fallback only after 3500 ms.

### Human hold scenario

#### `human-hold-continuous-filler-7000`

- Priority: exhaustive until stable, then MVP if product relies on it
- Construction: human-recorded or generated sustained nonlexical vocalization
- Human audio required: yes if validating actual microphone/user behavior; synthetic allowed for timer path.
- Prompt: sustain “uhhhhh” / thinking hum without forming words for 8-10 seconds.
- Cue timeline: `SPEAK/HOLD` coarse band 3000-11000 ms, `STOP` after hold event or at 12000 ms.
- Assertions: `turn_human_hold reason=speech_like_audio_without_tokens` with `ms_without_tokens >= 7000`, followed by exactly one `turn_closed source=human_hold degraded=true|false as implemented`. The hardening target is closing behavior; a non-closing human-hold profile is out of scope for this suite.
- Rearm assertion: if speech-like input remains continuously high after the human-hold forced close, no new `turn_started` may occur until a low-release interval is observed. After the low release, a later onset may start a new turn.
- Note: Accepted takes must keep VAD active continuously long enough; old 6.7 s fixture cannot qualify.

### Transcript silence scenario

#### `synthetic-transcript-silence-close-700`

- Priority: MVP
- Construction: synthetic event-harness or daemon test adapter that injects transcript token without VAD signal.
- Audio: silence or below-threshold audio; inject `TranscriptTokenCommitted` at sample 0-3200 if using internal harness.
- Expected: transcript-backed turn starts; after low VAD silence >= 700 ms, `turn_closed source=transcript_silence degraded=true`.
- Rationale: This is difficult to create with pure WAV because transcript tokens require ASR; use deterministic harness first.

### Lifecycle/gap/drop/late-revision/disconnect/reused-ID suite

These are exhaustive gates and should be Rust integration tests or a protocol-level runner rather than human WAV fixtures.

1. `protocol-sequence-gap`
   - Send seq 0, 1, 3.
   - Expect `audio_gap expected_seq=2 observed_seq=3 missing_frames=1`.
2. `protocol-sample-gap`
   - Send `source_sample_start` 0, 320, 960.
   - Expect `audio_sample_gap expected_sample_start=640 observed_sample_start=960 delta_samples=320`.
3. `protocol-declared-source-gap`
   - Send frame with `preceding_source_gap`.
   - Expect `audio_sample_gap.declared_source_gap` preserved.
4. `protocol-drop-invalid-payload`
   - Send malformed frame/payload length mismatch.
   - Expect `error` or `audio_drop` and session remains recoverable if protocol allows.
5. `session-disconnect-open-turn`
   - Start eligible speech, disconnect before acoustic close.
   - Expect terminal markers and `turn_closed source=session_end degraded=true` if a turn is open.
6. `session-reused-id-after-terminal`
   - Reuse a previous `stream_session_id` after terminal markers.
   - Expected disposition must be explicit: reject with `error` preferred; if allowed, require a new run namespace and no state leakage.
7. `session-reused-id-concurrent`
   - Open two connections with same `stream_session_id` concurrently.
   - Expect rejection of second connection or deterministic error; never merge streams silently.
8. `model-late-revision-after-close`
   - Force transcript update/token that arrives near or after accepted close.
   - Expect no new transcript-backed turn from punctuation-only or late tokens belonging to closed audio; preserved transcript-before-close ordering invariant.
9. `adapter-disconnect-before-hello`
   - Connect and close before `Hello`.
   - Expect no panic, no orphaned session.
10. `adapter-disconnect-after-hello-before-audio`
    - Hello then close.
    - Expect session terminal markers and zero turns.

Additional required exhaustive lifecycle/error/concurrency cases:

11. `model-error-during-open-turn`
    - Force model worker error while VAD turn is open.
    - Expect declared error event, no panic, no orphan turn, and either scenario-declared degraded close or explicit invalid capture.
12. `smart-turn-error-or-timeout`
    - Force Smart Turn unavailable, error, and timeout paths independently.
    - Expect semantic failure classification and fail-open/suppress behavior defined by profile, not silent fallback.
13. `model-end-session-queue-full`
    - Saturate bounded model end-session queue during teardown.
    - Expect `queue_full`/error diagnostic and capture invalid or explicit lifecycle close; never a normal endpoint pass.
14. `subscriber-lag-or-log-delay`
    - Delay JSONL writing or event subscriber consumption while direct subscription remains active.
    - Expect terminal-marker synchronization to pass only from subscribed events; global-log delay must not produce stale or partial evidence.
15. `concurrent-independent-sessions`
    - Run two different `stream_session_id` values concurrently.
    - Expect isolated frame/sample/turn ownership and no event leakage between capture files.
16. `late-transcript-revision-after-close`
    - Deliver token or transcript revision after accepted close.
    - Expect no resurrection, no reassignment to a new turn, and a quarantined late-revision diagnostic if emitted.

## 10. Exact prompts and cue timelines for human scenarios

Human cue timelines use bands. The recorder displays exact elapsed time, but assertions use observed bands.

### `human-clean-complete`

```yaml
prompt: "The weather looks great today. I think I will go outside."
cues:
  - {band_ms: [0, 3000], label: READY, visual: "Read silently. Breathe normally."}
  - {band_ms: [3000, 9000], label: SPEAK, visual: "Say: The weather looks great today. I think I will go outside."}
  - {band_ms: [9000, 11000], label: STOP, visual: "Stay quiet, then stop."}
```

### `human-trailing-off`

```yaml
prompt: "I was going to say something about the... actually, never mind."
cues:
  - {band_ms: [0, 3000], label: READY, visual: "Prepare to trail off naturally at the ellipsis."}
  - {band_ms: [3000, 11000], label: SPEAK, visual: "Say the line, trailing off before actually never mind."}
  - {band_ms: [11000, 13000], label: STOP, visual: "Stay quiet."}
```

### `human-pause-resume-incomplete`

```yaml
prompt: "I need to check... one more thing before I answer."
cues:
  - {band_ms: [0, 3000], label: READY, visual: "Prepare for a real pause."}
  - {band_ms: [3000, 5200], label: SPEAK, visual: "Say: I need to check"}
  - {band_ms: [5200, 6800], label: PAUSE, visual: "Pause silently. Do not breathe loudly into mic."}
  - {band_ms: [6800, 10500], label: RESUME, visual: "Say: one more thing before I answer."}
  - {band_ms: [10500, 12500], label: STOP, visual: "Stay quiet."}
```

### `human-hold-continuous-filler-7000`

```yaml
prompt: "Sustain a non-word thinking sound, like uhhhh, without saying recognizable words."
cues:
  - {band_ms: [0, 3000], label: READY, visual: "Prepare to sustain a non-word vocalization."}
  - {band_ms: [3000, 11200], label: HOLD, visual: "Hold: uhhhh / thinking hum. No words."}
  - {band_ms: [11200, 13000], label: STOP, visual: "Stop and stay quiet."}
```

### `human-rapid-question`

```yaml
prompt: "What time is it right now?"
cues:
  - {band_ms: [0, 3000], label: READY, visual: "Prepare a quick natural question."}
  - {band_ms: [3000, 5500], label: SPEAK, visual: "Say: What time is it right now?"}
  - {band_ms: [5500, 7500], label: STOP, visual: "Stay quiet."}
```

## 11. Disposition and migration for existing eight fixtures

Existing fixture names are retained as legacy IDs only until re-recorded/reclassified. Their current baselines are not acceptable release gates because they lack config/build/model/WAV hash provenance and several do not match their stated intent.

| Existing fixture | Current observed behavior | Disposition | Migration requirement |
| --- | --- | --- | --- |
| `01-clean-sentence` | One degraded `vad_acoustic_fallback`, smart-turn incomplete; transcript has intended sentence. | Quarantine as `legacy-clean-fallback-regression`; not MVP. | Re-record as `human-clean-complete` until clean `smart_turn` close is achieved, or rewrite intent to fallback. |
| `02-trailing-off` | One degraded `vad_acoustic_fallback`, transcript matches trailing-off. | Candidate fallback fixture. | Import with WAV hash and config if fallback intent is explicit; add assertion for semantic incomplete then fallback. |
| `03-pause-resume` | Two turns; closes on first phrase; does not test resume-without-close. | Quarantine. | Re-record with a deliberate incomplete pre-pause phrase and observed 800-1600 ms pause; require no close before resume. |
| `04-human-hold` | 6.7 s hum; too short for 7000 ms hold; closes via fallback. | Quarantine as fallback/no-token fixture only. | New hold fixture must sustain VAD-active no-token audio for at least 8 s and assert `turn_human_hold`. |
| `05-short-word` | Short word passes min speech and closes via smart_turn with no tokens. | Reclassify as `human-short-complete-no-transcript` diagnostic, not min-speech boundary. | Add synthetic below/at/above min-speech triplet for actual threshold coverage. |
| `06-rapid-question` | Smart-turn close plus extra `session_end` close; transcript mismatch. | Candidate after recapture. | Recapture with terminal-marker capture to eliminate session-end artifact; transcript assertion must be tolerant or omitted. |
| `07-self-interrupt` | Two VAD segments, one smart-turn close on recheck; minor transcript mismatch. | Candidate exhaustive natural scenario. | Import with coarse assertions around interruption/recheck; no exact transcript requirement. |
| `08-slow-thoughtful` | Four VAD segments, earlier incomplete decisions, final smart-turn close. | Best candidate for exhaustive. | Import with provenance and assertions for multiple incomplete decisions before final close. |

Migration steps:

1. Compute SHA-256 and WAV metadata for each legacy WAV if files are present.
2. Store them under `tests/golden/legacy/` with `privacy.retention=delete-after-run` unless explicit consent exists.
3. Generate `legacy-import` provenance stating config/build/model are unknown.
4. Mark all legacy scenarios `nonblocking` until recaptured under a named profile.
5. Do not use legacy raw baselines as expected behavior for release gating.

## 12. MVP versus exhaustive gates

### MVP gate

Required for first implementation:

1. Manifest validator for schema version 1.
2. `golden-mvp` profile and effective config capture.
3. Direct event subscription and terminal marker waiting.
4. Semantic assertion runner with `require`, `forbid`, `order`, numeric tolerances, and balanced-turn invariant.
5. Deterministic synthetic/stub triplets for min-VAD-speech plus the MVP subset of exact threshold contracts: VAD probability, Smart Turn probability, installed fallback, transcript silence, and turn ownership/rearm.
6. Synthetic acoustic fallback for installed 1700 ms profile.
7. Transcript-silence close via deterministic harness.
8. At least two accepted human fixtures: `human-clean-complete` and `human-rapid-question`, both with WAV hash, consent, cue timeline, observed speech timing, and no session-end extra close.
9. Quarantine report for all legacy eight fixtures.
10. CLI commands and exit codes implemented as specified below.

MVP can be merged when all MVP scenarios pass locally with required models available. CI may run synthetic/harness subset if model artifacts are unavailable, but release cannot rely only on model-free CI.

### Exhaustive gate

Required before declaring endpointing policy stable:

1. Full natural catalog: clean, trailing off, pause-resume, human hold, rapid question, self-interrupt, slow thoughtful.
2. VAD onset/hangover/min-speech triplets.
3. Smart Turn recheck schedule, cancellation, exhaustion, and complete-close cases.
4. Acoustic fallback under both `installed-live` 1700 ms and `code-defaults` 3500 ms profiles.
5. Lifecycle/gap/drop/late-revision/disconnect/reused-ID suite.
6. Live diagnostic subscription coverage for `vad_meter` and `turn_hold`.
7. Three accepted takes for human-natural scenarios across at least two recording sessions.
8. Privacy deletion workflow tested.

## 13. CLI command surface

The implementation should provide one top-level command, namespaced under the repo scripts or a Rust binary. Exact binary name may be `speech-core-golden`; aliases may be shell scripts.

### Commands

```bash
speech-core-golden validate-manifest \
  --manifest tests/golden/manifest.yaml
```

Validates schema, profile references, scenario IDs, and assertion DSL.

```bash
speech-core-golden record \
  --manifest tests/golden/manifest.yaml \
  --scenario human-clean-complete \
  --out tests/golden-runs/<run_id> \
  --practice|--take \
  --device <name> \
  --randomize
```

Runs guided recorder UX.

```bash
speech-core-golden synth \
  --manifest tests/golden/manifest.yaml \
  --scenario synthetic-min-vad-speech-below \
  --out tests/golden-runs/<run_id>
```

Builds deterministic synthetic/generated WAVs and cue timelines.

```bash
speech-core-golden capture \
  --manifest tests/golden/manifest.yaml \
  --profile golden-mvp \
  --scenario synthetic-acoustic-fallback-installed-1700 \
  --wav tests/golden/fixtures/<scenario>/audio.wav \
  --url ws://127.0.0.1:8765/ws/audio-ingress \
  --out tests/golden-runs/<run_id>
```

Subscribes directly, replays WAV, waits for terminal markers, writes event stream.

```bash
speech-core-golden assert \
  --scenario-dir tests/golden-runs/<run_id>/scenarios/<scenario>/takes/take-001
```

Runs assertion DSL against captured artifacts.

```bash
speech-core-golden run \
  --manifest tests/golden/manifest.yaml \
  --profile golden-mvp \
  --priority mvp \
  --out tests/golden-runs/<run_id>
```

Runs synth/capture/assert for non-human scenarios and prompts for required human fixture paths if recordings already exist.

```bash
speech-core-golden promote \
  --take tests/golden-runs/<run_id>/scenarios/<scenario>/takes/take-001 \
  --dest tests/golden/fixtures/<scenario>
```

Promotes accepted take to repo fixture after consent/privacy checks.

```bash
speech-core-golden delete \
  --run tests/golden-runs/<run_id> \
  --scenario <scenario> \
  --purge-audio
```

Deletes human audio and derived transcript artifacts per retention policy.

### Exit codes

```text
0  PASS
1  ASSERTION_FAILED
2  MANIFEST_INVALID
3  QUALITY_FAILED
4  CONSENT_REQUIRED
5  DEPENDENCY_MISSING
6  DAEMON_UNREACHABLE
7  MODEL_UNAVAILABLE
8  CAPTURE_TIMEOUT
9  TERMINAL_MARKER_MISSING
10 ARTIFACT_HASH_MISMATCH
11 PRIVACY_POLICY_VIOLATION
12 CONFIG_MISMATCH
13 UNSUPPORTED_PROFILE
14 SCENARIO_NOT_FOUND
15 RECORDER_ABORTED
16 SYNTH_GENERATION_FAILED
17 WAV_FORMAT_INVALID
18 EVENT_SCHEMA_INVALID
19 BASELINE_REQUIRES_REVIEW
20 INTERNAL_ERROR
21 CAPTURE_INCOMPLETE
```

## 14. Privacy, consent, retention, and deletion

### Consent

Human-recorded fixtures require explicit local consent before recording. `consent.json` must state:

- Purpose: local speech-core endpointing diagnostics.
- Stored data: WAV audio, transcripts/events containing text, timing metadata, quality metrics.
- Storage location: local workspace/repo path.
- Upload policy: never uploaded by the golden tool.
- Sharing policy: not committed unless operator explicitly promotes fixture and confirms consent for repo storage.
- Retention policy and deletion command.
- Speaker label or pseudonym.

### Retention classes

1. `delete-after-run`: human audio and transcripts deleted after report generation unless explicitly accepted.
2. `local-dev-ttl-90d`: local-only artifacts kept for up to 90 days, then deletion report required.
3. `repo-fixture-explicit`: committed fixture with explicit consent; WAV and transcript-bearing event baselines may enter git.

### Access, path, and CI artifact controls

Privacy controls are release blockers for any human audio collection:

- Human recording paths must use scenario ids, take ids, and pseudonymous speaker labels only. No personally identifying path components are allowed: personal names, emails, issue titles containing names, and free-form PII are forbidden in filenames and run ids.
- Default storage is local-only with least-privilege filesystem permissions. The golden tool must not upload audio, transcripts, or raw event streams.
- CI may run synthetic/model-free or explicitly approved repo fixtures only. No raw CI artifacts: CI artifacts must not include raw human WAVs, raw transcript-bearing JSONL, unredacted reports, or filtered diagnostics by default.
- Reports intended for PR/CI upload must redact transcript text unless the fixture is `repo-fixture-explicit` and consent permits committed transcript-bearing artifacts.
- Access and retention state must be machine-readable in `privacy.json` and `consent.json`; missing state fails promotion.
- `.gitignore` or equivalent repo policy must exclude local run directories, rejected takes, raw recordings, and diagnostic corpora unless a fixture is explicitly promoted.

### Deletion requirements

`delete` must remove:

- WAVs and hashes.
- Event streams containing transcript text.
- Filtered diagnostics if they include transcript text or speaker timing.
- Playback caches.
- Reports containing committed/tentative text unless redacted.

Deletion leaves a tombstone:

```json
{
  "deleted_at": "...",
  "deleted_by": "local operator",
  "scenario_id": "...",
  "artifacts_removed": ["audio.wav", "event-stream.jsonl"],
  "reason": "retention expired|operator request|consent withdrawn"
}
```

## 15. Optional English phoneme/minimal-pair ASR diagnostic smoke set

This corpus is explicitly separate from the endpointing golden suite, compact, non-release, and nonblocking for release gates.

### Scope

- English only for now. The active Nemotron model is English-only; no multilingual corpus is created until a multilingual model profile exists.
- Purpose: ASR acoustic discrimination diagnostics, not endpoint timing.
- Initial pass is much smaller than a full phonology suite: 8-12 English-only word contrasts, one speaker/accent for local smoke, two repetitions. It makes no accent or population claim.

### Initial non-release smoke set

```text
bit / beat
full / fool
ship / sheep
sip / zip
thin / tin
light / right
wine / vine
map / nap
pin / pen
cot / caught
```

### Recording and scoring

- Record each word in isolation with 500 ms leading/trailing silence.
- Normalize transcript by lowercasing, stripping punctuation, and trimming whitespace.
- Homophone-tolerant scoring where applicable.
- Accent-tolerant scoring for known mergers, e.g. `cot/caught`, `pin/pen`.
- Report confusion matrix; do not fail endpointing gates.
- Do not include multilingual prompts or multilingual scoring now; the active model profile is English-only.

Suggested layout:

```text
diagnostic-asr-corpus/
├── README.md
├── manifest.jsonl
├── recordings/
├── references/
├── scoring/
└── results/
```

## 16. Acceptance and release gates

### PR acceptance for golden tooling

A PR implementing this spec is acceptable when:

1. `validate-manifest` rejects malformed schemas and duplicate scenario IDs.
2. Direct event subscription is used; no global log tail or fixed sleep is required for completion.
3. Terminal markers are enforced and missing markers fail.
4. Effective config is captured and compared to the selected profile.
5. Assertion DSL supports required MVP operators and rejects forbidden exact fields.
6. Synthetic MVP scenarios can run unattended.
7. Guided recorder can create, retry, play back, accept, and delete a human take.
8. Privacy/consent artifacts are written for human recordings.
9. Legacy eight fixtures are quarantined or migrated with explicit disposition.
10. CLI exit codes match this spec.

### Release gate

Endpointing release is allowed when:

1. MVP gate passes on a machine with required ASR/VAD/Smart Turn models.
2. No MVP normal endpointing scenario closes via `session_end`; disconnect/session-end/queue-full behavior is tested only in explicit lifecycle scenarios.
3. No MVP fixture depends on unknown config/build/model provenance.
4. Human fixtures have consent and retention metadata.
5. A failing golden assertion prints a concise diff: missing/extra semantic events, order violation, field predicate violation, and relevant event excerpts.
6. Exhaustive gate is either passing or has documented nonblocking waivers for scenarios outside the release scope.

## 17. Implementation notes and stable assertion seams

Stable seams to prefer:

- `turn_closed` count, `source`, `degraded`, `reason`.
- Presence/absence of `turn_human_hold`, `vad_acoustic_fallback`, `turn_eou_suppressed` reason.
- Partial order: `vad_speech_start -> turn_started`, `vad_speech_end -> smart_turn_candidate -> smart_turn_decision`, semantic decision before close.
- VAD sample fields with tolerances.
- Audio frame sequence/sample progression.
- Terminal markers and turns-started/closed counters.

Unstable fields to avoid:

- Monotonic timestamps and duration fields.
- Floating probabilities/confidences except relationally.
- Generated IDs.
- Whole event stream equality.
- High-frequency diagnostic event counts unless explicitly declared diagnostic.

### Turn-close alignment and rearm contract from hardening commit `83e191c`

The golden suite must encode the hardening behavior introduced by commit `83e191c` (`fix: harden turn close input state`) as observable requirements:

1. **`turn_close_alignment` diagnostic required for close paths that wait on model progress.** The event or assertion artifact must include at least `turn_id`, `source`, `decision_sample`, `target_audio_sample`, `model_audio_committed_sample`, `last_token_end_sample`, `alignment_deadline_ms`, `timed_out`, and `transcript_quiescent`.
2. **Bounded audio alignment, not impossible token equality.** A close may be valid when model audio has advanced to the bounded target or the alignment deadline is reached and recorded. The suite must not require `last_token_end_sample >= decision_sample`, because no-token speech, punctuation-only output, ASR silence, and alignment uncertainty are valid cases. Instead, assert bounded audio progress plus transcript quiescence: no speech-evidence token for the closing turn remains unprocessed inside the declared quiescence window.
3. **Late transcript revisions cannot resurrect closed turns.** Punctuation-only, no-token, stale, or late model updates after `turn_closed` are either ignored for turn ownership or recorded as `late_revision` diagnostics. They must not open a new transcript-backed turn unless a scenario explicitly asserts that behavior.
4. **Forced-close rearm requires low release.** For forced closes while VAD/input remains high, including human-hold and lifecycle/error forced closes, continuous high input after the close cannot create a fresh `vad_speech_start` or resurrect the closed turn. A new onset is valid only after an observed low-release interval satisfying the VAD stop/hangover contract.
5. **Normal accepted boundaries immediately rearm.** For normal accepted boundaries after an actual endpoint (`smart_turn`, VAD close, acoustic fallback, transcript silence, or model EOU as allowed by the scenario), the next independent onset after the boundary may start a new turn without stale state from the prior turn.
6. **Continuous high input cannot resurrect.** A scenario with sustained high VAD probability across a forced close must assert no duplicate `turn_started`, no duplicate `turn_closed`, no stale `turn_id` reuse, and no second close for the same ownership key.

## 18. Traceability checklist for adversarial review requirements

| Review item | Required coverage in this spec |
| --- | --- |
| Fail-closed capture validity for empty/stale/malformed/incomplete/error evidence and nonzero exits | Sections 7 and 13: fail-closed validity table, terminal markers, exit codes |
| Audio format/quality, chain-of-custody hashes, structured human review | Sections 4 and 6: immutable provenance, capture-time/accepted hashes, quality checks, review record |
| Exactly one terminal close per normal started turn; separate session_end/disconnect/queue-full | Sections 7, 9, 12, 16, 17: turn ownership, lifecycle cases, release gates, rearm contract |
| Named immutable build/effective-config/model/runtime/fixture provenance | Sections 3 and 4: effective config artifact and five named provenance identities |
| Synthetic exact threshold triplets versus coarse human bands | Sections 6, 9, 10: human band rules and exact sample-domain triplet table |
| Noncontaminating cue rules | Section 6: cue-channel, headphones, cue-exclusion, review rules |
| Partial-order/turn-ownership/sample-window DSL and unstable field blacklist | Section 8 and Section 17: DSL operators, ownership invariants, blacklist, alignment diagnostics |
| Lifecycle/gap/drop/late-revision/errors/concurrency/log-delay cases | Section 9 lifecycle suite and additional required cases 11-16 |
| Privacy consent/access/retention/deletion/no-PII paths/no raw CI artifacts | Section 14: consent, retention, access/path/CI controls, deletion |
| Explicit fixture disposition | Sections 4 and 11: disposition states and legacy fixture migration table |
| Compact 8-12 non-release ASR smoke set, no multilingual | Section 15: optional English-only non-release smoke set |
| Traceability from critical/required adversarial findings to requirements | This Section 18 |
| Commit `83e191c` turn-close alignment/rearm contract | Section 17: `turn_close_alignment`, bounded audio alignment, transcript quiescence, forced/normal rearm |
| 16 kHz sample conversions and quantization | Section 9: sample-domain table and quantization note |
| Human hold hardening behavior is closing | Sections 9 and 17: `turn_closed source=human_hold` and forced-close low-release rearm |

This spec intentionally separates what must be deterministic from what must be observed and tolerated. The suite should fail loudly on missing provenance and capture races, because those failures otherwise produce false confidence in endpoint behavior.
