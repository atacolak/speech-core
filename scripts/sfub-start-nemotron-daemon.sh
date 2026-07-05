#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
bind="${SPEECH_CORE_DAEMON_BIND:-0.0.0.0:8765}"
log_dir="${SPEECH_CORE_LOG_DIR:-$repo_root/logs/live-$(date +%Y%m%d-%H%M%S)}"
model_path="${SPEECH_CORE_MODEL_PATH:-/home/sf/workspace/external/transcribe.cpp/models/nemotron-speech-streaming-en-0.6b/nemotron-speech-streaming-en-0.6b-Q4_K_M.gguf}"
stream_chunk_ms="${SPEECH_CORE_STREAM_CHUNK_MS:-160}"
att_context_right="${SPEECH_CORE_ATT_CONTEXT_RIGHT:-1}"
model_queue_frames="${SPEECH_CORE_MODEL_QUEUE_FRAMES:-2048}"
vad_model_path="${SPEECH_CORE_VAD_MODEL_PATH:-/home/sf/workspace/handy-tailnet-api/src-tauri/resources/models/silero_vad_v4.onnx}"
eou_model_dir="${SPEECH_CORE_EOU_MODEL_DIR:-/home/sf/workspace/external/parakeet-eou/realtime_eou_120m-v1-onnx}"
detector_queue_frames="${SPEECH_CORE_DETECTOR_QUEUE_FRAMES:-2048}"
turn_vad_close_enabled="${SPEECH_CORE_TURN_VAD_CLOSE_ENABLED:-false}"
turn_model_eou_close_enabled="${SPEECH_CORE_TURN_MODEL_EOU_CLOSE_ENABLED:-true}"
turn_min_model_eou_speech_ms="${SPEECH_CORE_TURN_MIN_MODEL_EOU_SPEECH_MS:-300}"
turn_model_eou_refractory_ms="${SPEECH_CORE_TURN_MODEL_EOU_REFRACTORY_MS:-700}"

mkdir -p "$log_dir"
cd "$repo_root"

cargo build -p speech-core-daemon >/dev/null

echo "speech-core daemon"
echo "  bind:              $bind"
echo "  log_dir:           $log_dir"
echo "  events_jsonl:       $log_dir/events.jsonl"
echo "  model_path:        $model_path"
echo "  stream_chunk_ms:   $stream_chunk_ms"
echo "  att_context_right: $att_context_right"
echo "  vad_model_path:    $vad_model_path"
echo "  eou_model_dir:     $eou_model_dir"
echo
echo "sfnix client example:"
echo "  SPEECH_CORE_WS_URL=ws://$(tailscale ip -4 | head -n1):8765/ws/audio-ingress /tmp/speech-core-native-build/scripts/sfnix-live-session.sh"
echo

export SPEECH_CORE_TURN_VAD_CLOSE_ENABLED="$turn_vad_close_enabled"
export SPEECH_CORE_TURN_MODEL_EOU_CLOSE_ENABLED="$turn_model_eou_close_enabled"
export SPEECH_CORE_TURN_MIN_MODEL_EOU_SPEECH_MS="$turn_min_model_eou_speech_ms"
export SPEECH_CORE_TURN_MODEL_EOU_REFRACTORY_MS="$turn_model_eou_refractory_ms"

exec target/debug/speech-core-daemon \
  --bind "$bind" \
  --log-dir "$log_dir" \
  --model-path "$model_path" \
  --stream-chunk-ms "$stream_chunk_ms" \
  --att-context-right "$att_context_right" \
  --model-queue-frames "$model_queue_frames" \
  --vad-model-path "$vad_model_path" \
  --eou-model-dir "$eou_model_dir" \
  --detector-queue-frames "$detector_queue_frames"
