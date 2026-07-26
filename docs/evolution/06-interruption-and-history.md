# 06 — Interruption and history

**status:** accepted target specification; implementation partial
**authority:** subordinate to `CHARTER.md`; not proof of implementation

## Authority hierarchy

- VAD activity may pre-arm resources.
- VAD alone does not visibly duck or stop playback.
- The first meaningful/alphanumeric Nemotron token confirms a real spoken interruption.
- The player owns physical stop/flush and cursor evidence.
- BFA asynchronously classifies the likely heard text.
- Session control owns conversational-history decisions.

## Coordinated interruption transaction

One `interruption_id` correlates all legs:

```text
1. record Nemotron-confirmed trigger
2. request player duck/stop plan
3. cancel queued and in-flight egress for affected utterances
4. send the committed user turn to Pi as steer if the exact run remains active
5. otherwise promote it to a new run
6. capture player stop cursor and uncertainty
7. classify a provisional audible prefix
8. mark assistant history provisional
9. freeze exact retained PCM evidence
10. request BFA refinement asynchronously
11. commit at most one safe monotonic refinement
```

If audio is not playing, skip physical stop legs and route the committed turn directly according to Pi run state.

## Natural egress

After barge confirmation, choose the best available stop within a measured/configurable maximum lookahead. The original 120 ms value is an experimental SLO, not an assumed truth.

Initial PCM features may include:

- RMS energy and slope;
- voicing/pitch presence;
- zero-crossing friendliness;
- transient/plosive penalty;
- strong-vowel penalty;
- waiting-time penalty;
- trustworthy word/phoneme hints when present.

Selection hierarchy:

1. quiet word boundary with trustworthy hint;
2. quiet phoneme boundary with trustworthy hint;
3. best local acoustic minimum;
4. deadline fallback with short fade.

The planner records candidates, scores, chosen boundary and unavailable evidence.

## Truth states

Do not collapse these:

```text
text_generated                 exact
text_submitted_to_tts          exact
audio_synthesized              exact
audio_queued                   exact
audio_submitted_to_device      exact
audible_prefix_estimate        estimate + calibration uncertainty
heard_alignment_result         model classification + confidence/method
conversation_history_committed session-control policy decision
```

## Provisional classification

Immediately after player stop, session control derives a provisional text span from immutable speakable-segment mappings and the player cursor estimate. It records method, uncertainty and raw inputs.

The system must not label elapsed-wall-clock/WPS approximation as sample-accurate or ground truth.

## BFA refinement

BFA consumes:

- exact utterance/segment/audio identities;
- intended immutable text and pronunciation information;
- retained PCM evidence;
- player cursor and uncertainty;
- model/version/calibration provenance.

It returns:

- word and phoneme spans;
- last complete word and possible partial word;
- confidence and method;
- alignment sample domain;
- terminal status and latency.

BFA never blocks physical stopping. Timeout preserves the provisional state.

## History safety

- Only session control mutates history.
- Intermediate and final assistant messages remain authored session history.
- The speech ledger records audible estimates separately.
- A late BFA result cannot rewrite a newer binding epoch, assistant response or user turn.
- Refinement follows a documented monotonic rule and happens at most once per interruption unless explicitly reprocessed offline.
- Partial transaction failure is visible; a stop-only result cannot be reported as complete conversational interruption success.

## Steering race

When `transcript_committed` arrives:

- if the exact Pi run is active, send native `steer`;
- if it completed first, create a new run;
- if the shadow target is lost, fail loudly and do not redirect;
- idempotency binds delivery to `turn_id`, `agent_run_id` and `binding_epoch`.

Pi owns steer delivery semantics. Speech-core records the decision and native acceptance rather than inventing an internal delivery boundary.
