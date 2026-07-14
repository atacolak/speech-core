#!/usr/bin/env bash
# shellcheck shell=bash
# Hardened speech-out live session harness.
# See tests/test-speech-out-live-session.sh for deterministic validation.
set -euo pipefail

# ── Clock-domain helpers ────────────────────────────────────────────────
# The TUI event stream spans two clock domains:
#   - Server/daemon events carry `daemon_mono_ns` (server CLOCK_MONOTONIC).
#   - Client/playback events carry `client_mono_ns` (client CLOCK_MONOTONIC).
#
# Harness-emitted events (barge-in, param changes, echo suppression, etc.)
# must NOT pretend to be in either domain by injecting a zero or fake
# timestamp.  Instead, they carry two fields:
#   - `diagnostic_mono_ns`:  harness-local elapsed ns since script start,
#     computed from /proc/uptime (same CLOCK_MONOTONIC as client side).
#   - `diagnostic_clock_origin`:  literal string "harness_local_monotonic"
#     so renderers can display it differently or exclude it from timing
#     calculations that assume a single domain.
#
# Server events pass through with their original `daemon_mono_ns`;
# playback events pass through with their original `client_mono_ns`.

# Snapshot of harness start in monotonic ns (or 0 if unavailable).
_harness_start_ns=0
_harness_start_ns_init() {
  if [[ -r /proc/uptime ]]; then
    local sec nsec
    IFS=' .' read -r sec nsec _ < /proc/uptime 2>/dev/null || true
    if [[ -n "$sec" && "$sec" =~ ^[0-9]+$ ]]; then
      nsec="${nsec:-0}"
      nsec="$(printf '%-09s' "$nsec" | tr ' ' '0')"
      nsec="${nsec:0:9}"
      _harness_start_ns="${sec}$(printf '%09d' "$((10#$nsec))" 2>/dev/null || printf '%09d' 0)"
      return
    fi
  fi
  _harness_start_ns=0
}
_harness_start_ns_init

diagnostic_mono_ns() {
  if (( _harness_start_ns == 0 )); then
    printf '0'
    return
  fi
  local sec nsec now_ns
  if [[ -r /proc/uptime ]]; then
    IFS=' .' read -r sec nsec _ < /proc/uptime 2>/dev/null || true
    if [[ -n "$sec" && "$sec" =~ ^[0-9]+$ ]]; then
      nsec="${nsec:-0}"
      nsec="$(printf '%-09s' "$nsec" | tr ' ' '0')"
      nsec="${nsec:0:9}"
      now_ns="${sec}$(printf '%09d' "$((10#$nsec))" 2>/dev/null || printf '%09d' 0)"
      # Emit elapsed ns since harness start as a bare number (no quotes in JSON).
      printf '%s' "$(( now_ns - _harness_start_ns ))" 2>/dev/null || printf '0'
      return
    fi
  fi
  printf '0'
}

# ── JSON field extraction without per-line jq spawning ──────────────────
# Extracts a string value for a top-level key from a flat-ish JSON object.
# Avoids spawning jq for every event line; falls back to jq for nested objects.
json_get_string() {
  local key="$1" raw input
  if IFS= read -r -d '' input 2>/dev/null || true; then
    :
  else
    input="$(cat)"
  fi
  raw="$input"
  # Attempt fast-path: extract "key":"value" with bash builtins.
  local pat='"'"$key"'"[[:space:]]*:[[:space:]]*"([^"]*)"'
  if [[ "$raw" =~ $pat ]]; then
    printf '%s\n' "${BASH_REMATCH[1]}"
  elif command -v jq >/dev/null 2>&1; then
    printf '%s\n' "$raw" | jq -r --arg k "$key" '.[$k] // ""' 2>/dev/null || true
  else
    printf '\n'
  fi
}

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
incoming_vad_energy_enabled="${SPEECH_CORE_VAD_ENERGY_ENABLED:-}"
incoming_vad_energy_threshold="${SPEECH_CORE_VAD_ENERGY_THRESHOLD:-}"
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
if [[ -n "$incoming_vad_energy_enabled" ]]; then SPEECH_CORE_VAD_ENERGY_ENABLED="$incoming_vad_energy_enabled"; fi
if [[ -n "$incoming_vad_energy_threshold" ]]; then SPEECH_CORE_VAD_ENERGY_THRESHOLD="$incoming_vad_energy_threshold"; fi

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

core_ws_url="${SPEECH_CORE_WS_URL:-}"
out_ws_url="${SPEECH_OUT_WS_URL:-}"
stream_id="${SPEECH_CORE_STREAM_ID:-laptop.live_mic}"
adapter_id="${SPEECH_CORE_ADAPTER_ID:-laptop.cpal.default}"
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
vad_energy_enabled="${SPEECH_CORE_VAD_ENERGY_ENABLED:-0}"
vad_energy_threshold="${SPEECH_CORE_VAD_ENERGY_THRESHOLD:-0.01}"
# This harness is diagnostic by design. Mirror the speech-core-live-session --debug-tui surface by default.
watch_mode="${SPEECH_CORE_WATCH_MODE:-debug}"
watch_verbose="${SPEECH_CORE_WATCH_VERBOSE:-0}"
watch_trace_asr="${SPEECH_CORE_WATCH_TRACE_ASR:-0}"
watch_trace_vad="${SPEECH_CORE_WATCH_TRACE_VAD:-0}"
watch_trace_tokens="${SPEECH_CORE_WATCH_TRACE_TOKENS:-0}"
device_arg=()
if [[ -n "${SPEECH_CORE_DEVICE:-}" ]]; then
  # CPAL does not necessarily expose PipeWire/Pulse source names as device names
  # (on some systems CPAL exposes a generic "pipewire" device). Treat an unavailable
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
    --vad-energy-enabled) vad_energy_enabled=1; shift ;;
    --vad-energy-threshold) vad_energy_threshold="$2"; shift 2 ;;
    --run-dir) run_base="$2"; shift 2 ;;
    --record-audio) record_audio=1; shift ;;
    --no-record-audio) record_audio=0; shift ;;
    --device) device_arg=(--device "$2"); shift 2 ;;
    --help|-h)
      cat <<'EOF_HELP'
usage: speech-out-live-session [--debug-tui|--tui|--transcript|--jsonl|--mode MODE] [--steps N] [--speed X] [--voice ID] [--lang CODE] [--reference REF] [--style STYLE] [--response-text TEXT] [--core-url WS] [--out-url WS] [--play-command CMD] [--device NAME] [--run-dir DIR] [--vad-energy-enabled] [--vad-energy-threshold FLOAT]

Developer harness: reuse the speech-core live mic/session behavior and the same debug TUI, subscribe to speech-in turn_closed events, then append/trigger a short speech-out response (default: "heard you."). Defaults to --debug-tui because this is the useful diagnostic surface for testing the end-to-end speech loop.

Expected topology: speech-core daemon and speech-out daemon run on the server; this laptop/client script streams mic audio and plays speech-out websocket chunks locally.

For deterministic output-only diagnostics that bypass mic/VAD/ASR and canned turn replies, use:
  scripts/speech-out-diagnostics.py mock --fixture chunked --jsonl-out /tmp/speech-out.jsonl
  cargo run -p speech-core-watch -- --replay-events /tmp/speech-out.jsonl --speech-out-ui --mode debug
  scripts/speech-out-diagnostics.py run --fixture chunked --url ws://<server>:8788/ws/speech-out
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
# Wall-clock ms when current speech-out play child was spawned (for barge cut).
assistant_play_start_ms_file="$run_dir/assistant_play_start_ms"
assistant_play_start_ms=0
# Frozen at first barge: played duration used for approx word cut (do not re-measure later).
assistant_barge_played_ms_file="$run_dir/assistant_barge_played_ms"
echo_suppress_secs="${SPEECH_OUT_ECHO_SUPPRESS_SECS:-2}"
echo_suppress_enabled="${SPEECH_OUT_ECHO_SUPPRESS:-1}"

# Assistant self-ASR (Nemotron B) — real path inside this live session only.
# Tee captures WAV chunks that were actually handed to the local player.
# Dual-instance assistant self-ASR (optional): feed teed assistant audio into a
# SECOND speech-core-daemon process (default ws port 8766), not the mic daemon.
# Nemotron B dual-instance: OFF by default (too heavy on CPU; mic starvation).
# Cut path is cheap forced align (CTC) or wall-clock — not a second Nemotron.
assistant_self_asr_enabled="${SPEECH_OUT_ASSISTANT_SELF_ASR:-0}"
assistant_capture_dir="$run_dir/assistant_self_asr/played_chunks"
assistant_manifest="$run_dir/assistant_self_asr/played_manifest.jsonl"
assistant_b_watch_log="$run_dir/assistant_self_asr/b_watch.jsonl"
assistant_cut_dir="$run_dir/assistant_self_asr"
assistant_pad_words="${SPEECH_OUT_ASSISTANT_CUT_PAD_WORDS:-2}"
# Align backend: ctc_forced (default) | approx_wallclock
# ctc uses played audio + intended text (torchaudio MMS_FA). Not text-only.
align_backend="${SPEECH_OUT_ALIGN_BACKEND:-ctc_forced}"
align_python="${SPEECH_CORE_ALIGN_PYTHON:-}"
align_remote="${SPEECH_OUT_ALIGN_REMOTE:-auto}"  # auto|1|0 — ssh to core host if no local torch
align_script=""
assistant_b_feed_pid=""
assistant_b_watch_pid=""
assistant_cut_gen=0
assistant_active_gen=""
# B daemon URL: separate Nemotron process. Override with SPEECH_OUT_ASSISTANT_SELF_ASR_WS_URL.
assistant_self_asr_ws_url="${SPEECH_OUT_ASSISTANT_SELF_ASR_WS_URL:-}"
if [[ -z "$assistant_self_asr_ws_url" && -n "${core_ws_url:-}" ]]; then
  # Derive :8766 from primary :8765 (or any host) for the second instance.
  assistant_self_asr_ws_url="$(printf '%s' "$core_ws_url" | sed -E 's/:(8765)(\/|$)/:8766\2/; t; s|/$||; s|$|:8766|')"
  # If sed didn't change a 8765 port, force host swap more carefully:
  if [[ "$assistant_self_asr_ws_url" == "$core_ws_url" ]]; then
    assistant_self_asr_ws_url="$(printf '%s' "$core_ws_url" | sed -E 's#^(ws://[^/:]+):[0-9]+#\1:8766#')"
  fi
fi

# Helpers may live next to this script (~/.local/bin) or under libexec when
# installed via client deploy (not a full git checkout on the laptop).
libexec_dir="${SPEECH_CORE_LIBEXEC_DIR:-$HOME/.local/libexec/speech-core}"
helper_dir="${SPEECH_CORE_HELPER_DIR:-$script_dir}"
if [[ ! -f "$helper_dir/speech-out-tee-play.sh" && -f "$libexec_dir/speech-out-tee-play.sh" ]]; then
  helper_dir="$libexec_dir"
fi
# CTC / aligner package (scripts/barge_in_align)
align_script=""
for cand in \
  "$helper_dir/barge_in_align/run_align.py" \
  "$libexec_dir/barge_in_align/run_align.py" \
  "$script_dir/barge_in_align/run_align.py" \
  "$repo_root/scripts/barge_in_align/run_align.py"; do
  if [[ -f "$cand" ]]; then align_script="$cand"; break; fi
done
if [[ -z "$align_python" ]]; then
  for cand in \
    "$HOME/workspace/.venvs/pyannote-cpu/bin/python" \
    /home/sf/workspace/.venvs/pyannote-cpu/bin/python \
    "${SPEECH_CORE_PYTHON3:-}" \
    "$PYTHON3_BIN"; do
    [[ -n "$cand" && -x "$cand" ]] || continue
    align_python="$cand"
    break
  done
fi
if [[ -z "$align_python" ]]; then
  align_python="${PYTHON3_BIN:-python3}"
fi

dual_asr_dir="${SPEECH_CORE_DUAL_ASR_DIR:-}"

if [[ -z "$dual_asr_dir" ]]; then
  if [[ -d "$helper_dir/barge-in-dual-asr" ]]; then
    dual_asr_dir="$helper_dir/barge-in-dual-asr"
  elif [[ -d "$script_dir/barge-in-dual-asr" ]]; then
    dual_asr_dir="$script_dir/barge-in-dual-asr"
  else
    dual_asr_dir="$libexec_dir/barge-in-dual-asr"
  fi
fi
tee_play_script="$helper_dir/speech-out-tee-play.sh"
finalize_cut_py="$helper_dir/assistant-self-asr-finalize-cut.py"
if [[ ! -f "$finalize_cut_py" && -f "$libexec_dir/assistant-self-asr-finalize-cut.py" ]]; then
  finalize_cut_py="$libexec_dir/assistant-self-asr-finalize-cut.py"
fi
if [[ ! -x "$tee_play_script" && -f "$tee_play_script" ]]; then
  chmod +x "$tee_play_script" 2>/dev/null || true
fi
if [[ -f "$finalize_cut_py" ]]; then
  chmod +x "$finalize_cut_py" 2>/dev/null || true
fi

# NixOS laptops often have no `python3` on bare PATH; resolve a real interpreter.
resolve_python3() {
  if command -v python3 >/dev/null 2>&1; then
    command -v python3
    return 0
  fi
  local cand
  for cand in \
    "${SPEECH_CORE_PYTHON3:-}" \
    "$HOME/.nix-profile/bin/python3" \
    /etc/profiles/per-user/"$USER"/bin/python3 \
    /run/current-system/sw/bin/python3; do
    [[ -n "$cand" && -x "$cand" ]] || continue
    printf '%s' "$cand"
    return 0
  done
  # Last resort: any python3 in the nix store (first match).
  cand="$(ls -1 /nix/store/*python3*-env/bin/python3 2>/dev/null | head -n1 || true)"
  if [[ -n "$cand" && -x "$cand" ]]; then
    printf '%s' "$cand"
    return 0
  fi
  return 1
}
PYTHON3_BIN="$(resolve_python3 || true)"

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
  vad_energy:     enabled=$vad_energy_enabled threshold=$vad_energy_threshold (server daemon must be restarted with these env vars to take effect)
  assistant_cut:  backend=$align_backend (nemotron_B=$assistant_self_asr_enabled) align_py=${align_python:-n/a} script=${align_script:-n/a}
  run_dir:        $run_dir
  watch_log:      $watch_log
  trigger_log:    $trigger_log
  live params:    $run_dir/params.env
EOF_START
mkdir -p "$assistant_capture_dir" "$assistant_cut_dir"
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
# Server-side VAD energy gate config. These only take effect if the server
# speech-core-daemon is restarted with the corresponding env vars.
SPEECH_CORE_VAD_ENERGY_ENABLED=$(quote_sh "$vad_energy_enabled")
SPEECH_CORE_VAD_ENERGY_THRESHOLD=$(quote_sh "$vad_energy_threshold")
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
  emit_ui_event "$(printf '{"event":"speech_out_params_updated","diagnostic_mono_ns":%s,"diagnostic_clock_origin":"harness_local_monotonic","stream_session_id":"%s","selected":"%s","speed":%s,"steps":%s,"voice":"%s"}' \
    "$(diagnostic_mono_ns)" "$session_id" "$selected_param" "$speed" "$steps" "$voice")"
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

# Global TTY restore state. keyboard_loop puts the tty into raw/-echo;
# if that background job is killed (session failure, Ctrl-C, watch disconnect),
# it never restores. cleanup() always restores from these globals.
_tty_saved_stty=""
_tty_restore_fd=""

restore_tty() {
  # Always try to leave the user's terminal usable after this harness.
  # Prefer the saved mode; fall back to sane cooked settings.
  local fd="${_tty_restore_fd:-/dev/tty}"
  if [[ -n "${_tty_saved_stty:-}" ]]; then
    stty "$_tty_saved_stty" <"$fd" 2>/dev/null || stty sane <"$fd" 2>/dev/null || true
  else
    stty sane <"$fd" 2>/dev/null || true
  fi
  # Also reset common alternate-screen / cursor mess from TUI watchers.
  if [[ -w "$fd" ]]; then
    printf '\033[?25h\033[0m\033[?1049l' >"$fd" 2>/dev/null || true
  fi
  # Best-effort even if fd is wrong.
  stty sane 2>/dev/null || true
}

keyboard_loop() {
  local input_fd="$1"
  if [[ ! -r "$input_fd" ]]; then
    echo "keyboard: no readable input fd $input_fd — controls disabled" >&2
    return 0
  fi
  emit_params_event
  local key
  # Save terminal settings so we can restore on exit (also stored globally
  # so cleanup can restore if this job is killed mid-loop).
  local saved_stty
  saved_stty="$(stty -g <"$input_fd" 2>/dev/null || true)"
  if [[ -n "$saved_stty" ]]; then
    _tty_saved_stty="$saved_stty"
    _tty_restore_fd="$input_fd"
    stty raw -echo min 1 time 0 <"$input_fd" 2>/dev/null || true
  fi
  # Ensure restore even if this background job is killed via EXIT/INT/TERM.
  trap 'restore_tty' EXIT INT TERM
  while IFS= read -rsn1 -t 0.25 key <"$input_fd" 2>/dev/null; do
    [[ -z "$key" ]] && continue
    case "$key" in
      j) select_param 1 ;;
      k) select_param -1 ;;
      h) adjust_selected_param -1 ;;
      l) adjust_selected_param 1 ;;
      q) kill -INT $$ 2>/dev/null || true; break ;;
      $'\x03') kill -INT $$ 2>/dev/null || true; break ;;  # Ctrl-C
    esac
  done
  restore_tty
  trap - EXIT INT TERM
}

# Resolve the best keyboard input file descriptor.
# Prefer /dev/tty for interactive sessions; fall back to stdin if it is a terminal;
# otherwise leave keyboard disabled.
_keyboard_fd=""
# The subshell owns both the redirection and its diagnostic output. Without
# this wrapper, bash itself reports `/dev/tty: No such device or address`
# before `stty`'s redirection is applied when no controlling terminal exists.
if [[ -r /dev/tty && -c /dev/tty ]] && (stty -g </dev/tty) &>/dev/null; then
  _keyboard_fd=/dev/tty
elif [[ -t 0 ]]; then
  _keyboard_fd=/dev/stdin
fi
if [[ -n "$_keyboard_fd" ]]; then
  # Capture cooked mode in the parent *before* the background job flips raw.
  _tty_saved_stty="$(stty -g <"$_keyboard_fd" 2>/dev/null || true)"
  _tty_restore_fd="$_keyboard_fd"
  keyboard_loop "$_keyboard_fd" &
  keyboard_pid=$!
else
  keyboard_pid=""
  if [[ "${SPEECH_OUT_NO_TTY_OK:-0}" != "1" && "${SPEECH_OUT_NO_TTY_OK:-0}" != "true" && "${SPEECH_OUT_NO_TTY_OK:-0}" != "yes" ]]; then
    echo "keyboard: no tty available — j/k/h/l/q controls disabled (set SPEECH_OUT_NO_TTY_OK=1 to suppress this message)" >&2
  fi
fi

# ── Process management ──────────────────────────────────────────────────

_kill_tree_timeout_secs="${SPEECH_OUT_KILL_TIMEOUT_SECS:-3}"

# Collect all PIDs in the descendant tree rooted at $1 (including $1).
# Returns space-separated list, children before parents, so a simple
# left-to-right iteration sends TERM to leaves first.
# Uses pgrep -P and DFS-style stack iteration; capped at _COLLECT_MAX_NODES
# to prevent runaway walks on huge trees (e.g. accidental PID 1).
_COLLECT_MAX_NODES=256
_collect_descendants() {
  local root="$1"
  local -A seen=()
  local stack=("$root")
  local all=()
  local max="${_COLLECT_MAX_NODES:-256}"
  local p children child
  while ((${#stack[@]} > 0 && ${#all[@]} < max)); do
    p="${stack[-1]}"
    unset 'stack[-1]'
    [[ -n "${seen[$p]:-}" ]] && continue
    seen[$p]=1
    all+=("$p")
    children="$(pgrep -P "$p" 2>/dev/null || true)"
    for child in $children; do
      [[ -n "$child" && -z "${seen[$child]:-}" ]] && stack+=("$child")
    done
  done
  # Reverse for leaf-first ordering.
  local idx
  for ((idx = ${#all[@]} - 1; idx >= 0; idx--)); do
    printf '%s ' "${all[$idx]}"
  done
}

# ── PID liveness helpers (zombie-aware) ──────────────────────────────

# True if PID exists as a zombie (defunct, waiting for parent reap).
# Zombies cannot be signalled, but they are effectively dead.
_pid_is_zombie() {
  local p="$1"
  [[ -n "$p" ]] || return 1
  local state
  state="$(awk '/^State:/ {print $2}' /proc/"$p"/status 2>/dev/null || true)"
  [[ "$state" == "Z" || "$state" == "z" ]]
}

# True if PID exists AND is not a zombie (i.e. it is actually running).
_pid_is_alive() {
  local p="$1"
  [[ -n "$p" ]] || return 1
  kill -0 "$p" 2>/dev/null || return 1
  _pid_is_zombie "$p" && return 1
  return 0
}

# Capture /proc starttime for PID reuse defence (field 22 of stat).
_pid_starttime() {
  local p="$1"
  awk '{print $22}' /proc/"$p"/stat 2>/dev/null || printf '0'
}

kill_tree() {
  local pid="${1:-}"
  [[ -n "$pid" ]] || return 0
  kill -0 "$pid" 2>/dev/null || return 0

  # Collect every PID in the descendant tree, deepest first.
  local pids
  pids="$(_collect_descendants "$pid")"

  # Capture start times for PID reuse safety.
  local -A _kt_starttimes=()
  local p
  for p in $pids; do
    _kt_starttimes[$p]="$(_pid_starttime "$p")"
  done

  # Phase 1: Send SIGTERM to every PID (leaf-first order).
  for p in $pids; do
    kill -TERM "$p" 2>/dev/null || true
  done

  # Phase 2: Wait for ALL captured PIDs to be dead or zombie.
  # Unlike the previous implementation that returned as soon as the
  # root exited, we now hold until every captured descendant is gone.
  # This closes the escalation hole where a TERM-ignoring descendant
  # survived because the root exited first.
  local waited=0 max_ticks=$(( _kill_tree_timeout_secs * 10 ))
  local all_dead=0
  while (( waited < max_ticks )); do
    all_dead=1
    for p in $pids; do
      if _pid_is_alive "$p"; then
        all_dead=0
        break
      fi
    done
    (( all_dead )) && break
    sleep 0.1 2>/dev/null || true
    waited=$((waited + 1))
  done

  if (( all_dead )); then
    wait "$pid" 2>/dev/null || true
    return 0
  fi

  # Phase 3: Escalate — KILL every survivor.
  # Verify starttime hasn't changed to avoid killing a reused PID.
  for p in $pids; do
    if _pid_is_alive "$p"; then
      local cur_st
      cur_st="$(_pid_starttime "$p")"
      if [[ -n "${_kt_starttimes[$p]:-}" && "${_kt_starttimes[$p]}" != "0" && \
            -n "$cur_st" && "$cur_st" != "0" && \
            "${_kt_starttimes[$p]}" != "$cur_st" ]]; then
        # PID was reused; don't kill the new occupant.
        continue
      fi
      kill -KILL "$p" 2>/dev/null || true
    fi
  done

  # Phase 4: Bounded verify — a brief poll to confirm KILL took effect.
  local verify_waited=0 verify_max=20  # 2 seconds
  while (( verify_waited < verify_max )); do
    all_dead=1
    for p in $pids; do
      if _pid_is_alive "$p"; then
        all_dead=0
        break
      fi
    done
    (( all_dead )) && break
    sleep 0.1 2>/dev/null || true
    verify_waited=$((verify_waited + 1))
  done

  # Best-effort reap: only succeeds when we are the direct parent.
  # When called from a different shell context (e.g. cleanup in main
  # script targeting a child of the while-pipeline subshell), the
  # process is reaped by its real parent or reparented to init.
  wait "$pid" 2>/dev/null || true
}

kill_tree_no_wait() {
  local pid="${1:-}"
  [[ -n "$pid" ]] || return 0
  kill -0 "$pid" 2>/dev/null || return 0
  local pgid
  pgid="$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ' || true)"
  if [[ -n "$pgid" && "$pgid" == "$pid" ]]; then
    kill -TERM -"$pgid" 2>/dev/null || true
  else
    kill -TERM "$pid" 2>/dev/null || true
  fi
}

tts_active() {
  [[ -f "$tts_pid_file" ]] || return 1
  local pid
  pid="$(cat "$tts_pid_file" 2>/dev/null || true)"
  [[ -n "$pid" ]] || return 1
  kill -0 "$pid" 2>/dev/null
}

# ── Cancellation state tracking ─────────────────────────────────────────
# Tracks whether the current speech-out was cancelled by user action
# (barge-in, keyboard quit) vs natural completion or failure.
_speech_out_cancelled=0
_speech_out_cancel_reason=""

count_assistant_played_chunks() {
  local dir="${1:-$assistant_capture_dir}"
  local n=0
  if compgen -G "$dir/chunk_*.wav" > /dev/null 2>&1; then
    n="$(find "$dir" -maxdepth 1 -name 'chunk_*.wav' 2>/dev/null | wc -l | tr -d ' ')"
  fi
  printf '%s' "${n:-0}"
}

emit_assistant_truncated_event() {
  local cut_text="$1" source="$2" confidence="${3:-low}" intended played_ms words mono_ns payload
  intended="$(cat "$tts_expected_file" 2>/dev/null || true)"
  played_ms="$(cat "$assistant_barge_played_ms_file" 2>/dev/null || echo 0)"
  words="$(printf '%s' "$cut_text" | awk '{print NF}')"
  mono_ns="$(diagnostic_mono_ns)"
  [[ -n "${PYTHON3_BIN:-}" && -n "$intended" ]] || return 0
  payload="$("$PYTHON3_BIN" -c '
import json, sys
print(json.dumps({
  "event": "assistant_turn_truncated",
  "diagnostic_mono_ns": int(sys.argv[1]),
  "diagnostic_clock_origin": "harness_local_monotonic",
  "stream_session_id": sys.argv[2],
  "text": sys.argv[3],
  "intended_text": sys.argv[4],
  "cut_text": sys.argv[3],
  "spoken_prefix": sys.argv[3],
  "primary_cut_source": sys.argv[5],
  "played_ms": int(sys.argv[6]),
  "estimated_words": int(sys.argv[7]),
  "confidence": sys.argv[8],
  "cut_gen": sys.argv[9],
}, ensure_ascii=False))
' "$mono_ns" "$session_id" "$cut_text" "$intended" "$source" "${played_ms:-0}" "${words:-0}" "$confidence" "${assistant_active_gen:-0}")" || payload=""
  [[ -n "$payload" ]] && emit_ui_event "$payload"
}

approx_cut_words() {
  local intended="$1" played_ms="$2"
  [[ -n "${PYTHON3_BIN:-}" ]] || return 1
  "$PYTHON3_BIN" -c '
import sys
intended = sys.argv[1]
played_ms = int(sys.argv[2])
speed = float(sys.argv[3])
words = intended.split()
if not words:
    print("")
    raise SystemExit
wps = max(0.8, 2.5 * max(0.5, speed))
n = int(round((played_ms / 1000.0) * wps))
n = max(1, min(len(words), n))
print(" ".join(words[:n]))
' "$intended" "$played_ms" "$speed"
}

# Live dual-instance B: follow teed 16k chunks while TTS plays.
start_assistant_live_b() {
  [[ "$assistant_self_asr_enabled" == "1" || "$assistant_self_asr_enabled" == "true" || "$assistant_self_asr_enabled" == "yes" ]] || return 0
  local b_url b_session adapter follow_dir stop_file
  adapter="$bin_dir/speech-core-file-adapter"
  if [[ ! -x "$adapter" ]]; then
    adapter="$(command -v speech-core-file-adapter 2>/dev/null || true)"
  fi
  if [[ ! -x "$adapter" ]]; then
    echo "[$(date --iso-8601=seconds)] assistant_self_asr: file-adapter missing; live B disabled" >>"$trigger_log"
    return 0
  fi

  assistant_cut_gen=$((assistant_cut_gen + 1))
  assistant_active_gen="$assistant_cut_gen"
  follow_dir="$assistant_cut_dir/live-$assistant_active_gen/follow"
  stop_file="$assistant_cut_dir/live-$assistant_active_gen/stop"
  mkdir -p "$follow_dir"
  rm -f "$stop_file"
  : >"$assistant_b_watch_log"
  printf '%s\n' "$follow_dir" >"$assistant_cut_dir/current_follow_dir"
  printf '%s\n' "$stop_file" >"$assistant_cut_dir/current_stop_file"
  printf '%s\n' "$assistant_active_gen" >"$assistant_cut_dir/current_gen"

  b_url="${assistant_self_asr_ws_url:-$core_ws_url}"
  b_session="${session_id}-assistant-self-asr-live-${assistant_active_gen}"
  echo "[$(date --iso-8601=seconds)] assistant_self_asr: LIVE start gen=$assistant_active_gen b_url=$b_url session=$b_session" >>"$trigger_log"
  emit_ui_event "$(printf '{"event":"assistant_self_asr_live_started","diagnostic_mono_ns":%s,"diagnostic_clock_origin":"harness_local_monotonic","stream_session_id":"%s","cut_gen":%s,"b_url":"%s"}' "$(diagnostic_mono_ns)" "$session_id" "$assistant_active_gen" "$b_url")"

  # Watch B daemon events (second Nemotron process).
  "$bin_dir/speech-core-watch" \
    --url "$b_url" \
    --stream-id "assistant.self_asr" \
    --stream-session-id "$b_session" \
    --mode jsonl \
    >>"$assistant_b_watch_log" 2>>"$trigger_log" &
  assistant_b_watch_pid=$!

  # Follow teed chunks in realtime while TTS plays.
  "$adapter" \
    --url "$b_url" \
    --stream-id "assistant.self_asr" \
    --stream-session-id "$b_session" \
    --adapter-id "assistant.self_asr.live" \
    --frame-ms 20 \
    --realtime \
    --append-silence-ms 400 \
    --hold-open-ms 200 \
    --follow-dir "$follow_dir" \
    --stop-file "$stop_file" \
    --follow-first-chunk-timeout-ms 60000 \
    >>"$trigger_log" 2>&1 &
  assistant_b_feed_pid=$!
  printf '%s\n' "$assistant_b_feed_pid" >"$assistant_cut_dir/live_feed.pid"
  echo "[$(date --iso-8601=seconds)] assistant_self_asr: live feed pid=$assistant_b_feed_pid watch pid=$assistant_b_watch_pid follow=$follow_dir" >>"$trigger_log"
}

stop_assistant_live_b() {
  local stop_file feed_pid barge_ms start_ms now_ms
  stop_file="$(cat "$assistant_cut_dir/current_stop_file" 2>/dev/null || true)"
  start_ms="$(cat "$assistant_play_start_ms_file" 2>/dev/null || echo 0)"
  now_ms="$(($(date +%s%N) / 1000000))"
  if [[ "$start_ms" =~ ^[0-9]+$ ]] && (( start_ms > 0 && now_ms > start_ms )); then
    barge_ms=$((now_ms - start_ms))
  else
    barge_ms=800
  fi
  if (( barge_ms < 150 )); then barge_ms=150; fi
  printf '%s\n' "$barge_ms" >"$assistant_barge_played_ms_file"
  printf '%s\n' "$now_ms" >"$assistant_cut_dir/barge_mono_ms"

  if [[ -n "$stop_file" ]]; then
    : >"$stop_file"
  fi
  echo "[$(date --iso-8601=seconds)] assistant_self_asr: LIVE stop requested gen=${assistant_active_gen:-?} played_ms=$barge_ms" >>"$trigger_log"

  # Let follow-mode finish silence pad + close; measure residual drain.
  feed_pid="${assistant_b_feed_pid:-}"
  if [[ -z "$feed_pid" ]]; then
    feed_pid="$(cat "$assistant_cut_dir/live_feed.pid" 2>/dev/null || true)"
  fi
  if [[ -n "$feed_pid" ]] && kill -0 "$feed_pid" 2>/dev/null; then
    local _w=0
    while (( _w < 80 )) && kill -0 "$feed_pid" 2>/dev/null; do
      sleep 0.05
      _w=$((_w + 1))
    done
    if kill -0 "$feed_pid" 2>/dev/null; then
      kill_tree_no_wait "$feed_pid" 2>/dev/null || true
    fi
  fi
  assistant_b_feed_pid=""
  local residual_ms
  residual_ms="$(( ($(date +%s%N) / 1000000) - now_ms ))"
  printf '%s\n' "$residual_ms" >"$assistant_cut_dir/live_drain_residual_ms"
  echo "[$(date --iso-8601=seconds)] assistant_self_asr: live residual_drain_ms=$residual_ms gen=${assistant_active_gen:-?}" >>"$trigger_log"
}

# Latest teed wav (full TTS chunk as handed to the speaker).
latest_assistant_wav() {
  local f=""
  if compgen -G "$assistant_capture_dir/chunk_*.wav" > /dev/null 2>&1; then
    f="$(ls -1 "$assistant_capture_dir"/chunk_*.wav 2>/dev/null | sort | tail -n1)"
  fi
  # Prefer 16k follow sibling if present (same index under live-*/follow)
  local follow_dir
  follow_dir="$(cat "$assistant_cut_dir/current_follow_dir" 2>/dev/null || true)"
  if [[ -n "$follow_dir" && -d "$follow_dir" ]]; then
    local ff
    ff="$(ls -1 "$follow_dir"/chunk_*.wav 2>/dev/null | sort | tail -n1 || true)"
    if [[ -n "$ff" && -f "$ff" ]]; then
      printf '%s' "$ff"
      return 0
    fi
  fi
  printf '%s' "$f"
}

record_barge_played_ms() {
  local start_ms now_ms barge_ms
  start_ms="$(cat "$assistant_play_start_ms_file" 2>/dev/null || echo 0)"
  now_ms="$(($(date +%s%N) / 1000000))"
  if [[ "$start_ms" =~ ^[0-9]+$ ]] && (( start_ms > 0 && now_ms > start_ms )); then
    barge_ms=$((now_ms - start_ms))
  else
    barge_ms=800
  fi
  if (( barge_ms < 80 )); then barge_ms=80; fi
  printf '%s\n' "$barge_ms" >"$assistant_barge_played_ms_file"
  printf '%s\n' "$now_ms" >"$assistant_cut_dir/barge_mono_ms"
  printf '%s' "$barge_ms"
}

# CTC forced align on teed clip (audio + intended text). Ground-truth cut.
finalize_assistant_cut_ctc() {
  local intended played_ms wav out_json mono_ns payload cut_line backend conf lat
  intended="$(cat "$tts_expected_file" 2>/dev/null || true)"
  [[ -n "$intended" ]] || return 0
  played_ms="$(cat "$assistant_barge_played_ms_file" 2>/dev/null || echo 0)"
  if ! [[ "$played_ms" =~ ^[0-9]+$ ]]; then played_ms=0; fi
  wav="$(latest_assistant_wav)"
  mkdir -p "$assistant_cut_dir"

  if [[ -z "$align_script" || ! -f "$align_script" ]]; then
    echo "[$(date --iso-8601=seconds)] assistant_cut: ctc skipped (no align script)" >>"$trigger_log"
    return 1
  fi

  out_json="$assistant_cut_dir/ctc_align.json"
  backend="$align_backend"
  local t0 t1
  t0="$(($(date +%s%N) / 1000000))"

  # Prefer local align_python with torch; optional remote to core host.
  local use_remote=0
  if [[ "$align_remote" == "1" || "$align_remote" == "true" || "$align_remote" == "yes" ]]; then
    use_remote=1
  elif [[ "$align_remote" == "auto" ]]; then
    if ! "$align_python" -c 'import torchaudio' >/dev/null 2>&1; then
      use_remote=1
    fi
  fi

  if (( use_remote == 0 )); then
    "$align_python" "$align_script" \
      --backend "$backend" \
      --wav "${wav:-}" \
      --intended "$intended" \
      --played-ms "$played_ms" \
      --speed "$speed" \
      --out "$out_json" \
      >>"$trigger_log" 2>&1 || true
  else
    # Remote align on speech-core host (where pyannote-cpu / torch lives).
    local host remote_wav remote_py remote_script
    host="$(printf '%s' "$core_ws_url" | sed -E 's#^ws://([^/:]+).*#\1#')"
    remote_py="${SPEECH_CORE_ALIGN_PYTHON_REMOTE:-/home/sf/workspace/.venvs/pyannote-cpu/bin/python}"
    remote_script="${SPEECH_CORE_ALIGN_SCRIPT_REMOTE:-/home/sf/workspace/speech-core/scripts/barge_in_align/run_align.py}"
    if [[ -n "$wav" && -f "$wav" && -n "$host" ]]; then
      remote_wav="/tmp/speech-align-${session_id}-$(date +%s).wav"
      scp -o BatchMode=yes -o ConnectTimeout=3 "$wav" "sf@${host}:${remote_wav}" >>"$trigger_log" 2>&1 || true
      ssh -o BatchMode=yes -o ConnectTimeout=3 "sf@${host}" \
        "PYTHONPATH=/home/sf/workspace/speech-core/scripts $remote_py $remote_script --backend $backend --wav $remote_wav --intended $(printf '%q' "$intended") --played-ms $played_ms --speed $speed" \
        >"$out_json" 2>>"$trigger_log" || true
      ssh -o BatchMode=yes -o ConnectTimeout=3 "sf@${host}" "rm -f $remote_wav" >/dev/null 2>&1 || true
    else
      echo "[$(date --iso-8601=seconds)] assistant_cut: remote ctc skipped (no wav/host)" >>"$trigger_log"
    fi
  fi

  t1="$(($(date +%s%N) / 1000000))"
  if [[ ! -s "$out_json" ]]; then
    echo "[$(date --iso-8601=seconds)] assistant_cut: ctc produced no json wall_ms=$((t1-t0))" >>"$trigger_log"
    return 1
  fi

  # out may be pretty or single-line; normalize
  if [[ -n "${PYTHON3_BIN:-}" ]]; then
    cut_line="$("$PYTHON3_BIN" -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d.get("spoken_prefix") or "")' "$out_json" 2>/dev/null || true)"
    conf="$("$PYTHON3_BIN" -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d.get("confidence",0))' "$out_json" 2>/dev/null || echo 0)"
    lat="$("$PYTHON3_BIN" -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d.get("align_latency_ms",0))' "$out_json" 2>/dev/null || echo 0)"
    backend="$("$PYTHON3_BIN" -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d.get("backend_id","ctc_forced"))' "$out_json" 2>/dev/null || echo ctc_forced)"
  else
    cut_line=""
  fi
  [[ -n "$cut_line" ]] || return 1

  printf '%s\n' "$cut_line" >"$assistant_cut_dir/production_cut_text"
  printf '%s\n' "$intended" >"$assistant_cut_dir/assistant_intended.txt"
  echo "[$(date --iso-8601=seconds)] assistant_cut: ground_truth source=$backend played_ms=$played_ms align_ms=$lat wall_ms=$((t1-t0)) cut=$cut_line" >>"$trigger_log"
  emit_assistant_truncated_event "$cut_line" "$backend" "high"
  return 0
}

# Provisional approx cut (immediate UI). Ground truth is CTC (or legacy B).
finalize_assistant_cut_approx() {
  local intended played_ms cut_line
  intended="$(cat "$tts_expected_file" 2>/dev/null || true)"
  [[ -n "$intended" ]] || return 0
  played_ms="$(cat "$assistant_barge_played_ms_file" 2>/dev/null || echo 800)"
  if ! [[ "$played_ms" =~ ^[0-9]+$ ]]; then played_ms=800; fi
  cut_line="$(approx_cut_words "$intended" "$played_ms" 2>/dev/null || true)"
  [[ -n "$cut_line" ]] || return 0
  mkdir -p "$assistant_cut_dir"
  printf '%s\n' "$intended" >"$assistant_cut_dir/assistant_intended.txt"
  printf '%s\n' "$cut_line" >"$assistant_cut_dir/production_cut_text"
  if [[ -n "${PYTHON3_BIN:-}" ]]; then
    "$PYTHON3_BIN" -c '
import json,sys
from pathlib import Path
Path(sys.argv[1]).write_text(json.dumps({
  "label":"live_session",
  "primary_cut_source":"approx_wallclock",
  "production_cut_text":sys.argv[2],
  "intended_text":sys.argv[3],
  "played_ms":int(sys.argv[4]),
  "confidence":"low",
  "mode":"live_b",
}, indent=2)+"\n", encoding="utf-8")
' "$assistant_cut_dir/metrics_approx.json" "$cut_line" "$intended" "$played_ms" 2>/dev/null || true
  fi
  echo "[$(date --iso-8601=seconds)] assistant_cut: provisional source=approx_wallclock played_ms=$played_ms cut=$cut_line gen=${assistant_active_gen:-?}" >>"$trigger_log"
  emit_assistant_truncated_event "$cut_line" "approx_wallclock" "low"
}

# Ground-truth cut from live B watch log (Nemotron transcribed chunks).
finalize_assistant_cut_from_live_b() {
  [[ "$assistant_self_asr_enabled" == "1" || "$assistant_self_asr_enabled" == "true" || "$assistant_self_asr_enabled" == "yes" ]] || return 0
  local intended drain_cut drain_source residual_ms gen
  intended="$(cat "$tts_expected_file" 2>/dev/null || true)"
  [[ -n "$intended" && -n "${PYTHON3_BIN:-}" && -f "$finalize_cut_py" ]] || return 0
  gen="${assistant_active_gen:-0}"
  residual_ms="$(cat "$assistant_cut_dir/live_drain_residual_ms" 2>/dev/null || echo 0)"

  # Brief settle for last B transcript events after stop.
  local _s=0
  while (( _s < 30 )); do
    if [[ -s "$assistant_b_watch_log" ]] && rg -q '"event":"transcript_update"|"event":"transcript_committed"|"event":"turn_transcript_committed"|"event":"transcript_token_committed"' "$assistant_b_watch_log" 2>/dev/null; then
      break
    fi
    sleep 0.05
    _s=$((_s + 1))
  done

  drain_cut="$("$PYTHON3_BIN" "$finalize_cut_py" \
    --intended-text "$intended" \
    --out-dir "$assistant_cut_dir" \
    --watch-jsonl "$assistant_b_watch_log" \
    --played-chunks "$(count_assistant_played_chunks)" \
    --pad-words "$assistant_pad_words" \
    --drain-complete \
    2>>"$trigger_log" | head -n1 || true)"
  drain_source="fallback"
  if [[ -f "$assistant_cut_dir/metrics.json" ]]; then
    drain_source="$("$PYTHON3_BIN" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("primary_cut_source","fallback"))' "$assistant_cut_dir/metrics.json" 2>/dev/null || echo fallback)"
    # Annotate live metrics.
    "$PYTHON3_BIN" -c '
import json,sys
from pathlib import Path
p=Path(sys.argv[1])
try:
  m=json.loads(p.read_text())
except Exception:
  m={}
m["mode"]="live_b"
m["live_drain_residual_ms"]=int(sys.argv[2])
m["cut_gen"]=sys.argv[3]
p.write_text(json.dumps(m, indent=2)+"\n")
' "$assistant_cut_dir/metrics.json" "${residual_ms:-0}" "$gen" 2>/dev/null || true
  fi

  if [[ -n "$drain_cut" ]]; then
    echo "[$(date --iso-8601=seconds)] assistant_cut: ground_truth source=$drain_source residual_ms=$residual_ms cut=$drain_cut gen=$gen" >>"$trigger_log"
    emit_assistant_truncated_event "$drain_cut" "$drain_source" "high"
  else
    echo "[$(date --iso-8601=seconds)] assistant_cut: live B produced no cut gen=$gen residual_ms=$residual_ms" >>"$trigger_log"
  fi

  if [[ -n "$assistant_b_watch_pid" ]]; then
    kill_tree_no_wait "$assistant_b_watch_pid" 2>/dev/null || true
    assistant_b_watch_pid=""
  fi
}

# Compatibility name used by older call sites.
finalize_assistant_cut() {
  # Prefer CTC ground truth; legacy Nemotron B only if explicitly enabled + log present.
  if [[ "$align_backend" == "ctc_forced" ]]; then
    finalize_assistant_cut_ctc || finalize_assistant_cut_approx || true
  elif [[ -s "$assistant_b_watch_log" ]]; then
    finalize_assistant_cut_from_live_b || true
  else
    finalize_assistant_cut_approx || true
  fi
}

cancel_speech_out() {
  local trigger="${1:-user_speech}"
  if ! tts_active; then
    rm -f "$tts_pid_file"
    return 0
  fi
  local pid
  pid="$(cat "$tts_pid_file" 2>/dev/null || true)"
  _speech_out_cancelled=1
  _speech_out_cancel_reason="$trigger"

  echo "[$(date --iso-8601=seconds)] barge-in ($trigger) -> cancel speech-out pid=$pid" >>"$trigger_log"
  emit_ui_event "$(printf '{"event":"speech_out_barge_in","diagnostic_mono_ns":%s,"diagnostic_clock_origin":"harness_local_monotonic","stream_session_id":"%s","trigger":"%s","tts_pid":%s}'     "$(diagnostic_mono_ns)" "$session_id" "$trigger" "${pid:-0}")"

  kill_tree "$pid"
  rm -f "$tts_pid_file"
  echo $(( $(date +%s) + echo_suppress_secs )) >"$tts_echo_deadline_file" 2>/dev/null || true

  if [[ "$trigger" == "transcript_token_committed" || "$trigger" == "user_speech" ]]; then
    # Freeze play duration for cut.
    record_barge_played_ms >/dev/null || true
    # Legacy Nemotron B only when explicitly enabled.
    if [[ "$assistant_self_asr_enabled" == "1" || "$assistant_self_asr_enabled" == "true" || "$assistant_self_asr_enabled" == "yes" ]]; then
      stop_assistant_live_b || true
    fi
    # 1) provisional greying immediately (wall-clock)
    finalize_assistant_cut_approx || true
    # 2) ground truth: CTC forced align (async) — default product path
    if [[ "$align_backend" == "ctc_forced" ]]; then
      finalize_assistant_cut_ctc &
    elif [[ "$assistant_self_asr_enabled" == "1" || "$assistant_self_asr_enabled" == "true" || "$assistant_self_asr_enabled" == "yes" ]]; then
      finalize_assistant_cut_from_live_b &
    fi
  elif [[ "$trigger" == "session_end" || "$trigger" == "new_response" ]]; then
    if [[ "$assistant_self_asr_enabled" == "1" || "$assistant_self_asr_enabled" == "true" || "$assistant_self_asr_enabled" == "yes" ]]; then
      stop_assistant_live_b || true
    fi
  fi
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
    emit_ui_event "$(printf '{"event":"speech_out_echo_suppressed","diagnostic_mono_ns":%s,"diagnostic_clock_origin":"harness_local_monotonic","stream_session_id":"%s","context":"%s"}' "$(diagnostic_mono_ns)" "$session_id" "$context")"
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
  # Tee played WAVs for cut alignment (CTC needs audio). Optional legacy Nemotron B.
  local effective_play_command="$play_command"
  local need_tee=0
  if [[ "$align_backend" == "ctc_forced" || "$align_backend" == "approx_wallclock" ]]; then
    need_tee=1
  fi
  if [[ "$assistant_self_asr_enabled" == "1" || "$assistant_self_asr_enabled" == "true" || "$assistant_self_asr_enabled" == "yes" ]]; then
    need_tee=1
  fi
  if (( need_tee == 1 )); then
    if [[ -x "$tee_play_script" || -f "$tee_play_script" ]]; then
      mkdir -p "$assistant_capture_dir"
      rm -f "$assistant_capture_dir"/chunk_*.wav
      : >"$assistant_manifest"
      : >"$assistant_b_watch_log"
      # Legacy dual Nemotron only when explicitly enabled (default OFF).
      if [[ "$assistant_self_asr_enabled" == "1" || "$assistant_self_asr_enabled" == "true" || "$assistant_self_asr_enabled" == "yes" ]]; then
        start_assistant_live_b || true
      fi
      local follow_dir=""
      follow_dir="$(cat "$assistant_cut_dir/current_follow_dir" 2>/dev/null || true)"
      # Always emit 16k follow dir for CTC even without Nemotron B.
      if [[ -z "$follow_dir" ]]; then
        follow_dir="$assistant_cut_dir/follow"
        mkdir -p "$follow_dir"
        rm -f "$follow_dir"/chunk_*.wav
        printf '%s
' "$follow_dir" >"$assistant_cut_dir/current_follow_dir"
      fi
      export SPEECH_OUT_TEE_REAL_PLAY="$play_command"
      export SPEECH_OUT_TEE_CAPTURE_DIR="$assistant_capture_dir"
      export SPEECH_OUT_TEE_MANIFEST="$assistant_manifest"
      export SPEECH_OUT_TEE_FOLLOW_DIR="$follow_dir"
      effective_play_command="$tee_play_script"
      # Warm CTC weights once per session (non-blocking).
      if [[ "$align_backend" == "ctc_forced" && -n "$align_script" && -f "$align_script" ]]; then
        if [[ ! -f "$assistant_cut_dir/.ctc_preloaded" ]]; then
          if "$align_python" -c 'import torchaudio' >/dev/null 2>&1; then
            "$align_python" "$align_script" --preload >>"$trigger_log" 2>&1 &
            touch "$assistant_cut_dir/.ctc_preloaded"
          fi
        fi
      fi
    else
      echo "[$(date --iso-8601=seconds)] assistant_cut: tee script missing; play without capture" >>"$trigger_log"
    fi
  fi

  local play_args=(
    play
    --url "$out_ws_url"
    --voice "$current_voice"
    --lang "$current_lang"
    --steps "$current_steps"
    --speed "$current_speed"
    --play-command "$effective_play_command"
    --chunk-min-chars "$chunk_min_chars"
    --chunk-max-chars "$chunk_max_chars"
  )
  if [[ -n "$current_reference" ]]; then play_args+=(--reference "$current_reference"); fi
  if [[ -n "$current_style" ]]; then play_args+=(--style "$current_style"); fi
  printf '%s\n' "$current_response_text" >"$tts_expected_file"
  echo 0 >"$tts_echo_deadline_file"

  # Launch speech-out directly; $! now captures the real binary PID
  # rather than a transient bash subshell PID.  This pid file is the
  # authoritative handle used by cancel_speech_out and tts_active,
  # both of which may be called from a different shell context
  # (main-script cleanup vs while-pipeline barge-in).
  "$bin_dir/speech-out" "${play_args[@]}" "$current_response_text" \
    2> >(while IFS= read -r out_line; do
      printf '%s\n' "$out_line" >>"$trigger_log"
      # JSON diagnostics go to the TUI event fifo. Plain-text Error: lines used
      # to print to stderr and flash over the TUI for one frame — convert them
      # into sticky operator_alert events instead of raw stderr spam.
      if [[ "$out_line" == "{"* ]]; then
        emit_ui_event "$out_line"
      elif [[ "$out_line" == Error:* || "$out_line" == error:* || "$out_line" == *"playback failed"* ]]; then
        if [[ -n "${PYTHON3_BIN:-}" ]]; then
          emit_ui_event "$("$PYTHON3_BIN" -c 'import json,sys; print(json.dumps({"event":"operator_alert","diagnostic_mono_ns":0,"diagnostic_clock_origin":"harness_local_monotonic","message":sys.argv[1]}))' "$out_line")"
        fi
      fi
    done) &
  local child_pid=$!
  printf '%s\n' "$child_pid" >"$tts_pid_file"
  # Mark play start for barge-time trim of teed assistant audio (B feed must not
  # include unheard tail of the full TTS chunk).
  assistant_play_start_ms="$(($(date +%s%N) / 1000000))"
  printf '%s\n' "$assistant_play_start_ms" >"$assistant_play_start_ms_file"
  rm -f "$assistant_barge_played_ms_file"

  # Block until the child exits (or is killed by cancel_speech_out).
  local rc=0
  wait "$child_pid" 2>/dev/null || rc=$?

  # The process-substitution stderr helper (> >(while ...)) is a sibling
  # process tied to a pipe that closes when speech-out exits.  Wait
  # briefly for any remaining children (the helper, if still flushing).
  # emit_ui_event already uses "|| true" on the fifo write, so a stalled
  # pipe won't hang the helper indefinitely.
  local _ps_grace=0
  while (( _ps_grace < 10 )); do
    wait -n 2>/dev/null || break
    _ps_grace=$((_ps_grace + 1))
  done

  echo $(( $(date +%s) + echo_suppress_secs )) >"$tts_echo_deadline_file" 2>/dev/null || true
  # Only remove the pid file if it still points to *our* child —
  # a newer dispatch may have already overwritten it.
  if [[ "$(cat "$tts_pid_file" 2>/dev/null || true)" == "$child_pid" ]]; then
    rm -f "$tts_pid_file"
  fi
  return $rc
}



dispatch_turn_response() {
  local dispatch_source="${1:-turn_closed_legacy}"
  if [[ -z "${turn_text//[[:space:]]/}" ]]; then
    echo "[$(date --iso-8601=seconds)] $dispatch_source with empty transcript -> skip speech-out" >>"$trigger_log"
    emit_ui_event "$(printf '{"event":"speech_out_skipped","diagnostic_mono_ns":%s,"diagnostic_clock_origin":"harness_local_monotonic","stream_session_id":"%s","reason":"empty_transcript","dispatch_source":"%s"}' "$(diagnostic_mono_ns)" "$session_id" "$dispatch_source")"
  elif suppress_self_echo_if_needed "$turn_text" "$dispatch_source"; then
    echo "[$(date --iso-8601=seconds)] $dispatch_source self-echo -> skip speech-out" >>"$trigger_log"
    emit_ui_event "$(printf '{"event":"speech_out_skipped","diagnostic_mono_ns":%s,"diagnostic_clock_origin":"harness_local_monotonic","stream_session_id":"%s","reason":"self_echo","dispatch_source":"%s"}' "$(diagnostic_mono_ns)" "$session_id" "$dispatch_source")"
  else
    cancel_speech_out new_response
    # run_speech_out writes the real child PID to tts_pid_file
    # internally (the pid file is the cross-context authority).
    # The background subshell that wraps this call is reaped by
    # the owning while-pipeline subshell when the child exits.
    run_speech_out &
  fi
}

turn_text=""
turn_committed_seen=0
# ── JSONL watcher topology ──────────────────────────────────────────────
# Owned, explicit topology: the watcher binary writes to a named FIFO;
# the consumer while-loop reads from the same FIFO.  Each side gets
# its own PID so cleanup can kill_tree the actual binary directly.
# The previous pipe-based approach captured a pipeline subshell PID
# that did not reliably own the watcher binary.

watch_fifo="$run_dir/watch.fifo"
rm -f "$watch_fifo"
mkfifo "$watch_fifo"

"$bin_dir/speech-core-watch" \
  --url "$core_ws_url" \
  --stream-id "$stream_id" \
  --stream-session-id "$session_id" \
  --mode jsonl \
  > "$watch_fifo" &
watch_pid=$!

while IFS= read -r line; do
      printf '%s\n' "$line" >>"$watch_log"
      emit_ui_event "$line"
      event="$(printf '%s\n' "$line" | json_get_string event)"
      case "$event" in
        turn_started)
          turn_text=""
          turn_committed_seen=0
          ;;
        transcript_token_committed)
          token="$(printf '%s
' "$line" | json_get_string text)"
          if is_speech_evidence_text "$token"; then
            turn_text+="$token"
            if tts_active; then
              # The first alphanumeric Nemotron token is authoritative barge-in
              # evidence. VAD alone never controls playback, and we do not wait
              # for accumulated text or echo classification before cancelling.
              cancel_speech_out transcript_token_committed
            fi
          else
            echo "[$(date --iso-8601=seconds)] ignore punctuation-only transcript token: $(printf '%q' "$token")" >>"$trigger_log"
          fi
          ;;
        transcript_committed|turn_transcript_committed)
          # Authoritative controller dispatch seam. Closed-turn text is immutable;
          # do not infer it from cumulative updates or revise it after this event.
          turn_text="$(printf '%s\n' "$line" | json_get_string text)"
          turn_committed_seen=1
          # Cut should already be provisional (+ async CTC) from barge.
          # Do not block next speak on aligner.
          dispatch_turn_response transcript_committed
          ;;
        turn_closed)
          # Backward compatibility for older daemons that do not emit
          # transcript_committed. New daemons dispatch exactly once above.
          if [[ "$turn_committed_seen" != "1" ]]; then
            dispatch_turn_response turn_closed_legacy
          fi
          turn_text=""
          turn_committed_seen=0
          ;;
      esac
    done < "$watch_fifo" &
consumer_pid=$!

# ── Cleanup and signal handling ─────────────────────────────────────────
#
# Design constraints:
# 1. First SIGINT triggers exactly one idempotent cleanup path.
# 2. Nested signals (double Ctrl-C) are ignored after the first.
# 3. No parent wait deadlock — all child waits have timeouts.
# 4. trap is only on INT TERM (not EXIT) — the explicit cleanup call at
#    script end handles the normal-exit path, preventing double-invocation
#    from EXIT firing after a signal-driven cleanup.

_signal_received=0
_cleaned_up=0

_on_signal() {
  # Ignore nested signals: the first one owns cleanup.
  if (( _signal_received )); then
    echo "" >&2
    echo "signal already handled; waiting for cleanup to finish..." >&2
    return
  fi
  _signal_received=1
  printf '\n' >&2
  echo "received signal — shutting down..." >&2
  cleanup
}

reap_stale_session_processes() {
  local sid="$1"
  # Non-recursive: list and kill in one pass.
  local pids
  pids="$(pgrep -af "speech-core-watch .*--stream-session-id $sid" 2>/dev/null | awk '{print $1}' || true)"
  for pid in $pids; do
    [[ -n "$pid" ]] && kill_tree_no_wait "$pid"
  done
}

cleanup() {
  # Idempotent: only execute once regardless of how many times called.
  if (( _cleaned_up )); then
    return 0
  fi
  _cleaned_up=1

  # Cancel any active speech-out before tearing down processes.
  cancel_speech_out session_end || true

  # Terminate adapter with a brief wait (not indefinite).
  if [[ -n "${adapter_pid:-}" ]]; then
    kill_tree "$adapter_pid"
    wait "$adapter_pid" 2>/dev/null || true
  fi

  # Close the event fifo writer so watchers see EOF.
  exec 3>&- 2>/dev/null || true

  # Terminate keyboard loop, then force TTY restore. Killing the keyboard
  # job can skip its local restore path (SIGKILL / race), which left the
  # user's shell with echo off after connection failures.
  if [[ -n "${keyboard_pid:-}" ]]; then
    kill_tree "$keyboard_pid"
    wait "$keyboard_pid" 2>/dev/null || true
    keyboard_pid=""
  fi
  restore_tty

  # Terminate JSONL consumer first (closes the FIFO read end so the
  # watcher sees a broken pipe and exits cleanly).
  if [[ -n "${consumer_pid:-}" ]]; then
    kill_tree "$consumer_pid"
    wait "$consumer_pid" 2>/dev/null || true
  fi

  # Terminate watchers.
  if [[ -n "${watch_pid:-}" ]]; then
    kill_tree "$watch_pid"
    wait "$watch_pid" 2>/dev/null || true
  fi
  if [[ -n "${ui_pid:-}" ]]; then
    kill_tree "$ui_pid"
    wait "$ui_pid" 2>/dev/null || true
  fi

  # Reap any remaining session watchers.
  reap_stale_session_processes "$session_id" || true

  # Clean up named pipes.
  rm -f "$event_fifo" "$param_update_fifo" "$watch_fifo" 2>/dev/null || true

  echo
  echo "session ended"
  echo "  session_id: $session_id"
  echo "  local logs: $run_dir"
  if [[ ${#record_arg[@]} -gt 0 ]]; then
    echo "  audio_wav:  $record_wav"
  fi
  if (( _speech_out_cancelled )); then
    echo "  cancelled:   $_speech_out_cancel_reason"
  fi
}

trap _on_signal INT TERM

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
