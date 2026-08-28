#!/usr/bin/env bash
# Reproduce sc-e71.4 CosyVoice GPU qualification without touching Supertonic.
set -euo pipefail

CODE_COMMIT="074ca6dc9e80a2f424f1f74b48bdd7d3fea531cc"
MODEL_ID="FunAudioLLM/Fun-CosyVoice3-0.5B-2512"
MODEL_REV="29e01c4e8d000f4bcd70751be16fa94bf3d85a18"
QUAL_ROOT="${COSYVOICE_QUAL_ROOT:-$HOME/.cache/speech-out/cosyvoice-qual-sc-e71.4}"
SKIP_DOWNLOAD=0
SKIP_PROCESS_CANCEL=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --qual-root) QUAL_ROOT="$2"; shift 2 ;;
    --skip-download) SKIP_DOWNLOAD=1; shift ;;
    --skip-process-cancel) SKIP_PROCESS_CANCEL=1; shift ;;
    -h|--help)
      echo "Usage: $0 [--qual-root DIR] [--skip-download] [--skip-process-cancel]"
      exit 0
      ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
HARNESS="$REPO_ROOT/scripts/cosyvoice_qual/run_qualification.py"
VENV="$QUAL_ROOT/venv"
SRC="$QUAL_ROOT/src/CosyVoice"
MODEL_DIR="$QUAL_ROOT/models/Fun-CosyVoice3-0.5B"
LOG_DIR="$QUAL_ROOT/logs"
mkdir -p "$QUAL_ROOT"/{logs,models,runs,artifacts,scripts}

echo "== host snapshot (services must stay up) =="
ps -eo pid=,etime=,cmd= | rg 'speech-core-daemon|speech-out daemon' || true
ss -ltnp 2>/dev/null | rg '8788|7788' || true
echo "NOTE: nvidia-smi may fail (NVML mismatch); torch.cuda is the oracle."

if [[ ! -d "$SRC/.git" ]]; then
  echo "== clone code pin =="
  git clone https://github.com/QwenAudio/CosyVoice.git "$SRC"
  git -C "$SRC" checkout "$CODE_COMMIT"
  git -C "$SRC" submodule update --init --recursive
else
  echo "== verify code pin =="
  HEAD=$(git -C "$SRC" rev-parse HEAD)
  if [[ "$HEAD" != "$CODE_COMMIT" ]]; then
    git -C "$SRC" fetch --all --tags
    git -C "$SRC" checkout "$CODE_COMMIT"
    git -C "$SRC" submodule update --init --recursive
  fi
  git -C "$SRC" rev-parse HEAD
fi

if [[ ! -x "$VENV/bin/python" ]]; then
  echo "== create python3.10 venv (uv) =="
  uv venv --python 3.10 "$VENV"
fi

echo "== install lean inference deps =="
# torch cu121 first
uv pip install --python "$VENV/bin/python" \
  --index-url https://download.pytorch.org/whl/cu121 \
  --extra-index-url https://pypi.org/simple \
  'torch==2.3.1' 'torchaudio==2.3.1'

uv pip install --python "$VENV/bin/python" 'setuptools==69.5.1' 'wheel' 'packaging'

uv pip install --python "$VENV/bin/python" \
  --index-strategy unsafe-best-match \
  --extra-index-url https://download.pytorch.org/whl/cu121 \
  --extra-index-url https://aiinfra.pkgs.visualstudio.com/PublicPackages/_packaging/onnxruntime-cuda-12/pypi/simple/ \
  'numpy==1.26.4' \
  'HyperPyYAML==1.2.3' \
  'omegaconf==2.3.0' \
  'hydra-core==1.3.2' \
  'soundfile==0.12.1' \
  'librosa==0.10.2' \
  'onnxruntime-gpu==1.18.0' \
  'onnx==1.16.0' \
  'transformers==4.51.3' \
  'diffusers==0.29.0' \
  'conformer==0.3.2' \
  'lightning==2.2.4' \
  'wetext==0.0.4' \
  'inflect==7.3.1' \
  'networkx==3.1' \
  'protobuf==4.25' \
  'pyarrow==18.1.0' \
  'pydantic==2.7.0' \
  'x-transformers==2.11.24' \
  'huggingface_hub[cli]' \
  'matplotlib==3.7.5' \
  'rich==13.7.1' \
  'gdown==5.1.0' \
  'wget==3.2' \
  'modelscope==1.20.0' \
  'pyworld==0.3.4'

uv pip install --python "$VENV/bin/python" --no-build-isolation 'openai-whisper==20231117'

uv pip freeze --python "$VENV/bin/python" | tee "$LOG_DIR/requirements-lock.txt"

echo "== cuda probe =="
"$VENV/bin/python" - <<'PY'
import torch
print("torch", torch.__version__)
print("cuda_available", torch.cuda.is_available())
print("cuda", torch.version.cuda)
assert torch.cuda.is_available(), "CUDA required for this qualification"
print("device", torch.cuda.get_device_name(0))
free, total = torch.cuda.mem_get_info()
print(f"mem_free_gb={free/1e9:.3f} mem_total_gb={total/1e9:.3f}")
PY

if [[ "$SKIP_DOWNLOAD" -eq 0 ]]; then
  echo "== model download pin =="
  "$VENV/bin/hf" download "$MODEL_ID" \
    --revision "$MODEL_REV" \
    --local-dir "$MODEL_DIR"
fi

echo "== verify key sha256 =="
"$VENV/bin/python" - <<PY
import hashlib
from pathlib import Path
model = Path("$MODEL_DIR")
expected = {
  "llm.pt": ("69f43bd545131c30e98947fb360ea8b4dc9916d8e83dded7757c7ea4f5a24970", 2024669519),
  "flow.pt": ("a6fab32a7825e5b0bc855ddd948f8db9370b0a786fbc249caa4595e95b608e4b", 1329116148),
  "hift.pt": ("b279d7641eb97ae55b3b540cfba4f953c26492a2df758328a89a4d007ab87a65", 83202622),
  "speech_tokenizer_v3.onnx": ("23236a74175dbdda47afc66dbadd5bcb41303c467a57c261cb8539ad9db9208d", 969451503),
  "campplus.onnx": ("a6ac6a63997761ae2997373e2ee1c47040854b4b759ea41ec48e4e42df0f4d73", 28303423),
  "CosyVoice-BlankEN/model.safetensors": ("130282af0dfa9fe5840737cc49a0d339d06075f83c5a315c3372c9a0740d0b96", 988097824),
}
for rel,(sha,size) in expected.items():
    p = model/rel
    assert p.exists(), rel
    assert p.stat().st_size == size, (rel, p.stat().st_size, size)
    h=hashlib.sha256()
    with p.open('rb') as f:
        for chunk in iter(lambda: f.read(1<<20), b''):
            h.update(chunk)
    got=h.hexdigest()
    assert got==sha, (rel, got, sha)
    print("ok", rel)
print("all hashes match sc-e71.4 report")
PY

cp "$HARNESS" "$QUAL_ROOT/scripts/run_qualification.py"
RUN_ID=$(date -u +%Y%m%dT%H%M%SZ)
echo "== run harness run_id=$RUN_ID =="
ARGS=(--qual-root "$QUAL_ROOT" --run-id "$RUN_ID")
if [[ "$SKIP_PROCESS_CANCEL" -eq 1 ]]; then
  ARGS+=(--skip-process-cancel)
fi
"$VENV/bin/python" "$QUAL_ROOT/scripts/run_qualification.py" "${ARGS[@]}" \
  | tee "$LOG_DIR/qual-run-$RUN_ID.log"

echo "== services still up? =="
ps -eo pid=,etime=,cmd= | rg 'speech-core-daemon|speech-out daemon' || true
echo "report: $QUAL_ROOT/runs/$RUN_ID/report.json"
echo "done"
