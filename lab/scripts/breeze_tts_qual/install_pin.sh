#!/usr/bin/env bash
# not on the voicecat path.
set -euo pipefail
CODE_COMMIT="43e2ea1595297c4059477e2e4a300653761c759b"
QUAL_ROOT="${QUAL_ROOT:-$HOME/.cache/speech-out/breeze-tts-qual-sc-breeze-hybrid-81p}"
SRC="$QUAL_ROOT/src/breeze-tts"
VENV="$QUAL_ROOT/venv"
MODELS="$QUAL_ROOT/models"
mkdir -p "$QUAL_ROOT"/{logs,models,runs,fixtures,src}
if [[ ! -d "$SRC/.git" ]]; then
  git clone https://github.com/breezeblue-ai/breeze-tts.git "$SRC"
fi
git -C "$SRC" fetch --all --tags
git -C "$SRC" checkout "$CODE_COMMIT"

# Task 5 left a CPU-only torch 2.9.1+cpu venv (~856 MB). Overlaying official
# requirements.txt (torch==2.9.1) would keep that CPU wheel because pip treats
# 2.9.1+cpu as satisfying ==2.9.1. Recreate and install CUDA torch so later
# GPU tasks can reuse this venv. Do not install ComfyUI.
recreate_venv=0
if [[ ! -x "$VENV/bin/python" ]]; then
  recreate_venv=1
else
  torch_ver="$("$VENV/bin/python" -c 'import importlib.metadata as m; print(m.version("torch"))' 2>/dev/null || true)"
  case "$torch_ver" in
    *+cpu*|"") recreate_venv=1 ;;
  esac
fi
if [[ "$recreate_venv" -eq 1 ]]; then
  rm -rf "$VENV"
  uv venv --python 3.10 --seed "$VENV" || python3 -m venv "$VENV"
  export UV_HTTP_TIMEOUT="${UV_HTTP_TIMEOUT:-300}"
  uv pip install --python "$VENV/bin/python" \
    --index-url https://download.pytorch.org/whl/cu128 \
    --extra-index-url https://pypi.org/simple \
    'torch==2.9.1' 'torchaudio==2.9.1' || \
    "$VENV/bin/pip" install --index-url https://download.pytorch.org/whl/cu128 \
      --extra-index-url https://pypi.org/simple \
      'torch==2.9.1' 'torchaudio==2.9.1'
fi

# Official requirements from breeze-tts/requirements.txt at the pin.
uv pip install --python "$VENV/bin/python" -r "$SRC/requirements.txt" || \
  "$VENV/bin/pip" install -r "$SRC/requirements.txt"
# Kernel for ConvRot; must not install ComfyUI.
uv pip install --python "$VENV/bin/python" 'comfy-kitchen' 'safetensors' 'huggingface_hub' || \
  "$VENV/bin/pip" install 'comfy-kitchen' 'safetensors' 'huggingface_hub'
# Weights: official bf16 + hybrid + full-int8 + shared audio_tokenizer.
QUAL_ROOT="$QUAL_ROOT" "$VENV/bin/python" - <<'PY'
from huggingface_hub import snapshot_download
import os
root = os.environ["QUAL_ROOT"]
snapshot_download("BreezeBlue/Breeze-TTS-2", local_dir=f"{root}/models/official")
snapshot_download(
    "drbaph/Breeze-TTS-2-comfyui",
    local_dir=f"{root}/models/comfyui-deriv",
    allow_patterns=[
        "Breeze-TTS-2-bf16.safetensors",
        "Breeze-TTS-2-int8-hybrid.safetensors",
        "Breeze-TTS-2-int8-convrot.safetensors",
        "config.json",
        "generation_config.json",
        "tokenizer.json",
        "tokenizer_config.json",
        "special_tokens_map.json",
        "audio_tokenizer/*",
    ],
)
print("ok")
PY
echo "HEAD=$(git -C "$SRC" rev-parse HEAD)"
echo "MODELS=$MODELS"
