#!/usr/bin/env bash
# Real playback tee for speech-out live-session.
# speech-out play invokes: <play-command> <wav-path>
# We copy the WAV into the assistant-self-asr capture dir (what was actually
# handed to the speaker), then exec the real player (pw-play).
set -euo pipefail

wav="${1:-}"
if [[ -z "$wav" || ! -f "$wav" ]]; then
  echo "speech-out-tee-play: missing wav path: ${wav:-}" >&2
  exit 2
fi

real_play="${SPEECH_OUT_TEE_REAL_PLAY:-pw-play}"
capture_dir="${SPEECH_OUT_TEE_CAPTURE_DIR:-}"
manifest="${SPEECH_OUT_TEE_MANIFEST:-}"

if [[ -n "$capture_dir" ]]; then
  mkdir -p "$capture_dir"
  # Monotonic chunk index from existing files.
  n=0
  if compgen -G "$capture_dir/chunk_*.wav" > /dev/null 2>&1; then
    n="$(find "$capture_dir" -maxdepth 1 -name 'chunk_*.wav' | wc -l | tr -d ' ')"
  fi
  n=$((n + 1))
  dest="$(printf '%s/chunk_%04d.wav' "$capture_dir" "$n")"
  cp -f "$wav" "$dest"
  if [[ -n "$manifest" ]]; then
    mkdir -p "$(dirname "$manifest")"
    # harness-local log only (not a protocol event)
    printf '{"event":"assistant_chunk_played","chunk_index":%s,"path":"%s","bytes":%s,"mono_ms":%s}\n' \
      "$n" "$dest" "$(wc -c <"$wav" | tr -d ' ')" "$(($(date +%s%N) / 1000000))" \
      >>"$manifest"
  fi
fi

exec "$real_play" "$wav"
