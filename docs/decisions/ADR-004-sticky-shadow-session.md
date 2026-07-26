# ADR-004 — Sticky exact-session shadowing

**status:** accepted direction
**ratified by:** operator in charter workshop, 2026-07-26
**implementation:** not implied by this record

## Decision

Talker shadows one exact Pi agent session until explicit disconnect. Connected and disconnected modes project different minimal prompts and active connection-management tools.

After connection, discovery/connect tools are removed; only a minimal disconnect control remains if needed. Ordinary user-message routing is infrastructure, not a Talker tool.

If the target session dies, Talker remains alive but fails loudly. It does not answer independently, create a replacement, or attach by similar label. Reattachment may target the same durable identity; selecting a different target requires explicit operator action.

## Consequences

- Runtime binding state is authoritative.
- `binding_epoch` rejects stale events.
- Discord, speech and TUI expose connected/disconnected/lost state.
