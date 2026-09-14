"""AuK Flux2Edit inference backbone.

Ported from the MIT-licensed ComfyUI-AuK node pack (``nodes/auk_model.py``); the
state keys follow Tencent's release. The port drops ``comfy.ops`` entirely —
checkpoint weights are injected by :mod:`tts.auk.ops` — and keeps SDPA as the
only attention path, since flash-attn and sage-attention are not installed in
this deployment.
"""

from __future__ import annotations

import math

import torch
from torch import nn
import torch.nn.functional as F

from tts.auk.ops import apply_rope, attend


class RotaryEmbedding(nn.Module):
    def __init__(self, dim: int) -> None:
        super().__init__()
        self.register_buffer("inv_freq", torch.empty(dim // 2))

    def forward(self, length: int, device: torch.device) -> torch.Tensor:
        angle = torch.arange(length, device=device, dtype=torch.float32)[:, None] * self.inv_freq.to(
            device=device, dtype=torch.float32
        )
        return torch.stack((angle.cos(), -angle.sin(), angle.sin(), angle.cos()), -1).reshape(
            1, 1, length, -1, 2, 2
        )


class TimestepEmbedding(nn.Module):
    def __init__(self, dim: int) -> None:
        super().__init__()
        self.time_mlp = nn.Sequential(nn.Linear(256, dim), nn.SiLU(), nn.Linear(dim, dim))

    def forward(self, time: torch.Tensor, dtype: torch.dtype) -> torch.Tensor:
        freq = torch.exp(torch.arange(128, device=time.device, dtype=torch.float32) * (-math.log(10000) / 127))
        angle = 1000 * time[:, None] * freq[None]
        return self.time_mlp(torch.cat((angle.sin(), angle.cos()), -1).to(dtype))


class ConvPositionEmbedding(nn.Module):
    def __init__(self, dim: int) -> None:
        super().__init__()
        self.conv1d = nn.Sequential(
            nn.Conv1d(dim, dim, 31, padding=15, groups=16),
            nn.Mish(),
            nn.Conv1d(dim, dim, 31, padding=15, groups=16),
            nn.Mish(),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.conv1d(x.transpose(1, 2)).transpose(1, 2)


class AudioPromptEmbedding(nn.Module):
    def __init__(self, latent_dim: int, dim: int) -> None:
        super().__init__()
        self.linear = nn.Linear(latent_dim, dim)
        self.conv_pos_embed = ConvPositionEmbedding(dim)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = self.linear(x)
        return x + self.conv_pos_embed(x)


class AdaLayerNorm(nn.Module):
    def __init__(self, dim: int, final: bool = False) -> None:
        super().__init__()
        self.linear = nn.Linear(dim, dim * (2 if final else 6))
        self.norm = nn.LayerNorm(dim, elementwise_affine=False, eps=1e-6)
        self.final = final

    def forward(self, x: torch.Tensor, t: torch.Tensor) -> torch.Tensor | tuple[torch.Tensor, ...]:
        values = self.linear(F.silu(t))[:, None]
        if self.final:
            scale, shift = values.chunk(2, -1)
            return self.norm(x) * (1 + scale) + shift
        shift, scale, gate, ff_shift, ff_scale, ff_gate = values.chunk(6, -1)
        return self.norm(x) * (1 + scale) + shift, gate, ff_shift, ff_scale, ff_gate


class FeedForward(nn.Module):
    def __init__(self, dim: int, mult: float) -> None:
        super().__init__()
        self.linear_in = nn.Linear(dim, int(dim * mult) * 2, bias=False)
        self.linear_out = nn.Linear(int(dim * mult), dim, bias=False)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        gate, value = self.linear_in(x).chunk(2, -1)
        return self.linear_out(F.silu(gate) * value)


class Attention(nn.Module):
    def __init__(self, dim: int, heads: int, joint: bool = False) -> None:
        super().__init__()
        self.heads = heads
        self.to_qkv = nn.Linear(dim, dim * 3)
        self.q_norm = nn.RMSNorm(dim // heads, eps=None)
        self.k_norm = nn.RMSNorm(dim // heads, eps=None)
        self.to_out = nn.ModuleList([nn.Linear(dim, dim), nn.Identity()])
        if joint:
            self.to_qkv_c = nn.Linear(dim, dim * 3)
            self.c_q_norm = nn.RMSNorm(dim // heads, eps=None)
            self.c_k_norm = nn.RMSNorm(dim // heads, eps=None)
            self.to_out_c = nn.Linear(dim, dim)

    def qkv(
        self,
        x: torch.Tensor,
        projection: nn.Linear,
        q_norm: nn.Module,
        k_norm: nn.Module,
        rope: torch.Tensor,
    ) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        q, k, v = (
            part.reshape(x.shape[0], x.shape[1], self.heads, -1).transpose(1, 2)
            for part in projection(x).chunk(3, -1)
        )
        # Separate norm and RoPE preserve PyTorch RMSNorm's dtype-dependent epsilon
        # and intermediate rounding; the shared paired RoPE handles the rotation.
        q, k = apply_rope(q_norm(q), k_norm(k), rope)
        return q, k, v

    def forward(
        self,
        x: torch.Tensor,
        rope: torch.Tensor,
        c: torch.Tensor | None = None,
        c_rope: torch.Tensor | None = None,
        mask: torch.Tensor | None = None,
    ) -> torch.Tensor | tuple[torch.Tensor, torch.Tensor]:
        q, k, v = self.qkv(x, self.to_qkv, self.q_norm, self.k_norm, rope)
        if c is not None:
            cq, ck, cv = self.qkv(c, self.to_qkv_c, self.c_q_norm, self.c_k_norm, c_rope)
            q, k, v = torch.cat((q, cq), 2), torch.cat((k, ck), 2), torch.cat((v, cv), 2)
        out = attend(q, k, v, mask)
        if c is None:
            return self.to_out[0](out)
        return self.to_out[0](out[:, : x.shape[1]]), self.to_out_c(out[:, x.shape[1] :])


class DoubleBlock(nn.Module):
    def __init__(self, dim: int, heads: int, mult: float) -> None:
        super().__init__()
        self.attn_norm_x = AdaLayerNorm(dim)
        self.attn_norm_c = AdaLayerNorm(dim)
        self.attn = Attention(dim, heads, joint=True)
        self.ff_norm_x = nn.LayerNorm(dim, eps=1e-6, elementwise_affine=False)
        self.ff_norm_c = nn.LayerNorm(dim, eps=1e-6, elementwise_affine=False)
        self.ff_x = FeedForward(dim, mult)
        self.ff_c = FeedForward(dim, mult)

    def forward(
        self,
        x: torch.Tensor,
        c: torch.Tensor,
        t: torch.Tensor,
        rope: torch.Tensor,
        c_rope: torch.Tensor,
        mask: torch.Tensor | None,
        c_mask: torch.Tensor,
    ) -> tuple[torch.Tensor, torch.Tensor]:
        nx, gx, sx, ax, fx = self.attn_norm_x(x, t)
        nc, gc, sc, ac, fc = self.attn_norm_c(c, t)
        dx, dc = self.attn(nx, rope, nc, c_rope, mask)
        dc = dc.masked_fill(~c_mask[:, :, None], 0)
        x = x + gx * dx
        c = c + gc * dc
        return x + fx * self.ff_x(self.ff_norm_x(x) * (1 + ax) + sx), c + fc * self.ff_c(
            self.ff_norm_c(c) * (1 + ac) + sc
        )


class SingleBlock(nn.Module):
    def __init__(self, dim: int, heads: int, mult: float) -> None:
        super().__init__()
        self.attn_norm = AdaLayerNorm(dim)
        self.attn = Attention(dim, heads)
        self.ff_norm = nn.LayerNorm(dim, eps=1e-6, elementwise_affine=False)
        self.ff = FeedForward(dim, mult)

    def forward(
        self, x: torch.Tensor, t: torch.Tensor, rope: torch.Tensor, mask: torch.Tensor | None
    ) -> torch.Tensor:
        norm, gate, shift, scale, ff_gate = self.attn_norm(x, t)
        out = self.attn(norm, rope, mask=mask)
        if mask is not None:
            out = out.masked_fill(~mask[:, 0, 0, :, None], 0)
        x = x + gate * out
        return x + ff_gate * self.ff(self.ff_norm(x) * (1 + scale) + shift)


class Flux2Edit(nn.Module):
    def __init__(
        self,
        *,
        dim: int = 1536,
        heads: int = 24,
        ff_mult: float = 2,
        text_hidden_dim: int = 2048,
        num_layers: int = 10,
        num_single_layers: int = 20,
        latent_dim: int = 64,
    ) -> None:
        super().__init__()
        self.time_embed = TimestepEmbedding(dim)
        self.txt_proj = nn.Linear(text_hidden_dim, dim)
        self.txt_norm = nn.RMSNorm(dim, eps=None)
        self.audio_embed = AudioPromptEmbedding(latent_dim, dim)
        self.rotary_embed = RotaryEmbedding(dim // heads)
        self.transformer_blocks = nn.ModuleList([DoubleBlock(dim, heads, ff_mult) for _ in range(num_layers)])
        self.single_transformer_blocks = nn.ModuleList(
            [SingleBlock(dim, heads, ff_mult) for _ in range(num_single_layers)]
        )
        self.norm_out = AdaLayerNorm(dim, final=True)
        self.proj_out = nn.Linear(dim, latent_dim)

    def prepare(
        self, text: torch.Tensor, ref: torch.Tensor, guided: bool
    ) -> tuple[torch.Tensor, torch.Tensor | None]:
        c = self.txt_norm(self.txt_proj(text))
        prompt = self.audio_embed(ref) if ref.shape[1] else None
        if guided:
            c = torch.cat((c, torch.zeros_like(c)))
            if prompt is not None:
                prompt = torch.cat((prompt, self.audio_embed(torch.zeros_like(ref))))
        return c, prompt

    def forward(
        self,
        x: torch.Tensor,
        time: torch.Tensor,
        context: torch.Tensor,
        prompt: torch.Tensor | None,
        c_mask: torch.Tensor,
        guided: bool = False,
    ) -> torch.Tensor:
        if guided:
            x = torch.cat((x, x))
            c_mask = torch.cat((c_mask, c_mask))
        t = self.time_embed(time.expand(x.shape[0]), x.dtype)
        x = self.audio_embed(x)
        prompt_len = 0 if prompt is None else prompt.shape[1]
        if prompt is not None:
            x = torch.cat((prompt, x), 1)
        c = context
        rope = self.rotary_embed(x.shape[1], x.device)
        c_rope = self.rotary_embed(c.shape[1], x.device)
        # Upstream enables the joint padding mask only when reference audio exists.
        mask = None
        if prompt is not None:
            audio_mask = torch.ones(x.shape[:2], dtype=torch.bool, device=x.device)
            mask = torch.cat((audio_mask, c_mask), 1)[:, None, None]
        for block in self.transformer_blocks:
            x, c = block(x, c, t, rope, c_rope, mask, c_mask)
        text_len = c.shape[1]
        x = torch.cat((c, x), 1)
        rope = self.rotary_embed(x.shape[1], x.device)
        if prompt is not None:
            mask = torch.cat((c_mask, audio_mask), 1)[:, None, None]
        for block in self.single_transformer_blocks:
            x = block(x, t, rope, mask)
        return self.proj_out(self.norm_out(x[:, text_len + prompt_len :], t))


class AuKModel(nn.Module):
    """The backbone plus the learned Qwen layer-fusion weights it feeds the encoder."""

    def __init__(self, *, variant: str, encoder_layers: int = 36, architecture: dict | None = None) -> None:
        super().__init__()
        self.transformer = Flux2Edit(**(architecture or {}))
        self.layer_weights = nn.Parameter(torch.empty(encoder_layers))
        self.layer_scale = nn.Parameter(torch.empty(1))
        self.is_flash = variant == "flash"
