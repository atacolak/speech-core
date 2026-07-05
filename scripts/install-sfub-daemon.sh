#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
bin_dir="${SPEECH_CORE_INSTALL_BIN_DIR:-$HOME/.local/bin}"
state_dir="${SPEECH_CORE_STATE_DIR:-$HOME/.local/state/speech-core}"
config_dir="${SPEECH_CORE_CONFIG_DIR:-$HOME/.config/speech-core}"
systemd_user_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
model_path="${SPEECH_CORE_MODEL_PATH:-/home/sf/workspace/external/transcribe.cpp/models/nemotron-speech-streaming-en-0.6b/nemotron-speech-streaming-en-0.6b-Q4_K_M.gguf}"
bind="${SPEECH_CORE_DAEMON_BIND:-0.0.0.0:8765}"
stream_chunk_ms="${SPEECH_CORE_STREAM_CHUNK_MS:-160}"
att_context_right="${SPEECH_CORE_ATT_CONTEXT_RIGHT:-1}"
model_queue_frames="${SPEECH_CORE_MODEL_QUEUE_FRAMES:-2048}"
vad_model_path="${SPEECH_CORE_VAD_MODEL_PATH:-/home/sf/workspace/handy-tailnet-api/src-tauri/resources/models/silero_vad_v4.onnx}"
vad_threshold="${SPEECH_CORE_VAD_THRESHOLD:-0.3}"
vad_onset_frames="${SPEECH_CORE_VAD_ONSET_FRAMES:-2}"
vad_hangover_frames="${SPEECH_CORE_VAD_HANGOVER_FRAMES:-8}"
vad_pre_speech_frames="${SPEECH_CORE_VAD_PRE_SPEECH_FRAMES:-5}"
vad_emit_frames="${SPEECH_CORE_VAD_EMIT_FRAMES:-false}"
eou_model_dir="${SPEECH_CORE_EOU_MODEL_DIR:-}"
eou_chunk_ms="${SPEECH_CORE_EOU_CHUNK_MS:-160}"
eou_reset_on_token="${SPEECH_CORE_EOU_RESET_ON_TOKEN:-false}"
eou_emit_transcript="${SPEECH_CORE_EOU_EMIT_TRANSCRIPT:-true}"
detector_queue_frames="${SPEECH_CORE_DETECTOR_QUEUE_FRAMES:-2048}"
turn_vad_close_enabled="${SPEECH_CORE_TURN_VAD_CLOSE_ENABLED:-true}"
turn_model_eou_close_enabled="${SPEECH_CORE_TURN_MODEL_EOU_CLOSE_ENABLED:-false}"
turn_min_vad_speech_ms="${SPEECH_CORE_TURN_MIN_VAD_SPEECH_MS:-700}"
turn_min_model_eou_speech_ms="${SPEECH_CORE_TURN_MIN_MODEL_EOU_SPEECH_MS:-300}"
turn_model_eou_refractory_ms="${SPEECH_CORE_TURN_MODEL_EOU_REFRACTORY_MS:-700}"

mkdir -p "$bin_dir" "$state_dir/logs" "$config_dir" "$systemd_user_dir"
cd "$repo_root"

cargo build -p speech-core-daemon -p speech-core-watch -p speech-core-file-adapter >/dev/null
install -m 0755 target/debug/speech-core-daemon "$bin_dir/speech-core-daemon"
install -m 0755 target/debug/speech-core-watch "$bin_dir/speech-core-watch"
install -m 0755 target/debug/speech-core-file-adapter "$bin_dir/speech-core-file-adapter"

cat >"$config_dir/daemon.env" <<EOF_ENV
SPEECH_CORE_DAEMON_BIND=$bind
SPEECH_CORE_LOG_DIR=$state_dir/logs
SPEECH_CORE_MODEL_PATH=$model_path
SPEECH_CORE_STREAM_CHUNK_MS=$stream_chunk_ms
SPEECH_CORE_ATT_CONTEXT_RIGHT=$att_context_right
SPEECH_CORE_MODEL_QUEUE_FRAMES=$model_queue_frames
SPEECH_CORE_VAD_MODEL_PATH=$vad_model_path
SPEECH_CORE_VAD_THRESHOLD=$vad_threshold
SPEECH_CORE_VAD_ONSET_FRAMES=$vad_onset_frames
SPEECH_CORE_VAD_HANGOVER_FRAMES=$vad_hangover_frames
SPEECH_CORE_VAD_PRE_SPEECH_FRAMES=$vad_pre_speech_frames
SPEECH_CORE_VAD_EMIT_FRAMES=$vad_emit_frames
SPEECH_CORE_EOU_CHUNK_MS=$eou_chunk_ms
SPEECH_CORE_EOU_RESET_ON_TOKEN=$eou_reset_on_token
SPEECH_CORE_EOU_EMIT_TRANSCRIPT=$eou_emit_transcript
SPEECH_CORE_DETECTOR_QUEUE_FRAMES=$detector_queue_frames
SPEECH_CORE_TURN_VAD_CLOSE_ENABLED=$turn_vad_close_enabled
SPEECH_CORE_TURN_MODEL_EOU_CLOSE_ENABLED=$turn_model_eou_close_enabled
SPEECH_CORE_TURN_MIN_VAD_SPEECH_MS=$turn_min_vad_speech_ms
# Parakeet realtime EOU is intentionally retired from default startup. To re-enable for experiments,
# set SPEECH_CORE_EOU_MODEL_DIR and SPEECH_CORE_TURN_MODEL_EOU_CLOSE_ENABLED=true before install.
SPEECH_CORE_TURN_MIN_MODEL_EOU_SPEECH_MS=$turn_min_model_eou_speech_ms
SPEECH_CORE_TURN_MODEL_EOU_REFRACTORY_MS=$turn_model_eou_refractory_ms
RUST_LOG=speech_core_daemon=info
EOF_ENV

if [[ -n "$eou_model_dir" ]]; then
  echo "SPEECH_CORE_EOU_MODEL_DIR=$eou_model_dir" >>"$config_dir/daemon.env"
fi

cat >"$systemd_user_dir/speech-core-daemon.service" <<EOF_UNIT
[Unit]
Description=Speech Core Nemotron websocket daemon
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=%h/.config/speech-core/daemon.env
ExecStart=%h/.local/bin/speech-core-daemon
Restart=on-failure
RestartSec=2
WorkingDirectory=$repo_root

[Install]
WantedBy=default.target
EOF_UNIT

systemctl --user daemon-reload
systemctl --user enable speech-core-daemon.service
systemctl --user restart speech-core-daemon.service

cat <<EOF_DONE
installed sfub speech-core daemon
  binary:  $bin_dir/speech-core-daemon
  watcher: $bin_dir/speech-core-watch
  file-adapter: $bin_dir/speech-core-file-adapter
  env:     $config_dir/daemon.env
  service: $systemd_user_dir/speech-core-daemon.service
  logs:    $state_dir/logs/events.jsonl

status:
EOF_DONE
systemctl --user --no-pager --full status speech-core-daemon.service || true
