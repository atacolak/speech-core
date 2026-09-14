"""Local Qwen2.5-Omni instruction encoder for AuK.

Ported from the MIT-licensed ComfyUI-AuK node pack (``nodes/encoder.py``). The
converted encoder checkpoint is W4A8-quantized, so the graph is built on the meta
device and the quantized weights are injected by :mod:`tts.auk.ops`; the module
classes themselves stay the ones ``transformers`` builds, and attention runs on
SDPA rather than ComfyUI's ``optimized_attention``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import torch
from torch import nn
import torch.nn.functional as F
import torchaudio
from transformers import AutoFeatureExtractor, AutoTokenizer, Qwen2_5OmniConfig
from transformers.models.qwen2_5_omni.modeling_qwen2_5_omni import (
    Qwen2_5OmniRotaryEmbedding,
    Qwen2_5OmniThinkerForConditionalGeneration,
    SinusoidsPositionEmbedding,
)

from tts.auk import ops

AUDIO_ATTENTION_IMPLEMENTATION = "sdpa"


class OmniAudioProcessor:
    """The audio-only half of ``Qwen2_5OmniProcessor``.

    ``AutoProcessor`` demands an image *and* a video processor for the Omni family,
    neither of which this port ever feeds, and the video one drags torchvision into the
    AuK venv. The branch below mirrors ``Qwen2_5OmniProcessor.__call__`` for a
    text+audio batch: the audio tower emits one frame per ``<|AUDIO|>`` token, so the
    chat template's single placeholder is expanded to the frame count before the
    tokenizer runs, and the feature extractor's attention mask is renamed.
    """

    AUDIO_TOKEN = "<|AUDIO|>"
    PLACEHOLDER = "<|audio_placeholder|>"
    SAMPLING_RATE = 16000

    def __init__(self, tokenizer: Any, feature_extractor: Any, chat_template: str) -> None:
        self.tokenizer = tokenizer
        self.feature_extractor = feature_extractor
        self.chat_template = chat_template

    @classmethod
    def from_pretrained(cls, assets: Path) -> OmniAudioProcessor:
        template = json.loads((assets / "chat_template.json").read_text(encoding="utf-8"))
        return cls(
            AutoTokenizer.from_pretrained(str(assets), local_files_only=True),
            AutoFeatureExtractor.from_pretrained(str(assets), local_files_only=True),
            template["chat_template"],
        )

    @staticmethod
    def _frames(mask: torch.Tensor) -> list[int]:
        lengths = (mask.sum(-1) - 1) // 2 + 1
        return [int(value) for value in (lengths - 2) // 2 + 1]

    def __call__(
        self,
        *,
        text: list[str],
        audio: list[Any],
        padding: bool = False,
        return_tensors: str | None = None,
    ) -> dict[str, torch.Tensor]:
        features = self.feature_extractor(
            audio,
            sampling_rate=self.SAMPLING_RATE,
            padding="max_length",
            return_attention_mask=True,
            return_tensors=return_tensors,
        )
        mask = features.pop("attention_mask")
        expanded = [
            sample.replace(self.AUDIO_TOKEN, self.PLACEHOLDER * frames, 1).replace(
                self.PLACEHOLDER, self.AUDIO_TOKEN
            )
            for sample, frames in zip(text, self._frames(mask))
        ]
        tokens = self.tokenizer(expanded, padding=padding, return_tensors=return_tensors)
        return {**dict(tokens), **features, "feature_attention_mask": mask}


class QwenEncoder(Qwen2_5OmniThinkerForConditionalGeneration):
    """The Omni thinker without the vision tower, plus a fused-layer entry point."""

    def __init__(self, config: Qwen2_5OmniConfig) -> None:
        super().__init__(config)
        del self.visual

    def encode_layers(self, inputs: dict[str, torch.Tensor]) -> list[torch.Tensor]:
        """Run every text layer once and return their hidden states, embedding-last."""
        input_ids = inputs["input_ids"]
        mask = inputs["attention_mask"]
        embeds = self.model.embed_tokens(input_ids)
        lengths = None
        if "input_features" in inputs:
            features = self.get_audio_features(
                inputs["input_features"],
                feature_attention_mask=inputs["feature_attention_mask"],
                return_dict=True,
            ).last_hidden_state
            slots = (input_ids == self.config.audio_token_index)[:, :, None]
            frames = int(slots.sum())
            # masked_scatter pads a short source with its last row and truncates a long
            # one, so a tokenizer expansion that disagrees with the tower would condition
            # the sampler on the wrong audio. The tower returns [frames, dim].
            if features.shape[0] != frames:
                raise RuntimeError(
                    f"the instruction has {frames} audio tokens but the tower emitted "
                    f"{features.shape[0]} frames"
                )
            embeds = embeds.masked_scatter(slots, features.to(embeds))
            lengths = inputs["feature_attention_mask"].sum(-1)
        positions, _ = self.get_rope_index(
            input_ids, attention_mask=mask, use_audio_in_video=False, audio_seqlens=lengths
        )
        output = self.model(
            inputs_embeds=embeds,
            attention_mask=mask,
            position_ids=positions,
            use_cache=False,
            output_hidden_states=True,
            return_dict=True,
        )
        return output.hidden_states[1:]


class AuKEncoder:
    """Instruction + reference audio in, the backbone's text conditioning out."""

    def __init__(
        self,
        model: QwenEncoder,
        processor: OmniAudioProcessor,
        *,
        device: torch.device,
        compute: torch.dtype,
    ) -> None:
        self.model = model
        self.processor = processor
        self.device = device
        self.compute = compute

    @classmethod
    def build(
        cls,
        assets: Path,
        state: dict[str, torch.Tensor],
        *,
        device: torch.device,
        compute: torch.dtype,
    ) -> AuKEncoder:
        config = Qwen2_5OmniConfig.from_pretrained(
            str(assets), local_files_only=True, attn_implementation=AUDIO_ATTENTION_IMPLEMENTATION
        ).thinker_config
        with torch.device("meta"):
            model = QwenEncoder(config)
        # Both carry buffers that transformers computes in __init__, so the meta build
        # leaves them device-less; rebuild them for real and put them on the device.
        model.model.rotary_emb = Qwen2_5OmniRotaryEmbedding(config.text_config, device=device)
        model.audio_tower.positional_embedding = SinusoidsPositionEmbedding(
            config.audio_config.max_source_positions, config.audio_config.d_model
        ).to(device)
        ops.load_weights(
            model, state, compute=compute, device=device, allow_missing=ops.OPTIONAL_ENCODER_KEYS
        )
        model.eval()
        processor = OmniAudioProcessor.from_pretrained(assets)
        return cls(model, processor, device=device, compute=compute)

    def encode(
        self,
        instruction: str,
        waveform: torch.Tensor,
        sample_rate: int,
        layer_weights: torch.Tensor,
        layer_scale: torch.Tensor,
    ) -> tuple[torch.Tensor, torch.Tensor]:
        """Fuse the layer states the way the backbone's learned weights ask for.

        ``waveform`` is mono at ``sample_rate``; the encoder wants its own sampling
        rate, so it is resampled here. Returns CPU tensors: the text conditioning and
        the matching attention mask.
        """
        content: list[dict[str, Any]] = [
            {"type": "text", "text": instruction},
            {"type": "audio", "audio": "local-reference"},
        ]
        rate = self.processor.feature_extractor.sampling_rate
        resampled = torchaudio.functional.resample(waveform.reshape(-1).to(torch.float32), sample_rate, rate)
        text = self.processor.tokenizer.apply_chat_template(
            [{"role": "user", "content": content}],
            chat_template=self.processor.chat_template,
            tokenize=False,
            add_generation_prompt=True,
        )
        inputs = self.processor(
            text=[text], padding=True, return_tensors="pt", audio=[resampled.cpu().numpy()]
        )
        inputs = {
            key: value.to(device=self.device, dtype=self.compute if value.is_floating_point() else value.dtype)
            for key, value in inputs.items()
        }
        states = self.model.encode_layers(inputs)
        weights = layer_weights.to(device=self.device, dtype=torch.float32).softmax(0)
        if len(states) != weights.numel():
            raise RuntimeError(
                f"the encoder has {len(states)} layers but the backbone fuses {weights.numel()}"
            )
        fused = None
        for weight, hidden in zip(weights, states):
            value = F.layer_norm(hidden, (hidden.shape[-1],)).float() * weight
            fused = value if fused is None else fused + value
        fused = fused * layer_scale.to(device=self.device, dtype=torch.float32)
        return fused.cpu(), inputs["attention_mask"].bool().cpu()
