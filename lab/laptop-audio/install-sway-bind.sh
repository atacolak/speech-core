#!/usr/bin/env bash
set -euo pipefail

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
if command -v systemctl >/dev/null 2>&1; then
  while IFS= read -r line; do
    case "$line" in
      WAYLAND_DISPLAY=*|DISPLAY=*|SWAYSOCK=*|DBUS_SESSION_BUS_ADDRESS=*|XDG_CURRENT_DESKTOP=*)
        export "$line"
        ;;
    esac
  done < <(systemctl --user show-environment 2>/dev/null || true)
fi
if [[ -z "${SWAYSOCK:-}" ]]; then
  for sock in "$XDG_RUNTIME_DIR"/sway-ipc.*.sock; do
    if [[ -S "$sock" ]]; then
      export SWAYSOCK="$sock"
      break
    fi
  done
fi
if [[ -z "${SWAYSOCK:-}" || ! -S "${SWAYSOCK:-}" ]]; then
  echo "no sway socket found" >&2
  exit 1
fi

swaymsg -s "$SWAYSOCK" 'bindsym --no-repeat Mod4+F9 exec --no-startup-id ${SPEECH_CORE_INSTALL_BIN_DIR:-$HOME/.local/bin}/speech-core-aec-toggle'
