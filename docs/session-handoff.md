# session handoff

use this file when starting a fresh assistant session.

## current repo

```text
/home/sf/workspace/speech-core
```

this repo is git-tracked. current runtime source of truth is this repo, especially:

```text
README.md
docs/current-state.md
docs/turn-detection.md
docs/seams.md
```

older planning docs live at:

```text
/home/sf/workspace/docs/speech-core
```

they contain useful history, but some sections are stale because implementation moved to rust and parakeet realtime eou was retired.

## current runtime summary

```text
sfnix mic adapter -> sfub daemon -> nemotron transcript + silero vad turn close -> watcher prints transcript + <EOU>
```

parakeet realtime eou is disabled by default.

parakeet unified is not integrated. no local parakeet unified gguf artifact was found during the last check.

## recent commits to know

```text
a933e9c fix retired eou deployment scripts
c4aa771 retire parakeet eou default path
511b386 document eou reset policy and replay probing
3f72fbb anchor eou stream resets and test reset actions
62bffd2 instrument eou latency and boolean detector flags
0391ab7 gate parakeet eou resets through turn manager
19979bf prefer fast vad turn closure
```

## if the user asks “why is `<EOU>` firing while i am still speaking?”

answer:

- `<EOU>` is printed on `turn_closed`.
- default `turn_closed` comes from silero vad `vad_speech_end`.
- vad ends speech after 12 non-speech frames.
- 12 frames * 30ms = 360ms.
- if the spoken segment lasted at least 700ms, the turn closes.
- therefore a natural mid-sentence pause of ~360ms can produce `<EOU>`.

this is tunable:

```bash
SPEECH_CORE_VAD_HANGOVER_FRAMES=12 ./scripts/install-sfub-daemon.sh
```

but do not pretend the current vad-only policy understands sentence completeness. it does not.

## next useful documentation pass

recommended next session goal:

1. review the low-latency speech document the user found.
2. compare it against the current seams in `docs/seams.md`.
3. decide what belongs in speech-to-speech spec:
   - wake/listen behavior
   - barge-in
   - endpointing policy
   - streaming llm handoff
   - tts interruption
   - latency budget by stage
   - transcript correction policy
4. benchmark any proposed model before putting it in the live path.

## do not do this blindly

- do not re-enable parakeet realtime eou by default.
- do not integrate parakeet unified before obtaining the artifact and benchmarking latency.
- do not assume cross-host monotonic clocks are comparable.
- do not treat transcript text as sample-accurate unless token timestamps or alignment prove it.
