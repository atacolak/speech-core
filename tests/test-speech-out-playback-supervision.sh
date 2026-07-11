#!/usr/bin/env bash
# Deterministic regression tests for playback child supervision (P1 repair).
#
# Tests cover:
#   P1a – _collect_descendants returns all tree PIDs, children before parents
#   P1b – kill_tree terminates the entire descendant tree, no orphans remain
#   P1c – kill_tree exit is bounded within configured timeout
#   P1d – kill_tree repeated calls are idempotent / harmless
#   P1e – Normal completion: process reaps naturally, wait succeeds
#   P1f – Process-substitution stderr helper is cleaned up with the tree
#   P1g – Harness dispatch_turn_response no longer captures subshell PID
#   P1h – kill_tree uses descendant tree walk (pgrep -P), not pgid heuristic
#   P1i – TERM-escaping descendant: root exits, child traps TERM, escalation to KILL
#   P1j – TERM-cooperative tree exits fast without unnecessary timeout
#   P1k – Zombie-aware liveness helpers (_pid_is_alive, _pid_is_zombie)
#   P1l – PID reuse defence: starttime captured, KILL guards against reuse
#
# Uses controlled fake children (sleep). No real audio, no network, no daemons.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
HARNESS="$REPO_ROOT/scripts/speech-out-live-session.sh"
TEST_DIR="$(mktemp -d)"
trap 'rm -rf "$TEST_DIR"' EXIT

PASSED=0
FAILED=0

pass() { PASSED=$((PASSED + 1)); echo "  PASS: $1"; }
fail() { FAILED=$((FAILED + 1)); echo "  FAIL: $1 — $2" >&2; }

# ── Inlined functions from harness (same code, test-isolated) ───────────

_kill_tree_timeout_secs="${SPEECH_OUT_KILL_TIMEOUT_SECS:-3}"
_COLLECT_MAX_NODES=256

_collect_descendants() {
  local root="$1"
  local -A seen=()
  local stack=("$root")
  local all=()
  local max="${_COLLECT_MAX_NODES:-256}"
  local p children child
  while ((${#stack[@]} > 0 && ${#all[@]} < max)); do
    p="${stack[-1]}"
    unset 'stack[-1]'
    [[ -n "${seen[$p]:-}" ]] && continue
    seen[$p]=1
    all+=("$p")
    children="$(pgrep -P "$p" 2>/dev/null || true)"
    for child in $children; do
      [[ -n "$child" && -z "${seen[$child]:-}" ]] && stack+=("$child")
    done
  done
  local idx
  for ((idx = ${#all[@]} - 1; idx >= 0; idx--)); do
    printf '%s ' "${all[$idx]}"
  done
}

# ── Zombie-aware liveness helpers ────────────────────────────────────

_pid_is_zombie() {
  local p="$1"
  [[ -n "$p" ]] || return 1
  local state
  state="$(awk '/^State:/ {print $2}' /proc/"$p"/status 2>/dev/null || true)"
  [[ "$state" == "Z" || "$state" == "z" ]]
}

_pid_is_alive() {
  local p="$1"
  [[ -n "$p" ]] || return 1
  kill -0 "$p" 2>/dev/null || return 1
  _pid_is_zombie "$p" && return 1
  return 0
}

_pid_starttime() {
  local p="$1"
  awk '{print $22}' /proc/"$p"/stat 2>/dev/null || printf '0'
}

kill_tree() {
  local pid="${1:-}"
  [[ -n "$pid" ]] || return 0
  kill -0 "$pid" 2>/dev/null || return 0

  local pids
  pids="$(_collect_descendants "$pid")"

  # Capture start times for PID reuse safety.
  local -A _kt_starttimes=()
  local p
  for p in $pids; do
    _kt_starttimes[$p]="$(_pid_starttime "$p")"
  done

  # Phase 1: SIGTERM to every PID (leaf-first).
  for p in $pids; do
    kill -TERM "$p" 2>/dev/null || true
  done

  # Phase 2: Wait for ALL captured PIDs to be dead or zombie.
  # This closes the escalation hole where a TERM-ignoring descendant
  # survived because the root exited first.
  local waited=0 max_ticks=$(( _kill_tree_timeout_secs * 10 ))
  local all_dead=0
  while (( waited < max_ticks )); do
    all_dead=1
    for p in $pids; do
      if _pid_is_alive "$p"; then
        all_dead=0
        break
      fi
    done
    (( all_dead )) && break
    sleep 0.1 2>/dev/null || true
    waited=$((waited + 1))
  done

  if (( all_dead )); then
    wait "$pid" 2>/dev/null || true
    return 0
  fi

  # Phase 3: Escalate — KILL every survivor with PID reuse defence.
  for p in $pids; do
    if _pid_is_alive "$p"; then
      local cur_st
      cur_st="$(_pid_starttime "$p")"
      if [[ -n "${_kt_starttimes[$p]:-}" && "${_kt_starttimes[$p]}" != "0" && \
            -n "$cur_st" && "$cur_st" != "0" && \
            "${_kt_starttimes[$p]}" != "$cur_st" ]]; then
        continue  # PID was reused; don't kill the new occupant.
      fi
      kill -KILL "$p" 2>/dev/null || true
    fi
  done

  # Phase 4: Bounded verify — confirm KILL took effect.
  local verify_waited=0 verify_max=20  # 2 seconds
  while (( verify_waited < verify_max )); do
    all_dead=1
    for p in $pids; do
      if _pid_is_alive "$p"; then
        all_dead=0
        break
      fi
    done
    (( all_dead )) && break
    sleep 0.1 2>/dev/null || true
    verify_waited=$((verify_waited + 1))
  done

  wait "$pid" 2>/dev/null || true
}

# ── Helper: spawn a controlled fake process tree ───────────────────────
# Creates a root that spawns depth-1 sleep children using a flat script.
# Returns the root PID.
#   $1 = number of leaf sleep processes (fan-out, not depth)
#   $2 = sleep seconds per process
spawn_fake_tree() {
  local fanout="${1:-2}"
  local sleep_secs="${2:-60}"
  local tmp="$TEST_DIR/spawn_$$"
  mkdir -p "$tmp"

  # Write a simple spawner: background N sleep processes, then wait.
  cat >"$tmp/spawner.sh" <<'ENDSCRIPT'
#!/usr/bin/env bash
fanout="${1:-2}"
secs="${2:-60}"
declare -a pids=()
for ((i = 0; i < fanout; i++)); do
  sleep "$secs" &
  pids+=($!)
done
# Wait for all children; if one dies, continue waiting for the rest.
for pid in "${pids[@]}"; do
  wait "$pid" 2>/dev/null || true
done
ENDSCRIPT
  chmod +x "$tmp/spawner.sh"

  # Redirect all fds: inside a command substitution, inherited stdout
  # keeps the capture pipe open until every writer exits.  Closing
  # stdin/stdout/stderr for the background spawner prevents that.
  "$tmp/spawner.sh" "$fanout" "$sleep_secs" </dev/null >/dev/null 2>&1 &
  local root_pid=$!
  # Brief settle so children are visible in /proc.
  sleep 0.3
  echo "$root_pid"
}

# ── Helper: ensure no PIDs from a list are still alive ─────────────────
# Treats zombies as effectively dead (cannot run code, cannot be killed).
assert_all_dead() {
  local pids="$1"
  local label="$2"
  local all_ok=1
  for p in $pids; do
    [[ -n "$p" ]] || continue
    if _pid_is_alive "$p"; then
      kill -KILL "$p" 2>/dev/null || true
      fail "$label" "pid $p still alive (not zombie)"
      all_ok=0
    fi
  done
  [[ $all_ok -eq 1 ]] && pass "$label"
}

# ── P1a ────────────────────────────────────────────────────────────────

echo "=== P1a: _collect_descendants returns full tree, leaf-first order ==="

test_p1a() {
  local root_pid
  root_pid="$(spawn_fake_tree 3 60)"
  [[ -n "$root_pid" ]] || { fail "P1a: spawn" "failed to create fake tree"; return; }

  if ! kill -0 "$root_pid" 2>/dev/null; then
    fail "P1a: root alive" "root $root_pid died immediately"
    return
  fi
  pass "P1a: root $root_pid is alive"

  # Collect descendants.
  local pids
  pids="$(_collect_descendants "$root_pid")"

  # Should have at least 4 PIDs (root + 3 sleep children).
  local pid_count
  pid_count="$(echo "$pids" | tr ' ' '\n' | grep -c '^[0-9]' || echo 0)"
  if (( pid_count >= 4 )); then
    pass "P1a: _collect_descendants found $pid_count PIDs (>=4 for fanout 3)"
  else
    fail "P1a: descendant count" "expected >=4, got $pid_count (pids: $pids)"
  fi

  # Verify leaf-first: the first PID should be a leaf (sleep), last is root.
  local first_pid last_pid
  first_pid="$(echo "$pids" | awk '{print $1}')"
  last_pid="$(echo "$pids" | awk '{print $NF}')"

  local leaf_children
  leaf_children="$(pgrep -P "$first_pid" 2>/dev/null || true)"
  if [[ -z "${leaf_children//[[:space:]]/}" ]]; then
    pass "P1a: first pid ($first_pid) is a leaf (no children)"
  else
    fail "P1a: leaf check" "first pid $first_pid has children: $leaf_children"
  fi

  if [[ "$last_pid" == "$root_pid" ]]; then
    pass "P1a: last pid ($last_pid) is the root"
  else
    fail "P1a: root position" "expected root $root_pid at end, got $last_pid"
  fi

  # Cleanup.
  kill_tree "$root_pid"
}

test_p1a

# ── P1b ────────────────────────────────────────────────────────────────

echo ""
echo "=== P1b: kill_tree terminates entire tree, no orphans ==="

test_p1b() {
  local root_pid
  root_pid="$(spawn_fake_tree 3 60)"

  local all_pids
  all_pids="$(_collect_descendants "$root_pid")"
  local pid_count
  pid_count="$(echo "$all_pids" | tr ' ' '\n' | grep -c '^[0-9]' || echo 0)"
  echo "  INFO: tree has $pid_count PIDs"

  # Verify root is alive before kill.
  if kill -0 "$root_pid" 2>/dev/null; then
    pass "P1b: root alive before kill_tree"
  else
    fail "P1b: pre-kill" "root died before kill_tree"
    return
  fi

  kill_tree "$root_pid"

  # Verify root is dead.
  if ! kill -0 "$root_pid" 2>/dev/null; then
    pass "P1b: root dead after kill_tree"
  else
    fail "P1b: root alive after" "root $root_pid still alive after kill_tree"
  fi

  # Verify no descendants are still alive.
  assert_all_dead "$all_pids" "P1b: all descendants dead (no orphans)"
}

test_p1b

# ── P1c ────────────────────────────────────────────────────────────────

echo ""
echo "=== P1c: kill_tree exit bounded within configured timeout ==="

test_p1c() {
  local saved_timeout="${_kill_tree_timeout_secs:-3}"
  _kill_tree_timeout_secs=1

  local root_pid
  root_pid="$(spawn_fake_tree 2 120)"

  local start_sec
  start_sec="$(date +%s)"

  kill_tree "$root_pid"

  local end_sec
  end_sec="$(date +%s)"
  local elapsed=$(( end_sec - start_sec ))

  if (( elapsed <= 5 )); then
    pass "P1c: kill_tree finished in ${elapsed}s (bounded <= 5s)"
  else
    fail "P1c: bounded exit" "kill_tree took ${elapsed}s, expected <= 5s"
  fi

  if ! kill -0 "$root_pid" 2>/dev/null; then
    pass "P1c: root dead after bounded kill_tree"
  else
    kill -KILL "$root_pid" 2>/dev/null || true
    fail "P1c: root dead" "still alive after kill_tree"
  fi

  _kill_tree_timeout_secs="$saved_timeout"
}

test_p1c

# ── P1d ────────────────────────────────────────────────────────────────

echo ""
echo "=== P1d: kill_tree repeated calls are idempotent / harmless ==="

test_p1d() {
  local root_pid
  root_pid="$(spawn_fake_tree 2 60)"

  # First kill.
  kill_tree "$root_pid"
  if ! kill -0 "$root_pid" 2>/dev/null; then
    pass "P1d: root dead after first kill_tree"
  else
    fail "P1d: first kill" "root still alive"
  fi

  # Second kill — should be harmless.
  if kill_tree "$root_pid" 2>&1; then
    pass "P1d: second kill_tree exits cleanly (already dead)"
  else
    fail "P1d: second kill" "unexpected error on re-kill"
  fi

  # Third kill on a non-existent PID.
  if kill_tree 99999999 2>&1; then
    pass "P1d: kill_tree on non-existent PID is harmless"
  else
    fail "P1d: fake PID" "unexpected error"
  fi
}

test_p1d

# ── P1e ────────────────────────────────────────────────────────────────

echo ""
echo "=== P1e: Normal completion — process reaps naturally ==="

test_p1e() {
  local root_pid
  root_pid="$(spawn_fake_tree 1 0.3)"  # Short sleep, exits naturally

  # Wait for root to exit naturally.
  if wait "$root_pid" 2>/dev/null; then
    pass "P1e: root reaped naturally via wait"
  else
    # May have already exited.
    sleep 0.5
    if ! kill -0 "$root_pid" 2>/dev/null; then
      pass "P1e: root exited naturally (already gone)"
    else
      kill -KILL "$root_pid" 2>/dev/null || true
      fail "P1e: natural reap" "root still alive after expected exit"
    fi
  fi
}

test_p1e

# ── P1f ────────────────────────────────────────────────────────────────

echo ""
echo "=== P1f: Process-substitution stderr helper cleanup ==="

test_p1f() {
  local tmp="$TEST_DIR/stderr_test"
  mkdir -p "$tmp"
  local log="$tmp/stderr.log"
  :> "$log"

  # Simulate harness stderr capture pattern.
  (
    for i in $(seq 1 3); do
      echo "stderr line $i" >&2
      sleep 0.1
    done
  ) 2> >(while IFS= read -r line; do
    echo "$line" >> "$log"
  done) &
  local child_pid=$!

  wait "$child_pid" 2>/dev/null || true
  sleep 0.3

  local line_count
  line_count="$(wc -l < "$log" 2>/dev/null || echo 0)"
  if (( line_count == 3 )); then
    pass "P1f: process substitution delivered all $line_count lines"
  else
    fail "P1f: stderr delivery" "expected 3 lines, got $line_count"
  fi

  # Test escalated: parent killed while stderr still flowing.
  local log2="$tmp/stderr_killed.log"
  :> "$log2"

  (
    while true; do
      echo "still alive" >&2
      sleep 0.2
    done
  ) 2> >(while IFS= read -r line; do
    echo "$line" >> "$log2"
  done) &
  local long_pid=$!

  sleep 0.5

  kill_tree "$long_pid"
  wait "$long_pid" 2>/dev/null || true
  sleep 0.3

  # The process-substitution bash should have exited when its input pipe closed.
  # Check no orphaned "while IFS= read" processes tied to our log file.
  local orphans
  orphans="$(pgrep -f "while IFS= read" 2>/dev/null | while read op; do
    # Check if this process has our log file open.
    if ls -l /proc/"$op"/fd 2>/dev/null | grep -q "$log2"; then
      echo "$op"
    fi
  done || true)"
  if [[ -z "${orphans//[[:space:]]/}" ]]; then
    pass "P1f: no orphaned stderr process substitution after kill_tree"
  else
    for op in $orphans; do kill -KILL "$op" 2>/dev/null || true; done
    fail "P1f: stderr cleanup" "orphaned: $orphans"
  fi
}

test_p1f

# ── P1g ────────────────────────────────────────────────────────────────

echo ""
echo "=== P1g: dispatch_turn_response no longer captures subshell PID ==="

test_p1g() {
  # Old pattern: tts_pid=$! in dispatch_turn_response
  if grep -q 'tts_pid=\$!' "$HARNESS"; then
    fail "P1g: old tts_pid" "dispatch_turn_response still contains tts_pid=\$!"
  else
    pass "P1g: old tts_pid=\$! removed from dispatch_turn_response"
  fi

  # New pattern: child_pid=$! inside run_speech_out
  if grep -q 'child_pid=\$!' "$HARNESS"; then
    pass "P1g: run_speech_out captures real child_pid=\$!"
  else
    fail "P1g: child PID" "run_speech_out missing child_pid=\$!"
  fi

  # run_speech_out writes child_pid to tts_pid_file
  if grep -q 'child_pid.*tts_pid_file' "$HARNESS" || \
     grep -q '"$child_pid".*"$tts_pid_file"' "$HARNESS"; then
    pass "P1g: run_speech_out writes child_pid to tts_pid_file"
  else
    fail "P1g: pid file write" "run_speech_out does not write child_pid to tts_pid_file"
  fi
}

test_p1g

# ── P1h ────────────────────────────────────────────────────────────────

echo ""
echo "=== P1h: kill_tree uses descendant tree walk ==="

test_p1h() {
  if grep -q '_collect_descendants' "$HARNESS"; then
    pass "P1h: _collect_descendants function defined in harness"
  else
    fail "P1h: _collect_descendants" "function not found in harness"
  fi

  if grep -q 'pgrep -P' "$HARNESS"; then
    pass "P1h: kill_tree traverses descendants via pgrep -P"
  else
    fail "P1h: pgrep -P" "tree walk missing pgrep -P"
  fi

  # Verify the function is invoked from kill_tree.
  if grep -A10 '^kill_tree()' "$HARNESS" | grep -q '_collect_descendants'; then
    pass "P1h: kill_tree calls _collect_descendants"
  else
    fail "P1h: kill_tree integration" "kill_tree does not call _collect_descendants"
  fi
}

test_p1h

# ── P1i ────────────────────────────────────────────────────────────────

echo ""
echo "=== P1i: TERM-escaping descendant — root exits, escalation to KILL ==="

test_p1i() {
  local tmp="$TEST_DIR/p1i"
  mkdir -p "$tmp"

  # Child script: explicitly trap (ignore) SIGTERM, sleep forever.
  cat >"$tmp/ignore_term.sh" <<'ENDSCRIPT'
#!/usr/bin/env bash
trap '' TERM
sleep 300
ENDSCRIPT
  chmod +x "$tmp/ignore_term.sh"

  # Root script: spawn TERM-ignoring child, then sleep (will die on TERM).
  cat >"$tmp/root.sh" <<'ENDSCRIPT'
#!/usr/bin/env bash
"$1" &
sleep 300
ENDSCRIPT
  chmod +x "$tmp/root.sh"

  # Start the tree.
  "$tmp/root.sh" "$tmp/ignore_term.sh" </dev/null >/dev/null 2>&1 &
  local root_pid=$!
  sleep 0.5  # Let child settle in /proc.

  # Collect all PIDs before kill.
  local all_pids
  all_pids="$(_collect_descendants "$root_pid")"
  local pid_count
  pid_count="$(echo "$all_pids" | tr ' ' '\n' | grep -c '^[0-9]' || echo 0)"
  echo "  INFO: tree has $pid_count PIDs: $all_pids"

  if (( pid_count < 2 )); then
    # Tree wasn't fully formed yet, but the root may have died quickly.
    # Try a second attempt with a longer settle.
    kill -KILL "$root_pid" 2>/dev/null || true
    "$tmp/root.sh" "$tmp/ignore_term.sh" </dev/null >/dev/null 2>&1 &
    root_pid=$!
    sleep 0.8
    all_pids="$(_collect_descendants "$root_pid")"
    pid_count="$(echo "$all_pids" | tr ' ' '\n' | grep -c '^[0-9]' || echo 0)"
    echo "  INFO: retry tree has $pid_count PIDs: $all_pids"
  fi

  if (( pid_count < 2 )); then
    kill -KILL "$root_pid" 2>/dev/null || true
    fail "P1i: spawn" "could not create 2-process tree (got $pid_count)"
    return
  fi

  # Use a short timeout so we prove escalation happens.
  local saved_timeout="${_kill_tree_timeout_secs}"
  _kill_tree_timeout_secs=1

  kill_tree "$root_pid"

  _kill_tree_timeout_secs="$saved_timeout"

  # Verify: every captured PID must now be dead (or zombie).
  local survivor=0
  for p in $all_pids; do
    if _pid_is_alive "$p"; then
      echo "  FAIL: pid $p survived kill_tree" >&2
      survivor=1
      kill -KILL "$p" 2>/dev/null || true
    fi
  done

  if (( survivor == 0 )); then
    pass "P1i: all PIDs dead after escalation (TERM-escaping descendant killed)"
  else
    fail "P1i: TERM-escaping" "$survivor PIDs survived kill_tree"
  fi
}

test_p1i

# ── P1j ────────────────────────────────────────────────────────────────

echo ""
echo "=== P1j: TERM-cooperative tree exits fast without unnecessary timeout ==="

test_p1j() {
  # spawn_fake_tree creates sleep children that die immediately on TERM.
  local root_pid
  root_pid="$(spawn_fake_tree 3 120)"

  local start_sec
  start_sec="$(date +%s)"

  kill_tree "$root_pid"

  local end_sec
  end_sec="$(date +%s)"
  local elapsed=$(( end_sec - start_sec ))

  # With TERM-cooperative children and default 3s timeout, we should
  # finish well within the timeout — all PIDs die on TERM immediately.
  if (( elapsed < 3 )); then
    pass "P1j: TERM-cooperative tree exited in ${elapsed}s (under timeout)"
  else
    fail "P1j: fast exit" "took ${elapsed}s, expected < 3s"
  fi

  # Root must be dead.
  if ! kill -0 "$root_pid" 2>/dev/null; then
    pass "P1j: root dead after cooperative kill_tree"
  else
    kill -KILL "$root_pid" 2>/dev/null || true
    fail "P1j: root dead" "still alive after cooperative kill_tree"
  fi
}

test_p1j

# ── P1k ────────────────────────────────────────────────────────────────

echo ""
echo "=== P1k: Zombie-aware liveness helpers ==="

test_p1k() {
  # _pid_is_zombie on a non-existent PID.
  if _pid_is_zombie 99999999; then
    fail "P1k: _pid_is_zombie" "non-existent PID reported as zombie"
  else
    pass "P1k: _pid_is_zombie returns false for non-existent PID"
  fi

  # _pid_is_alive on a non-existent PID.
  if _pid_is_alive 99999999; then
    fail "P1k: _pid_is_alive" "non-existent PID reported as alive"
  else
    pass "P1k: _pid_is_alive returns false for non-existent PID"
  fi

  # _pid_is_alive on our own shell PID.
  if _pid_is_alive $$; then
    pass "P1k: _pid_is_alive returns true for current shell PID"
  else
    fail "P1k: _pid_is_alive" "current shell PID $$ reported as dead"
  fi

  # _pid_is_zombie on our own shell PID (should not be zombie).
  if _pid_is_zombie $$; then
    fail "P1k: _pid_is_zombie" "current shell PID reported as zombie"
  else
    pass "P1k: _pid_is_zombie returns false for live process"
  fi

  # _pid_starttime returns a number for a live PID.
  local st
  st="$(_pid_starttime $$)"
  if [[ -n "$st" && "$st" =~ ^[0-9]+$ && "$st" != "0" ]]; then
    pass "P1k: _pid_starttime returns numeric value for live PID ($st)"
  else
    fail "P1k: _pid_starttime" "got '$st' for $$, expected non-zero number"
  fi

  # _pid_starttime returns 0 for non-existent PID.
  st="$(_pid_starttime 99999999)"
  if [[ "$st" == "0" ]]; then
    pass "P1k: _pid_starttime returns 0 for non-existent PID"
  else
    fail "P1k: _pid_starttime fake" "got '$st' for non-existent PID, expected 0"
  fi
}

test_p1k

# ── P1l ────────────────────────────────────────────────────────────────

echo ""
echo "=== P1l: PID reuse defence — starttime guard on KILL escalation ==="

test_p1l() {
  # Start a process, capture its starttime, then let it die.
  # Run kill_tree on the PID; the starttime mismatch on the reused
  # slot (if any) should prevent killing the new occupant.
  #
  # We can't force PID reuse deterministically, but we can prove:
  #   a) starttime is captured before TERM
  #   b) kill_tree with an already-dead PID is harmless
  #   c) starttime check exits cleanly when /proc is readable

  local tmp="$TEST_DIR/p1l"
  mkdir -p "$tmp"

  # Spawn a short-lived process to get a PID that will die.
  sleep 0.1 &
  local short_pid=$!
  wait "$short_pid" 2>/dev/null || true

  # kill_tree on an already-dead PID should be harmless and fast.
  local start_sec
  start_sec="$(date +%s)"
  kill_tree "$short_pid" 2>&1
  local elapsed
  elapsed=$(($(date +%s) - start_sec))

  if (( elapsed <= 2 )); then
    pass "P1l: kill_tree on dead PID exits quickly (${elapsed}s)"
  else
    fail "P1l: dead PID" "kill_tree took ${elapsed}s on dead PID"
  fi

  # Verify the KILL escalation path handles starttime mismatch gracefully.
  # Spawn a tree, capture PIDs, kill the root (so children are reparented),
  # then call kill_tree again — it should handle the already-dead root.
  local root_pid
  root_pid="$(spawn_fake_tree 2 60)"
  local all_pids
  all_pids="$(_collect_descendants "$root_pid")"

  # Kill and verify.
  kill_tree "$root_pid"

  # Second call should be a no-op (already dead).
  if kill_tree "$root_pid" 2>&1; then
    pass "P1l: re-kill on dead tree exits cleanly (starttime guard harmless)"
  else
    fail "P1l: re-kill" "unexpected error on re-kill of dead tree"
  fi

  # All PIDs gone.
  assert_all_dead "$all_pids" "P1l: no survivors after double kill_tree"
}

test_p1l

# ── Summary ────────────────────────────────────────────────────────────

echo ""
echo "============================================"
echo "Results: $PASSED passed, $FAILED failed"
echo "============================================"

if (( FAILED > 0 )); then
  exit 1
fi
exit 0
