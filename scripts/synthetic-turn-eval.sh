#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

ws_url="${SPEECH_CORE_WS_URL:-ws://127.0.0.1:8765/ws/audio-ingress}"
text="${1:-okay right now i am testing a short pause and then i keep talking because this should resume instead of closing}"
out_dir="${SPEECH_CORE_EVAL_DIR:-/tmp/speech-core-turn-eval-$(date +%Y%m%d-%H%M%S)}"
session_id="${SPEECH_CORE_STREAM_SESSION_ID:-synthetic-turn-eval-$(date +%s)-$RANDOM}"
mkdir -p "$out_dir"

raw_wav="$out_dir/espeak.wav"
eval_wav="$out_dir/eval.wav"
events="$out_dir/events.jsonl"
tui="$out_dir/tui.txt"
debug_tui="$out_dir/debug-tui.txt"

printf '%s\n' "$text" >"$out_dir/text.txt"
espeak-ng -s "${ESPEAK_SPEED:-165}" -w "$raw_wav" "$text" >/dev/null 2>&1
ffmpeg -hide_banner -loglevel error -y -i "$raw_wav" -ac 1 -ar 16000 -sample_fmt s16 "$eval_wav"

cargo build --release -p speech-core-file-adapter -p speech-core-watch >/dev/null

before_lines=0
log_path="${SPEECH_CORE_EVENTS_PATH:-$HOME/.local/state/speech-core/logs/events.jsonl}"
if [[ -f "$log_path" ]]; then
  before_lines="$(wc -l <"$log_path")"
fi

target/release/speech-core-file-adapter \
  --url "$ws_url" \
  --stream-id synthetic.turn.eval \
  --stream-session-id "$session_id" \
  --adapter-id synthetic.turn.eval \
  --frame-ms 20 \
  --realtime \
  --append-silence-ms "${SPEECH_CORE_EVAL_APPEND_SILENCE_MS:-1800}" \
  --hold-open-ms "${SPEECH_CORE_EVAL_HOLD_OPEN_MS:-1400}" \
  "$eval_wav" \
  >"$out_dir/file-adapter.out" \
  2>"$out_dir/file-adapter.err"

sleep "${SPEECH_CORE_EVAL_LOG_WAIT_SECS:-1}"
if [[ -f "$log_path" ]]; then
  tail -n +$((before_lines + 1)) "$log_path" | grep -F "\"stream_session_id\":\"$session_id\"" >"$events" || true
fi

target/release/speech-core-watch --mode tui --replay-events "$events" >"$tui"
target/release/speech-core-watch --mode debug --replay-events "$events" >"$debug_tui"

printf 'synthetic turn eval\n'
printf '  session_id: %s\n' "$session_id"
printf '  wav:        %s\n' "$eval_wav"
printf '  events:     %s\n' "$events"
printf '  tui:        %s\n' "$tui"
printf '  debug_tui:  %s\n\n' "$debug_tui"
cat "$tui"
