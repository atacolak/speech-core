#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
bind="${SPEECH_CORE_DAEMON_BIND:-0.0.0.0:8765}"
log_dir="${SPEECH_CORE_LOG_DIR:-$repo_root/logs/live-$(date +%Y%m%d-%H%M%S)}"
model_path="${SPEECH_CORE_MODEL_PATH:-$HOME/workspace/external/transcribe.cpp/models/nemotron-speech-streaming-en-0.6b/nemotron-speech-streaming-en-0.6b-Q4_K_M.gguf}"
stream_chunk_ms="${SPEECH_CORE_STREAM_CHUNK_MS:-160}"
att_context_right="${SPEECH_CORE_ATT_CONTEXT_RIGHT:-1}"
model_queue_frames="${SPEECH_CORE_MODEL_QUEUE_FRAMES:-2048}"
vad_model_path="${SPEECH_CORE_VAD_MODEL_PATH:-$HOME/.cache/speech-core/models/silero_vad_v4.onnx}"
vad_threshold="${SPEECH_CORE_VAD_THRESHOLD:-0.5}"
vad_onset_frames="${SPEECH_CORE_VAD_ONSET_FRAMES:-2}"
vad_hangover_frames="${SPEECH_CORE_VAD_HANGOVER_FRAMES:-3}"
vad_pre_speech_frames="${SPEECH_CORE_VAD_PRE_SPEECH_FRAMES:-5}"
vad_emit_frames="${SPEECH_CORE_VAD_EMIT_FRAMES:-false}"
vad_smoothing_alpha="${SPEECH_CORE_VAD_SMOOTHING_ALPHA:-0.1}"
vad_stop_threshold="${SPEECH_CORE_VAD_STOP_THRESHOLD:-0.2}"
vad_fallback_threshold="${SPEECH_CORE_VAD_FALLBACK_THRESHOLD:-0.1}"
vad_acoustic_fallback_silence_ms="${SPEECH_CORE_VAD_ACOUSTIC_FALLBACK_SILENCE_MS:-2500}"
eou_model_dir="${SPEECH_CORE_EOU_MODEL_DIR:-}"
smart_turn_model_path="${SPEECH_CORE_SMART_TURN_MODEL_PATH:-$HOME/workspace/external/smart-turn-v3/smart-turn-v3.2-cpu.onnx}"
smart_turn_threshold="${SPEECH_CORE_SMART_TURN_THRESHOLD:-0.5}"
smart_turn_timeout_ms="${SPEECH_CORE_SMART_TURN_TIMEOUT_MS:-250}"
smart_turn_cpu_count="${SPEECH_CORE_SMART_TURN_CPU_COUNT:-1}"
smart_turn_max_audio_secs="${SPEECH_CORE_SMART_TURN_MAX_AUDIO_SECS:-8}"
smart_turn_pre_speech_ms="${SPEECH_CORE_SMART_TURN_PRE_SPEECH_MS:-500}"
smart_turn_recheck_interval_ms="${SPEECH_CORE_SMART_TURN_RECHECK_INTERVAL_MS:-0}"
smart_turn_recheck_max_attempts="${SPEECH_CORE_SMART_TURN_RECHECK_MAX_ATTEMPTS:-0}"
smart_turn_recheck_offsets_ms="${SPEECH_CORE_SMART_TURN_RECHECK_OFFSETS_MS:-96,192,384,768,1536}"
eou_chunk_ms="${SPEECH_CORE_EOU_CHUNK_MS:-160}"
eou_reset_on_token="${SPEECH_CORE_EOU_RESET_ON_TOKEN:-false}"
eou_emit_transcript="${SPEECH_CORE_EOU_EMIT_TRANSCRIPT:-true}"
detector_queue_frames="${SPEECH_CORE_DETECTOR_QUEUE_FRAMES:-2048}"
turn_vad_close_enabled="${SPEECH_CORE_TURN_VAD_CLOSE_ENABLED:-true}"
turn_semantic_gate_enabled="${SPEECH_CORE_TURN_SEMANTIC_GATE_ENABLED:-true}"
turn_semantic_gate_close_enabled="${SPEECH_CORE_TURN_SEMANTIC_GATE_CLOSE_ENABLED:-true}"
turn_model_eou_close_enabled="${SPEECH_CORE_TURN_MODEL_EOU_CLOSE_ENABLED:-false}"
turn_min_vad_speech_ms="${SPEECH_CORE_TURN_MIN_VAD_SPEECH_MS:-400}"
turn_human_hold_silence_ms="${SPEECH_CORE_TURN_HUMAN_HOLD_SILENCE_MS:-7500}"
turn_transcript_silence_close_ms="${SPEECH_CORE_TURN_TRANSCRIPT_SILENCE_CLOSE_MS:-700}"
turn_min_model_eou_speech_ms="${SPEECH_CORE_TURN_MIN_MODEL_EOU_SPEECH_MS:-300}"
turn_model_eou_refractory_ms="${SPEECH_CORE_TURN_MODEL_EOU_REFRACTORY_MS:-700}"

mkdir -p "$log_dir"
cd "$repo_root"

cargo build --release -p speech-core-daemon >/dev/null

echo "speech-core daemon"
echo "  bind:              $bind"
echo "  log_dir:           $log_dir"
echo "  events_jsonl:       $log_dir/events.jsonl"
echo "  model_path:        $model_path"
echo "  stream_chunk_ms:   $stream_chunk_ms"
echo "  att_context_right: $att_context_right"
echo "  vad_model_path:    $vad_model_path"
echo "  vad_threshold:     $vad_threshold"
echo "  vad_hangover_ms:   $((vad_hangover_frames * 32))"
echo "  eou_model_dir:     $eou_model_dir"
echo "  smart_turn_model:  $smart_turn_model_path"
echo "  smart_turn_gate:   $turn_semantic_gate_enabled / close=$turn_semantic_gate_close_enabled"
echo "  eou_chunk_ms:      $eou_chunk_ms"
echo "  eou_reset_on_token:$eou_reset_on_token"
echo "  vad_close:         $turn_vad_close_enabled"
echo "  min_vad_speech_ms:$turn_min_vad_speech_ms"
echo "  human_hold_ms:     $turn_human_hold_silence_ms"
echo "  transcript_close_ms:$turn_transcript_silence_close_ms"
echo "  model_eou_close:   $turn_model_eou_close_enabled"
echo
echo "client example:"
echo "  SPEECH_CORE_WS_URL=ws://$(tailscale ip -4 | head -n1):8765/ws/audio-ingress /tmp/speech-core-native-build/scripts/speech-core-live-session.sh"
echo

export SPEECH_CORE_VAD_THRESHOLD="$vad_threshold"
export SPEECH_CORE_VAD_ONSET_FRAMES="$vad_onset_frames"
export SPEECH_CORE_VAD_HANGOVER_FRAMES="$vad_hangover_frames"
export SPEECH_CORE_VAD_PRE_SPEECH_FRAMES="$vad_pre_speech_frames"
export SPEECH_CORE_VAD_EMIT_FRAMES="$vad_emit_frames"
export SPEECH_CORE_VAD_SMOOTHING_ALPHA="$vad_smoothing_alpha"
export SPEECH_CORE_VAD_STOP_THRESHOLD="$vad_stop_threshold"
export SPEECH_CORE_VAD_FALLBACK_THRESHOLD="$vad_fallback_threshold"
export SPEECH_CORE_VAD_ACOUSTIC_FALLBACK_SILENCE_MS="$vad_acoustic_fallback_silence_ms"
export SPEECH_CORE_EOU_CHUNK_MS="$eou_chunk_ms"
export SPEECH_CORE_SMART_TURN_THRESHOLD="$smart_turn_threshold"
export SPEECH_CORE_SMART_TURN_TIMEOUT_MS="$smart_turn_timeout_ms"
export SPEECH_CORE_SMART_TURN_CPU_COUNT="$smart_turn_cpu_count"
export SPEECH_CORE_SMART_TURN_MAX_AUDIO_SECS="$smart_turn_max_audio_secs"
export SPEECH_CORE_SMART_TURN_PRE_SPEECH_MS="$smart_turn_pre_speech_ms"
export SPEECH_CORE_SMART_TURN_RECHECK_INTERVAL_MS="$smart_turn_recheck_interval_ms"
export SPEECH_CORE_SMART_TURN_RECHECK_MAX_ATTEMPTS="$smart_turn_recheck_max_attempts"
export SPEECH_CORE_SMART_TURN_RECHECK_OFFSETS_MS="$smart_turn_recheck_offsets_ms"
export SPEECH_CORE_TURN_SEMANTIC_GATE_ENABLED="$turn_semantic_gate_enabled"
export SPEECH_CORE_TURN_SEMANTIC_GATE_CLOSE_ENABLED="$turn_semantic_gate_close_enabled"
export SPEECH_CORE_EOU_RESET_ON_TOKEN="$eou_reset_on_token"
export SPEECH_CORE_EOU_EMIT_TRANSCRIPT="$eou_emit_transcript"
export SPEECH_CORE_TURN_VAD_CLOSE_ENABLED="$turn_vad_close_enabled"
export SPEECH_CORE_TURN_MODEL_EOU_CLOSE_ENABLED="$turn_model_eou_close_enabled"
export SPEECH_CORE_TURN_MIN_VAD_SPEECH_MS="$turn_min_vad_speech_ms"
export SPEECH_CORE_TURN_HUMAN_HOLD_SILENCE_MS="$turn_human_hold_silence_ms"
export SPEECH_CORE_TURN_TRANSCRIPT_SILENCE_CLOSE_MS="$turn_transcript_silence_close_ms"
export SPEECH_CORE_TURN_MIN_MODEL_EOU_SPEECH_MS="$turn_min_model_eou_speech_ms"
export SPEECH_CORE_TURN_MODEL_EOU_REFRACTORY_MS="$turn_model_eou_refractory_ms"
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-1}"
export OMP_WAIT_POLICY="${OMP_WAIT_POLICY:-PASSIVE}"

eou_args=()
if [[ -n "$eou_model_dir" ]]; then
  eou_args=(
    --eou-model-dir "$eou_model_dir"
    --eou-chunk-ms "$eou_chunk_ms"
    --eou-reset-on-token "$eou_reset_on_token"
    --eou-emit-transcript "$eou_emit_transcript"
  )
fi

smart_turn_args=()
if [[ -n "$smart_turn_model_path" ]]; then
  smart_turn_args=(
    --smart-turn-model-path "$smart_turn_model_path"
    --smart-turn-threshold "$smart_turn_threshold"
    --smart-turn-timeout-ms "$smart_turn_timeout_ms"
    --smart-turn-cpu-count "$smart_turn_cpu_count"
    --smart-turn-max-audio-secs "$smart_turn_max_audio_secs"
    --smart-turn-pre-speech-ms "$smart_turn_pre_speech_ms"
    --smart-turn-recheck-interval-ms "$smart_turn_recheck_interval_ms"
    --smart-turn-recheck-max-attempts "$smart_turn_recheck_max_attempts"
    --smart-turn-recheck-offsets-ms "$smart_turn_recheck_offsets_ms"
  )
fi

exec target/release/speech-core-daemon \
  --bind "$bind" \
  --log-dir "$log_dir" \
  --model-path "$model_path" \
  --stream-chunk-ms "$stream_chunk_ms" \
  --att-context-right "$att_context_right" \
  --model-queue-frames "$model_queue_frames" \
  --vad-model-path "$vad_model_path" \
  --vad-threshold "$vad_threshold" \
  --vad-onset-frames "$vad_onset_frames" \
  --vad-hangover-frames "$vad_hangover_frames" \
  --vad-pre-speech-frames "$vad_pre_speech_frames" \
  --vad-emit-frames "$vad_emit_frames" \
  --vad-smoothing-alpha "$vad_smoothing_alpha" \
  --vad-stop-threshold "$vad_stop_threshold" \
  --vad-fallback-threshold "$vad_fallback_threshold" \
  --vad-acoustic-fallback-silence-ms "$vad_acoustic_fallback_silence_ms" \
  "${eou_args[@]}" \
  "${smart_turn_args[@]}" \
  --turn-human-hold-silence-ms "$turn_human_hold_silence_ms" \
  --turn-transcript-silence-close-ms "$turn_transcript_silence_close_ms" \
  --detector-queue-frames "$detector_queue_frames"
