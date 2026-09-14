#!/usr/bin/env bash
# Deterministic tests for dual-Nemotron barge-in path (harness / eval_only).
# No daemons, no network, no audio devices.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENTRY="$REPO_ROOT/scripts/barge-in-dual-asr.py"
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
if python3 "$ENTRY" --self-check >"$TEST_DIR/self-check.out" 2>"$TEST_DIR/self-check.err"; then
  pass "self-check exits 0"
else
  fail "self-check exits 0" "exit $? stderr=$(cat "$TEST_DIR/self-check.err")"
fi
assert_contains "$(cat "$TEST_DIR/self-check.out")" "self-check: PASS" "self-check prints PASS"

echo "=== T2: dry-run primary-drain (pause → drain → cut_source=drain) ==="
OUT="$TEST_DIR/primary"
python3 "$ENTRY" --mode dry-run \
  --scenario primary-drain \
  --out-dir "$OUT" \
  --intended-text "one two three four five six seven eight nine ten" \
  --pad-words 2 \
  >"$TEST_DIR/primary.out" 2>"$TEST_DIR/primary.err"

assert_file "$OUT/assistant_intended.txt" "primary: assistant_intended.txt"
assert_file "$OUT/production_cut_text" "primary: production_cut_text"
assert_file "$OUT/metrics.json" "primary: metrics.json"
assert_file "$OUT/commit.json" "primary: commit.json"
assert_file "$OUT/events.jsonl" "primary: events.jsonl"
assert_file "$OUT/cut_decision.json" "primary: cut_decision.json"
assert_file "$OUT/drained_asr_text.txt" "primary: drained_asr_text.txt"

cut="$(tr -d '\n' <"$OUT/production_cut_text")"
assert_eq "$cut" "one two three four five six" "primary production_cut is drained aligned prefix"

src="$(python3 -c 'import json; print(json.load(open("'"$OUT/metrics.json"'"))["cut_source"])')"
assert_eq "$src" "drain" "primary cut_source=drain"

prefix_valid="$(python3 -c 'import json; print(json.load(open("'"$OUT/metrics.json"'"))["prefix_valid"])')"
assert_eq "$prefix_valid" "True" "primary prefix_valid"

immutable="$(python3 -c 'import json; print(json.load(open("'"$OUT/commit.json"'"))["immutable"])')"
assert_eq "$immutable" "True" "commit immutable"

revises="$(python3 -c 'import json; print(json.load(open("'"$OUT/commit.json"'"))["late_self_asr_revises"])')"
assert_eq "$revises" "False" "late self-ASR does not revise"

events="$(cat "$OUT/events.jsonl")"
assert_contains "$events" "user_first_alphanumeric_token" "events: pause"
assert_contains "$events" "assistant_self_asr_drain_started" "events: drain started"
assert_contains "$events" "user_transcript_committed" "events: user commit"
assert_contains "$events" "assistant_turn_truncated_eval_only" "events: truncated commit"

order="$(python3 -c '
import json, sys
names = [json.loads(l)["event"] for l in open(sys.argv[1])]
i_pause = names.index("user_first_alphanumeric_token")
i_drain = names.index("assistant_self_asr_drain_started")
i_user = names.index("user_transcript_committed")
i_commit = names.index("assistant_turn_truncated_eval_only")
print("ok" if i_pause < i_drain < i_user < i_commit else "bad")
' "$OUT/events.jsonl")"
assert_eq "$order" "ok" "event order pause → drain → user commit → cut commit"

shared="$(python3 -c 'import json; print(json.load(open("'"$OUT/metrics.json"'"))["topology"]["shared_worker"])')"
assert_eq "$shared" "False" "topology shared_worker=false"

echo "=== T3: dry-run fallback-incomplete (cut_source=fallback) ==="
OUT2="$TEST_DIR/fallback"
python3 "$ENTRY" --mode dry-run \
  --scenario fallback-incomplete \
  --out-dir "$OUT2" \
  --intended-text "one two three four five six seven eight nine ten" \
  --pad-words 2 \
  >"$TEST_DIR/fallback.out" 2>"$TEST_DIR/fallback.err"

cut2="$(tr -d '\n' <"$OUT2/production_cut_text")"
# last_pos=2 + pad=2 → four words
assert_eq "$cut2" "one two three four" "fallback production_cut is last_pos+pad"

src2="$(python3 -c 'import json; print(json.load(open("'"$OUT2/metrics.json"'"))["cut_source"])')"
assert_eq "$src2" "fallback" "fallback cut_source=fallback"

echo "=== T4: live mode refuses without --allow-live-stub when daemons down ==="
set +e
python3 "$ENTRY" --mode live --out-dir "$TEST_DIR/live-no" \
  >"$TEST_DIR/live-no.out" 2>"$TEST_DIR/live-no.err"
live_rc=$?
set -e
if [[ "$live_rc" -ne 0 ]]; then
  pass "live without stub flag exits non-zero when not ready"
else
  if [[ -f "$TEST_DIR/live-no/live_runbook.json" ]] || [[ -f "$TEST_DIR/live-no/live_wiring.json" ]]; then
    pass "live without stub flag exited 0 with daemons reachable"
  else
    fail "live without stub flag" "exit 0 but no wiring artifacts"
  fi
fi

echo "=== T5: live stub checklist ==="
STUB_OUT="$TEST_DIR/live-stub"
python3 "$ENTRY" --mode live --allow-live-stub --out-dir "$STUB_OUT" \
  >"$TEST_DIR/live-stub.out" 2>"$TEST_DIR/live-stub.err"
assert_file "$STUB_OUT/live_wiring.json" "live_wiring.json"
assert_file "$STUB_OUT/probes.json" "probes.json"
topo="$(python3 -c 'import json; print(json.load(open("'"$STUB_OUT/live_wiring.json"'"))["topology"]["shared_worker"])')"
assert_eq "$topo" "False" "live wiring shared_worker=false"
stream_b="$(python3 -c 'import json; print(json.load(open("'"$STUB_OUT/live_wiring.json"'"))["topology"]["nemotron_b"]["stream_id"])')"
assert_eq "$stream_b" "assistant.self_asr" "B stream_id=assistant.self_asr"

echo "=== T6: package files exist ==="
assert_file "$REPO_ROOT/scripts/barge-in-dual-asr/cut.py" "cut.py"
assert_file "$REPO_ROOT/scripts/barge-in-dual-asr/simulator.py" "simulator.py"
assert_file "$REPO_ROOT/scripts/barge-in-dual-asr/live_wiring.py" "live_wiring.py"
assert_file "$REPO_ROOT/scripts/barge-in-dual-asr/record_client.py" "record_client.py"
assert_file "$REPO_ROOT/scripts/barge-in-dual-asr/feed_assistant_asr.py" "feed_assistant_asr.py"
assert_file "$REPO_ROOT/docs/barge-in-dual-asr.md" "docs/barge-in-dual-asr.md"

echo
echo "Results: $PASSED passed, $FAILED failed"
if (( FAILED > 0 )); then
  exit 1
fi
exit 0
