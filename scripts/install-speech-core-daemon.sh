#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
bin_dir="${SPEECH_CORE_INSTALL_BIN_DIR:-$HOME/.local/bin}"
state_dir="${SPEECH_CORE_STATE_DIR:-$HOME/.local/state/speech-core}"
config_dir="${SPEECH_CORE_CONFIG_DIR:-$HOME/.config/speech-core}"
systemd_user_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
model_path="${SPEECH_CORE_MODEL_PATH:-$HOME/workspace/external/transcribe.cpp/models/nemotron-speech-streaming-en-0.6b/nemotron-speech-streaming-en-0.6b-Q4_K_M.gguf}"
bind="${SPEECH_CORE_DAEMON_BIND:-0.0.0.0:8765}"
stream_chunk_ms="${SPEECH_CORE_STREAM_CHUNK_MS:-160}"
att_context_right="${SPEECH_CORE_ATT_CONTEXT_RIGHT:-1}"
model_queue_frames="${SPEECH_CORE_MODEL_QUEUE_FRAMES:-2048}"
vad_model_path="${SPEECH_CORE_VAD_MODEL_PATH:-$HOME/.cache/speech-core/models/silero_vad_v4.onnx}"
vad_threshold="${SPEECH_CORE_VAD_THRESHOLD:-0.5}"
vad_onset_frames="${SPEECH_CORE_VAD_ONSET_FRAMES:-2}"
vad_hangover_frames="${SPEECH_CORE_VAD_HANGOVER_FRAMES:-3}"
vad_pre_speech_frames="${SPEECH_CORE_VAD_PRE_SPEECH_FRAMES:-5}"
vad_emit_frames="${SPEECH_CORE_VAD_EMIT_FRAMES:-false}"
vad_smoothing_alpha="${SPEECH_CORE_VAD_SMOOTHING_ALPHA:-0.1}"
vad_stop_threshold="${SPEECH_CORE_VAD_STOP_THRESHOLD:-0.2}"
vad_fallback_threshold="${SPEECH_CORE_VAD_FALLBACK_THRESHOLD:-0.1}"
vad_acoustic_fallback_silence_ms="${SPEECH_CORE_VAD_ACOUSTIC_FALLBACK_SILENCE_MS:-2500}"
eou_model_dir="${SPEECH_CORE_EOU_MODEL_DIR:-}"
smart_turn_model_path="${SPEECH_CORE_SMART_TURN_MODEL_PATH:-$HOME/workspace/external/smart-turn-v3/smart-turn-v3.2-cpu.onnx}"
smart_turn_threshold="${SPEECH_CORE_SMART_TURN_THRESHOLD:-0.5}"
smart_turn_timeout_ms="${SPEECH_CORE_SMART_TURN_TIMEOUT_MS:-250}"
smart_turn_cpu_count="${SPEECH_CORE_SMART_TURN_CPU_COUNT:-1}"
smart_turn_max_audio_secs="${SPEECH_CORE_SMART_TURN_MAX_AUDIO_SECS:-8}"
smart_turn_pre_speech_ms="${SPEECH_CORE_SMART_TURN_PRE_SPEECH_MS:-500}"
smart_turn_recheck_interval_ms="${SPEECH_CORE_SMART_TURN_RECHECK_INTERVAL_MS:-0}"
smart_turn_recheck_max_attempts="${SPEECH_CORE_SMART_TURN_RECHECK_MAX_ATTEMPTS:-0}"
smart_turn_recheck_offsets_ms="${SPEECH_CORE_SMART_TURN_RECHECK_OFFSETS_MS:-96,192,384,768,1536}"
eou_chunk_ms="${SPEECH_CORE_EOU_CHUNK_MS:-160}"
eou_reset_on_token="${SPEECH_CORE_EOU_RESET_ON_TOKEN:-false}"
eou_emit_transcript="${SPEECH_CORE_EOU_EMIT_TRANSCRIPT:-true}"
detector_queue_frames="${SPEECH_CORE_DETECTOR_QUEUE_FRAMES:-2048}"
turn_vad_close_enabled="${SPEECH_CORE_TURN_VAD_CLOSE_ENABLED:-true}"
turn_semantic_gate_enabled="${SPEECH_CORE_TURN_SEMANTIC_GATE_ENABLED:-true}"
turn_semantic_gate_close_enabled="${SPEECH_CORE_TURN_SEMANTIC_GATE_CLOSE_ENABLED:-true}"
turn_model_eou_close_enabled="${SPEECH_CORE_TURN_MODEL_EOU_CLOSE_ENABLED:-false}"
turn_min_vad_speech_ms="${SPEECH_CORE_TURN_MIN_VAD_SPEECH_MS:-400}"
turn_human_hold_silence_ms="${SPEECH_CORE_TURN_HUMAN_HOLD_SILENCE_MS:-7500}"
turn_transcript_silence_close_ms="${SPEECH_CORE_TURN_TRANSCRIPT_SILENCE_CLOSE_MS:-700}"
turn_min_model_eou_speech_ms="${SPEECH_CORE_TURN_MIN_MODEL_EOU_SPEECH_MS:-300}"
turn_model_eou_refractory_ms="${SPEECH_CORE_TURN_MODEL_EOU_REFRACTORY_MS:-700}"

speech_out_bind="${SPEECH_OUT_DAEMON_BIND:-0.0.0.0:8788}"
speech_out_supertonic_url="${SPEECH_OUT_SUPERTONIC_URL:-http://127.0.0.1:7788/v1/tts}"
speech_out_external_supertonic="${SPEECH_OUT_EXTERNAL_SUPERTONIC:-false}"
speech_out_supertonic_command="${SPEECH_OUT_SUPERTONIC_COMMAND:-$HOME/.cache/speech-out/supertonic-venv/bin/supertonic}"
speech_out_supertonic_args="${SPEECH_OUT_SUPERTONIC_ARGS:-serve --host 127.0.0.1 --port 7788 --model supertonic-3}"
speech_out_supertonic_startup_grace_ms="${SPEECH_OUT_SUPERTONIC_STARTUP_GRACE_MS:-0}"
speech_out_warm_ttl_secs="${SPEECH_OUT_WARM_TTL_SECS:-1200}"
speech_out_timeout_secs="${SPEECH_OUT_TIMEOUT_SECS:-90}"
speech_out_curl_command="${SPEECH_OUT_CURL_COMMAND:-curl}"

mkdir -p "$bin_dir" "$state_dir/logs" "$config_dir" "$systemd_user_dir"

# Bundle Silero VAD model into the local cache so the daemon can find it.
mkdir -p "$HOME/.cache/speech-core/models"
cp "$repo_root/models/silero_vad_v4.onnx" "$HOME/.cache/speech-core/models/"
cd "$repo_root"

cargo build --release -p speech-core-daemon -p speech-core-watch -p speech-core-file-adapter -p speech-out >/dev/null
install -m 0755 target/release/speech-core-daemon "$bin_dir/speech-core-daemon"
install -m 0755 target/release/speech-core-watch "$bin_dir/speech-core-watch"
install -m 0755 target/release/speech-core-file-adapter "$bin_dir/speech-core-file-adapter"
install -m 0755 target/release/speech-out "$bin_dir/speech-out"

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
SPEECH_CORE_VAD_SMOOTHING_ALPHA=$vad_smoothing_alpha
SPEECH_CORE_VAD_STOP_THRESHOLD=$vad_stop_threshold
SPEECH_CORE_VAD_FALLBACK_THRESHOLD=$vad_fallback_threshold
SPEECH_CORE_VAD_ACOUSTIC_FALLBACK_SILENCE_MS=$vad_acoustic_fallback_silence_ms
SPEECH_CORE_EOU_CHUNK_MS=$eou_chunk_ms
SPEECH_CORE_SMART_TURN_THRESHOLD=$smart_turn_threshold
SPEECH_CORE_SMART_TURN_TIMEOUT_MS=$smart_turn_timeout_ms
SPEECH_CORE_SMART_TURN_CPU_COUNT=$smart_turn_cpu_count
SPEECH_CORE_SMART_TURN_MAX_AUDIO_SECS=$smart_turn_max_audio_secs
SPEECH_CORE_SMART_TURN_PRE_SPEECH_MS=$smart_turn_pre_speech_ms
SPEECH_CORE_SMART_TURN_RECHECK_INTERVAL_MS=$smart_turn_recheck_interval_ms
SPEECH_CORE_SMART_TURN_RECHECK_MAX_ATTEMPTS=$smart_turn_recheck_max_attempts
SPEECH_CORE_SMART_TURN_RECHECK_OFFSETS_MS=$smart_turn_recheck_offsets_ms
SPEECH_CORE_EOU_RESET_ON_TOKEN=$eou_reset_on_token
SPEECH_CORE_EOU_EMIT_TRANSCRIPT=$eou_emit_transcript
SPEECH_CORE_DETECTOR_QUEUE_FRAMES=$detector_queue_frames
SPEECH_CORE_TURN_VAD_CLOSE_ENABLED=$turn_vad_close_enabled
SPEECH_CORE_TURN_SEMANTIC_GATE_ENABLED=$turn_semantic_gate_enabled
SPEECH_CORE_TURN_SEMANTIC_GATE_CLOSE_ENABLED=$turn_semantic_gate_close_enabled
SPEECH_CORE_TURN_MODEL_EOU_CLOSE_ENABLED=$turn_model_eou_close_enabled
SPEECH_CORE_TURN_MIN_VAD_SPEECH_MS=$turn_min_vad_speech_ms
SPEECH_CORE_TURN_HUMAN_HOLD_SILENCE_MS=$turn_human_hold_silence_ms
SPEECH_CORE_TURN_TRANSCRIPT_SILENCE_CLOSE_MS=$turn_transcript_silence_close_ms
# Parakeet realtime EOU is intentionally retired from default startup. To re-enable for experiments,
# set SPEECH_CORE_EOU_MODEL_DIR and SPEECH_CORE_TURN_MODEL_EOU_CLOSE_ENABLED=true before install.
SPEECH_CORE_TURN_MIN_MODEL_EOU_SPEECH_MS=$turn_min_model_eou_speech_ms
SPEECH_CORE_TURN_MODEL_EOU_REFRACTORY_MS=$turn_model_eou_refractory_ms
OMP_NUM_THREADS=1
OMP_WAIT_POLICY=PASSIVE
RUST_LOG=speech_core_daemon=info
EOF_ENV

if [[ -n "$eou_model_dir" ]]; then
  echo "SPEECH_CORE_EOU_MODEL_DIR=$eou_model_dir" >>"$config_dir/daemon.env"
fi
if [[ -n "$smart_turn_model_path" ]]; then
  echo "SPEECH_CORE_SMART_TURN_MODEL_PATH=$smart_turn_model_path" >>"$config_dir/daemon.env"
fi

cat >"$config_dir/speech-out.env" <<EOF_ENV
SPEECH_OUT_DAEMON_BIND=$speech_out_bind
SPEECH_OUT_SUPERTONIC_URL=$speech_out_supertonic_url
SPEECH_OUT_EXTERNAL_SUPERTONIC=$speech_out_external_supertonic
SPEECH_OUT_SUPERTONIC_COMMAND=$speech_out_supertonic_command
SPEECH_OUT_SUPERTONIC_ARGS=$speech_out_supertonic_args
SPEECH_OUT_SUPERTONIC_STARTUP_GRACE_MS=$speech_out_supertonic_startup_grace_ms
SPEECH_OUT_WARM_TTL_SECS=$speech_out_warm_ttl_secs
SPEECH_OUT_TIMEOUT_SECS=$speech_out_timeout_secs
SPEECH_OUT_CURL_COMMAND=$speech_out_curl_command
RUST_LOG=speech_out=info
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

cat >"$systemd_user_dir/speech-out-daemon.service" <<EOF_UNIT
[Unit]
Description=Speech Out Supertonic websocket daemon
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=%h/.config/speech-core/speech-out.env
ExecStart=%h/.local/bin/speech-out daemon
Restart=on-failure
RestartSec=2
WorkingDirectory=$repo_root

[Install]
WantedBy=default.target
EOF_UNIT

systemctl --user daemon-reload
systemctl --user enable speech-core-daemon.service speech-out-daemon.service
systemctl --user restart speech-core-daemon.service speech-out-daemon.service

cat <<EOF_DONE
installed speech-core + speech-out daemons
  speech-core:  $bin_dir/speech-core-daemon
  speech-out:   $bin_dir/speech-out
  watcher:      $bin_dir/speech-core-watch
  file-adapter: $bin_dir/speech-core-file-adapter
  core env:     $config_dir/daemon.env
  out env:      $config_dir/speech-out.env
  core service: $systemd_user_dir/speech-core-daemon.service
  out service:  $systemd_user_dir/speech-out-daemon.service
  logs:         $state_dir/logs/events.jsonl

status:
EOF_DONE
systemctl --user --no-pager --full status speech-core-daemon.service || true

systemctl --user --no-pager --full status speech-out-daemon.service || true
