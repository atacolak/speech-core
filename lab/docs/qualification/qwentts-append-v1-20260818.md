# llm→qwen append v1 — 2026-08-18

source hop for `sc-qwen-text-in-jxf`. **not** a live pin swap.

## contract

```text
speak  {type:speak, utterance_id, text, hold_open?}
append {type:append, utterance_id, text}
finish {type:finish, utterance_id}            # alias: finish_utterance
cancel {type:cancel, utterance_id}
```

honest v1: queue the next unit. do not restart already-emitted pcm.
`hold_open:true` (or first append) keeps the utterance open after unit-complete.
client-local `finish_utterance()` is not enough — send `{type:finish}`.
not a qwen token socket. not CosyVoice bistream. not `Hello,` flush.

## what landed (aa86b67 worktree source)

tree: `/home/sf/workspace/orchestrator-worktrees/demiurge.cosy3-accelerators`

- `ClientMessage::Append` / `Finish` on `:8788`
- `hold_open` on speak (default false for speak-only compat)
- worker accepts one extra same-`utterance_id` while the first unit is live
- completed units stay reusable (`forget_live` does not mark finished)
- fail/cancel/crash still `abandon_live`s
- daemon swallows unit `completed` while held; remaps `sample_offset`/`seq`
- extra rejected append does not tear down the open utterance
- idle `append` (no open speak) is an error

## proof

```text
cargo test -p speech-out --test progressive_worker --test progressive_daemon_ws --offline -- --test-threads=1
38 passed
```

new coverage:

- `same_id_append_emits_two_unit_terminals`
- `cancel_drops_queued_same_id_append`
- `append_same_utterance_contiguous_offsets_one_terminal`
- `hold_open_allows_append_after_first_unit_completes`
- `idle_append_rejected`

## leftover / honesty

- live mouth is still qwentts.cpp / `qwentts-tts-server` @ :18091. this binary is **not** the running unit.
- handshake still says `backend=progressive-cosyvoice`.
- join across two units (breath, pitch) unmeasured.
- qwentts.cpp still has no generator-in. each unit is a new `qt_synthesize`.
- voicecat SENTENCE-wait is niru / `sdc-4a9`. leftover client name still CosyVoiceTTSService.
- first-clause opener flush stays dead.
- niru must send `{type:finish}` on llm end. client-local seal is not the hop.

## numbers to beat (do not mix domains)

from `docs/qualification/qwentts-stream-baseline-20260818.md`:

- sit `llm_text` → `tts_start` **374 ms** (SENTENCE)
- isolated http first-use p50 **~70 ms** clause or paragraph
- live `:8788` D first-pcm warm **~33–35 ms** (first codec frame)
