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

mkdir -p "$bin_dir" "$state_dir/logs" "$config_dir" "$systemd_user_dir"
cd "$repo_root"

cargo build -p speech-core-daemon -p speech-core-watch >/dev/null
install -m 0755 target/debug/speech-core-daemon "$bin_dir/speech-core-daemon"
install -m 0755 target/debug/speech-core-watch "$bin_dir/speech-core-watch"

cat >"$config_dir/daemon.env" <<EOF_ENV
SPEECH_CORE_DAEMON_BIND=$bind
SPEECH_CORE_LOG_DIR=$state_dir/logs
SPEECH_CORE_MODEL_PATH=$model_path
SPEECH_CORE_STREAM_CHUNK_MS=$stream_chunk_ms
SPEECH_CORE_ATT_CONTEXT_RIGHT=$att_context_right
SPEECH_CORE_MODEL_QUEUE_FRAMES=$model_queue_frames
RUST_LOG=speech_core_daemon=info
EOF_ENV

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
systemctl --user enable --now speech-core-daemon.service

cat <<EOF_DONE
installed sfub speech-core daemon
  binary:  $bin_dir/speech-core-daemon
  watcher: $bin_dir/speech-core-watch
  env:     $config_dir/daemon.env
  service: $systemd_user_dir/speech-core-daemon.service
  logs:    $state_dir/logs/events.jsonl

status:
EOF_DONE
systemctl --user --no-pager --full status speech-core-daemon.service || true
