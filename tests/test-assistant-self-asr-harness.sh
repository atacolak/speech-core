#!/usr/bin/env bash
# Deterministic tests for scripts/assistant-self-asr-harness.py (eval_only).
# No daemons, no network, no audio devices.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
HARNESS="$REPO_ROOT/scripts/assistant-self-asr-harness.py"
TEST_DIR="$(mktemp -d)"
trap 'rm -rf "$TEST_DIR"' EXIT

PASSED=0
FAILED=0

pass() { PASSED=$((PASSED + 1)); echo "  PASS: $1"; }
fail() { FAILED=$((FAILED + 1)); echo "  FAIL: $1 — $2" >&2; }

assert_eq() { [[ "$1" == "$2" ]] && pass "$3" || fail "$3" "expected '$2', got '$1'"; }
assert_file() { [[ -f "$1" ]] && pass "$2" || fail "$2" "missing file $1"; }
assert_contains() { [[ "$1" == *"$2"* ]] && pass "$3" || fail "$3" "expected to contain '$2'"; }

echo "=== T1: --self-check ==="
if python3 "$HARNESS" --self-check >"$TEST_DIR/self-check.out" 2>"$TEST_DIR/self-check.err"; then
  pass "self-check exits 0"
else
  fail "self-check exits 0" "exit $? stderr=$(cat "$TEST_DIR/self-check.err")"
fi
assert_contains "$(cat "$TEST_DIR/self-check.out")" "self-check: PASS" "self-check prints PASS"

echo "=== T2: dry-run artifacts ==="
OUT="$TEST_DIR/run1"
python3 "$HARNESS" --mode dry-run \
  --out-dir "$OUT" \
  --intended-text "one two three four five six seven eight nine ten" \
  --pad-words 2 \
  --pause-at-ms 1000 \
  --user-stop-at-ms 2000 \
  --words-per-second 3 \
  --playback-lag-words 1 \
  --asr-lag-words 2 \
  >"$TEST_DIR/run1.out" 2>"$TEST_DIR/run1.err"

assert_file "$OUT/assistant_intended.txt" "assistant_intended.txt"
assert_file "$OUT/production_cut_text" "production_cut_text"
assert_file "$OUT/metrics.json" "metrics.json"
assert_file "$OUT/commit.json" "commit.json"
assert_file "$OUT/events.jsonl" "events.jsonl"
assert_file "$OUT/intended_at_playback.txt" "intended_at_playback.txt"
assert_file "$OUT/asr_recovered_at_stop.txt" "asr_recovered_at_stop.txt"

# 3 words/sec * 2.0s = 6 words Nemotron @ stop; pad 2 → 8 words cut
cut="$(tr -d '\n' <"$OUT/production_cut_text")"
assert_eq "$cut" "one two three four five six seven eight" "production_cut applies +2 pad"

# playback lags by 1 → 5 words intended_at_playback
play="$(tr -d '\n' <"$OUT/intended_at_playback.txt")"
assert_eq "$play" "one two three four five" "intended_at_playback uses playback lag"

prefix_valid="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["prefix_valid"])' "$OUT/metrics.json")"
assert_eq "$prefix_valid" "True" "prefix_valid true"

label="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["label"])' "$OUT/metrics.json")"
assert_eq "$label" "eval_only" "metrics labeled eval_only"

pad="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["pad_words"])' "$OUT/metrics.json")"
assert_eq "$pad" "2" "pad_words default/config 2"

overspeak="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["overspeak_words"])' "$OUT/metrics.json")"
assert_eq "$overspeak" "3" "overspeak_words = cut(8) - shared_prefix_with_play(5)"

immutable="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["immutable"])' "$OUT/commit.json")"
assert_eq "$immutable" "True" "commit is immutable / late ASR non-revising"

echo "=== T3: live mode refuses without --allow-live-stub ==="
if python3 "$HARNESS" --mode live >"$TEST_DIR/live.out" 2>"$TEST_DIR/live.err"; then
  fail "live without stub flag" "expected non-zero exit"
else
  pass "live without stub flag exits non-zero"
fi

echo "=== T4: live stub checklist ==="
STUB_OUT="$TEST_DIR/live-stub"
python3 "$HARNESS" --mode live --allow-live-stub --out-dir "$STUB_OUT" \
  >"$TEST_DIR/live-stub.out" 2>"$TEST_DIR/live-stub.err"
assert_file "$STUB_OUT/live_wiring_gap.json" "live_wiring_gap.json"

echo
echo "Results: $PASSED passed, $FAILED failed"
if (( FAILED > 0 )); then
  exit 1
fi
exit 0
