#!/usr/bin/env bash
set -euo pipefail

env_file="${SPEECH_CORE_CONFIG_FILE:-$HOME/.config/speech-core/client.env}"
if [[ -f "$env_file" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$env_file"
  set +a
fi

state_root="${SPEECH_CORE_STATE_DIR:-$HOME/.local/state/speech-core}"
run_dir="$state_root/dictation"
pid_file="$run_dir/current.pid"
ledger="$run_dir/ledger.json"
mkdir -p "$run_dir"

is_running() {
  [[ -f "$pid_file" ]] || return 1
  local pid
  pid="$(cat "$pid_file" 2>/dev/null || true)"
  [[ -n "$pid" ]] || return 1
  kill -0 "$pid" 2>/dev/null
}

if is_running; then
  pid="$(cat "$pid_file")"
  kill "$pid" 2>/dev/null || true
  rm -f "$pid_file"
  notify-send "live transcribe stopped" "speech-core dictation stopped; nemotron daemon remains warm" || true
  now="$(date --iso-8601=seconds)"
  printf '{"state":"stopping","updated_at":"%s","pid":%s}\n' "$now" "$pid" >"$ledger"
  exit 0
fi

export SPEECH_CORE_DICTATION_RUN_DIR="$run_dir/session"
nohup "$HOME/.local/bin/speech-core-dictation-run" >"$run_dir/runner.out" 2>"$run_dir/runner.err" &
pid=$!
echo "$pid" >"$pid_file"
now="$(date --iso-8601=seconds)"
printf '{"state":"starting","updated_at":"%s","pid":%s,"run_dir":"%s"}\n' "$now" "$pid" "$SPEECH_CORE_DICTATION_RUN_DIR" >"$ledger"
notify-send "live transcribe begin" "speech-core dictation starting" || true
