"""Inference-only AuK BigVGAN codec.

Ported from the MIT-licensed ComfyUI-AuK node pack and ComfyUI's aliased-free
resampling helpers, with ``comfy.ops`` replaced by stock torch modules: the VAE
runs in fp32 on every pin, so nothing here needs a precision cast layer. Weight
normalization is folded on load, which is why the module holds plain ``weight``
parameters rather than ``weight_g``/``weight_v``.
"""

from __future__ import annotations

from typing import Mapping

import torch
from torch import nn
import torch.nn.functional as F

from tts.auk.ops import cast_to


class SnakeBeta(nn.Module):
    """``x + 1/beta * sin(alpha * x)^2`` with alpha and beta in log space."""

    def __init__(self, channels: int) -> None:
        super().__init__()
        self.alpha_logscale = True
        self.alpha = nn.Parameter(torch.empty(channels))
        self.beta = nn.Parameter(torch.empty(channels))
        self.no_div_by_zero = 1e-9

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        alpha = cast_to(self.alpha, x).unsqueeze(0).unsqueeze(-1)
        beta = cast_to(self.beta, x).unsqueeze(0).unsqueeze(-1)
        alpha, beta = torch.exp(alpha), torch.exp(beta)
        return x + (1.0 / (beta + self.no_div_by_zero)) * torch.sin(x * alpha) ** 2


class UpSample1d(nn.Module):
    """Alias-free upsample; the sinc filter comes from the checkpoint."""

    def __init__(self) -> None:
        super().__init__()
        self.ratio = self.stride = 2
        self.pad = 5
        self.pad_left = self.pad_right = 15
        self.register_buffer("filter", torch.empty(1, 1, 12))

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        channels = x.shape[1]
        x = F.pad(x, (self.pad, self.pad), mode="replicate")
        x = self.ratio * F.conv_transpose1d(
            x, cast_to(self.filter, x).expand(channels, -1, -1), stride=self.stride, groups=channels
        )
        return x[..., self.pad_left : -self.pad_right]


class LowPassFilter1d(nn.Module):
    def __init__(self) -> None:
        super().__init__()
        self.register_buffer("filter", torch.empty(1, 1, 12))

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        filt = cast_to(self.filter, x).expand(x.shape[1], -1, -1)
        return F.conv1d(F.pad(x, (11, 0), mode="replicate"), filt, stride=2, groups=x.shape[1])


class DownSample1d(nn.Module):
    """Alias-free downsample; the checkpoint names the filter one level deeper."""

    def __init__(self) -> None:
        super().__init__()
        self.lowpass = LowPassFilter1d()

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.lowpass(x)


class Activation1d(nn.Module):
    def __init__(self, channels: int) -> None:
        super().__init__()
        self.act = SnakeBeta(channels)
        self.upsample = UpSample1d()
        self.downsample = DownSample1d()

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.downsample(self.act(self.upsample(x)))


class CausalConv1d(nn.Conv1d):
    """``Conv1d`` padded on the left only. The checkpoint names its weight directly."""

    def __init__(
        self, in_channels: int, out_channels: int, kernel_size: int, dilation: int = 1, bias: bool = True
    ) -> None:
        super().__init__(
            in_channels, out_channels, kernel_size, dilation=dilation, bias=bias
        )
        self.left_padding = dilation * (kernel_size - 1)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return super().forward(F.pad(x, (self.left_padding, 0)))


class CausalConvTranspose1d(nn.ConvTranspose1d):
    """``ConvTranspose1d`` with the right edge trimmed, named like the checkpoint."""

    def __init__(self, in_channels: int, out_channels: int, kernel_size: int, stride: int) -> None:
        super().__init__(in_channels, out_channels, kernel_size, stride=stride)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return super().forward(x)[..., : -self.stride[0]]


class Conv1dS(nn.Module):
    """Plain padded conv under the ``layer`` name the checkpoint uses."""

    def __init__(self, in_channels: int, out_channels: int, kernel_size: int, stride: int = 1) -> None:
        super().__init__()
        self.layer = nn.Conv1d(
            in_channels, out_channels, kernel_size, stride=stride, padding=(kernel_size - 1) // 2
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.layer(x)


class Encoder(nn.Module):
    """Strided waveform encoder; emits mean and log-std for the flow latent."""

    def __init__(self) -> None:
        super().__init__()
        channels = [12, 24, 48, 96, 192, 384, 768]
        layers: list[nn.Module] = [Conv1dS(1, 12, 3), nn.LeakyReLU(0.2)]
        for inputs, outputs, factor in zip(channels[:-1], channels[1:], [2, 2, 2, 3, 4, 5]):
            layers.extend([Conv1dS(inputs, outputs, factor * 2, factor), ResStack(outputs), nn.LeakyReLU(0.2)])
        layers.append(Conv1dS(768, 128, 3))
        self.generator = nn.Sequential(*layers)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.generator(x)


class ResStack(nn.Module):
    def __init__(self, channels: int) -> None:
        super().__init__()
        self.layers = nn.ModuleList(
            [
                nn.Sequential(
                    nn.LeakyReLU(),
                    nn.Conv1d(channels, channels, 3, dilation=2**i, padding=2**i),
                    nn.LeakyReLU(),
                    nn.Conv1d(channels, channels, 3, padding=1),
                )
                for i in range(6)
            ]
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        for layer in self.layers:
            x = x + layer(x)
        return x


class AMPBlock(nn.Module):
    def __init__(self, channels: int, kernel: int) -> None:
        super().__init__()
        self.convs1 = nn.ModuleList([CausalConv1d(channels, channels, kernel, dilation=d) for d in (1, 3, 5)])
        self.convs2 = nn.ModuleList([CausalConv1d(channels, channels, kernel) for _ in range(3)])
        self.activations = nn.ModuleList([Activation1d(channels) for _ in range(6)])

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        for index, (convs1, convs2) in enumerate(zip(self.convs1, self.convs2)):
            x = x + convs2(self.activations[2 * index + 1](convs1(self.activations[2 * index](x))))
        return x


class BigVGANFlowVAE(nn.Module):
    """24 kHz mono codec: ``encode`` latent -> [B, T, 64], ``decode`` -> [B, 1, N]."""

    sample_rate = 24000

    def __init__(self) -> None:
        super().__init__()
        self.register_buffer("global_mean", torch.empty(64))
        self.register_buffer("global_log_std", torch.empty(64))
        self.audio_encoder = Encoder()
        self.conv_pre = nn.Conv1d(64, 1536, 7, padding=3)
        self.ups = nn.ModuleList()
        self.resblocks = nn.ModuleList()
        for index, (rate, kernel) in enumerate(zip([5, 4, 3, 2, 2, 2], [10, 8, 6, 4, 4, 4])):
            inputs, outputs = 1536 // 2**index, 1536 // 2 ** (index + 1)
            self.ups.append(nn.ModuleList([CausalConvTranspose1d(inputs, outputs, kernel, stride=rate)]))
            self.resblocks.extend([AMPBlock(outputs, size) for size in (3, 7, 11)])
        self.activation_post = Activation1d(24)
        self.conv_post = CausalConv1d(24, 1, 7, bias=False)

    def encode(self, waveform: torch.Tensor, generator: torch.Generator | None = None) -> torch.Tensor:
        mean, log_std = self.audio_encoder(waveform).chunk(2, 1)
        noise = torch.randn(mean.shape, device=mean.device, dtype=mean.dtype, generator=generator)
        latent = (mean + noise * log_std.exp()).transpose(1, 2)
        center = cast_to(self.global_mean, latent)
        variance = cast_to(self.global_log_std, latent)
        return ((latent - center) / variance.sqrt())[:, : waveform.shape[-1] // 480].clone()

    def decode(self, latent: torch.Tensor) -> torch.Tensor:
        center = cast_to(self.global_mean, latent)
        variance = cast_to(self.global_log_std, latent)
        x = self.conv_pre((latent * variance.sqrt() + center).transpose(1, 2))
        for index, up in enumerate(self.ups):
            x = up[0](x)
            x = sum(self.resblocks[index * 3 + offset](x) for offset in range(3)) / 3
        return self.conv_post(self.activation_post(x)).clamp(-1, 1)


def fold_weight_norm(state: Mapping[str, torch.Tensor]) -> dict[str, torch.Tensor]:
    """Fold ``weight_g``/``weight_v`` pairs into ``weight`` and drop the flow keys."""
    folded = {key: value for key, value in state.items() if not key.startswith("flow.")}
    for key in [key for key in folded if key.endswith("weight_g")]:
        prefix = key[: -len("weight_g")]
        gain, value = folded.pop(key), folded.pop(prefix + "weight_v")
        folded[prefix + "weight"] = torch._weight_norm(value, gain, 0)
    return folded
