#!/usr/bin/env bash
set -euo pipefail

secs="${1:-8}"
out_dir="${2:-${SPEECH_CORE_STATE_DIR:-$HOME/.local/state/speech-core}/dpdfnet-test/$(date +%Y%m%d-%H%M%S)}"
libexec_dir="${SPEECH_CORE_LIBEXEC_DIR:-$HOME/.local/libexec/speech-core}"
mkdir -p "$out_dir"

echo "recording DPDFNet dry-run comparison for ${secs}s"
echo "  out_dir: $out_dir"

timeout "${secs}s" "$libexec_dir/speech-core-dpdfnet-mic" \
  --dry-run \
  --save-wav "$out_dir/mic.wav" \
  >"$out_dir/dpdfnet.out" \
  2>"$out_dir/dpdfnet.err" || code=$?
code="${code:-0}"
# timeout exits 124, which is expected for this recording mode.
if [[ "$code" != "0" && "$code" != "124" ]]; then
  echo "DPDFNet test failed with exit code $code" >&2
  tail -80 "$out_dir/dpdfnet.err" >&2 || true
  exit "$code"
fi

echo "files:"
ls -lh "$out_dir" || true

echo
echo "timing:"
grep -E "Mean:|Median:|P95:|Headroom:|Status:" "$out_dir/dpdfnet.out" || true

echo
echo "playback commands:"
echo "  pw-play '$out_dir/mic_raw.wav'"
echo "  pw-play '$out_dir/mic_denoised.wav'"
