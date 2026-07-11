#!/usr/bin/env bash
# Deterministic tests for scripts/assistant-self-asr-harness.py (eval_only).
# No daemons, no network, no audio devices.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
HARNESS="$REPO_ROOT/scripts/assistant-self-asr-harness.py"
TTS_EVAL="$REPO_ROOT/scripts/assistant_self_asr_tts_eval.py"
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

echo "=== T1: --self-check ==="
if python3 "$HARNESS" --self-check >"$TEST_DIR/self-check.out" 2>"$TEST_DIR/self-check.err"; then
  pass "self-check exits 0"
else
  fail "self-check exits 0" "exit $? stderr=$(cat "$TEST_DIR/self-check.err")"
fi
assert_contains "$(cat "$TEST_DIR/self-check.out")" "self-check: PASS" "self-check prints PASS"

echo "=== T2: dry-run drain-primary artifacts ==="
OUT="$TEST_DIR/run1"
python3 "$HARNESS" --mode dry-run \
  --out-dir "$OUT" \
  --intended-text "one two three four five six seven eight nine ten" \
  --pad-words 2 \
  --pause-at-ms 1000 \
  --user-stop-at-ms 2000 \
  --words-per-second 3 \
  --drain-words-per-second 12 \
  --playback-lag-words 1 \
  >"$TEST_DIR/run1.out" 2>"$TEST_DIR/run1.err"

assert_file "$OUT/assistant_intended.txt" "assistant_intended.txt"
assert_file "$OUT/production_cut_text" "production_cut_text"
assert_file "$OUT/metrics.json" "metrics.json"
assert_file "$OUT/commit.json" "commit.json"
assert_file "$OUT/events.jsonl" "events.jsonl"
assert_file "$OUT/intended_at_playback.txt" "intended_at_playback.txt"
assert_file "$OUT/asr_recovered_at_stop.txt" "asr_recovered_at_stop.txt"

# 3 wps * 1.0s = 3 emitted @ pause; drain catches up → primary=drain, cut = 3 words
cut="$(tr -d '\n' <"$OUT/production_cut_text")"
assert_eq "$cut" "one two three" "production_cut uses drain primary (no pad)"

# playback lags by 1 → 2 words intended_at_playback
play="$(tr -d '\n' <"$OUT/intended_at_playback.txt")"
assert_eq "$play" "one two" "intended_at_playback uses playback lag"

src="$(jq_field "$OUT/metrics.json" "primary_cut_source")"
assert_eq "$src" "drain" "primary_cut_source=drain"

prefix_valid="$(jq_field "$OUT/metrics.json" "prefix_valid")"
assert_eq "$prefix_valid" "True" "prefix_valid true"

label="$(jq_field "$OUT/metrics.json" "label")"
assert_eq "$label" "eval_only" "metrics labeled eval_only"

pad="$(jq_field "$OUT/metrics.json" "pad_words")"
assert_eq "$pad" "2" "pad_words config 2 (fallback only)"

overspeak="$(jq_field "$OUT/metrics.json" "overspeak_words")"
assert_eq "$overspeak" "1" "overspeak_words = cut(3) - shared_prefix_with_play(2)"

immutable="$(jq_field "$OUT/commit.json" "immutable")"
assert_eq "$immutable" "True" "commit is immutable / late ASR non-revising"

commit_src="$(jq_field "$OUT/commit.json" "primary_cut_source")"
assert_eq "$commit_src" "drain" "commit records primary_cut_source"

echo "=== T3: dry-run force-fallback uses pad ==="
OUT_FB="$TEST_DIR/run-fb"
python3 "$HARNESS" --mode dry-run \
  --out-dir "$OUT_FB" \
  --intended-text "one two three four five six seven eight nine ten" \
  --pad-words 2 \
  --pause-at-ms 1000 \
  --user-stop-at-ms 2000 \
  --words-per-second 3 \
  --drain-words-per-second 12 \
  --playback-lag-words 1 \
  --force-fallback \
  >"$TEST_DIR/run-fb.out" 2>"$TEST_DIR/run-fb.err"

fb_src="$(jq_field "$OUT_FB/metrics.json" "primary_cut_source")"
assert_eq "$fb_src" "fallback" "force-fallback → primary_cut_source=fallback"

fb_cut="$(tr -d '\n' <"$OUT_FB/production_cut_text")"
# last_aligned from drain pos 3 + pad 2 → five words
assert_eq "$fb_cut" "one two three four five" "fallback cut applies +2 pad"

echo "=== T4: live mode refuses without --allow-live-stub ==="
if python3 "$HARNESS" --mode live >"$TEST_DIR/live.out" 2>"$TEST_DIR/live.err"; then
  fail "live without stub flag" "expected non-zero exit"
else
  pass "live without stub flag exits non-zero"
fi

echo "=== T5: live stub checklist ==="
STUB_OUT="$TEST_DIR/live-stub"
python3 "$HARNESS" --mode live --allow-live-stub --out-dir "$STUB_OUT" \
  >"$TEST_DIR/live-stub.out" 2>"$TEST_DIR/live-stub.err"
assert_file "$STUB_OUT/live_wiring_gap.json" "live_wiring_gap.json"

echo "=== T6: tts-tts dual-role dry path ==="
OUT_TTS="$TEST_DIR/tts-tts"
python3 "$HARNESS" --mode tts-tts \
  --out-dir "$OUT_TTS" \
  --intended-text "one two three four five six seven eight nine ten" \
  --user-barge-text "wait stop please" \
  --pad-words 2 \
  --pause-at-ms 1000 \
  --user-speech-ms 1000 \
  --words-per-second 3 \
  --drain-words-per-second 20 \
  --playback-lag-words 1 \
  >"$TEST_DIR/tts.out" 2>"$TEST_DIR/tts.err"

assert_file "$OUT_TTS/metrics.json" "tts-tts metrics.json"
assert_file "$OUT_TTS/user_barge_text.txt" "user_barge_text.txt"
assert_file "$OUT_TTS/production_cut_text" "tts-tts production_cut_text"
assert_file "$OUT_TTS/commit.json" "tts-tts commit.json"
assert_file "$OUT_TTS/events.jsonl" "tts-tts events.jsonl"

tts_src="$(jq_field "$OUT_TTS/metrics.json" "primary_cut_source")"
assert_eq "$tts_src" "drain" "tts-tts primary_cut_source=drain"

tts_mode="$(jq_field "$OUT_TTS/metrics.json" "mode")"
assert_eq "$tts_mode" "tts-tts" "metrics mode=tts-tts"

tts_label="$(jq_field "$OUT_TTS/metrics.json" "label")"
assert_eq "$tts_label" "eval_only" "tts-tts label=eval_only"

tts_cut="$(tr -d '\n' <"$OUT_TTS/production_cut_text")"
assert_eq "$tts_cut" "one two three" "tts-tts drain cut equals emitted@pause"

# biases labeled
biases="$(python3 -c 'import json,sys; print(" ".join(json.load(open(sys.argv[1]))["biases_labeled"]))' "$OUT_TTS/metrics.json")"
assert_contains "$biases" "tts_tts_not_acoustic_echo_truth" "bias: not acoustic-echo truth"
assert_contains "$biases" "no_mic_dirt" "bias: no mic dirt"

user_barge="$(tr -d '\n' <"$OUT_TTS/user_barge_text.txt")"
assert_eq "$user_barge" "wait stop please" "user barge text artifact"

echo "=== T7: tts-tts force-fallback ==="
OUT_TTS_FB="$TEST_DIR/tts-fb"
python3 "$HARNESS" --mode tts-tts \
  --out-dir "$OUT_TTS_FB" \
  --intended-text "one two three four five six seven eight nine ten" \
  --pad-words 2 \
  --pause-at-ms 1000 \
  --user-speech-ms 1000 \
  --words-per-second 3 \
  --force-fallback \
  >"$TEST_DIR/tts-fb.out" 2>"$TEST_DIR/tts-fb.err"

tts_fb_src="$(jq_field "$OUT_TTS_FB/metrics.json" "primary_cut_source")"
assert_eq "$tts_fb_src" "fallback" "tts-tts force-fallback source"

echo "=== T8: live-synth stub ==="
if python3 "$HARNESS" --mode live-synth >"$TEST_DIR/ls.out" 2>"$TEST_DIR/ls.err"; then
  fail "live-synth without stub flag" "expected non-zero exit"
else
  pass "live-synth without stub flag exits non-zero"
fi
LS_OUT="$TEST_DIR/live-synth"
python3 "$HARNESS" --mode live-synth --allow-live-stub --out-dir "$LS_OUT" \
  >"$TEST_DIR/ls-ok.out" 2>"$TEST_DIR/ls-ok.err"
assert_file "$LS_OUT/live_synth_wiring_gap.json" "live_synth_wiring_gap.json"

echo "=== T9: module-level tts eval self-check ==="
if python3 "$TTS_EVAL" >"$TEST_DIR/tts-mod.out" 2>"$TEST_DIR/tts-mod.err"; then
  pass "assistant_self_asr_tts_eval self-check exits 0"
else
  fail "tts eval module self-check" "stderr=$(cat "$TEST_DIR/tts-mod.err")"
fi

echo "=== T10: interactive tui auto-barge (no keypress required) ==="
OUT_TUI="$TEST_DIR/tui"
# Redirect stdin from /dev/null so TUI takes no-tty auto path if needed;
# --auto-barge-ms makes the timeline deterministic either way.
if python3 "$HARNESS" --mode tui \
  --out-dir "$OUT_TUI" \
  --intended-text "one two three four five six seven eight nine ten" \
  --user-barge-text "wait stop" \
  --pad-words 2 \
  --auto-barge-ms 400 \
  --user-speech-ms 600 \
  --words-per-second 3 \
  --drain-words-per-second 20 \
  --playback-lag-words 1 \
  < /dev/null \
  >"$TEST_DIR/tui.out" 2>"$TEST_DIR/tui.err"; then
  pass "tui auto-barge exits 0"
else
  fail "tui auto-barge exits 0" "exit $? stderr=$(tail -c 500 "$TEST_DIR/tui.err")"
fi

assert_file "$OUT_TUI/metrics.json" "tui metrics.json"
assert_file "$OUT_TUI/production_cut_text" "tui production_cut_text"

tui_src="$(jq_field "$OUT_TUI/metrics.json" "primary_cut_source")"
assert_eq "$tui_src" "drain" "tui primary_cut_source=drain"

tui_label="$(jq_field "$OUT_TUI/metrics.json" "label")"
assert_eq "$tui_label" "eval_only" "tui label=eval_only"

# metrics mode is annotated as tui by interactive path
tui_mode="$(jq_field "$OUT_TUI/metrics.json" "mode")"
assert_eq "$tui_mode" "tui" "tui metrics mode=tui"

echo
echo "Results: $PASSED passed, $FAILED failed"
if (( FAILED > 0 )); then
  exit 1
fi
exit 0
