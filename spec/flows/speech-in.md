# Flow: a spoken sentence through Speech Core

A reading lens, not a contract. Every claim below points at the chapter that
owns it; when a cited chapter changes, this flow gets re-checked.

1. **An adapter takes the mic.** A separate process (mic or file adapter)
   connects over one websocket and says `Hello`, declaring who it is and how
   honest its clock is → *chapter: audio-ingress-transport, ingress.1*.
2. **Frames flow, the daemon listens silently.** Audio ships as SCF1 binary
   envelopes; healthy streams draw no per-frame replies → *ingress.2, ingress.4*.
3. **Every frame is frisked.** Metadata mismatches against the hello drop the
   frame with an error → *ingress.3*.
4. **Detectors watch; only TurnManager may decide.** VAD/smart-turn/model hints
   emit *evidence*; promotion to a close belongs to one component → *chapter:
   speech-in-turn-lifecycle, turn.1–2*.
5. **The turn closes in a fixed order.** Bounded alignment wait → `turn_eou` →
   `transcript_committed` → `turn_closed` — the charter's invariant 1
   → *turn.3, turn.8*. If speech-like noise persists 7500 ms with zero
   committed words, the human-hold closer fires → *turn.6*.
6. **One authoritative transcript per turn.** The committed snapshot is what
   history will remember; late tokens are fenced out → *turn.4, turn.9*.
7. **Everything worth knowing becomes an event.** One JSON-line funnel:
   live subscribers first, durable log second → *chapter:
   event-observability-surface, events.1*. Three noisy names are live-only;
   everything else rotates through 256 MiB × 8 → *events.3–4*.
8. **A human watches if they like.** `watch` renders the swimlane — Transcript,
   Tui, Debug, Jsonl — or replays yesterday's log → *events.7*.

## Reading beyond this lens

- Where speech goes next (synthesis out): chapter speech-out *(parked on
  ADR-001 — see spec/README map)*.
- What may never change while any of this evolves: `../CHARTER.md`.
