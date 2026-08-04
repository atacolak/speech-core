# Speech-in turn lifecycle

| | |
|---|---|
| **Status** | ACCEPTED — 2026-08-04, by the operator ("i guess it's fine?" → confirmed); converged from recon draft A, verified against code 2026-08-04 |
| **Charter** | implements invariants 1 (authoritative commit), 3 (bounded resources, one terminal outcome), 9 (stop does not wait) |

## 1. What is this, and why does it exist?

When a person talks to Speech Core, *something* must decide where one utterance
ends and the next begins — and what, exactly, was said in it. That something is
the daemon's **TurnManager**. It listens to detectors (VAD, smart-turn, the ASR
model's own end-of-utterance hints), decides a turn is over, publishes the
events around that decision, and commits **one authoritative transcript per
turn**.

Everything downstream — the future controller, intermediate-assistant speech,
history — is expected to hang off the two events this lifecycle guarantees:
`transcript_committed` (what was said) immediately followed by `turn_closed`
(the turn is over). Ordering between them is a charter invariant, so this
lifecycle is one of the load-bearing seams of the whole product.

A websocket connection is one session; turns live inside it and are numbered
locally within it.

## 2. What does it do?

### turn.1 — opening a turn
The daemon SHALL open exactly one open turn at a time per session and announce
it with a `turn_started` event naming its source (`vad` | `transcript` |
`model`). Turn IDs have the form `{stream_session_id}:turn:{index}` with a
session-local, monotonically increasing index.
*WHEN* detector evidence indicates speech *THEN* a `turn_started` event precedes
any close events for that turn. *(OBSERVED: `turn.rs:1289-1313` `next_turn_id`,
`start_turn`)*

### turn.2 — end-of-utterance evidence
While a turn is open, detector conclusions flow through as `turn_eou_candidate`
or `turn_eou_suppressed`. Only TurnManager may promote evidence into closings —
detectors themselves are evidence-only by contract.
*WHEN* a detector reports end-of-utterance evidence *THEN* TurnManager emits a
candidate/suppressed event and no other component emits close events.
*(OBSERVED: `detectors/mod.rs:958-962` trait doc + `AudioDetector`;
`turn.rs:106` `handle_signal`)*

### turn.3 — the close sequence (charter invariant 1)
A turn close SHALL emit, in this order: close-time model alignment → `turn_eou`
→ `transcript_committed` → `turn_closed`. Late ASR revisions after close appear
only in a *diagnostic* `transcript_finalized` event and never re-commit the
turn.
*WHEN* any close source fires *THEN* `transcript_committed` always precedes
`turn_closed`, carrying `is_degraded` and `close_source`, and no later event
mutates that committed text. *(OBSERVED: `turn.rs:1371-1466`;
`model.rs:1045-1058` diagnostic-only path)*

### turn.4 — what gets committed
The committed text SHALL be the per-turn slice of the shared token snapshots:
tokens from the turn-start baseline whose audio falls inside the turn's sample
boundaries, extended to the latest committed token end so final words aren't
dropped when the detector's end sample ran early. Trailing punctuation attached
to the last speech token is included.
*WHEN* `transcript_committed` is constructed *THEN* its text comes from
`per_turn_committed_snapshot` with the turn-start token baseline and extended
sample boundary. *(OBSERVED: `turn.rs:1390-1413` incl. boundary-extension
comment; `model.rs:353-360`)*

### turn.5 — empty commits are real
A turn MAY commit with empty text — including a pure *human-hold* close
(speech-like audio, zero committed tokens for the hold threshold). This is
deliberate present behavior, not a bug.
*WHEN* a close fires with no tokens in the turn window *THEN* the committed
snapshot yields an empty string and the events still flow in the guaranteed
order. *(OBSERVED: `turn.rs:1411` `unwrap_or_default`; human-hold path
`turn.rs:455-529`)*

### turn.6 — close sources and the degraded flag
Closers: `vad`, `smart_turn`, `model_eou` (disabled by default), `human_hold`,
`transcript_silence`, `vad_acoustic_fallback`, `session_end`, `audio_gap`.
A VAD-only close marks the turn `degraded=true`; a smart-turn close is
`degraded=false`. *(INFERRED: `degraded` is the code's operationalization of
charter invariant 5 — qualified estimates, not unqualified facts; the charter
does not name the flag. OBSERVED: close-source arms in `handle_signal`)*

### turn.7 — semantic gating fails open
Smart-turn's "not complete" verdict suppresses a VAD close only when semantic
gating is enabled; smart-turn timeouts or unavailability fall back to VAD
rather than blocking closure.
*WHEN* smart-turn is slow or down *THEN* the turn still closes via VAD with
reason `smart_turn_timeout_vad_fallback` /
`smart_turn_unavailable_vad_fallback`. *(OBSERVED: `turn.rs` VadSegmentEnd arm)*

### turn.8 — close-time latency is bounded
Before committing, the daemon aligns the ASR model, with two live bounds:
default wait `turn_model_alignment_timeout_ms = 3000` (CLI/env tunable) and a
hard finalize floor `CLOSE_FINALIZE_TIMEOUT_MS = 800` used as
`max(timeout, 800)`; preferred path drains real audio plus 320 ms synthetic
silence through `sc_transcribe_finalize`, with a progress-wait + token-
quiescence (60 ms, trailing extension ≤320 ms) fallback.
*WHEN* a close begins *THEN* total close work is bounded by the configured
budget (never below 800 ms) and one terminal outcome always results.
*(OBSERVED: `main.rs:318-323`; `turn.rs:1826-1829`, `1933-1934`, `1964-1966`;
`model.rs:1166` `finalize_turn_stream`; fallback `turn.rs:1879`, `2014`)*

### turn.9 — ghost-turn guard and the frozen ring
After a close, tokens ending at/before the closed decision sample are dropped;
late orphan tokens without fresh VAD speech are suppressed
(`transcript_token_late_orphan`); the snapshot ring is frozen through the
closing audio (`freeze_tokens_through`) until the next turn opens; punctuation-
only tokens never open turns.
*WHEN* late ASR tokens arrive after `turn_closed` *THEN* they cannot leak into
the next turn's committed text. *(OBSERVED: `turn.rs:574-597`;
`model.rs:279` freeze, `319` `record_token_snapshot`, `1724`
`is_speech_evidence_text`)*

### turn.10 — many turns per session; reconnect is not yet a lifecycle citizen
Barge-in and successive closes are normal within one session. The wire defines
a `Drain` control message, but the daemon's connection handler does not act on
it (unhandled control messages are debug-logged), so reconnect/Drain today
does not interact with turn-id allocation.
*WHEN* a client sends `Drain` *THEN* no turn-lifecycle behavior results.
*(OBSERVED: `protocol/src/lib.rs:294-298`; `main.rs:991-993` fall-through arm;
multi-turn exercised in `turn.rs mod tests`)*

## 3. What is load-bearing?

- **Evidence-only detectors.** The `AudioDetector` trait contract (`detectors/
  mod.rs:958-962`) forbids detectors from closing turns. All promotion power
  lives in TurnManager. Any change that lets a second component emit
  `turn_eou`/`turn_closed` breaks this chapter and charter invariant 1.
- **ModelProgressMap** (`model.rs`) is the single synchronization point between
  the model worker thread and TurnManager (including `current_turn_id`
  tagging). The commit path's correctness depends on its token ring being the
  only source of per-turn text.
- **The two-event consumer contract.** `transcript_committed` then
  `turn_closed`, with `is_degraded` + `close_source`, is the seam controllers
  will build on. Reordering or splitting it is a charter-level event.
- **Session-per-connection.** Turn identity is scoped to one websocket
  connection (`main.rs:557` `start_session`); anything promising identity
  *across* connections is new design, not refinement.

## 4. What is undecided?

- **turn-id stability across reconnect** — UNRESOLVED (revisit when a
  controller, reconnect harness, or cross-session consumer needs a stable
  turn-identity contract).
- **Drain semantics** — UNRESOLVED (revisit when Drain is implemented or a
  reconnect/barge-in consumer documents turn_id reuse rules).
- **Are empty-text commits acceptable product behavior?** — UNRESOLVED (revisit
  when a controller or acceptance criterion requires or rejects them).
- **One authoritative alignment budget** — two numbers live (3000 default,
  800 floor) — UNRESOLVED (revisit when an operator latency budget or SLO must
  pick one).
- **human-hold default conflict** — running daemon wires CLI default 7500 ms;
  `TurnManagerConfig::default()` says 12000 ms; a code comment says "typically
  7500ms". Both values exist today — UNRESOLVED (revisit when tests build
  `TurnManagerConfig` via `Default`, or a config/UX change must name one
  threshold).
- **Who consumes `transcript_committed`** — currently no controller; event log
  and broadcast only — UNRESOLVED (revisit when a controller or external
  consumer is introduced).

## Evidence & reconciliation notes

*Verified 2026-08-04 against `crates/` @ `spec/recon-001` (14d6fe4).*
All structural claims of recon draft A confirmed. Divergences from the draft:

- **Test count drifted**: draft said "69 unit tests in daemon"; today 59
  `#[test]` functions exist in the daemon crate (plus 7 in the protocol
  crate). Counts churn; this figure is a 2026-08-04 snapshot.
- Line references re-cited to current positions (draft's cites were ±2 lines).

Suggested probes (from the draft, still valid): run
`tests/test-barge-in-dual-asr.sh` for event-order confirmation; file-adapter
replay for a known-close wav; noise-only session to exercise the empty-commit
path (turn.5).
