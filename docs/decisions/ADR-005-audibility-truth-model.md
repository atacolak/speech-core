# ADR-005 — Qualified audibility and heard truth

**status:** accepted direction
**ratified by:** operator in charter workshop, 2026-07-26
**implementation:** not implied by this record

## Decision

The system does not expose a bare boolean `heard` or an unqualified heard-sample cursor.

It distinguishes exact generated/queued/submitted facts from calibrated audible estimates, BFA classifications and session-control history decisions. Estimates carry method, calibration and uncertainty; model classifications carry method/version/confidence.

## Consequences

- Player events report submitted cursor and device-consumed/audible estimates separately.
- BFA is asynchronous refinement, not ground truth and not on the stop path.
- Conversational history records the policy decision and its evidence provenance.
