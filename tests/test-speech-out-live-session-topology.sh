#!/usr/bin/env bash
# Execute the actual speech-out live harness against fake binaries and verify
# that one signal tears down the complete owned process topology.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
HARNESS="$REPO_ROOT/scripts/speech-out-live-session.sh"
TEST_DIR="$(mktemp -d)"
BIN_DIR="$TEST_DIR/bin"
STATE_DIR="$TEST_DIR/state"
RUN_DIR="$TEST_DIR/run"
mkdir -p "$BIN_DIR" "$STATE_DIR" "$RUN_DIR"

HARNESS_PID=""
CAPTURED_PIDS=()

authoritative_alive() {
  local pid="$1" expected_start="${2:-}"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  local state start
  state="$(awk '/^State:/ {print $2}' "/proc/$pid/status" 2>/dev/null || true)"
  [[ "$state" == "Z" || "$state" == "z" ]] && return 1
  if [[ -n "$expected_start" ]]; then
    start="$(awk '{print $22}' "/proc/$pid/stat" 2>/dev/null || true)"
    [[ "$start" == "$expected_start" ]] || return 1
  fi
  return 0
}

cleanup_test() {
  set +e
  if [[ -n "$HARNESS_PID" ]]; then
    kill -KILL "$HARNESS_PID" 2>/dev/null || true
  fi
  local pid
  for pid in "${CAPTURED_PIDS[@]:-}"; do
    [[ "$pid" =~ ^[0-9]+$ ]] && kill -KILL "$pid" 2>/dev/null || true
  done
  # Unique-path fallback only: never match global speech process names.
  while read -r pid; do
    [[ "$pid" =~ ^[0-9]+$ ]] && kill -KILL "$pid" 2>/dev/null || true
  done < <(grep -lF -- "$TEST_DIR" /proc/[0-9]*/cmdline 2>/dev/null | sed -E 's#/proc/([0-9]+)/cmdline#\1#')
  rm -rf "$TEST_DIR"
}
trap cleanup_test EXIT

cat >"$BIN_DIR/speech-core-watch" <<'FAKE_WATCH'
#!/usr/bin/env bash
set -u
for arg in "$@"; do
  [[ "$arg" == "--help" ]] && { echo 'fake speech-core-watch'; exit 0; }
done
# Ignore TERM/INT deliberately. Direct kill_tree ownership must escalate us to
# KILL; the secondary no-wait stale-process sweep cannot make this test pass.
trap '' TERM INT
if [[ " $* " == *" --stdin-events "* ]]; then
  printf '%s\n' "$$" >"$TOPOLOGY_STATE/ui.pid"
  cat >/dev/null
else
  printf '%s\n' "$$" >"$TOPOLOGY_STATE/json.pid"
  printf '%s\n' '{"event":"turn_started","stream_session_id":"topology-session"}'
  printf '%s\n' '{"event":"transcript_token_committed","stream_session_id":"topology-session","text":"hello"}'
  printf '%s\n' '{"event":"transcript_committed","stream_session_id":"topology-session","text":"hello world"}'
  while :; do sleep 300; done
fi
FAKE_WATCH

cat >"$BIN_DIR/speech-core-mic-adapter" <<'FAKE_ADAPTER'
#!/usr/bin/env bash
set -u
for arg in "$@"; do
  [[ "$arg" == "--help" ]] && { echo 'fake speech-core-mic-adapter'; exit 0; }
done
printf '%s\n' "$$" >"$TOPOLOGY_STATE/adapter.pid"
trap '' TERM INT
while :; do sleep 300; done
FAKE_ADAPTER

cat >"$BIN_DIR/speech-out" <<'FAKE_OUT'
#!/usr/bin/env bash
set -u
for arg in "$@"; do
  [[ "$arg" == "--help" ]] && { echo 'fake speech-out'; exit 0; }
done
printf '%s\n' "$$" >"$TOPOLOGY_STATE/out.pid"
# The real client handles TERM and may have a stubborn playback descendant.
# Simulate exactly that: root exits on TERM; descendant ignores TERM/INT.
bash -c 'trap "" TERM INT; printf "%s\n" "$$" >"$TOPOLOGY_STATE/out-desc.pid"; while :; do sleep 300; done' &
desc=$!
trap 'exit 0' TERM INT
wait "$desc"
FAKE_OUT

chmod +x "$BIN_DIR/speech-core-watch" "$BIN_DIR/speech-core-mic-adapter" "$BIN_DIR/speech-out"
cp "$HARNESS" "$BIN_DIR/speech-out-live-session.sh"
chmod +x "$BIN_DIR/speech-out-live-session.sh"

export TOPOLOGY_STATE="$STATE_DIR"
export SPEECH_CORE_WS_URL='ws://127.0.0.1:1/fake-core'
export SPEECH_OUT_WS_URL='ws://127.0.0.1:1/fake-out'
export SPEECH_CORE_STREAM_ID='topology.test.stream'
export SPEECH_CORE_STREAM_SESSION_ID='topology-session'
export SPEECH_CORE_RECORD_AUDIO=0
export SPEECH_OUT_REAP_STALE=0
export SPEECH_OUT_RUN_DIR="$RUN_DIR"
export SPEECH_OUT_KILL_TIMEOUT_SECS=1
export SPEECH_OUT_NO_TTY_OK=1

"$BIN_DIR/speech-out-live-session.sh" --no-record-audio \
  >"$RUN_DIR/harness.out" 2>"$RUN_DIR/harness.err" &
HARNESS_PID=$!
printf '%s\n' "$HARNESS_PID" >"$STATE_DIR/harness.pid"

required=(ui.pid json.pid adapter.pid out.pid out-desc.pid)
deadline=$((SECONDS + 10))
while (( SECONDS < deadline )); do
  ready=1
  for file in "${required[@]}"; do
    [[ -s "$STATE_DIR/$file" ]] || ready=0
  done
  (( ready )) && break
  authoritative_alive "$HARNESS_PID" || {
    echo 'FAIL: harness exited before topology became ready' >&2
    cat "$RUN_DIR/harness.err" >&2 || true
    exit 1
  }
  sleep 0.05
done
for file in "${required[@]}"; do
  [[ -s "$STATE_DIR/$file" ]] || {
    echo "FAIL: missing readiness PID file: $file" >&2
    cat "$RUN_DIR/harness.out" >&2 || true
    cat "$RUN_DIR/harness.err" >&2 || true
    exit 1
  }
done

# Capture the full tree before signalling, with starttime for PID-reuse defence.
collect_tree() {
  local root="$1" pid child
  local -a queue=("$root")
  local -A seen=()
  while ((${#queue[@]})); do
    pid="${queue[0]}"
    queue=("${queue[@]:1}")
    [[ -n "${seen[$pid]:-}" ]] && continue
    seen[$pid]=1
    CAPTURED_PIDS+=("$pid")
    while read -r child; do
      [[ "$child" =~ ^[0-9]+$ ]] && queue+=("$child")
    done < <(pgrep -P "$pid" 2>/dev/null || true)
  done
}
collect_tree "$HARNESS_PID"

declare -A STARTTIMES=()
for pid in "${CAPTURED_PIDS[@]}"; do
  STARTTIMES[$pid]="$(awk '{print $22}' "/proc/$pid/stat" 2>/dev/null || true)"
done

# Every fake component that advertised readiness must already be in the
# harness-owned tree. This prevents broad stale-process cleanup from masking a
# sibling/ownership regression.
for file in "${required[@]}"; do
  advertised_pid="$(cat "$STATE_DIR/$file")"
  found=0
  for pid in "${CAPTURED_PIDS[@]}"; do
    [[ "$pid" == "$advertised_pid" ]] && found=1
  done
  (( found )) || {
    echo "FAIL: $file advertised PID $advertised_pid outside harness-owned tree" >&2
    exit 1
  }
done

direct_children="$(pgrep -P "$HARNESS_PID" 2>/dev/null | wc -l)"
(( direct_children >= 4 )) || {
  echo "FAIL: expected UI watcher, JSON watcher, consumer, and adapter as direct owned children; got $direct_children" >&2
  exit 1
}

out_desc="$(cat "$STATE_DIR/out-desc.pid")"

# A rapid repeated signal exercises the idempotence guard without requiring a
# second cleanup path. The second signal may arrive after process exit, which is
# also valid and harmless.
kill -TERM "$HARNESS_PID"
sleep 0.05
kill -TERM "$HARNESS_PID" 2>/dev/null || true

exit_deadline=$((SECONDS + 12))
while authoritative_alive "$HARNESS_PID" "${STARTTIMES[$HARNESS_PID]}" && (( SECONDS < exit_deadline )); do
  sleep 0.05
done
if authoritative_alive "$HARNESS_PID" "${STARTTIMES[$HARNESS_PID]}"; then
  echo 'FAIL: harness did not exit within 12 seconds after one TERM' >&2
  ps -o pid,ppid,pgid,stat,etime,cmd --forest -p "$(IFS=,; echo "${CAPTURED_PIDS[*]}")" >&2 2>/dev/null || true
  exit 1
fi
wait "$HARNESS_PID" 2>/dev/null || true
HARNESS_PID=""

survivors=()
for pid in "${CAPTURED_PIDS[@]}"; do
  [[ "$pid" == "$(cat "$STATE_DIR/harness.pid")" ]] && continue
  if authoritative_alive "$pid" "${STARTTIMES[$pid]}"; then survivors+=("$pid"); fi
done
if ((${#survivors[@]})); then
  echo "FAIL: owned descendants survived cleanup: ${survivors[*]}" >&2
  ps -o pid,ppid,pgid,stat,etime,cmd --forest -p "$(IFS=,; echo "${survivors[*]}")" >&2 2>/dev/null || true
  exit 1
fi

# No process whose command line references this unique test tree may remain.
path_survivors=()
while read -r procfile; do
  pid="${procfile#/proc/}"; pid="${pid%/cmdline}"
  [[ "$pid" == "$$" || "$pid" == "$BASHPID" ]] && continue
  authoritative_alive "$pid" && path_survivors+=("$pid")
done < <(grep -lF -- "$TEST_DIR" /proc/[0-9]*/cmdline 2>/dev/null || true)
if ((${#path_survivors[@]})); then
  echo "FAIL: unique-path processes survived: ${path_survivors[*]}" >&2
  exit 1
fi

[[ "$(grep -c '^session ended$' "$RUN_DIR/harness.out" || true)" == 1 ]] || {
  echo 'FAIL: cleanup summary was not emitted exactly once' >&2
  cat "$RUN_DIR/harness.out" >&2
  exit 1
}
if grep -qi 'No such device or address' "$RUN_DIR/harness.err"; then
  echo 'FAIL: attempted to use /dev/tty without a controlling terminal' >&2
  cat "$RUN_DIR/harness.err" >&2
  exit 1
fi

echo 'PASS: actual live harness exits on one signal and leaves no owned descendants'
