#!/usr/bin/env bash
set -euo pipefail

source_name="${SPEECH_CORE_AEC_SOURCE_NAME:-speech_core_echo_cancel}"
sink_name="${SPEECH_CORE_AEC_SINK_NAME:-speech_core_echo_cancel_sink}"
state_dir="${SPEECH_CORE_STATE_DIR:-$HOME/.local/state/speech-core}/aec"
state_file="$state_dir/defaults.env"
mode="toggle"
if [[ $# -gt 0 ]]; then
  case "$1" in
    --on|on) mode="on" ;;
    --off|off) mode="off" ;;
    --status|status) mode="status" ;;
    *) echo "usage: speech-core-aec-toggle [--on|--off|--status]" >&2; exit 2 ;;
  esac
fi

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing required command: $1" >&2; exit 1; }; }
need pactl

notify() { notify-send "$@" >/dev/null 2>&1 || true; }

source_exists() { pactl list short sources 2>/dev/null | awk '{print $2}' | grep -Fxq "$source_name"; }
sink_exists() { pactl list short sinks 2>/dev/null | awk '{print $2}' | grep -Fxq "$sink_name"; }
current_source() { pactl info 2>/dev/null | awk -F': ' '/Default Source:/{print $2; exit}'; }
current_sink() { pactl info 2>/dev/null | awk -F': ' '/Default Sink:/{print $2; exit}'; }

pick_hardware_source() {
  local cur prev
  cur="$(current_source || true)"
  prev=""
  if [[ -f "$state_file" ]]; then
    # shellcheck disable=SC1090
    source "$state_file" || true
    prev="${PREV_SOURCE:-}"
  fi
  if [[ -n "$prev" && "$prev" != "$source_name" ]] && pactl list short sources | awk '{print $2}' | grep -Fxq "$prev"; then
    printf '%s\n' "$prev"; return
  fi
  if [[ -n "$cur" && "$cur" != "$source_name" && "$cur" != *.monitor ]]; then
    printf '%s\n' "$cur"; return
  fi
  pactl list short sources | awk -v aec="$source_name" '$2 != aec && $2 !~ /\.monitor$/ {print $2; exit}'
}

pick_hardware_sink() {
  local cur prev
  cur="$(current_sink || true)"
  prev=""
  if [[ -f "$state_file" ]]; then
    # shellcheck disable=SC1090
    source "$state_file" || true
    prev="${PREV_SINK:-}"
  fi
  if [[ -n "$prev" && "$prev" != "$sink_name" ]] && pactl list short sinks | awk '{print $2}' | grep -Fxq "$prev"; then
    printf '%s\n' "$prev"; return
  fi
  if [[ -n "$cur" && "$cur" != "$sink_name" ]]; then
    printf '%s\n' "$cur"; return
  fi
  pactl list short sinks | awk -v aec="$sink_name" '$2 != aec {print $2; exit}'
}

unload_aec_modules() {
  pactl list short modules 2>/dev/null \
    | awk -v src="$source_name" -v sink="$sink_name" '$0 ~ /module-echo-cancel/ && ($0 ~ src || $0 ~ sink) {print $1}' \
    | while read -r id; do
        [[ -n "$id" ]] && pactl unload-module "$id" >/dev/null 2>&1 || true
      done
}

move_existing_streams_to_aec() {
  # Playback must go through the echo-cancel sink so WebRTC AEC receives the
  # render/reference signal. Otherwise the source exists but cannot cancel TTS.
  pactl list short sink-inputs 2>/dev/null | awk '{print $1}' | while read -r id; do
    [[ -n "$id" ]] && pactl move-sink-input "$id" "$sink_name" >/dev/null 2>&1 || true
  done
  pactl list short source-outputs 2>/dev/null | awk '{print $1}' | while read -r id; do
    [[ -n "$id" ]] && pactl move-source-output "$id" "$source_name" >/dev/null 2>&1 || true
  done
}

turn_on() {
  mkdir -p "$state_dir"
  local prev_src prev_sink master_src master_sink
  prev_src="$(current_source || true)"
  prev_sink="$(current_sink || true)"
  master_src="$(pick_hardware_source)"
  master_sink="$(pick_hardware_sink)"
  if [[ -z "$master_src" || -z "$master_sink" ]]; then
    echo "could not identify hardware source/sink for AEC" >&2
    exit 1
  fi
  cat >"$state_file" <<EOF_STATE
PREV_SOURCE=$prev_src
PREV_SINK=$prev_sink
MASTER_SOURCE=$master_src
MASTER_SINK=$master_sink
EOF_STATE

  unload_aec_modules
  # Keep the module tied to the real mic and real speaker. Set both defaults to
  # the virtual pair: input apps record from the AEC source, output apps play to
  # the AEC sink, and the module forwards playback to MASTER_SINK while using it
  # as the echo reference.
  if ! pactl load-module module-echo-cancel \
      aec_method=webrtc \
      source_master="$master_src" \
      sink_master="$master_sink" \
      source_name="$source_name" \
      sink_name="$sink_name" \
      use_master_format=1 >/dev/null; then
    pactl load-module module-echo-cancel \
      aec_method=webrtc \
      source_master="$master_src" \
      sink_master="$master_sink" \
      source_name="$source_name" \
      sink_name="$sink_name" >/dev/null
  fi
  pactl set-default-source "$source_name" >/dev/null
  pactl set-default-sink "$sink_name" >/dev/null
  move_existing_streams_to_aec
  notify "Acoustic echo cancellation ON" "Default input/output now route through WebRTC AEC" -i audio-input-microphone
}

turn_off() {
  local restore_src restore_sink
  restore_src=""
  restore_sink=""
  if [[ -f "$state_file" ]]; then
    # shellcheck disable=SC1090
    source "$state_file" || true
    restore_src="${PREV_SOURCE:-${MASTER_SOURCE:-}}"
    restore_sink="${PREV_SINK:-${MASTER_SINK:-}}"
  fi
  [[ -z "$restore_src" || "$restore_src" == "$source_name" ]] && restore_src="$(pactl list short sources | awk -v aec="$source_name" '$2 != aec && $2 !~ /\.monitor$/ {print $2; exit}')"
  [[ -z "$restore_sink" || "$restore_sink" == "$sink_name" ]] && restore_sink="$(pactl list short sinks | awk -v aec="$sink_name" '$2 != aec {print $2; exit}')"
  [[ -n "$restore_src" ]] && pactl set-default-source "$restore_src" >/dev/null 2>&1 || true
  [[ -n "$restore_sink" ]] && pactl set-default-sink "$restore_sink" >/dev/null 2>&1 || true
  if [[ -n "$restore_sink" ]]; then
    pactl list short sink-inputs 2>/dev/null | awk '{print $1}' | while read -r id; do
      [[ -n "$id" ]] && pactl move-sink-input "$id" "$restore_sink" >/dev/null 2>&1 || true
    done
  fi
  unload_aec_modules
  notify "Acoustic echo cancellation OFF" "Default audio restored" -i audio-input-microphone
}

status() {
  echo "default_source=$(current_source)"
  echo "default_sink=$(current_sink)"
  echo "aec_source_exists=$(source_exists && echo yes || echo no)"
  echo "aec_sink_exists=$(sink_exists && echo yes || echo no)"
  pactl list short modules 2>/dev/null | grep -E "module-echo-cancel|$source_name|$sink_name" || true
}

case "$mode" in
  status) status ;;
  on) turn_on; status ;;
  off) turn_off; status ;;
  toggle)
    if [[ "$(current_source)" == "$source_name" && "$(current_sink)" == "$sink_name" ]] && source_exists && sink_exists; then
      turn_off
    else
      turn_on
    fi
    status
    ;;
esac
