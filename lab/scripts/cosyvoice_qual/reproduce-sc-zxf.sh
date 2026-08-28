#!/usr/bin/env bash
# Reproduce sc-zxf isolated CosyVoice2 qualification.
# Does NOT stop/restart/reload speech-out-daemon.service.
# Does NOT edit aa86b67, speech-out.env, or the unit.
# Does NOT start docker triton / trtllm-serve / compile a .plan.
set -euo pipefail

CAMPAIGN_DIR="${CAMPAIGN_DIR:-/home/sf/.local/state/discord-voice-agent/latency-campaign-20260812}"
PIN="${PIN:-/home/sf/.cache/speech-out/cosyvoice2-lane2-20260812}"
QUAL_VENV="${QUAL_VENV:-/home/sf/.cache/speech-out/cosyvoice-qual-sc-e71.4/venv}"
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
HARNESS="${HARNESS:-$CAMPAIGN_DIR/lane2/artifacts/ttfp_harness.py}"
WORKER="${WORKER:-$CAMPAIGN_DIR/lane2/artifacts/cosyvoice_progressive_worker.py}"
PROMPTS="${PROMPTS:-$REPO_ROOT/scripts/cosyvoice_qual/sc-zxf-prompts.txt}"
GPU_RUN="${GPU_RUN:-$CAMPAIGN_DIR/gpu-run}"
RES="${RES:-$CAMPAIGN_DIR/lane2/results/sc-zxf}"
PYTHON="${PYTHON:-$QUAL_VENV/bin/python}"
REF_TEXT='希望你以后能够做的比我还好呦。'
REF_SHA='c7b31d6dbe7cc6a716dded00550db5b50940bf209e424e4ad207b12e657c8ff6'

echo "== live unit must stay up (do not systemctl stop/restart) =="
systemctl --user show speech-out-daemon.service -p Description -p ActiveState -p MainPID -p ActiveEnterTimestamp || true
ps -o pid,etime,cmd -p 4345,4378 2>/dev/null || ps -eo pid,etime,cmd | rg 'speech-out-aa86|cosyvoice_progressive_worker' || true
ss -ltnp 2>/dev/null | rg 8788 || true
nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv || true
nvidia-smi --query-gpu=memory.used,memory.free,memory.total --format=csv || true

echo "== pin gate =="
test -x "$PYTHON"
test -f "$PIN/models/CosyVoice2-0.5B/llm.pt"
test -f "$CAMPAIGN_DIR/lane2/artifacts/model-download.status"
grep -qx OK "$CAMPAIGN_DIR/lane2/artifacts/model-download.status"
test -f "$PROMPTS"

mkdir -p "$RES" "$CAMPAIGN_DIR/lane2/logs"

echo "== optional live :8788 now-stamp (uses running unit; no extra model) =="
if [[ "${SKIP_LIVE_PROBE:-0}" != "1" ]]; then
  PLAY_BIN="${PLAY_BIN:-/home/sf/.local/share/discord-voice-agent/candidates/speech-out-cosy3-trt-aa86b67-20260812/bin/speech-out-aa86b67}"
  "$REPO_ROOT/scripts/cosyvoice_qual/live_8788_probe.py" \
    --play-bin "$PLAY_BIN" \
    --url "${SPEECH_OUT_WS_URL:-ws://127.0.0.1:8788/ws/speech-out}" \
    --prompts-file "$PROMPTS" \
    --n 5 --skip 2 \
    --out "$RES/live8788-nowstamp-n5.json"
fi

echo "== isolated arm A fp32 n=20 warmup=2 via gpu-run =="
CAMPAIGN_LANE=cosy2 "$GPU_RUN" env CAMPAIGN_LANE=cosy2 HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
  python3 "$HARNESS" \
    --label sc-zxf_speaker_cache_fp32 \
    --worker-script "$WORKER" \
    --python "$PYTHON" \
    --model-dir "$PIN/models/CosyVoice2-0.5B" \
    --code-root "$PIN/src/CosyVoice" \
    --reference-wav "$PIN/src/CosyVoice/asset/zero_shot_prompt.wav" \
    --reference-text "$REF_TEXT" \
    --reference-sha256 "$REF_SHA" \
    --encoding pcm_f32le --frame-samples 480 \
    --n 20 --warmup 2 --silence-rms 1e-4 \
    --out "$RES/speaker_cache_fp32_n20.json" \
    --stderr-log "$CAMPAIGN_DIR/lane2/logs/sc-zxf_speaker_cache_fp32_n20.stderr.log" \
    --prompts-file "$PROMPTS" \
    --config-note "sc-zxf arm A reproduce: CV2 AutoModel fp32 host opts ON"

echo "== isolated cancel =="
CAMPAIGN_LANE=cosy2 "$GPU_RUN" env CAMPAIGN_LANE=cosy2 HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
  "$PYTHON" "$CAMPAIGN_DIR/scripts/trt_cancel_survival.py" \
    --python "$PYTHON" \
    --worker "$WORKER" \
    --model-dir "$PIN/models/CosyVoice2-0.5B" \
    --code-root "$PIN/src/CosyVoice" \
    --reference-wav "$PIN/src/CosyVoice/asset/zero_shot_prompt.wav" \
    --reference-text "$REF_TEXT" \
    --reference-sha256 "$REF_SHA" \
    --stderr "$CAMPAIGN_DIR/lane2/logs/sc-zxf_cv2_cancel.stderr.log" \
    | tee "$RES/cv2-cancel-survival.json"

echo "== live unit still up? =="
systemctl --user show speech-out-daemon.service -p Description -p ActiveState -p MainPID -p ActiveEnterTimestamp
echo "historical CV3 n=25 (do not reload): $CAMPAIGN_DIR/lane1/results/trt_s4_h5_p1ms_n25.json"
echo "done"
