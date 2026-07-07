#!/usr/bin/env bash
set -euo pipefail

env_file="${SPEECH_CORE_CONFIG_FILE:-$HOME/.config/speech-core/client.env}"
# config file provides defaults; explicit environment variables should win.
incoming_ws_url="${SPEECH_CORE_WS_URL:-}"
incoming_stream_id="${SPEECH_CORE_STREAM_ID:-}"
incoming_stream_session_id="${SPEECH_CORE_STREAM_SESSION_ID:-}"
incoming_watch_verbose="${SPEECH_CORE_WATCH_VERBOSE:-}"
incoming_watch_mode="${SPEECH_CORE_WATCH_MODE:-}"
incoming_watch_trace_asr="${SPEECH_CORE_WATCH_TRACE_ASR:-}"
incoming_watch_trace_vad="${SPEECH_CORE_WATCH_TRACE_VAD:-}"
incoming_watch_trace_tokens="${SPEECH_CORE_WATCH_TRACE_TOKENS:-}"
incoming_record_audio="${SPEECH_CORE_RECORD_AUDIO:-}"
incoming_record_wav="${SPEECH_CORE_RECORD_WAV:-}"
if [[ -f "$env_file" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$env_file"
  set +a
fi
if [[ -n "$incoming_ws_url" ]]; then SPEECH_CORE_WS_URL="$incoming_ws_url"; fi
if [[ -n "$incoming_stream_id" ]]; then SPEECH_CORE_STREAM_ID="$incoming_stream_id"; fi
if [[ -n "$incoming_stream_session_id" ]]; then SPEECH_CORE_STREAM_SESSION_ID="$incoming_stream_session_id"; fi
if [[ -n "$incoming_watch_verbose" ]]; then SPEECH_CORE_WATCH_VERBOSE="$incoming_watch_verbose"; fi
if [[ -n "$incoming_watch_mode" ]]; then SPEECH_CORE_WATCH_MODE="$incoming_watch_mode"; fi
if [[ -n "$incoming_watch_trace_asr" ]]; then SPEECH_CORE_WATCH_TRACE_ASR="$incoming_watch_trace_asr"; fi
if [[ -n "$incoming_watch_trace_vad" ]]; then SPEECH_CORE_WATCH_TRACE_VAD="$incoming_watch_trace_vad"; fi
if [[ -n "$incoming_watch_trace_tokens" ]]; then SPEECH_CORE_WATCH_TRACE_TOKENS="$incoming_watch_trace_tokens"; fi
if [[ -n "$incoming_record_audio" ]]; then SPEECH_CORE_RECORD_AUDIO="$incoming_record_audio"; fi
if [[ -n "$incoming_record_wav" ]]; then SPEECH_CORE_RECORD_WAV="$incoming_record_wav"; fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"

bin_pair_runs() {
  local dir="$1"
  [[ -x "$dir/speech-core-mic-adapter" && -x "$dir/speech-core-watch" ]] || return 1
  "$dir/speech-core-mic-adapter" --help >/dev/null 2>&1 || return 1
  "$dir/speech-core-watch" --help >/dev/null 2>&1 || return 1
}

if bin_pair_runs "$script_dir"; then
  bin_dir="$script_dir"
else
  cd "$repo_root"
  if ! bin_pair_runs "$repo_root/target/debug"; then
    echo "building native NixOS client binaries..." >&2
    # Cargo can falsely consider copied foreign-ELF binaries fresh. Remove the
    # bin outputs so the NixOS linker actually relinks them inside nix-shell.
    rm -f target/debug/speech-core-mic-adapter target/debug/speech-core-watch
    nix-shell --run 'cargo build -p speech-core-mic-adapter -p speech-core-watch'
  fi
  if ! bin_pair_runs "$repo_root/target/debug"; then
    echo "speech-core client binaries still do not run after native rebuild" >&2
    command -v readelf >/dev/null 2>&1 && readelf -l "$repo_root/target/debug/speech-core-watch" | grep interpreter >&2 || true
    exit 1
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
watch_mode="${SPEECH_CORE_WATCH_MODE:-tui}"
watch_verbose="${SPEECH_CORE_WATCH_VERBOSE:-0}"
watch_trace_asr="${SPEECH_CORE_WATCH_TRACE_ASR:-0}"
watch_trace_vad="${SPEECH_CORE_WATCH_TRACE_VAD:-0}"
watch_trace_tokens="${SPEECH_CORE_WATCH_TRACE_TOKENS:-0}"
record_audio="${SPEECH_CORE_RECORD_AUDIO:-1}"
device_arg=()
if [[ -n "${SPEECH_CORE_DEVICE:-}" ]]; then
  device_arg=(--device "$SPEECH_CORE_DEVICE")
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --verbose)
      watch_verbose=1
      watch_mode=transcript
      shift
      ;;
    --tui)
      watch_mode=tui
      shift
      ;;
    --debug-tui)
      watch_mode=debug
      shift
      ;;
    --jsonl)
      watch_mode=jsonl
      shift
      ;;
    --trace-asr)
      watch_trace_asr=1
      shift
      ;;
    --trace-vad)
      watch_trace_vad=1
      shift
      ;;
    --trace-tokens)
      watch_trace_tokens=1
      shift
      ;;
    --record-audio)
      record_audio=1
      shift
      ;;
    --no-record-audio)
      record_audio=0
      shift
      ;;
    --mode)
      if [[ $# -lt 2 ]]; then
        echo "missing value for --mode" >&2
        exit 2
      fi
      watch_mode="$2"
      shift 2
      ;;
    --device)
      if [[ $# -lt 2 ]]; then
        echo "missing value for --device" >&2
        exit 2
      fi
      device_arg=(--device "$2")
      shift 2
      ;;
    --help|-h)
      cat <<'EOF_HELP'
usage: speech-core-live-session [--tui] [--debug-tui] [--verbose] [--trace-vad] [--trace-tokens] [--trace-asr] [--jsonl] [--record-audio|--no-record-audio] [--mode tui|debug|transcript|jsonl] [--device NAME]

--record-audio is enabled by default and writes mic.wav in the run dir for replay/debug.
--tui is the default compact symbolic turn surface.
--debug-tui shows the symbolic surface plus recent seam explanations.
--verbose switches to legacy transcript diagnostics.
--trace-vad adds VAD frame diagnostics in transcript mode; requires daemon SPEECH_CORE_VAD_EMIT_FRAMES=true.
--trace-tokens adds per-token ASR commit timing diagnostics in transcript mode.
--trace-asr adds high-volume model chunk timing spam for debugging latency.

env vars still work, e.g. SPEECH_CORE_WATCH_VERBOSE=1, but shell-local assignments need export unless passed inline.
EOF_HELP
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

mkdir -p "$run_dir"
session_id="${SPEECH_CORE_STREAM_SESSION_ID:-$(cat /proc/sys/kernel/random/uuid)}"
export SPEECH_CORE_STREAM_SESSION_ID="$session_id"

echo "speech-core live session"
echo "  ws_url:     $ws_url"
echo "  stream_id:  $stream_id"
echo "  session_id: $session_id"
record_wav="${SPEECH_CORE_RECORD_WAV:-$run_dir/mic.wav}"
record_arg=()
if [[ "$record_audio" == "1" || "$record_audio" == "true" || "$record_audio" == "yes" ]]; then
  record_arg=(--record-wav "$record_wav")
fi

echo "  adapter_id: $adapter_id"
echo "  run_dir:    $run_dir"
if [[ ${#record_arg[@]} -gt 0 ]]; then
  echo "  record_wav: $record_wav"
fi
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
if [[ "$watch_trace_asr" == "1" || "$watch_trace_asr" == "true" || "$watch_trace_asr" == "yes" ]]; then
  watch_args+=(--trace-asr)
fi
if [[ "$watch_trace_vad" == "1" || "$watch_trace_vad" == "true" || "$watch_trace_vad" == "yes" ]]; then
  watch_args+=(--trace-vad)
fi
if [[ "$watch_trace_tokens" == "1" || "$watch_trace_tokens" == "true" || "$watch_trace_tokens" == "yes" ]]; then
  watch_args+=(--trace-tokens)
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
  if [[ ${#record_arg[@]} -gt 0 ]]; then
    echo "  audio_wav:  $record_wav"
    echo "  replay:     speech-core-file-adapter --wav '$record_wav' --url '$ws_url' --stream-id file.replay --stream-session-id replay-$session_id --append-silence-ms 2500"
  fi
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
  "${record_arg[@]}" \
  >"$run_dir/adapter.out" \
  2>"$run_dir/adapter.err" &
adapter_pid=$!

wait "$adapter_pid"
cleanup
trap - INT TERM EXIT
