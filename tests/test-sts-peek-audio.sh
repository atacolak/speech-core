#!/usr/bin/env bash
# Deterministic offline tests for sts-peek audio+session layer (Track L).
# No daemons, no network, no audio devices required (--mock-audio path).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENTRY="$REPO_ROOT/scripts/sts-peek/run_audio.py"
TEST_DIR="$(mktemp -d)"
trap 'rm -rf "$TEST_DIR"' EXIT

PASSED=0
FAILED=0

pass() { PASSED=$((PASSED + 1)); echo "  PASS: $1"; }
fail() { FAILED=$((FAILED + 1)); echo "  FAIL: $1 — $2" >&2; }

assert_eq() { [[ "$1" == "$2" ]] && pass "$3" || fail "$3" "expected '$2', got '$1'"; }
assert_file() { [[ -f "$1" ]] && pass "$2" || fail "$2" "missing file $1"; }
assert_dir() { [[ -d "$1" ]] && pass "$2" || fail "$2" "missing dir $1"; }
assert_contains() { [[ "$1" == *"$2"* ]] && pass "$3" || fail "$3" "expected to contain '$2'"; }
assert_exit() {
  local got="$1" want="$2" label="$3"
  [[ "$got" == "$want" ]] && pass "$label" || fail "$label" "exit $got want $want"
}

echo "=== T0: --print-layout ==="
if python3 "$ENTRY" --print-layout >"$TEST_DIR/layout.json" 2>"$TEST_DIR/layout.err"; then
  pass "print-layout exits 0"
else
  fail "print-layout exits 0" "exit $? stderr=$(cat "$TEST_DIR/layout.err")"
fi
assert_contains "$(cat "$TEST_DIR/layout.json")" "barge.now" "layout mentions barge.now"
assert_contains "$(cat "$TEST_DIR/layout.json")" "assistant.self_asr" "layout mentions assistant stream"

echo "=== T1: mock dual-TTS with scheduled barge ==="
OUT="$TEST_DIR/mock-barge"
set +e
python3 "$ENTRY" --mock-audio \
  --intended-text "one two three four five six seven eight nine ten" \
  --user-tts-text "wait stop now" \
  --barge-after-ms 120 \
  --assistant-voice M1 \
  --user-voice F1 \
  --out-dir "$OUT" \
  --stream-session-id "test-mock-barge" \
  >"$TEST_DIR/mock-barge.out" 2>"$TEST_DIR/mock-barge.err"
rc=$?
set -e
assert_exit "$rc" "0" "mock barge session exits 0"

assert_file "$OUT/params.env" "params.env"
assert_file "$OUT/session.json" "session.json"
assert_file "$OUT/assistant_intended.txt" "assistant_intended.txt"
assert_file "$OUT/user_text.txt" "user_text.txt"
assert_file "$OUT/events.jsonl" "events.jsonl"
assert_file "$OUT/audio_result.json" "audio_result.json"
assert_file "$OUT/cancel/assistant_cancel.json" "assistant_cancel.json"
assert_dir "$OUT/control" "control/"
assert_dir "$OUT/pids" "pids/"
assert_dir "$OUT/speech_out" "speech_out/"

intended="$(tr -d '\n' <"$OUT/assistant_intended.txt")"
assert_eq "$intended" "one two three four five six seven eight nine ten" "intended text preserved"

user_txt="$(tr -d '\n' <"$OUT/user_text.txt")"
assert_eq "$user_txt" "wait stop now" "user text preserved"

sid="$(python3 -c 'import json; print(json.load(open("'"$OUT/session.json"'"))["stream_session_id"])')"
assert_eq "$sid" "test-mock-barge" "stream_session_id"

asst_stream="$(python3 -c 'import json; print(json.load(open("'"$OUT/session.json"'"))["assistant_stream_id"])')"
assert_eq "$asst_stream" "assistant.self_asr" "assistant_stream_id"

barge_path="$(python3 -c 'import json; print(json.load(open("'"$OUT/session.json"'"))["paths"]["barge_now"])')"
assert_contains "$barge_path" "control/barge.now" "session.json barge path"

cancelled="$(python3 -c 'import json; print(json.load(open("'"$OUT/audio_result.json"'"))["assistant_cancelled"])')"
assert_eq "$cancelled" "True" "assistant_cancelled true"

user_spoken="$(python3 -c 'import json; print(json.load(open("'"$OUT/audio_result.json"'"))["user_spoken"])')"
assert_eq "$user_spoken" "True" "user_spoken true"

trigger="$(python3 -c 'import json; print(json.load(open("'"$OUT/audio_result.json"'"))["barge_trigger"])')"
assert_contains "$trigger" "scheduled" "barge_trigger scheduled"

events="$(cat "$OUT/events.jsonl")"
assert_contains "$events" "session_created" "events: session_created"
assert_contains "$events" "assistant_speak_started" "events: assistant_speak_started"
assert_contains "$events" "barge_watch_started" "events: barge_watch_started"
assert_contains "$events" "assistant_playback_cancelled" "events: assistant cancelled"
assert_contains "$events" "user_speak_started" "events: user speak"
assert_contains "$events" "audio_sequence_complete" "events: complete"

order="$(python3 -c '
import json, sys
names = [json.loads(l)["event"] for l in open(sys.argv[1]) if l.strip()]
def idx(n):
    return names.index(n)
i_a = idx("assistant_speak_started")
i_b = next(i for i,n in enumerate(names) if n in ("barge_flag_written","barge_flag_observed"))
i_c = idx("assistant_playback_cancelled")
i_u = idx("user_speak_started")
i_done = idx("audio_sequence_complete")
print("ok" if i_a < i_b < i_c < i_u < i_done else "bad:"+",".join(names))
' "$OUT/events.jsonl")"
assert_eq "$order" "ok" "event order speak → barge → cancel → user → done"

params="$(cat "$OUT/params.env")"
assert_contains "$params" "STS_PEEK_STREAM_SESSION_ID=test-mock-barge" "params session id"
assert_contains "$params" "STS_PEEK_ASSISTANT_VOICE=M1" "params assistant voice"
assert_contains "$params" "STS_PEEK_USER_VOICE=F1" "params user voice"

cancel_reason="$(python3 -c 'import json; print(json.load(open("'"$OUT/cancel/assistant_cancel.json"'"))["reason"])')"
assert_eq "$cancel_reason" "barge" "cancel reason barge"

echo "=== T2: mock human mode (no user TTS) ==="
OUTH="$TEST_DIR/mock-human"
set +e
python3 "$ENTRY" --mock-audio --human \
  --intended-text "assistant only then human" \
  --user-tts-text "should-not-play" \
  --barge-after-ms 100 \
  --out-dir "$OUTH" \
  --stream-session-id "test-mock-human" \
  >"$TEST_DIR/mock-human.out" 2>"$TEST_DIR/mock-human.err"
rc=$?
set -e
assert_exit "$rc" "0" "human mock exits 0"

user_h="$(tr -d '\n' <"$OUTH/user_text.txt")"
assert_eq "$user_h" "" "human mode user_text empty"

user_spoken_h="$(python3 -c 'import json; print(json.load(open("'"$OUTH/audio_result.json"'"))["user_spoken"])')"
assert_eq "$user_spoken_h" "False" "human: user_spoken false"

assert_file "$OUTH/mic/human_mode.json" "human_mode.json"
events_h="$(cat "$OUTH/events.jsonl")"
assert_contains "$events_h" "user_path_human" "events: user_path_human"
assert_contains "$events_h" "human_mode_prepared" "events: human_mode_prepared"
if grep -q "user_speak_started" "$OUTH/events.jsonl"; then
  fail "human no user TTS" "user_speak_started present"
else
  pass "human no user TTS"
fi

echo "=== T3: dry path without mock when daemons down → clear error ==="
OUTD="$TEST_DIR/dry-fail"
set +e
python3 "$ENTRY" \
  --intended-text "will fail without daemon" \
  --out-url "ws://127.0.0.1:1/ws/speech-out" \
  --out-dir "$OUTD" \
  --stream-session-id "test-dry-fail" \
  >"$TEST_DIR/dry-fail.out" 2>"$TEST_DIR/dry-fail.err"
rc=$?
set -e
assert_exit "$rc" "2" "daemon-down exits 2"
assert_file "$OUTD/session.json" "dry-fail still writes session.json"
assert_file "$OUTD/probes.json" "dry-fail probes.json"
err="$(cat "$TEST_DIR/dry-fail.err")"
assert_contains "$err" "not ready" "error mentions not ready"
assert_contains "$err" "mock-audio" "error suggests --mock-audio"

echo "=== T4: probe-only with mock readiness ==="
OUTP="$TEST_DIR/probe"
set +e
python3 "$ENTRY" --mock-audio --probe-only \
  --out-dir "$OUTP" \
  --stream-session-id "test-probe" \
  >"$TEST_DIR/probe.out" 2>"$TEST_DIR/probe.err"
rc=$?
set -e
assert_exit "$rc" "0" "probe-only mock exits 0"
ready="$(python3 -c 'import json; print(json.load(open("'"$OUTP/probes.json"'"))["ready"])')"
assert_eq "$ready" "True" "probe ready under mock"

echo "=== T5: record-synthesis mock stub ==="
OUTR="$TEST_DIR/record"
set +e
python3 "$ENTRY" --mock-audio --record-synthesis \
  --intended-text "record me" \
  --user-tts-text "ok" \
  --barge-after-ms 80 \
  --out-dir "$OUTR" \
  >"$TEST_DIR/record.out" 2>"$TEST_DIR/record.err"
rc=$?
set -e
assert_exit "$rc" "0" "record mock exits 0"
assert_file "$OUTR/record/record_summary.json" "record_summary.json mock stub"

echo "=== T6: barge.now file trigger (no schedule) ==="
OUTF="$TEST_DIR/flag-barge"
# Start session in background without schedule; touch flag after short delay.
python3 "$ENTRY" --mock-audio \
  --intended-text "aaaaaaaa bbbbbbbb cccccccc dddddddd eeeeeeee ffffffff" \
  --user-tts-text "stop" \
  --out-dir "$OUTF" \
  --stream-session-id "test-flag-barge" \
  >"$TEST_DIR/flag.out" 2>"$TEST_DIR/flag.err" &
bg_pid=$!
# Wait for session dir + assistant start
for _ in $(seq 1 50); do
  if [[ -f "$OUTF/events.jsonl" ]] && grep -q "assistant_speak_started" "$OUTF/events.jsonl" 2>/dev/null; then
    break
  fi
  sleep 0.05
done
mkdir -p "$OUTF/control"
echo "test-ui" >"$OUTF/control/barge.now"
set +e
wait "$bg_pid"
rc=$?
set -e
assert_exit "$rc" "0" "flag barge session exits 0"
trigger_f="$(python3 -c 'import json; print(json.load(open("'"$OUTF/audio_result.json"'"))["barge_trigger"])')"
assert_eq "$trigger_f" "barge.now" "barge_trigger is barge.now"
cancelled_f="$(python3 -c 'import json; print(json.load(open("'"$OUTF/audio_result.json"'"))["assistant_cancelled"])')"
assert_eq "$cancelled_f" "True" "flag path cancelled assistant"

echo ""
echo "=== results: $PASSED passed, $FAILED failed ==="
if (( FAILED > 0 )); then
  exit 1
fi
exit 0
