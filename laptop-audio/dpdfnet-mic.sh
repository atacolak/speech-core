#!/usr/bin/env bash
# Wrapper for DPDFNet mic proxy — called by speech-core-dpdfnet-mic.service.
# Sources speech-core env, activates venv, sets LD_LIBRARY_PATH for NixOS,
# then runs sfnix-mic-denoise.py.
set -euo pipefail

VENV_DIR="${DPDFNET_VENV_DIR:-/tmp/dpdfnet-venv}"
CACHE_DIR="${DPDFNET_CACHE_DIR:-$HOME/.cache/dpdfnet}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_SCRIPT=""

# Locate sfnix-mic-denoise.py
for candidate in \
  "$SCRIPT_DIR/sfnix-mic-denoise.py" \
  "$SCRIPT_DIR/../experimental/sfnix-mic-denoise.py" \
  "$HOME/.local/libexec/speech-core/sfnix-mic-denoise.py" \
  "$HOME/workspace/speech-core/scripts/experimental/sfnix-mic-denoise.py"; do
  if [[ -f "$candidate" ]]; then
    PYTHON_SCRIPT="$candidate"
    break
  fi
done

if [[ -z "$PYTHON_SCRIPT" ]]; then
  echo "FATAL: sfnix-mic-denoise.py not found" >&2
  exit 1
fi

if [[ ! -f "$VENV_DIR/bin/python3" ]]; then
  echo "FATAL: dpdfnet venv not found at $VENV_DIR" >&2
  echo "Run speech-core-dpdfnet-toggle to bootstrap it." >&2
  exit 1
fi

# Source client.env for WS_URL etc.
env_file="${SPEECH_CORE_CONFIG_FILE:-$HOME/.config/speech-core/client.env}"
if [[ -f "$env_file" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$env_file"
  set +a
fi

# On NixOS, try to set LD_LIBRARY_PATH from a cached nix-shell derivation.
# The toggle script pre-computes and caches this at venv bootstrap time.
ld_cache="$CACHE_DIR/env_ld_library_path"
if [[ -f "$ld_cache" ]]; then
  export LD_LIBRARY_PATH="$(cat "$ld_cache")"
elif command -v nix-shell >/dev/null 2>&1; then
  # Attempt ad-hoc resolution (slow but works)
  cached="$(nix-shell -p python312 stdenv.cc.cc.lib zlib --run '
    echo "$NIX_LDFLAGS" | tr " " "\n" | while read -r f; do
      case "$f" in -L/*) dir="${f#-L}"; [ -d "$dir" ] && echo -n "$dir:"; esac
    done
  ' 2>/dev/null || true)"
  if [[ -n "$cached" ]]; then
    export LD_LIBRARY_PATH="$cached"
    mkdir -p "$CACHE_DIR"
    echo "$cached" >"$ld_cache"
  fi
fi

export DPDFNET_CACHE_DIR="$CACHE_DIR"
if [[ -z "${SPEECH_CORE_PORTAUDIO_LIB:-}" ]]; then
  IFS=':' read -ra _ld_dirs <<< "${LD_LIBRARY_PATH:-}"
  for _dir in "${_ld_dirs[@]}"; do
    if [[ -f "$_dir/libportaudio.so" ]]; then
      export SPEECH_CORE_PORTAUDIO_LIB="$_dir/libportaudio.so"
      break
    fi
  done
fi

args=(
  --url "${SPEECH_CORE_WS_URL:-ws://100.68.60.39:8765/ws/audio-ingress}"
  --stream-id "${SPEECH_CORE_DPDFNET_STREAM_ID:-${SPEECH_CORE_STREAM_ID:-sfnix.dpdfnet_mic}}"
  --adapter-id "${SPEECH_CORE_DPDFNET_ADAPTER_ID:-sfnix.dpdfnet}"
  --model "${DPDFNET_MODEL:-dpdfnet2}"
)
if [[ -n "${SPEECH_CORE_STREAM_SESSION_ID:-}" ]]; then
  args+=(--stream-session-id "$SPEECH_CORE_STREAM_SESSION_ID")
fi
if [[ -n "${SPEECH_CORE_DPDFNET_DEVICE_INDEX:-}" ]]; then
  args+=(--device "$SPEECH_CORE_DPDFNET_DEVICE_INDEX")
fi
exec "$VENV_DIR/bin/python3" "$PYTHON_SCRIPT" "${args[@]}" "$@"
