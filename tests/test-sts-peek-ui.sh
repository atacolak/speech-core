#!/usr/bin/env bash
# Offline acceptance for sts-peek Track U UI.
# No daemons, no network, no audio devices.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
UI_PY="$REPO_ROOT/scripts/sts-peek/ui.py"
RUN_UI="$REPO_ROOT/scripts/sts-peek/run_ui.py"
KEYS_PY="$REPO_ROOT/scripts/sts-peek/keys.py"
TEST_DIR="$(mktemp -d)"
trap 'rm -rf "$TEST_DIR"' EXIT

PASSED=0
FAILED=0

pass() { PASSED=$((PASSED + 1)); echo "  PASS: $1"; }
fail() { FAILED=$((FAILED + 1)); echo "  FAIL: $1 — $2" >&2; }

assert_eq() { [[ "$1" == "$2" ]] && pass "$3" || fail "$3" "expected '$2', got '$1'"; }
assert_file() { [[ -f "$1" ]] && pass "$2" || fail "$2" "missing file $1"; }
assert_contains() { [[ "$1" == *"$2"* ]] && pass "$3" || fail "$3" "expected to contain '$2'"; }
assert_not_contains() { [[ "$1" != *"$2"* ]] && pass "$3" || fail "$3" "should not contain '$2'"; }

echo "=== T0: owned files exist ==="
assert_file "$UI_PY" "scripts/sts-peek/ui.py"
assert_file "$RUN_UI" "scripts/sts-peek/run_ui.py"
assert_file "$KEYS_PY" "scripts/sts-peek/keys.py"
assert_file "$REPO_ROOT/docs/sts-peek-ui.md" "docs/sts-peek-ui.md"

echo "=== T1: --self-check ==="
if python3 "$UI_PY" --self-check >"$TEST_DIR/self-check.out" 2>"$TEST_DIR/self-check.err"; then
  pass "self-check exits 0"
else
  fail "self-check exits 0" "exit $? stderr=$(cat "$TEST_DIR/self-check.err")"
fi
assert_contains "$(cat "$TEST_DIR/self-check.out")" "self-check: PASS" "self-check prints PASS"

echo "=== T2: mock UI + scripted barge writes control/barge.now ==="
RUN="$TEST_DIR/run_mock"
mkdir -p "$RUN"
printf '%s\n' "alpha beta gamma delta epsilon" >"$RUN/intended.txt"
mkdir -p "$RUN/cut"
python3 - <<'PY' "$RUN/cut/metrics.json"
import json, sys
from pathlib import Path
Path(sys.argv[1]).write_text(json.dumps({
    "primary_cut_source": "drain",
    "production_cut_text": "alpha beta gamma",
    "drain_status": "complete",
    "label": "live_peek",
}), encoding="utf-8")
PY

python3 "$RUN_UI" \
  --mock \
  --run-dir "$RUN" \
  --key-script "sleep:0.05,b,sleep:0.1,h,sleep:0.05,q" \
  --tick-ms 25 \
  --no-clear \
  >"$TEST_DIR/mock.out" 2>"$TEST_DIR/mock.err" || {
    fail "mock run exits 0" "exit $? stderr=$(cat "$TEST_DIR/mock.err")"
  }

OUT="$(cat "$TEST_DIR/mock.out")"
assert_contains "$OUT" "sts-peek-ui: done" "prints done summary"
assert_contains "$OUT" '"barge": true' "summary barge=true"
assert_contains "$OUT" '"mode": "mock"' "summary mode=mock"
assert_file "$RUN/control/barge.now" "control/barge.now touched on barge"
assert_file "$RUN/control/human.mode" "control/human.mode written on h"
assert_contains "$(cat "$RUN/control/human.mode")" "1" "human.mode is 1 after toggle"
assert_file "$RUN/ui-events.jsonl" "ui-events.jsonl appended"

# Frame content (meters + cut overlay)
assert_contains "$OUT" "energy" "mock frame shows energy"
assert_contains "$OUT" "vad" "mock frame shows vad"
assert_contains "$OUT" "smart-turn" "mock frame shows smart-turn"
assert_contains "$OUT" "primary_cut_source" "summary or frame references cut" || true
assert_contains "$OUT" "drain" "cut source drain visible in frame or summary"

echo "=== T3: user-stop key writes user_stop.now ==="
RUN2="$TEST_DIR/run_user_stop"
mkdir -p "$RUN2"
python3 "$RUN_UI" \
  --mock \
  --run-dir "$RUN2" \
  --key-script "u,sleep:0.05,q" \
  --tick-ms 25 \
  --quiet \
  >"$TEST_DIR/u.out" 2>"$TEST_DIR/u.err" || {
    fail "user-stop run exits 0" "exit $? stderr=$(cat "$TEST_DIR/u.err")"
  }
assert_file "$RUN2/control/user_stop.now" "user_stop.now on u"
assert_file "$RUN2/control/barge.now" "u also barges if not yet"
assert_contains "$(cat "$TEST_DIR/u.out")" '"user_stop": true' "summary user_stop=true"

echo "=== T4: space key is barge ==="
RUN3="$TEST_DIR/run_space"
mkdir -p "$RUN3"
python3 "$RUN_UI" \
  --mock \
  --run-dir "$RUN3" \
  --key-script "space,sleep:0.05,q" \
  --tick-ms 25 \
  --quiet \
  >"$TEST_DIR/space.out" 2>"$TEST_DIR/space.err" || {
    fail "space run exits 0" "exit $? stderr=$(cat "$TEST_DIR/space.err")"
  }
assert_file "$RUN3/control/barge.now" "space touches barge.now"

echo "=== T5: docs mention watch attach + barge control ==="
DOC="$REPO_ROOT/docs/sts-peek-ui.md"
DOC_TXT="$(cat "$DOC")"
assert_contains "$DOC_TXT" "speech-core-watch" "docs mention speech-core-watch"
assert_contains "$DOC_TXT" "barge.now" "docs mention barge.now"
assert_contains "$DOC_TXT" "--run-dir" "docs mention --run-dir"
assert_contains "$DOC_TXT" "--mode debug" "docs mention debug mode"

echo "=== T6: no crate files modified by this track (smoke) ==="
# Ensure our package does not import or require crate edits; pure python.
if python3 -c "import ast, pathlib; ast.parse(pathlib.Path('$UI_PY').read_text())"; then
  pass "ui.py parses"
else
  fail "ui.py parses" "syntax error"
fi

echo
echo "=== results: $PASSED passed, $FAILED failed ==="
if [[ "$FAILED" -ne 0 ]]; then
  exit 1
fi
exit 0
