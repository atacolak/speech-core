#!/usr/bin/env bash
set -euo pipefail

env_file="${SPEECH_CORE_CONFIG_FILE:-$HOME/.config/speech-core/client.env}"
if [[ -f "$env_file" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$env_file"
  set +a
fi

# If launched from sway this is already present. If launched via ssh/systemd,
# reconstruct the Wayland/session env so notify-send and wtype do not faceplant.
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
if command -v systemctl >/dev/null 2>&1; then
  while IFS= read -r line; do
    case "$line" in
      WAYLAND_DISPLAY=*|DISPLAY=*|SWAYSOCK=*|DBUS_SESSION_BUS_ADDRESS=*|XDG_CURRENT_DESKTOP=*)
        export "$line"
        ;;
    esac
  done < <(systemctl --user show-environment 2>/dev/null || true)
fi
if [[ -z "${WAYLAND_DISPLAY:-}" && -S "$XDG_RUNTIME_DIR/wayland-1" ]]; then
  export WAYLAND_DISPLAY=wayland-1
fi
if [[ -z "${SWAYSOCK:-}" ]]; then
  for sock in "$XDG_RUNTIME_DIR"/sway-ipc.*.sock; do
    if [[ -S "$sock" ]]; then
      export SWAYSOCK="$sock"
      break
    fi
  done
fi

libexec_dir="${SPEECH_CORE_LIBEXEC_DIR:-$HOME/.local/libexec/speech-core}"
state_root="${SPEECH_CORE_STATE_DIR:-$HOME/.local/state/speech-core}"
ws_url="${SPEECH_CORE_WS_URL:-}"
stream_id="${SPEECH_CORE_DICTATION_STREAM_ID:-laptop.dictation}"
adapter_id="${SPEECH_CORE_ADAPTER_ID:-laptop.cpal.default}"
sample_rate_hz="${SPEECH_CORE_SAMPLE_RATE_HZ:-16000}"
channels="${SPEECH_CORE_CHANNELS:-1}"
format="${SPEECH_CORE_FORMAT:-pcm-s16-le}"
frame_ms="${SPEECH_CORE_FRAME_MS:-20}"
session_id="${SPEECH_CORE_STREAM_SESSION_ID:-dictation-$(cat /proc/sys/kernel/random/uuid)}"
run_dir="${SPEECH_CORE_DICTATION_RUN_DIR:-$state_root/dictation/session}"
record_wav="${SPEECH_CORE_DICTATION_RECORD_WAV:-$run_dir/mic.wav}"
ledger="$state_root/dictation/ledger.json"
mkdir -p "$run_dir" "$(dirname "$ledger")"

device_arg=()
if [[ -n "${SPEECH_CORE_DEVICE:-}" ]]; then
  device_arg=(--device "$SPEECH_CORE_DEVICE")
fi

write_ledger() {
  local state="$1"
  local extra="${2:-}"
  local now
  now="$(date --iso-8601=seconds)"
  cat >"$ledger" <<EOF_LEDGER
{"state":"$state","updated_at":"$now","session_id":"$session_id","stream_id":"$stream_id","run_dir":"$run_dir","record_wav":"$record_wav","adapter_pid":${adapter_pid:-0},"inject_pid":${inject_pid:-0}$extra}
EOF_LEDGER
}

adapter_pid=""
inject_pid=""
cleanup() {
  local code=$?
  trap - INT TERM EXIT
  if [[ -n "${adapter_pid:-}" ]]; then
    kill "$adapter_pid" 2>/dev/null || true
    wait "$adapter_pid" 2>/dev/null || true
  fi
  if [[ -n "${inject_pid:-}" ]]; then
    kill "$inject_pid" 2>/dev/null || true
    wait "$inject_pid" 2>/dev/null || true
  fi
  write_ledger stopped ",\"exit_code\":$code"
  exit "$code"
}
trap cleanup INT TERM EXIT

notify-send "live transcribe begin" "speech-core dictation is typing into the focused app" || true

"$libexec_dir/speech-core-watch" \
  --url "$ws_url" \
  --stream-id "$stream_id" \
  --stream-session-id "$session_id" \
  --mode inject \
  >"$run_dir/inject.out" \
  2>"$run_dir/inject.err" &
inject_pid=$!

"$libexec_dir/speech-core-mic-adapter" \
  --url "$ws_url" \
  --stream-id "$stream_id" \
  --stream-session-id "$session_id" \
  --adapter-id "$adapter_id" \
  --sample-rate-hz "$sample_rate_hz" \
  --channels "$channels" \
  --format "$format" \
  --frame-ms "$frame_ms" \
  --record-wav "$record_wav" \
  "${device_arg[@]}" \
  >"$run_dir/adapter.out" \
  2>"$run_dir/adapter.err" &
adapter_pid=$!

write_ledger running
wait "$adapter_pid"
cleanup
