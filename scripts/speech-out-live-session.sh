#!/usr/bin/env bash
set -euo pipefail

env_file="${SPEECH_CORE_CONFIG_FILE:-$HOME/.config/speech-core/client.env}"
# Preserve explicit environment values before sourcing defaults.
incoming_core_ws_url="${SPEECH_CORE_WS_URL:-}"
incoming_stream_id="${SPEECH_CORE_STREAM_ID:-}"
incoming_stream_session_id="${SPEECH_CORE_STREAM_SESSION_ID:-}"
incoming_out_ws_url="${SPEECH_OUT_WS_URL:-}"
incoming_steps="${SPEECH_OUT_STEPS:-}"
incoming_speed="${SPEECH_OUT_SPEED:-}"
incoming_voice="${SPEECH_OUT_VOICE:-}"
incoming_lang="${SPEECH_OUT_LANG:-}"
incoming_reference="${SPEECH_OUT_REFERENCE:-}"
incoming_style="${SPEECH_OUT_STYLE:-}"
incoming_play_command="${SPEECH_OUT_PLAY_COMMAND:-}"
incoming_chunk_min_chars="${SPEECH_OUT_CHUNK_MIN_CHARS:-}"
incoming_chunk_max_chars="${SPEECH_OUT_CHUNK_MAX_CHARS:-}"
incoming_watch_mode="${SPEECH_CORE_WATCH_MODE:-}"
incoming_watch_verbose="${SPEECH_CORE_WATCH_VERBOSE:-}"
incoming_watch_trace_asr="${SPEECH_CORE_WATCH_TRACE_ASR:-}"
incoming_watch_trace_vad="${SPEECH_CORE_WATCH_TRACE_VAD:-}"
incoming_watch_trace_tokens="${SPEECH_CORE_WATCH_TRACE_TOKENS:-}"
if [[ -f "$env_file" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$env_file"
  set +a
fi
if [[ -n "$incoming_core_ws_url" ]]; then SPEECH_CORE_WS_URL="$incoming_core_ws_url"; fi
if [[ -n "$incoming_stream_id" ]]; then SPEECH_CORE_STREAM_ID="$incoming_stream_id"; fi
if [[ -n "$incoming_stream_session_id" ]]; then SPEECH_CORE_STREAM_SESSION_ID="$incoming_stream_session_id"; fi
if [[ -n "$incoming_out_ws_url" ]]; then SPEECH_OUT_WS_URL="$incoming_out_ws_url"; fi
if [[ -n "$incoming_steps" ]]; then SPEECH_OUT_STEPS="$incoming_steps"; fi
if [[ -n "$incoming_speed" ]]; then SPEECH_OUT_SPEED="$incoming_speed"; fi
if [[ -n "$incoming_voice" ]]; then SPEECH_OUT_VOICE="$incoming_voice"; fi
if [[ -n "$incoming_lang" ]]; then SPEECH_OUT_LANG="$incoming_lang"; fi
if [[ -n "$incoming_reference" ]]; then SPEECH_OUT_REFERENCE="$incoming_reference"; fi
if [[ -n "$incoming_style" ]]; then SPEECH_OUT_STYLE="$incoming_style"; fi
if [[ -n "$incoming_play_command" ]]; then SPEECH_OUT_PLAY_COMMAND="$incoming_play_command"; fi
if [[ -n "$incoming_chunk_min_chars" ]]; then SPEECH_OUT_CHUNK_MIN_CHARS="$incoming_chunk_min_chars"; fi
if [[ -n "$incoming_chunk_max_chars" ]]; then SPEECH_OUT_CHUNK_MAX_CHARS="$incoming_chunk_max_chars"; fi
if [[ -n "$incoming_watch_mode" ]]; then SPEECH_CORE_WATCH_MODE="$incoming_watch_mode"; fi
if [[ -n "$incoming_watch_verbose" ]]; then SPEECH_CORE_WATCH_VERBOSE="$incoming_watch_verbose"; fi
if [[ -n "$incoming_watch_trace_asr" ]]; then SPEECH_CORE_WATCH_TRACE_ASR="$incoming_watch_trace_asr"; fi
if [[ -n "$incoming_watch_trace_vad" ]]; then SPEECH_CORE_WATCH_TRACE_VAD="$incoming_watch_trace_vad"; fi
if [[ -n "$incoming_watch_trace_tokens" ]]; then SPEECH_CORE_WATCH_TRACE_TOKENS="$incoming_watch_trace_tokens"; fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"

bin_triple_runs() {
  local dir="$1"
  [[ -x "$dir/speech-core-mic-adapter" && -x "$dir/speech-core-watch" && -x "$dir/speech-out" ]] || return 1
  "$dir/speech-core-mic-adapter" --help >/dev/null 2>&1 || return 1
  "$dir/speech-core-watch" --help >/dev/null 2>&1 || return 1
  "$dir/speech-out" --help >/dev/null 2>&1 || return 1
}

if bin_triple_runs "$script_dir"; then
  bin_dir="$script_dir"
else
  cd "$repo_root"
  if ! bin_triple_runs "$repo_root/target/debug"; then
    echo "building native live-session binaries..." >&2
    rm -f target/debug/speech-core-mic-adapter target/debug/speech-core-watch target/debug/speech-out
    if command -v nix-shell >/dev/null 2>&1; then
      nix-shell --run 'cargo build -p speech-core-mic-adapter -p speech-core-watch -p speech-out'
    else
      cargo build -p speech-core-mic-adapter -p speech-core-watch -p speech-out
    fi
  fi
  if ! bin_triple_runs "$repo_root/target/debug"; then
    echo "speech-out live-session binaries still do not run after native rebuild" >&2
    exit 1
  fi
  bin_dir="$repo_root/target/debug"
fi

core_ws_url="${SPEECH_CORE_WS_URL:-ws://100.68.60.39:8765/ws/audio-ingress}"
out_ws_url="${SPEECH_OUT_WS_URL:-ws://100.68.60.39:8788/ws/speech-out}"
stream_id="${SPEECH_CORE_STREAM_ID:-sfnix.live_mic}"
adapter_id="${SPEECH_CORE_ADAPTER_ID:-sfnix.cpal.default}"
sample_rate_hz="${SPEECH_CORE_SAMPLE_RATE_HZ:-16000}"
channels="${SPEECH_CORE_CHANNELS:-1}"
format="${SPEECH_CORE_FORMAT:-pcm-s16-le}"
frame_ms="${SPEECH_CORE_FRAME_MS:-20}"
run_base="${SPEECH_OUT_RUN_BASE:-${SPEECH_CORE_RUN_DIR:-$HOME/.local/state/speech-core/session}}"
record_audio="${SPEECH_CORE_RECORD_AUDIO:-1}"
response_text="${SPEECH_OUT_LIVE_RESPONSE_TEXT:-heard you.}"
steps="${SPEECH_OUT_STEPS:-5}"
speed="${SPEECH_OUT_SPEED:-1.30}"
voice="${SPEECH_OUT_VOICE:-M1}"
lang="${SPEECH_OUT_LANG:-en}"
reference="${SPEECH_OUT_REFERENCE:-}"
style="${SPEECH_OUT_STYLE:-}"
play_command="${SPEECH_OUT_PLAY_COMMAND:-pw-play}"
chunk_min_chars="${SPEECH_OUT_CHUNK_MIN_CHARS:-8}"
chunk_max_chars="${SPEECH_OUT_CHUNK_MAX_CHARS:-160}"
# This harness is diagnostic by design. Mirror the speech-core-live-session --debug-tui surface by default.
watch_mode="${SPEECH_CORE_WATCH_MODE:-debug}"
watch_verbose="${SPEECH_CORE_WATCH_VERBOSE:-0}"
watch_trace_asr="${SPEECH_CORE_WATCH_TRACE_ASR:-0}"
watch_trace_vad="${SPEECH_CORE_WATCH_TRACE_VAD:-0}"
watch_trace_tokens="${SPEECH_CORE_WATCH_TRACE_TOKENS:-0}"
device_arg=()
if [[ -n "${SPEECH_CORE_DEVICE:-}" ]]; then
  # CPAL does not necessarily expose PipeWire/Pulse source names as device names
  # (on sfnix it exposes a generic "pipewire" device). Treat an unavailable
  # configured device as a soft preference, not a hard failure that kills capture.
  if "$bin_dir/speech-core-mic-adapter" --list-devices 2>/dev/null | grep -Fqi -- "$SPEECH_CORE_DEVICE"; then
    device_arg=(--device "$SPEECH_CORE_DEVICE")
  else
    echo "warning: SPEECH_CORE_DEVICE=$SPEECH_CORE_DEVICE is not visible to CPAL; falling back to default input" >&2
  fi
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --steps) steps="$2"; shift 2 ;;
    --speed) speed="$2"; shift 2 ;;
    --voice) voice="$2"; shift 2 ;;
    --lang) lang="$2"; shift 2 ;;
    --reference) reference="$2"; shift 2 ;;
    --style) style="$2"; shift 2 ;;
    --response-text) response_text="$2"; shift 2 ;;
    --core-url) core_ws_url="$2"; shift 2 ;;
    --out-url) out_ws_url="$2"; shift 2 ;;
    --play-command) play_command="$2"; shift 2 ;;
    --chunk-min-chars) chunk_min_chars="$2"; shift 2 ;;
    --chunk-max-chars) chunk_max_chars="$2"; shift 2 ;;
    --debug-tui) watch_mode=debug; shift ;;
    --tui) watch_mode=tui; shift ;;
    --jsonl) watch_mode=jsonl; shift ;;
    --transcript) watch_mode=transcript; shift ;;
    --mode) watch_mode="$2"; shift 2 ;;
    --trace-asr) watch_trace_asr=1; shift ;;
    --trace-vad) watch_trace_vad=1; shift ;;
    --trace-tokens) watch_trace_tokens=1; shift ;;
    --run-dir) run_base="$2"; shift 2 ;;
    --record-audio) record_audio=1; shift ;;
    --no-record-audio) record_audio=0; shift ;;
    --device) device_arg=(--device "$2"); shift 2 ;;
    --help|-h)
      cat <<'EOF_HELP'
usage: speech-out-live-session [--debug-tui|--tui|--transcript|--jsonl|--mode MODE] [--steps N] [--speed X] [--voice ID] [--lang CODE] [--reference REF] [--style STYLE] [--response-text TEXT] [--core-url WS] [--out-url WS] [--play-command CMD] [--device NAME] [--run-dir DIR]

Developer harness: reuse the speech-core live mic/session behavior and the same debug TUI, subscribe to speech-in turn_closed events, then append/trigger a short speech-out response (default: "heard you."). Defaults to --debug-tui because this is the useful diagnostic surface for testing the end-to-end speech loop.

Expected topology: speech-core daemon and speech-out daemon run on sfub/desktop; this laptop/client script streams mic audio and plays speech-out websocket chunks locally.
EOF_HELP
      exit 0
      ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

session_id="${SPEECH_CORE_STREAM_SESSION_ID:-$(cat /proc/sys/kernel/random/uuid)}"
export SPEECH_CORE_STREAM_SESSION_ID="$session_id"
run_dir="${SPEECH_OUT_RUN_DIR:-$run_base/speech-out-$session_id}"
mkdir -p "$run_dir"
record_wav="${SPEECH_CORE_RECORD_WAV:-$run_dir/mic.wav}"
record_arg=()
if [[ "$record_audio" == "1" || "$record_audio" == "true" || "$record_audio" == "yes" ]]; then
  record_arg=(--record-wav "$record_wav")
fi

watch_log="$run_dir/watch.jsonl"
ui_events_log="$run_dir/ui-events.jsonl"
trigger_log="$run_dir/trigger.log"
tts_pid_file="$run_dir/speech-out-play.pid"
tts_expected_file="$run_dir/speech-out-expected.txt"
tts_echo_deadline_file="$run_dir/speech-out-echo-deadline"
echo_suppress_secs="${SPEECH_OUT_ECHO_SUPPRESS_SECS:-2}"
echo_suppress_enabled="${SPEECH_OUT_ECHO_SUPPRESS:-1}"

cat <<EOF_START
speech-out developer live session
  speech-core ws: $core_ws_url
  speech-out ws:  $out_ws_url
  stream_id:      $stream_id
  session_id:     $session_id
  adapter_id:     $adapter_id
  response:       $response_text
  voice/lang:     $voice / $lang
  steps/speed:    $steps / $speed
  watch_mode:     $watch_mode
  run_dir:        $run_dir
  watch_log:      $watch_log
  trigger_log:    $trigger_log
  live params:    $run_dir/params.env
EOF_START
if [[ -n "$reference" ]]; then echo "  reference:      $reference"; fi
if [[ -n "$style" ]]; then echo "  style:          $style"; fi
if [[ ${#record_arg[@]} -gt 0 ]]; then echo "  record_wav:     $record_wav"; fi
echo

quote_sh() {
  printf "%q" "$1"
}

write_params_file() {
  cat >"$run_dir/params.env" <<EOF_PARAMS
# editable fallback values; key controls rewrite this file too.
SPEECH_OUT_LIVE_RESPONSE_TEXT=$(quote_sh "$response_text")
SPEECH_OUT_STEPS=$(quote_sh "$steps")
SPEECH_OUT_SPEED=$(quote_sh "$speed")
SPEECH_OUT_VOICE=$(quote_sh "$voice")
SPEECH_OUT_LANG=$(quote_sh "$lang")
SPEECH_OUT_REFERENCE=$(quote_sh "$reference")
SPEECH_OUT_STYLE=$(quote_sh "$style")
EOF_PARAMS
}

touch "$watch_log" "$ui_events_log" "$trigger_log"
write_params_file

if [[ "${SPEECH_OUT_REAP_STALE:-1}" == "1" || "${SPEECH_OUT_REAP_STALE:-1}" == "true" || "${SPEECH_OUT_REAP_STALE:-1}" == "yes" ]]; then
  # Previous aborted TUI/watch processes are only subscribers, but they still
  # burn terminal/CPU and can make live sessions feel wedged. Reap old watchers
  # for this laptop mic stream before starting a fresh harness.
  while read -r stale_pid; do
    [[ -n "$stale_pid" ]] && kill "$stale_pid" 2>/dev/null || true
  done < <(pgrep -af "speech-core-watch .*--stream-id $stream_id" 2>/dev/null | awk '{print $1}' || true)
fi

event_fifo="$run_dir/ui-events.fifo"
rm -f "$event_fifo"
mkfifo "$event_fifo"

selected_param="speed"
voice_presets=(M1 F1 M2 F2)
param_update_fifo="$run_dir/param-updates.fifo"
rm -f "$param_update_fifo"
mkfifo "$param_update_fifo"

watch_args=(
  --stdin-events
  --speech-out-ui
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

"$bin_dir/speech-core-watch" "${watch_args[@]}" <"$event_fifo" &
ui_pid=$!
exec 3>"$event_fifo"

emit_ui_event() {
  local line="$1"
  printf '%s\n' "$line" >>"$ui_events_log"
  printf '%s\n' "$line" >&3 2>/dev/null || true
}

emit_params_event() {
  emit_ui_event "$(printf '{"event":"speech_out_params_updated","stream_session_id":"%s","selected":"%s","speed":%s,"steps":%s,"voice":"%s"}' \
    "$session_id" "$selected_param" "$speed" "$steps" "$voice")"
}

voice_index() {
  local i
  for i in "${!voice_presets[@]}"; do
    if [[ "${voice_presets[$i]}" == "$voice" ]]; then echo "$i"; return; fi
  done
  echo 0
}

adjust_selected_param() {
  local direction="$1"
  case "$selected_param" in
    speed)
      speed="$(awk -v v="$speed" -v d="$direction" 'BEGIN { v += (d > 0 ? 0.05 : -0.05); if (v < 0.50) v=0.50; if (v > 2.00) v=2.00; printf "%.2f", v }')"
      ;;
    steps)
      if (( direction > 0 )); then steps=$((steps + 1)); else steps=$((steps - 1)); fi
      if (( steps < 1 )); then steps=1; fi
      if (( steps > 20 )); then steps=20; fi
      ;;
    voice)
      local idx next max
      idx="$(voice_index)"
      max=$((${#voice_presets[@]} - 1))
      if (( direction > 0 )); then next=$((idx + 1)); else next=$((idx - 1)); fi
      if (( next < 0 )); then next=$max; fi
      if (( next > max )); then next=0; fi
      voice="${voice_presets[$next]}"
      ;;
  esac
  write_params_file
  emit_params_event
}

select_param() {
  local direction="$1"
  case "$selected_param:$direction" in
    speed:1) selected_param="steps" ;;
    steps:1) selected_param="voice" ;;
    voice:1) selected_param="speed" ;;
    speed:-1) selected_param="voice" ;;
    steps:-1) selected_param="speed" ;;
    voice:-1) selected_param="steps" ;;
  esac
  emit_params_event
}

keyboard_loop() {
  emit_params_event
  local key
  while IFS= read -rsn1 key; do
    case "$key" in
      j) select_param 1 ;;
      k) select_param -1 ;;
      h) adjust_selected_param -1 ;;
      l) adjust_selected_param 1 ;;
      q) kill -INT $$ 2>/dev/null || true; break ;;
    esac
  done
}

# Keep stdin attached to this harness for key controls; event flow uses websockets/fifos.
keyboard_loop &
keyboard_pid=$!

kill_tree() {
  local pid="${1:-}"
  [[ -n "$pid" ]] || return 0
  kill -0 "$pid" 2>/dev/null || return 0
  local child
  while IFS= read -r child; do
    [[ -n "$child" ]] && kill_tree "$child"
  done < <(pgrep -P "$pid" 2>/dev/null || true)
  kill "$pid" 2>/dev/null || true
}

tts_active() {
  [[ -f "$tts_pid_file" ]] || return 1
  local pid
  pid="$(cat "$tts_pid_file" 2>/dev/null || true)"
  [[ -n "$pid" ]] || return 1
  kill -0 "$pid" 2>/dev/null
}

cancel_speech_out() {
  local trigger="${1:-user_speech}"
  if ! tts_active; then
    rm -f "$tts_pid_file"
    return 0
  fi
  local pid
  pid="$(cat "$tts_pid_file" 2>/dev/null || true)"
  echo "[$(date --iso-8601=seconds)] barge-in ($trigger) -> cancel speech-out pid=$pid" >>"$trigger_log"
  emit_ui_event "$(printf '{"event":"speech_out_barge_in","stream_session_id":"%s","trigger":"%s","tts_pid":%s}'     "$session_id" "$trigger" "${pid:-0}")"
  kill_tree "$pid"
  rm -f "$tts_pid_file"
  echo $(( $(date +%s) + echo_suppress_secs )) >"$tts_echo_deadline_file" 2>/dev/null || true
}

normalize_for_echo() {
  printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+//g'
}

echo_guard_active() {
  [[ "$echo_suppress_enabled" == "1" || "$echo_suppress_enabled" == "true" || "$echo_suppress_enabled" == "yes" ]] || return 1
  [[ -s "$tts_expected_file" ]] || return 1
  if tts_active; then return 0; fi
  local deadline now
  deadline="$(cat "$tts_echo_deadline_file" 2>/dev/null || echo 0)"
  now="$(date +%s)"
  [[ "$deadline" =~ ^[0-9]+$ ]] || deadline=0
  (( now <= deadline ))
}

echo_like_text() {
  local heard expected h e extra
  heard="$1"
  expected="$2"
  h="$(normalize_for_echo "$heard")"
  e="$(normalize_for_echo "$expected")"
  [[ ${#h} -ge 4 && ${#e} -ge 4 ]] || return 1
  # Echo may begin at any currently-playing chunk, not just the start of the
  # response. During barge-in, ASR often commits "Your primary lens..." without
  # first committing "heard you". But do not suppress short/common words like
  # "that" or "you"; those are too ambiguous and delayed real barge-in in tests.
  if (( ${#h} >= 12 )) && [[ "$e" == *"$h"* ]]; then return 0; fi
  # Completed expected response, allowing tiny suffix from punctuation/artifact tokens.
  if [[ "$h" == "$e"* ]]; then
    extra=$(( ${#h} - ${#e} ))
    (( extra <= 3 )) && return 0
  fi
  return 1
}

current_tts_expected_text() {
  cat "$tts_expected_file" 2>/dev/null || true
}

is_speech_evidence_text() {
  local text norm
  text="$1"
  norm="$(normalize_for_echo "$text")"
  [[ "$norm" =~ [[:alnum:]] ]]
}

has_non_echo_transcript_evidence() {
  local heard expected h
  heard="$1"
  h="$(normalize_for_echo "$heard")"
  [[ ${#h} -ge 4 ]] || return 1
  if echo_guard_active; then
    expected="$(current_tts_expected_text)"
    if echo_like_text "$heard" "$expected"; then return 1; fi
  fi
  return 0
}

suppress_self_echo_if_needed() {
  local heard expected context
  heard="$1"
  context="${2:-transcript}"
  if ! echo_guard_active; then return 1; fi
  expected="$(current_tts_expected_text)"
  if echo_like_text "$heard" "$expected"; then
    echo "[$(date --iso-8601=seconds)] suppress self-echo ($context): heard=$(printf '%q' "$heard") expected=$(printf '%q' "$expected")" >>"$trigger_log"
    emit_ui_event "$(printf '{"event":"speech_out_echo_suppressed","stream_session_id":"%s","context":"%s"}' "$session_id" "$context")"
    return 0
  fi
  return 1
}

run_speech_out() {
  echo "[$(date --iso-8601=seconds)] turn_closed -> speech-out" >>"$trigger_log"
  local current_response_text="$response_text"
  local current_steps="$steps"
  local current_speed="$speed"
  local current_voice="$voice"
  local current_lang="$lang"
  local current_reference="$reference"
  local current_style="$style"
  if [[ -f "$run_dir/params.env" ]]; then
    # shellcheck disable=SC1090
    source "$run_dir/params.env"
    current_response_text="${SPEECH_OUT_LIVE_RESPONSE_TEXT:-$current_response_text}"
    current_steps="${SPEECH_OUT_STEPS:-$current_steps}"
    current_speed="${SPEECH_OUT_SPEED:-$current_speed}"
    current_voice="${SPEECH_OUT_VOICE:-$current_voice}"
    current_lang="${SPEECH_OUT_LANG:-$current_lang}"
    current_reference="${SPEECH_OUT_REFERENCE:-$current_reference}"
    current_style="${SPEECH_OUT_STYLE:-$current_style}"
  fi
  local play_args=(
    play
    --url "$out_ws_url"
    --voice "$current_voice"
    --lang "$current_lang"
    --steps "$current_steps"
    --speed "$current_speed"
    --play-command "$play_command"
    --chunk-min-chars "$chunk_min_chars"
    --chunk-max-chars "$chunk_max_chars"
  )
  if [[ -n "$current_reference" ]]; then play_args+=(--reference "$current_reference"); fi
  if [[ -n "$current_style" ]]; then play_args+=(--style "$current_style"); fi
  printf '%s\n' "$current_response_text" >"$tts_expected_file"
  echo 0 >"$tts_echo_deadline_file"
  "$bin_dir/speech-out" "${play_args[@]}" "$current_response_text" \
    2> >(while IFS= read -r out_line; do
      printf '%s\n' "$out_line" >>"$trigger_log"
      emit_ui_event "$out_line"
      printf '%s\n' "$out_line" >&2
    done) || true
  local self_pid="${BASHPID:-$$}"
  echo $(( $(date +%s) + echo_suppress_secs )) >"$tts_echo_deadline_file" 2>/dev/null || true
  if [[ "$(cat "$tts_pid_file" 2>/dev/null || true)" == "$self_pid" ]]; then
    rm -f "$tts_pid_file"
  fi
}

json_get_string() {
  local key="$1"
  jq -r --arg key "$key" '.[$key] // ""' 2>/dev/null || true
}

turn_text=""
barge_vad_seen=0
"$bin_dir/speech-core-watch" \
  --url "$core_ws_url" \
  --stream-id "$stream_id" \
  --stream-session-id "$session_id" \
  --mode jsonl \
  | while IFS= read -r line; do
      printf '%s\n' "$line" >>"$watch_log"
      emit_ui_event "$line"
      event="$(printf '%s\n' "$line" | json_get_string event)"
      case "$event" in
        turn_started)
          turn_text=""
          ;;
        transcript_token_committed)
          token="$(printf '%s
' "$line" | json_get_string text)"
          if is_speech_evidence_text "$token"; then
            turn_text+="$token"
            if suppress_self_echo_if_needed "$turn_text" transcript_token_committed; then
              :
            elif tts_active && has_non_echo_transcript_evidence "$turn_text"; then
              # Transcript is authoritative barge-in evidence. The self-echo guard
              # above keeps pure speaker loopback from cancelling TTS, so don't
              # also require VAD: VAD is exactly the component that can lag or miss
              # when the user starts talking over speaker playback.
              cancel_speech_out transcript_token_committed
            elif [[ "$barge_vad_seen" == 1 ]]; then
              cancel_speech_out transcript_token_committed
            fi
          else
            echo "[$(date --iso-8601=seconds)] ignore punctuation-only transcript token: $(printf '%q' "$token")" >>"$trigger_log"
          fi
          ;;
        vad_speech_start)
          barge_vad_seen=1
          ;;
        turn_closed)
          barge_vad_seen=0
          if [[ -z "${turn_text//[[:space:]]/}" ]]; then
            echo "[$(date --iso-8601=seconds)] turn_closed with empty transcript -> skip speech-out" >>"$trigger_log"
            emit_ui_event "$(printf '{"event":"speech_out_skipped","stream_session_id":"%s","reason":"empty_transcript"}' "$session_id")"
          elif suppress_self_echo_if_needed "$turn_text" turn_closed; then
            echo "[$(date --iso-8601=seconds)] turn_closed self-echo -> skip speech-out" >>"$trigger_log"
            emit_ui_event "$(printf '{"event":"speech_out_skipped","stream_session_id":"%s","reason":"self_echo"}' "$session_id")"
          else
            cancel_speech_out new_response
            run_speech_out &
            tts_pid=$!
            printf '%s\n' "$tts_pid" >"$tts_pid_file"
          fi
          turn_text=""
          ;;
      esac
    done &
watch_pid=$!

adapter_pid=""
cleaned_up=0
reap_stale_session_processes() {
  local sid="$1"
  pgrep -af "speech-core-watch .*--stream-session-id $sid" | awk '{print $1}' | while read -r pid; do
    [[ -n "$pid" ]] && kill_tree "$pid"
  done
}
cleanup() {
  if [[ "$cleaned_up" == 1 ]]; then return; fi
  cleaned_up=1
  cancel_speech_out session_end || true
  if [[ -n "${adapter_pid:-}" ]]; then kill_tree "$adapter_pid"; wait "$adapter_pid" 2>/dev/null || true; fi
  sleep "${SPEECH_CORE_FINAL_WAIT_SECS:-1}"
  exec 3>&- 2>/dev/null || true
  kill_tree "${keyboard_pid:-}"
  kill_tree "$watch_pid"
  kill_tree "$ui_pid"
  wait "$watch_pid" "$ui_pid" "${keyboard_pid:-}" 2>/dev/null || true
  reap_stale_session_processes "$session_id" || true
  rm -f "$event_fifo" "$param_update_fifo"
  echo
  echo "session ended"
  echo "  session_id: $session_id"
  echo "  local logs: $run_dir"
  if [[ ${#record_arg[@]} -gt 0 ]]; then
    echo "  audio_wav:  $record_wav"
  fi
}
trap cleanup INT TERM EXIT

"$bin_dir/speech-core-mic-adapter" \
  --url "$core_ws_url" \
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
