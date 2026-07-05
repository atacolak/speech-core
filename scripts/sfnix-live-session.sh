#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -x "$script_dir/speech-core-mic-adapter" && -x "$script_dir/speech-core-watch" ]]; then
  bin_dir="$script_dir"
else
  repo_root="$(cd "$script_dir/.." && pwd)"
  cd "$repo_root"
  if [[ ! -x target/debug/speech-core-mic-adapter || ! -x target/debug/speech-core-watch ]]; then
    nix-shell --run 'cargo build -p speech-core-mic-adapter -p speech-core-watch'
  fi
  bin_dir="$repo_root/target/debug"
fi

ws_url="${SPEECH_CORE_WS_URL:-ws://100.68.60.39:8765/ws/audio-ingress}"
stream_id="${SPEECH_CORE_STREAM_ID:-sfnix.live_mic}"
adapter_id="${SPEECH_CORE_ADAPTER_ID:-sfnix.cpal.default}"
sample_rate_hz="${SPEECH_CORE_SAMPLE_RATE_HZ:-16000}"
channels="${SPEECH_CORE_CHANNELS:-1}"
format="${SPEECH_CORE_FORMAT:-pcm-s16-le}"
frame_ms="${SPEECH_CORE_FRAME_MS:-20}"
run_dir="${SPEECH_CORE_RUN_DIR:-/tmp/speech-core-session-$(date +%Y%m%d-%H%M%S)}"
watch_mode="${SPEECH_CORE_WATCH_MODE:-transcript}"
watch_verbose="${SPEECH_CORE_WATCH_VERBOSE:-1}"
device_arg=()
if [[ -n "${SPEECH_CORE_DEVICE:-}" ]]; then
  device_arg=(--device "$SPEECH_CORE_DEVICE")
fi

mkdir -p "$run_dir"
session_id="${SPEECH_CORE_STREAM_SESSION_ID:-$(cat /proc/sys/kernel/random/uuid)}"
export SPEECH_CORE_STREAM_SESSION_ID="$session_id"

echo "speech-core live session"
echo "  ws_url:     $ws_url"
echo "  stream_id:  $stream_id"
echo "  session_id: $session_id"
echo "  adapter_id: $adapter_id"
echo "  run_dir:    $run_dir"
echo
echo "speak into this machine. press ctrl-c to stop."
echo

adapter_pid=""
watch_pid=""
cleaned_up=0

watch_args=(
  --url "$ws_url"
  --stream-id "$stream_id"
  --stream-session-id "$session_id"
  --mode "$watch_mode"
)
if [[ "$watch_verbose" == "1" || "$watch_verbose" == "true" || "$watch_verbose" == "yes" ]]; then
  watch_args+=(--verbose)
fi

"$bin_dir/speech-core-watch" "${watch_args[@]}" &
watch_pid=$!

cleanup() {
  if [[ "$cleaned_up" == 1 ]]; then
    return
  fi
  cleaned_up=1
  if [[ -n "${adapter_pid:-}" ]]; then
    kill "$adapter_pid" 2>/dev/null || true
    wait "$adapter_pid" 2>/dev/null || true
  fi
  # Give the daemon/model worker time to emit final transcript_update after websocket close/reset.
  sleep "${SPEECH_CORE_FINAL_WAIT_SECS:-4}"
  if [[ -n "${watch_pid:-}" ]]; then
    kill "$watch_pid" 2>/dev/null || true
    wait "$watch_pid" 2>/dev/null || true
  fi
  echo
  echo "session ended"
  echo "  session_id: $session_id"
  echo "  local logs: $run_dir"
}
trap cleanup INT TERM EXIT

"$bin_dir/speech-core-mic-adapter" \
  --url "$ws_url" \
  --stream-id "$stream_id" \
  --stream-session-id "$session_id" \
  --adapter-id "$adapter_id" \
  --sample-rate-hz "$sample_rate_hz" \
  --channels "$channels" \
  --format "$format" \
  --frame-ms "$frame_ms" \
  "${device_arg[@]}" \
  >"$run_dir/adapter.out" \
  2>"$run_dir/adapter.err" &
adapter_pid=$!

wait "$adapter_pid"
cleanup
trap - INT TERM EXIT
