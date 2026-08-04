# tasks.md — human-hold-threshold

Living checklist. Downstream implementation; the steward drafts and verifies,
workers execute. Spawn as bead(s) only on the operator's word.

- [x] Align `TurnManagerConfig::default()` `human_hold_silence_ms` 12000 → 7500
      (`crates/speech-core-daemon/src/detectors/turn.rs:52`) — shipped in
      `a8886ef` (bead `sc-rfi`), independent pass `sc-9li`
- [x] Grep the workspace for any other `12000`/human-hold literals and fix —
      remaining literals re-classified unrelated (fixtures/sample counts) by
      worker, verifier, and steward independently
- [x] Steward: re-verify, merge the delta into chapter A (`turn.6` + open
      questions), move this change to `spec/archive/` — done 2026-08-04
      (Road-1; regression test re-run, `1 passed`)
- [x] Steward: update `spec/README.md` map if chapter contents changed shape
