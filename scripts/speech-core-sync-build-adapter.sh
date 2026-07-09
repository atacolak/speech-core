#!/usr/bin/env bash
set -euo pipefail

remote_host="${SPEECH_CORE_REMOTE_HOST:-}"
remote_dir="${SPEECH_CORE_REMOTE_DIR:-/tmp/speech-core-native-build}"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

ssh "$remote_host" "rm -rf '$remote_dir' && mkdir -p '$remote_dir'"
rsync -az --delete \
  --exclude target \
  --exclude .git \
  "$repo_root/" "$remote_host:$remote_dir/"

ssh "$remote_host" "cd '$remote_dir' && nix-shell --run 'cargo build -p speech-core-mic-adapter -p speech-core-watch'"
ssh "$remote_host" "cd '$remote_dir' && target/debug/speech-core-mic-adapter --help >/dev/null && target/debug/speech-core-watch --help >/dev/null && ls -lh target/debug/speech-core-mic-adapter target/debug/speech-core-watch"

echo "native adapter: $remote_host:$remote_dir/target/debug/speech-core-mic-adapter"
echo "native watcher: $remote_host:$remote_dir/target/debug/speech-core-watch"
echo "client live command: cd $remote_dir && SPEECH_CORE_WS_URL=ws://<server-address>:8765/ws/audio-ingress ./scripts/speech-core-live-session.sh"
