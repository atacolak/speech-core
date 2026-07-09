#!/usr/bin/env bash
set -euo pipefail

echo "== PipeWire/WirePlumber default sources =="
wpctl status 2>/dev/null | sed -n '/Sources:/,/Filters:/p' || true

echo
echo "== Pulse/PipeWire sources =="
pactl list short sources 2>/dev/null || true

echo
echo "== Echo cancel module state =="
pactl list short modules 2>/dev/null | grep -E 'module-echo-cancel|speech_core_echo_cancel' || true

echo
echo "== Attempting to ensure speech_core_echo_cancel =="
if command -v pactl >/dev/null 2>&1; then
  # Unload stale speech_core_echo_cancel modules first; stale modules are a common
  # reason the source exists but carries no mic audio.
  pactl list short modules 2>/dev/null | awk '/module-echo-cancel/ && /speech_core_echo_cancel/ {print $1}' | while read -r id; do
    [[ -n "$id" ]] && pactl unload-module "$id" >/dev/null 2>&1 || true
  done
  pactl load-module module-echo-cancel aec_method=webrtc source_name=speech_core_echo_cancel sink_name=speech_core_echo_cancel_sink source_master=@DEFAULT_SOURCE@ sink_master=@DEFAULT_SINK@ >/dev/null 2>&1 || true
  pactl set-default-source speech_core_echo_cancel >/dev/null 2>&1 || true
fi

echo
echo "== After reload =="
wpctl status 2>/dev/null | sed -n '/Sources:/,/Filters:/p' || true
pactl list short sources 2>/dev/null | grep -E 'speech_core_echo_cancel|alsa_input|bluez_input' || true

echo
echo "If Echo Cancel Source is still silent, use DPDFNet toggle for noise reduction and keep software echo guard enabled for self-echo."
