"""Torch-only operations for the AuK sampler port.

The 4070 deployment runs the MIT-licensed ComfyUI-AuK node pack on top of
ComfyUI ops and Comfy Kitchen kernels. speech-core must not import ``comfy.*``
(see the AuK lab spec), so this module holds the little that port actually
needs:

* quantized-weight construction straight from the on-disk layout Comfy Kitchen
  wrote (``int8_tensorwise`` diffusion, ``asym_w4a8_int8`` encoder), so plain
  ``F.linear`` dispatches to the fused kernels;
* meta-device construction plus one validated injection pass, so a checkpoint
  key no module claims fails loudly instead of leaving a meta tensor in a graph;
* the fused RoPE helper ComfyUI itself calls at inference time.

Nothing here knows about the lab or the worker; it runs only inside the AuK venv.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

import torch
from torch import nn
import torch.nn.functional as F

import comfy_kitchen as ck
from comfy_kitchen.tensor import QuantizedTensor, get_layout_class


# On-disk quant formats this port can run, mapped to (Comfy Kitchen layout, storage dtype).
# These are the two entries of ComfyUI's QUANT_ALGOS the AuK pins actually carry; any
# other format fails closed at load time instead of falling back to something slower or lossy.
QUANT_ALGOS: dict[str, tuple[str, torch.dtype]] = {
    "int8_tensorwise": ("TensorWiseINT8Layout", torch.int8),
    "asym_w4a8_int8": ("AsymW4A8Int8Layout", torch.int8),
}

# The converted Qwen encoder carries a language head the AuK sampler never decodes.
OPTIONAL_ENCODER_KEYS = ("lm_head.weight",)


class UnknownQuantFormat(ValueError):
    def __init__(self, quant_format: str, prefix: str) -> None:
        super().__init__(
            f"unsupported quantized weight format {quant_format!r} at {prefix}; "
            f"this port runs {sorted(QUANT_ALGOS)}"
        )
        self.quant_format = quant_format


class IncompleteWeights(RuntimeError):
    """A quantized weight is missing the companion tensors its layout needs."""


class InjectedWeights(RuntimeError):
    """The checkpoint and the module graph do not line up."""


def compute_dtype(precision: str, device: torch.device) -> torch.dtype:
    """Activation dtype for a pin precision. Both pins compute in bf16 on the 4070."""
    del precision
    if device.type == "cuda" and torch.cuda.is_bf16_supported():
        return torch.bfloat16
    return torch.float32


def quant_config(state: Mapping[str, torch.Tensor], prefix: str) -> dict[str, Any] | None:
    """The ``comfy_quant`` JSON blob for one module, when the checkpoint quantized it."""
    blob = state.get(f"{prefix}.comfy_quant")
    if blob is None:
        return None
    return json.loads(bytes(blob.reshape(-1).cpu().numpy().tobytes()))


def build_quantized(
    state: dict[str, torch.Tensor],
    prefix: str,
    conf: Mapping[str, Any],
    orig_shape: tuple[int, ...],
    compute: torch.dtype,
    device: torch.device,
) -> QuantizedTensor:
    """Consume one module's quantized keys and wrap them in their layout tensor.

    ``orig_shape`` is the dense weight shape ``(out_features, in_features)``: the int8
    format stores it verbatim, W4A8 stores half as many columns because two nibbles
    share one int8 lane.
    """
    quant_format = conf.get("format")
    algo = QUANT_ALGOS.get(quant_format)
    if algo is None:
        raise UnknownQuantFormat(quant_format, prefix)
    layout_type, storage = algo
    params_conf = conf.get("params")
    if not isinstance(params_conf, dict):
        params_conf = {}

    def take(name: str, view: torch.dtype | None = None) -> torch.Tensor | None:
        value = state.pop(f"{prefix}.{name}", None)
        if value is None:
            return None
        value = value.to(device=device)
        if view is not None and value.dtype == torch.uint8:
            # Legacy checkpoints stored fp8 scales as raw bytes.
            value = value.view(view)
        return value

    weight = state.pop(f"{prefix}.weight")
    if quant_format == "asym_w4a8_int8":
        scale = take("weight_s_rel", torch.float8_e4m3fn)
        if scale is None:
            raise IncompleteWeights(f"missing weight_s_rel for {prefix}")
        params: dict[str, Any] = {
            "scale": scale,
            "s_channel": take("weight_s_channel"),
            "codebook": take("weight_codebook"),
            "group_size": int(conf.get("group_size", params_conf.get("group_size", 16))),
            "convrot_groupsize": int(conf.get("convrot_groupsize", params_conf.get("convrot_groupsize", 256))),
        }
    else:
        scale = take("weight_scale")
        if scale is None:
            raise IncompleteWeights(f"missing weight_scale for {prefix}")
        params = {"scale": scale}
        if conf.get("convrot", params_conf.get("convrot", False)):
            params["convrot"] = True
            params["convrot_groupsize"] = int(
                conf.get("convrot_groupsize", params_conf.get("convrot_groupsize", 256))
            )

    layout = get_layout_class(layout_type)
    params["orig_dtype"] = compute
    params["orig_shape"] = tuple(orig_shape)
    return QuantizedTensor(weight.to(device=device, dtype=storage), layout_type, layout.Params(**params))


def load_weights(
    module: nn.Module,
    state: dict[str, torch.Tensor],
    *,
    compute: torch.dtype,
    device: torch.device,
    allow_missing: tuple[str, ...] = (),
    keep_dtype: tuple[str, ...] = (),
) -> list[str]:
    """Inject a checkpoint into a meta-built module and prove nothing was left dangling.

    Checkpoints store each module's weights in whatever the converter found cheapest, so
    floating parameters are materialized in the activation dtype here — the same cast
    ComfyUI's ``manual_cast`` performs at every forward. Buffers keep their stored dtype
    (the sampler's RoPE tables are fp32 on disk for a reason), and ``keep_dtype`` exempts
    parameters the backbone reads in fp32 on purpose.

    Returns the missing keys that ``allow_missing`` covered. Raises if the graph keeps a
    meta tensor or if a checkpoint key went unclaimed — both mean the port and the
    checkpoint disagree, and both would otherwise show up as garbage audio.
    """
    quantized: set[str] = set()
    for name, sub in module.named_modules():
        if not name:
            continue
        conf = quant_config(state, name)
        if conf is None:
            continue
        state.pop(f"{name}.comfy_quant")
        quantized.add(f"{name}.weight")
        state[f"{name}.weight"] = nn.Parameter(
            build_quantized(state, name, conf, tuple(sub.weight.shape), compute, device),
            requires_grad=False,
        )

    for name, _ in module.named_parameters():
        if name in quantized or name in keep_dtype:
            continue
        value = state.get(name)
        if value is not None and value.is_floating_point() and value.dtype != compute:
            state[name] = value.to(compute)

    result = module.load_state_dict(state, strict=False, assign=True)
    # Meta-built parameters carry requires_grad=True into the assignment, so without this
    # every forward records an autograd graph that pins each block's intermediates
    # (measured ~36 MiB per block, >5 GiB per render). The port only ever infers.
    module.requires_grad_(False)
    if result.unexpected_keys:
        raise InjectedWeights(f"checkpoint keys no module claims: {sorted(result.unexpected_keys)[:4]}")
    covered = [key for key in result.missing_keys if key in allow_missing]
    missing = [key for key in result.missing_keys if key not in allow_missing]
    if missing:
        raise InjectedWeights(f"checkpoint missing module weights: {sorted(missing)[:4]}")
    stranded = [name for name, tensor in _tensors(module) if tensor.is_meta]
    if stranded:
        raise InjectedWeights(f"module still on meta after load: {sorted(stranded)[:4]}")
    return covered


def _tensors(module: nn.Module) -> list[tuple[str, torch.Tensor]]:
    found = [(name, param) for name, param in module.named_parameters()]
    found.extend((name, buffer) for name, buffer in module.named_buffers())
    return found


def cast_to(tensor: torch.Tensor, other: torch.Tensor) -> torch.Tensor:
    """ComfyUI's ``cast_to_input``: same dtype and device as the activation."""
    return tensor.to(dtype=other.dtype, device=other.device)


def apply_rope(q: torch.Tensor, k: torch.Tensor, freqs: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
    """The fused RoPE the deployment calls (``comfy.quant_ops.ck.apply_rope``)."""
    return ck.apply_rope(q, k, freqs)


def attend(q: torch.Tensor, k: torch.Tensor, v: torch.Tensor, mask: torch.Tensor | None = None) -> torch.Tensor:
    """(B, H, S, D) in, (B, S, H*D) out — the reference's skip_reshape contract.

    Flash and Sage are out of scope for the 4070 port, so this is always SDPA.
    """
    return F.scaled_dot_product_attention(q, k, v, attn_mask=mask).transpose(1, 2).flatten(2)
