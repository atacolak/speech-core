# Dual-cfg experimental path

not on the voicecat path.

Breeze's three-branch construction:

    logits = uncond
           + cfg_ref * (ref - uncond)
           + cfg_instruction * (instruction - uncond)

Controls (do **not** relabel as speaker identity / clone prosody):

- `cfg_scale_ref` — reference conditioning
- `cfg_scale_ins` — instruction conditioning

The reference branch carries speaker identity, acoustics, **and** the
performance prior from the reference. Dual-cfg does not disentangle those.
It is still the right experiment for the pareto surface between reference
adherence and requested performance.

## Isolation from E2

`FastBreezeStreamingRuntime.reject_dual_cfg`: graphs support no_cfg /
single_cfg only. This pass does **not** extend E2 graphs.

Implementation: `OfficialBackend._iter_dual_cfg` asserts the fast path
rejects dual inputs, then calls `model.generate(..., output_audio=True)`
on the already-loaded weights. Ordinary E2 generation still uses
`runtime.iter_audio_chunks`.

E2 graph warmup leaves `lm_head` / `codebooks_head` in fp32 while the
backbone stays bf16. Eager generate therefore wraps Linears with a
dtype guard (`_eager_generate_dtype_guard`) so dual-cfg scales such as
1/2 or 1/4 do not raise `expected scalar type BFloat16 but found Float`.

The same warmup `torch.compile`s depth-decoder layers and codec
SnakeBeta with `fullgraph=True`. Dual-cfg must not execute those
compiled graphs: growing sequence length recompiles until Dynamo
raises `recompile_limit reached with fullgraph=True`. `_iter_dual_cfg`
sets `disable_compile=True` and `torch.compiler.set_stance("force_eager")`
for that generate only. Do not raise `recompile_limit` as a fix.

Single-cfg remains the production default until dual-cfg earns otherwise.

## Latency

Not GPU-benchmarked in this pass. The experimental UI records wall /
first-audio on each dual take so the cost can be written here when a
run exists. Do not invent a number.

Next isolated task, if dual-cfg proves useful: decide whether E2 should
grow a three-branch graph, or stay an offline/eager path.
