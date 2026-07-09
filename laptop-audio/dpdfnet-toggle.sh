#!/usr/bin/env bash
# Toggle DPDFNet microphone denoising for speech-core ingestion.
# Start: bootstraps venv if needed, starts systemd user service, sends notify
# Stop:  stops systemd user service, sends notify
set -euo pipefail

SERVICE_NAME="speech-core-dpdfnet-mic.service"
VENV_DIR="${DPDFNET_VENV_DIR:-/tmp/dpdfnet-venv}"
CACHE_DIR="${DPDFNET_CACHE_DIR:-$HOME/.cache/dpdfnet}"
LIBEXEC_DIR="${SPEECH_CORE_LIBEXEC_DIR:-$HOME/.local/libexec/speech-core}"
STATE_DIR="${SPEECH_CORE_STATE_DIR:-$HOME/.local/state/speech-core}"
ENABLE_FILE="$STATE_DIR/dpdfnet/enabled"

# Source speech-core env for WS_URL etc.
env_file="${SPEECH_CORE_CONFIG_FILE:-$HOME/.config/speech-core/client.env}"
if [[ -f "$env_file" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$env_file"
  set +a
fi

is_running() {
  systemctl --user --quiet is-active "$SERVICE_NAME" 2>/dev/null
}

cache_nix_ld_path() {
  mkdir -p "$CACHE_DIR"
  if command -v nix-shell >/dev/null 2>&1; then
    nix-shell -p python312 stdenv.cc.cc.lib zlib portaudio libsndfile --run '
      echo "$NIX_LDFLAGS" | tr " " "\n" | while read -r f; do
        case "$f" in -L/*) dir="${f#-L}"; [ -d "$dir" ] && echo -n "$dir:"; esac
      done
    ' 2>/dev/null >"$CACHE_DIR/env_ld_library_path.tmp" || true
    if [[ -s "$CACHE_DIR/env_ld_library_path.tmp" ]]; then
      mv "$CACHE_DIR/env_ld_library_path.tmp" "$CACHE_DIR/env_ld_library_path"
    else
      rm -f "$CACHE_DIR/env_ld_library_path.tmp"
    fi
  fi
}

python_imports_ok() {
  [[ -f "$CACHE_DIR/env_ld_library_path" ]] && export LD_LIBRARY_PATH="$(cat "$CACHE_DIR/env_ld_library_path")"
  "$VENV_DIR/bin/python3" - <<'PY_CHECK' >/dev/null 2>&1
import numpy, sounddevice, websockets, dpdfnet
PY_CHECK
}

ensure_dpdfnet_env() {
  if [[ ! -f "$VENV_DIR/bin/python3" ]]; then
    notify-send "DPDFNet denoise" "Setting up Python venv at $VENV_DIR..." -i microphone-sensitivity-high 2>/dev/null || true
    python3 -m venv "$VENV_DIR"
  fi
  cache_nix_ld_path
  if ! python_imports_ok; then
    notify-send "DPDFNet denoise" "Installing Python packages..." -i microphone-sensitivity-high 2>/dev/null || true
    "$VENV_DIR/bin/python3" -m pip install --quiet --upgrade pip
    "$VENV_DIR/bin/python3" -m pip install --quiet --upgrade --force-reinstall 'numpy<2.0' sounddevice websockets dpdfnet
  fi
  if ! python_imports_ok; then
    echo "ERROR: DPDFNet Python environment is still broken." >&2
    echo "Try: rm -rf $VENV_DIR && speech-core-dpdfnet-toggle" >&2
    exit 1
  fi
}

# ── Stop path ──────────────────────────────────────────────────────────────
if is_running; then
  systemctl --user stop "$SERVICE_NAME" >/dev/null 2>&1 || true
  systemctl --user reset-failed "$SERVICE_NAME" >/dev/null 2>&1 || true
  rm -f "$ENABLE_FILE"
  notify-send "DPDFNet denoise OFF" "Microphone denoising stopped for speech-core" -i microphone-sensitivity-muted 2>/dev/null || true
  exit 0
fi

# ── Start path ─────────────────────────────────────────────────────────────

# Bootstrap/repair venv if missing or broken. The venv can exist while missing
# sounddevice/websockets or failing NumPy imports if LD_LIBRARY_PATH was absent.
ensure_dpdfnet_env

# Ensure model is cached. Do not hard-code cache layout; dpdfnet decides it.
notify-send "DPDFNet denoise" "Ensuring dpdfnet2 model is cached..." -i microphone-sensitivity-high 2>/dev/null || true
"$VENV_DIR/bin/python3" - <<'PY_MODEL'
import dpdfnet
dpdfnet.download('dpdfnet2', verbose=False)
print('dpdfnet2 model ready')
PY_MODEL

# Ensure libexec script is in place
if [[ ! -x "$LIBEXEC_DIR/speech-core-dpdfnet-mic" ]]; then
  echo "ERROR: $LIBEXEC_DIR/speech-core-dpdfnet-mic not found. Run install-speech-core-client.sh first." >&2
  exit 1
fi

mkdir -p "$(dirname "$ENABLE_FILE")"
touch "$ENABLE_FILE"
systemctl --user start "$SERVICE_NAME"

# Give it a moment to fail fast
sleep 1
if is_running; then
  notify-send "DPDFNet denoise ON" "Microphone denoising active for speech-core" -i microphone-sensitivity-high 2>/dev/null || true
else
  notify-send "DPDFNet denoise FAILED" "Check: systemctl --user status $SERVICE_NAME" -i dialog-error 2>/dev/null || true
  systemctl --user --no-pager status "$SERVICE_NAME" 2>&1 | tail -10
  exit 1
fi
