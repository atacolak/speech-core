# ADR-003 — Explicit operator-facing turns from asynchronous profile work

**status:** proposed — pending exact Founder ratification
**ratified by:** none
**implementation:** not implied by this record

## Decision

When Talker starts work on a target through tools (ask, follow-up/steer, or equivalent), that work may return **asynchronous explicit operator-facing profile turns** before and at final completion. Speakable turns include:

- intermediate assistant messages and brief pings the target (or Talker) deliberately addresses to the operator;
- clarification requests;
- failure and cancellation notices;
- final responses.

The system does **not** invent a special progress language, progress-emission tool, or automatic narration of tool lifecycle. Hidden reasoning, chain-of-thought, tool names, calls, arguments, raw results, and generic lifecycle events are never spoken merely because they occur. Infrastructure never fabricates openings or status text.

Talker may, for operator-facing delivery:

- attribute updates to the source profile/work when useful;
- conversationally adapt wording for speech without changing source meaning or inventing facts;
- coalesce or supersede stale updates still waiting to speak;
- queue outputs for the next safe speech opportunity.

**User speech has priority** over assistant playback. **Stopping playback is distinct from cancelling agent work**: physical or operator stop of audio does not automatically cancel in-flight target work; cancellation requires an explicit cancel path (see ADR-002 tools).

A message may begin speaking before the system knows whether it is intermediate or final. Classification is causal when message/run/work lifecycle provides that knowledge. Already submitted or estimated-audible content remains truthful playback evidence and is not rewritten as if it never occurred (audibility detail is ADR-005 territory; this record only preserves the separation).

Speakable units that cross the gateway carry correlation to durable work/subscription identity and source profile identity so scheduling and operators can attribute them. Exact schema is left to a later authorized contract.

## Consequences

- Speech can start before async work finishes without treating every update as a final result.
- Source-aware scheduling can bound frequency, queue growth, repetition, and stale speech without collapsing reception, queueing, speaking, interruption, and hearing into one flag.
- Intermediate and final operator-facing turns may remain in the emitting profile's normal history when that profile emitted them; Talker's spoken-thread adaptation does not silently rewrite target history.
- Playback and audibility evidence stay separate from reasoner completion state.
- This record grants no runtime implementation authority until exact Founder ratification of this diff.

## Plain-language consequence

If an agent is still working, the operator can hear real things that agent (or Talker) chose to say — not a fake status feed built from tool guts. Talker can smooth and order those updates for the ear, but the user can always interrupt speech, and stopping the voice is not the same as killing the job.

## Reversal cost

Low before scheduling and correlation contracts land. Moderate after clients expect async intermediate speech. UX reversal is material if operators come to rely on pings; frequency and cancel semantics should be fixture-tested before rollout once implementation is authorized.

## Current implementation status

- OBSERVED: prior ADR text allowed speaking ordinary intermediate assistant text without a progress tool; that explicit-talk-only rule is preserved and extended to async multi-profile work.
- OBSERVED: charter invariant 7 forbids automatic narration of hidden reasoning and tool activity; this record stays inside that invariant.
- INFERRED: coalescing/supersession policies will need product fixtures once a scheduler exists; no policy defaults are ratified here.
- UNKNOWN: whether every intermediate chunk enters durable Talker history, only target history, or both under final protocol.

## Ownership

| Authority | Owns |
|-----------|------|
| Emitting profile | whether an explicit operator-facing turn exists and its source text |
| Talker | attribution, speech adaptation, coalesce/supersede/queue choices for the spoken thread |
| Speech scheduling | pending presentation order and playback stop vs work cancel separation |
| Hybrid gateway | work/subscription correlation for async turns (ADR-002) |
| Playback / audibility | qualified heard estimates (ADR-005; out of scope here) |
