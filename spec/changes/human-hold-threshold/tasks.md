# tasks.md — human-hold-threshold

Living checklist. Downstream implementation; the steward drafts and verifies,
workers execute. Spawn as bead(s) only on the operator's word.

- [ ] Align `TurnManagerConfig::default()` `human_hold_silence_ms` 12000 → 7500
      (`crates/speech-core-daemon/src/detectors/turn.rs:51`) — or remove the
      duplicate literal so one home defines the value
- [ ] Grep the workspace for any other `12000`/human-hold literals and fix
- [ ] Steward: re-verify, merge the delta into chapter A (`turn.6` + open
      questions), move this change to `spec/archive/2026-08-human-hold-threshold/`
- [ ] Steward: update `spec/README.md` map if chapter contents changed shape
