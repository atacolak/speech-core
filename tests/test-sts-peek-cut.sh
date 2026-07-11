#!/usr/bin/env bash
# Deterministic offline tests for sts-peek cut coordinator (Track C).
# No daemons, no network, no audio devices.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENTRY="$REPO_ROOT/scripts/sts-peek/run_cut.py"
TEST_DIR="$(mktemp -d)"
trap 'rm -rf "$TEST_DIR"' EXIT

PASSED=0
FAILED=0

pass() { PASSED=$((PASSED + 1)); echo "  PASS: $1"; }
fail() { FAILED=$((FAILED + 1)); echo "  FAIL: $1 — $2" >&2; }

assert_eq() { [[ "$1" == "$2" ]] && pass "$3" || fail "$3" "expected '$2', got '$1'"; }
assert_file() { [[ -f "$1" ]] && pass "$2" || fail "$2" "missing file $1"; }
assert_contains() { [[ "$1" == *"$2"* ]] && pass "$3" || fail "$3" "expected to contain '$2'"; }

jq_field() {
  python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d[sys.argv[2]])' "$1" "$2"
}

jq_nested() {
  # jq_nested file key1 key2 ... → print d[k1][k2]...
  python3 -c '
import json,sys
d=json.load(open(sys.argv[1]))
for k in sys.argv[2:]:
    d=d[k]
print(d)
' "$@"
}

echo "=== T1: --self-check ==="
if python3 "$ENTRY" --self-check >"$TEST_DIR/self-check.out" 2>"$TEST_DIR/self-check.err"; then
  pass "self-check exits 0"
else
  fail "self-check exits 0" "exit $? stderr=$(cat "$TEST_DIR/self-check.err")"
fi
assert_contains "$(cat "$TEST_DIR/self-check.out")" "self-check: PASS" "self-check prints PASS"

echo "=== T2: dry-run primary-drain (primary_cut_source=drain) ==="
OUT="$TEST_DIR/primary"
python3 "$ENTRY" --mode dry-run \
  --scenario primary-drain \
  --run-dir "$OUT" \
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

src="$(jq_field "$OUT/metrics.json" "primary_cut_source")"
assert_eq "$src" "drain" "primary_cut_source=drain"

prefix_valid="$(jq_field "$OUT/metrics.json" "prefix_valid")"
assert_eq "$prefix_valid" "True" "primary prefix_valid"

pad="$(jq_field "$OUT/metrics.json" "pad_words")"
assert_eq "$pad" "2" "pad_words=2"

# three-way / overspeak fields present
overspeak="$(jq_field "$OUT/metrics.json" "overspeak_words")"
underspeak="$(jq_field "$OUT/metrics.json" "underspeak_words")"
# playback_pos=5, cut=6 → overspeak 1
assert_eq "$overspeak" "1" "overspeak_words=1 (cut 6 vs play 5)"
assert_eq "$underspeak" "0" "underspeak_words=0"

prod_metric="$(jq_field "$OUT/metrics.json" "production_cut_text")"
assert_eq "$prod_metric" "one two three four five six" "metrics.production_cut_text matches"

immutable="$(jq_field "$OUT/commit.json" "immutable")"
assert_eq "$immutable" "True" "commit immutable"

revises="$(jq_field "$OUT/commit.json" "late_self_asr_revises")"
assert_eq "$revises" "False" "late self-ASR does not revise"

commit_src="$(jq_field "$OUT/commit.json" "primary_cut_source")"
assert_eq "$commit_src" "drain" "commit primary_cut_source=drain"

events="$(cat "$OUT/events.jsonl")"
assert_contains "$events" "user_first_alphanumeric_token" "events: pause"
assert_contains "$events" "assistant_self_asr_drain_started" "events: drain started"
assert_contains "$events" "user_transcript_committed" "events: user commit"
assert_contains "$events" "assistant_turn_truncated_eval_only" "events: truncated commit"
assert_contains "$events" "sts_peek_cut_finalized" "events: cut finalized"

order="$(python3 -c '
import json, sys
names = [json.loads(l)["event"] for l in open(sys.argv[1]) if l.strip()]
i_pause = names.index("user_first_alphanumeric_token")
i_drain = names.index("assistant_self_asr_drain_started")
i_user = names.index("user_transcript_committed")
i_commit = names.index("assistant_turn_truncated_eval_only")
print("ok" if i_pause < i_drain < i_user < i_commit else "bad")
' "$OUT/events.jsonl")"
assert_eq "$order" "ok" "event order pause → drain → user commit → cut commit"

shared="$(jq_nested "$OUT/metrics.json" "topology" "shared_worker")"
assert_eq "$shared" "False" "topology shared_worker=false"

echo "=== T3: dry-run fallback-incomplete (primary_cut_source=fallback) ==="
OUT2="$TEST_DIR/fallback"
python3 "$ENTRY" --mode dry-run \
  --scenario fallback-incomplete \
  --run-dir "$OUT2" \
  --intended-text "one two three four five six seven eight nine ten" \
  --pad-words 2 \
  >"$TEST_DIR/fallback.out" 2>"$TEST_DIR/fallback.err"

cut2="$(tr -d '\n' <"$OUT2/production_cut_text")"
# last_pos=2 + pad=2 → four words
assert_eq "$cut2" "one two three four" "fallback production_cut is last_pos+pad"

src2="$(jq_field "$OUT2/metrics.json" "primary_cut_source")"
assert_eq "$src2" "fallback" "fallback primary_cut_source=fallback"

prefix2="$(jq_field "$OUT2/metrics.json" "prefix_valid")"
assert_eq "$prefix2" "True" "fallback prefix_valid"

pad2="$(jq_field "$OUT2/metrics.json" "pad_words")"
assert_eq "$pad2" "2" "fallback pad_words=2"

imm2="$(jq_field "$OUT2/commit.json" "immutable")"
assert_eq "$imm2" "True" "fallback commit immutable"

echo "=== T4: dry-run fallback-b-missing (fail closed) ==="
OUT3="$TEST_DIR/b-missing"
python3 "$ENTRY" --mode dry-run \
  --scenario fallback-b-missing \
  --run-dir "$OUT3" \
  --intended-text "one two three four five six seven eight nine ten" \
  --pad-words 2 \
  >"$TEST_DIR/b-missing.out" 2>"$TEST_DIR/b-missing.err"

src3="$(jq_field "$OUT3/metrics.json" "primary_cut_source")"
assert_eq "$src3" "fallback" "b-missing uses fallback"

missing="$(jq_field "$OUT3/metrics.json" "fail_closed_b_stream_missing")"
assert_eq "$missing" "True" "fail_closed_b_stream_missing=true"

# last_pos=0 + pad=2 → first two intended words
cut3="$(tr -d '\n' <"$OUT3/production_cut_text")"
assert_eq "$cut3" "one two" "b-missing fallback is pad from pos 0"

echo "=== T5: follow mode on pre-seeded run_dir ==="
FOLLOW="$TEST_DIR/follow"
mkdir -p "$FOLLOW/control"
# Pre-write intended + events as audio track would, then run follow with short timeout.
python3 - <<PY
import json, time
from pathlib import Path
root = Path("$FOLLOW")
(root / "assistant_intended.txt").write_text(
    "one two three four five six seven eight nine ten\n"
)
events = root / "events.jsonl"
# Write barge + complete drain + user commit up front.
lines = [
    {"event": "user_first_alphanumeric_token", "emitted_words_at_pause": 5,
     "b_pos_at_pause": 3, "playback_pos_words": 4},
    {"event": "assistant_self_asr_drain_started", "emitted_words": 5, "b_pos_at_pause": 3},
    {"event": "assistant_self_asr_drain_progress", "drained_text": "one two three four five",
     "b_pos_words": 5, "target_emitted_words": 5, "drain_complete": True,
     "last_aligned_pos_words": 5},
    {"event": "user_transcript_committed", "user_transcript": "stop please"},
]
with events.open("w") as fh:
    for ev in lines:
        fh.write(json.dumps(ev) + "\n")
PY

python3 "$ENTRY" --mode follow \
  --run-dir "$FOLLOW" \
  --intended-text "one two three four five six seven eight nine ten" \
  --pad-words 2 \
  --wait-timeout-s 2 \
  >"$TEST_DIR/follow.out" 2>"$TEST_DIR/follow.err"

assert_file "$FOLLOW/commit.json" "follow: commit.json"
assert_file "$FOLLOW/metrics.json" "follow: metrics.json"
fsrc="$(jq_field "$FOLLOW/metrics.json" "primary_cut_source")"
assert_eq "$fsrc" "drain" "follow primary_cut_source=drain"
fcut="$(tr -d '\n' <"$FOLLOW/production_cut_text")"
assert_eq "$fcut" "one two three four five" "follow production_cut drained"
fimm="$(jq_field "$FOLLOW/commit.json" "immutable")"
assert_eq "$fimm" "True" "follow commit immutable"

echo "=== T6: follow mode timeout when no user commit ==="
EMPTY="$TEST_DIR/follow-empty"
mkdir -p "$EMPTY"
echo "one two three" >"$EMPTY/assistant_intended.txt"
set +e
python3 "$ENTRY" --mode follow \
  --run-dir "$EMPTY" \
  --intended-text "one two three" \
  --wait-timeout-s 0.3 \
  >"$TEST_DIR/follow-empty.out" 2>"$TEST_DIR/follow-empty.err"
empty_rc=$?
set -e
assert_eq "$empty_rc" "3" "follow timeout exits 3"
assert_contains "$(cat "$TEST_DIR/follow-empty.err")" "follow timeout" "follow timeout message"

echo "=== T7: package files exist ==="
assert_file "$REPO_ROOT/scripts/sts-peek/cut.py" "cut.py"
assert_file "$REPO_ROOT/scripts/sts-peek/cut_coord.py" "cut_coord.py"
assert_file "$REPO_ROOT/scripts/sts-peek/run_cut.py" "run_cut.py"
assert_file "$REPO_ROOT/docs/sts-peek-cut.md" "docs/sts-peek-cut.md"

echo "=== T8: commit is not revised on second finalize (idempotent) ==="
# Re-run follow on same dir; commit production_cut_text must stay identical.
before="$(cat "$FOLLOW/commit.json")"
python3 "$ENTRY" --mode follow \
  --run-dir "$FOLLOW" \
  --intended-text "one two three four five six seven eight nine ten" \
  --wait-timeout-s 1 \
  >"$TEST_DIR/follow2.out" 2>"$TEST_DIR/follow2.err"
after="$(cat "$FOLLOW/commit.json")"
assert_eq "$before" "$after" "second finalize does not revise commit.json"

echo
echo "Results: $PASSED passed, $FAILED failed"
if (( FAILED > 0 )); then
  exit 1
fi
exit 0
