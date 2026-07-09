#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
bin_dir="${SPEECH_CORE_INSTALL_BIN_DIR:-$HOME/.local/bin}"
libexec_dir="${SPEECH_CORE_LIBEXEC_DIR:-$HOME/.local/libexec/speech-core}"
state_dir="${SPEECH_CORE_STATE_DIR:-$HOME/.local/state/speech-core}"
config_dir="${SPEECH_CORE_CONFIG_DIR:-$HOME/.config/speech-core}"
systemd_user_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
ws_url="${SPEECH_CORE_WS_URL:-ws://100.68.60.39:8765/ws/audio-ingress}"
stream_id="${SPEECH_CORE_STREAM_ID:-sfnix.live_mic}"
adapter_id="${SPEECH_CORE_ADAPTER_ID:-sfnix.cpal.default}"
sample_rate_hz="${SPEECH_CORE_SAMPLE_RATE_HZ:-16000}"
channels="${SPEECH_CORE_CHANNELS:-1}"
format="${SPEECH_CORE_FORMAT:-pcm-s16-le}"
frame_ms="${SPEECH_CORE_FRAME_MS:-20}"
watch_verbose="${SPEECH_CORE_WATCH_VERBOSE:-0}"
out_ws_url="${SPEECH_OUT_WS_URL:-ws://100.68.60.39:8788/ws/speech-out}"
out_steps="${SPEECH_OUT_STEPS:-5}"
out_speed="${SPEECH_OUT_SPEED:-1.30}"
out_voice="${SPEECH_OUT_VOICE:-M1}"
out_lang="${SPEECH_OUT_LANG:-en}"
out_play_command="${SPEECH_OUT_PLAY_COMMAND:-pw-play}"
core_device="${SPEECH_CORE_DEVICE:-}"
out_echo_suppress="${SPEECH_OUT_ECHO_SUPPRESS:-1}"
out_echo_suppress_secs="${SPEECH_OUT_ECHO_SUPPRESS_SECS:-2}"

mkdir -p "$bin_dir" "$libexec_dir" "$state_dir" "$config_dir" "$systemd_user_dir"
cd "$repo_root"

rm -rf target/debug
nix-shell --run 'cargo build -p speech-core-mic-adapter -p speech-core-watch -p speech-out' >/dev/null
install -m 0755 target/debug/speech-core-mic-adapter "$libexec_dir/speech-core-mic-adapter"
install -m 0755 target/debug/speech-core-watch "$libexec_dir/speech-core-watch"
install -m 0755 target/debug/speech-out "$libexec_dir/speech-out"
install -m 0755 scripts/sfnix-live-session.sh "$bin_dir/speech-core-live-session"
install -m 0755 scripts/speech-out-live-session.sh "$bin_dir/speech-out-live-session"
install -m 0755 scripts/speech-core-dictation-run.sh "$bin_dir/speech-core-dictation-run"
install -m 0755 scripts/speech-core-dictation-toggle.sh "$bin_dir/speech-core-dictation-toggle"

cat >"$config_dir/client.env" <<EOF_ENV
SPEECH_CORE_WS_URL=$ws_url
SPEECH_CORE_STREAM_ID=$stream_id
SPEECH_CORE_ADAPTER_ID=$adapter_id
SPEECH_CORE_SAMPLE_RATE_HZ=$sample_rate_hz
SPEECH_CORE_CHANNELS=$channels
SPEECH_CORE_FORMAT=$format
SPEECH_CORE_FRAME_MS=$frame_ms
SPEECH_CORE_DEVICE=$core_device
SPEECH_CORE_WATCH_VERBOSE=$watch_verbose
SPEECH_CORE_RUN_DIR=$state_dir/session
SPEECH_OUT_WS_URL=$out_ws_url
SPEECH_OUT_STEPS=$out_steps
SPEECH_OUT_SPEED=$out_speed
SPEECH_OUT_VOICE=$out_voice
SPEECH_OUT_LANG=$out_lang
SPEECH_OUT_PLAY_COMMAND=$out_play_command
SPEECH_OUT_ECHO_SUPPRESS=$out_echo_suppress
SPEECH_OUT_ECHO_SUPPRESS_SECS=$out_echo_suppress_secs
EOF_ENV

cat >"$bin_dir/speech-core-watch" <<'EOF_WATCH'
#!/usr/bin/env bash
set -euo pipefail
env_file="${SPEECH_CORE_CONFIG_FILE:-$HOME/.config/speech-core/client.env}"
if [[ -f "$env_file" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$env_file"
  set +a
fi
exec "${SPEECH_CORE_LIBEXEC_DIR:-$HOME/.local/libexec/speech-core}/speech-core-watch" "$@"
EOF_WATCH
chmod 0755 "$bin_dir/speech-core-watch"

cat >"$bin_dir/speech-core-mic-adapter" <<'EOF_ADAPTER'
#!/usr/bin/env bash
set -euo pipefail
env_file="${SPEECH_CORE_CONFIG_FILE:-$HOME/.config/speech-core/client.env}"
if [[ -f "$env_file" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$env_file"
  set +a
fi
exec "${SPEECH_CORE_LIBEXEC_DIR:-$HOME/.local/libexec/speech-core}/speech-core-mic-adapter" "$@"
EOF_ADAPTER
chmod 0755 "$bin_dir/speech-core-mic-adapter"

cat >"$bin_dir/speech-out" <<'EOF_SPEECH_OUT'
#!/usr/bin/env bash
set -euo pipefail
env_file="${SPEECH_CORE_CONFIG_FILE:-$HOME/.config/speech-core/client.env}"
if [[ -f "$env_file" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$env_file"
  set +a
fi
exec "${SPEECH_CORE_LIBEXEC_DIR:-$HOME/.local/libexec/speech-core}/speech-out" "$@"
EOF_SPEECH_OUT
chmod 0755 "$bin_dir/speech-out"

cat >"$systemd_user_dir/speech-core-mic-adapter.service" <<EOF_UNIT
[Unit]
Description=Speech Core CPAL microphone adapter
After=network-online.target sound.target pipewire.service pipewire-pulse.service
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=%h/.config/speech-core/client.env
ExecStart=%h/.local/libexec/speech-core/speech-core-mic-adapter
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
EOF_UNIT

cat <<EOF_DONE
installed sfnix speech-core client
  adapter wrapper: $bin_dir/speech-core-mic-adapter
  watcher wrapper: $bin_dir/speech-core-watch
  speech-out:      $bin_dir/speech-out
  real binaries:   $libexec_dir
  live session:    $bin_dir/speech-core-live-session
  speech-out dev:  $bin_dir/speech-out-live-session
  dictation:       $bin_dir/speech-core-dictation-toggle
  env:             $config_dir/client.env
  service:         $systemd_user_dir/speech-core-mic-adapter.service

interactive command:
  speech-core-live-session

standalone commands now source $config_dir/client.env:
  speech-core-watch
  speech-core-mic-adapter --frames 5
  speech-out play "heard you."

service control, if/when you want always-on mic streaming:
  systemctl --user enable --now speech-core-mic-adapter.service
  systemctl --user status speech-core-mic-adapter.service
EOF_DONE
