# ADR-003 — Speak explicit intermediate assistant messages

**status:** accepted direction
**ratified by:** operator in charter workshop, 2026-07-26
**implementation:** not implied by this record

## Decision

The system speaks ordinary explicit user-directed assistant text emitted while an agent run continues, including text before or between tool calls. It also streams the terminal final assistant message.

There is no progress-emission tool and no automatic conversion of tool lifecycle events into speech. Hidden reasoning, tool arguments and tool results are never spoken merely because they occur.

A message may begin speaking before the system knows whether it is intermediate or final. Classification occurs causally when the message/run lifecycle provides that knowledge.

## Consequences

- Prompts may encourage a brief natural opening before long/tool-heavy work.
- Infrastructure never fabricates the opening.
- Intermediate messages remain normal Pi conversation history.
- Speech playback/audibility is tracked separately in the speech ledger.
