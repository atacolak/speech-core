#!/usr/bin/env bash
# Real playback tee for speech-out live-session.
# speech-out play invokes: <play-command> <wav-path>
# We copy the WAV into the assistant-self-asr capture dir (what was actually
# handed to the speaker), optionally emit a 16 kHz mono follow chunk for live
# Nemotron B, then exec the real player (pw-play).
set -euo pipefail

wav="${1:-}"
if [[ -z "$wav" || ! -f "$wav" ]]; then
  echo "speech-out-tee-play: missing wav path: ${wav:-}" >&2
  exit 2
fi

real_play="${SPEECH_OUT_TEE_REAL_PLAY:-pw-play}"
capture_dir="${SPEECH_OUT_TEE_CAPTURE_DIR:-}"
manifest="${SPEECH_OUT_TEE_MANIFEST:-}"
follow_dir="${SPEECH_OUT_TEE_FOLLOW_DIR:-}"

next_chunk_index() {
  local dir="$1"
  local n=0
  if compgen -G "$dir/chunk_*.wav" > /dev/null 2>&1; then
    n="$(find "$dir" -maxdepth 1 -name 'chunk_*.wav' | wc -l | tr -d ' ')"
  fi
  printf '%s' "$((n + 1))"
}

if [[ -n "$capture_dir" ]]; then
  mkdir -p "$capture_dir"
  n="$(next_chunk_index "$capture_dir")"
  dest="$(printf '%s/chunk_%04d.wav' "$capture_dir" "$n")"
  cp -f "$wav" "$dest"
  if [[ -n "$manifest" ]]; then
    mkdir -p "$(dirname "$manifest")"
    printf '{"event":"assistant_chunk_played","chunk_index":%s,"path":"%s","bytes":%s,"mono_ms":%s}\n' \
      "$n" "$dest" "$(wc -c <"$wav" | tr -d ' ')" "$(($(date +%s%N) / 1000000))" \
      >>"$manifest"
  fi

  # Live B follow feed expects 16 kHz mono PCM16.
  if [[ -n "$follow_dir" ]]; then
    mkdir -p "$follow_dir"
    follow_dest="$(printf '%s/chunk_%04d.wav' "$follow_dir" "$n")"
    if command -v ffmpeg >/dev/null 2>&1; then
      ffmpeg -nostdin -hide_banner -loglevel error -y \
        -i "$wav" -ac 1 -ar 16000 -c:a pcm_s16le "$follow_dest" \
        || cp -f "$wav" "$follow_dest"
    else
      cp -f "$wav" "$follow_dest"
    fi
  fi
fi

exec "$real_play" "$wav"
