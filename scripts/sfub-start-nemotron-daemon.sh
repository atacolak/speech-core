#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
bind="${SPEECH_CORE_DAEMON_BIND:-0.0.0.0:8765}"
log_dir="${SPEECH_CORE_LOG_DIR:-$repo_root/logs/live-$(date +%Y%m%d-%H%M%S)}"
model_path="${SPEECH_CORE_MODEL_PATH:-/home/sf/workspace/external/transcribe.cpp/models/nemotron-speech-streaming-en-0.6b/nemotron-speech-streaming-en-0.6b-Q4_K_M.gguf}"
stream_chunk_ms="${SPEECH_CORE_STREAM_CHUNK_MS:-160}"
att_context_right="${SPEECH_CORE_ATT_CONTEXT_RIGHT:-1}"
model_queue_frames="${SPEECH_CORE_MODEL_QUEUE_FRAMES:-2048}"

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
echo
echo "sfnix client example:"
echo "  SPEECH_CORE_WS_URL=ws://$(tailscale ip -4 | head -n1):8765/ws/audio-ingress /tmp/speech-core-native-build/scripts/sfnix-live-session.sh"
echo

exec target/debug/speech-core-daemon \
  --bind "$bind" \
  --log-dir "$log_dir" \
  --model-path "$model_path" \
  --stream-chunk-ms "$stream_chunk_ms" \
  --att-context-right "$att_context_right" \
  --model-queue-frames "$model_queue_frames"
