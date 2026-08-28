#!/usr/bin/env bash
# Thin launcher for the Talker voice edge (MVP B).
# Prefer this over speech-out-live-session when you want the real Talker profile
# (not canned "heard you") with interrupt triple a/b/c.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
py="${SPEECH_TALKER_PYTHON:-python3}"
exec "$py" "$script_dir/speech_talker_session.py" "$@"
