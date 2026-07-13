//! speech-in event subscriber — connects to the daemon and displays live transcripts,
//! VAD diagnostics, and turn events (TUI, transcript, or JSONL modes).

use anyhow::{Context, Result};
use clap::{Parser, ValueEnum};
use futures_util::{SinkExt, StreamExt};
use serde_json::Value;
use speech_core_protocol::ControlMessage;
use std::collections::{HashMap, VecDeque};
use std::fmt;
use std::fs::File;
use std::io::{self, BufRead, BufReader, Write};
use std::path::PathBuf;
use std::process::Command;
use tokio_tungstenite::connect_async;
use tokio_tungstenite::tungstenite::Message;

const SAMPLE_RATE: u64 = 16_000;
const MAX_TUI_TURNS: usize = 10;
const MAX_TUI_NOTES: usize = 10;

/// Independent monotonic clock domains whose timestamps must never be
/// subtracted across domains without a known calibration offset.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
enum ClockDomainKey {
    /// `diagnostic_mono_ns` — harness-side diagnostic clock (elapsed since
    /// harness start). `diagnostic_clock_origin` is metadata that does not
    /// reclassify the domain.
    Harness,
    /// `client_mono_ns` — client-side monotonic (speech-out producer).
    Client,
    /// `daemon_mono_ns`, `ingress_receive_mono_ns`, `detector_end_mono_ns`,
    /// `model_feed_end_mono_ns` — daemon-side monotonic.
    Daemon,
}

#[derive(Debug, Parser)]
#[command(author, version, about = "watch live speech-core daemon events")]
struct Args {
    /// Websocket URL for daemon audio ingress/events endpoint.
    #[arg(
        long,
        default_value = "ws://127.0.0.1:8765/ws/audio-ingress",
        env = "SPEECH_CORE_WS_URL"
    )]
    url: String,

    /// Only show events for this stream id.
    #[arg(long, env = "SPEECH_CORE_STREAM_ID")]
    stream_id: Option<String>,

    /// Only show events for this stream session id.
    #[arg(long, env = "SPEECH_CORE_STREAM_SESSION_ID")]
    stream_session_id: Option<String>,

    /// Print mode. transcript is the normal operator surface; tui/debug are diagnostic-only.
    #[arg(long, value_enum, default_value_t = Mode::Transcript)]
    mode: Mode,

    /// Replay events from a jsonl file instead of opening a websocket.
    #[arg(long)]
    replay_events: Option<PathBuf>,

    /// Read jsonl events from stdin instead of opening a websocket. Useful for composing the TUI with sidecar event producers.
    #[arg(long)]
    stdin_events: bool,

    /// Enable speech-out-specific TUI additions: output glyphs, tuning row, and ASR dispatch seam notes.
    /// Keep this off for plain speech-core-live-session so speech-in and speech-out diagnostics stay separate.
    #[arg(long)]
    speech_out_ui: bool,

    /// Print compact endpointing diagnostics alongside transcript output.
    /// Only affects transcript mode; tui/debug modes are already endpoint-oriented.
    #[arg(long)]
    verbose: bool,

    /// Include high-volume ASR chunk timing diagnostics in transcript mode.
    #[arg(long)]
    trace_asr: bool,

    /// Include VAD frame diagnostics when the daemon emits vad_frame events.
    #[arg(long)]
    trace_vad: bool,

    /// Include per-token ASR commit timing diagnostics in transcript mode.
    #[arg(long)]
    trace_tokens: bool,

    /// Command used by inject mode to type committed transcript deltas into the focused Wayland client.
    #[arg(long, default_value = "wtype", env = "SPEECH_CORE_INJECT_COMMAND")]
    inject_command: String,

    /// Path to a ready-signal file. After connect_async succeeds and the
    /// SubscribeEvents message is sent, the file is atomically created/written.
    /// No ready signal is emitted before that point. Stale file at this path is
    /// removed at startup (caller should provide a fresh unique path).
    #[arg(long)]
    ready_file: Option<PathBuf>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, ValueEnum)]
enum Mode {
    /// Compact symbolic state surface: speech → pause → semantic probes → close/wait/resume.
    Tui,
    /// TUI plus a small turn-local explanation footer.
    Debug,
    /// Show the live committed transcript as it changes, plus <EOU>.
    Transcript,
    /// Type committed transcript deltas into the currently focused Wayland client.
    Inject,
    /// Print matching JSONL events exactly as received. Raw trace mode.
    Jsonl,
}

impl Mode {
    fn is_tui(self) -> bool {
        matches!(self, Mode::Tui | Mode::Debug)
    }

    fn is_debug(self) -> bool {
        matches!(self, Mode::Debug)
    }
}

#[derive(Debug, Clone, Copy)]
struct SmartTurnObservation {
    end_sample: u64,
    decision_sample: u64,
    probability: f64,
    threshold: f64,
    complete: bool,
    over_budget: bool,
    inference_duration_ms: f64,
    feature_duration_ms: f64,
    model_duration_ms: f64,
}

impl SmartTurnObservation {
    fn from_value(value: &Value) -> Self {
        Self {
            end_sample: value
                .get("end_sample")
                .and_then(|v| v.as_u64())
                .unwrap_or_default(),
            decision_sample: value
                .get("decision_sample")
                .and_then(|v| v.as_u64())
                .unwrap_or_default(),
            probability: value
                .get("probability")
                .and_then(|v| v.as_f64())
                .unwrap_or_default(),
            threshold: value
                .get("threshold")
                .and_then(|v| v.as_f64())
                .unwrap_or_default(),
            complete: value
                .get("complete")
                .and_then(|v| v.as_bool())
                .unwrap_or(false),
            over_budget: value
                .get("timed_out")
                .and_then(|v| v.as_bool())
                .unwrap_or(false),
            inference_duration_ms: value
                .get("inference_duration_ms")
                .and_then(|v| v.as_f64())
                .unwrap_or_default(),
            feature_duration_ms: value
                .get("feature_duration_ms")
                .and_then(|v| v.as_f64())
                .unwrap_or_default(),
            model_duration_ms: value
                .get("model_duration_ms")
                .and_then(|v| v.as_f64())
                .unwrap_or_default(),
        }
    }
}

#[derive(Debug, Clone, Copy)]
struct VadEndObservation {
    end_sample: u64,
}

#[derive(Debug, Clone)]
struct BoundaryState {
    end_sample: u64,
    check_count: usize,
    pending_check_glyph: Option<usize>,
}

#[derive(Debug, Default)]
struct TranscriptState {
    last_text: String,
    final_text: String,
    printed_transcript: bool,
    last_smart_turn: Option<SmartTurnObservation>,
    last_vad_end: Option<VadEndObservation>,
    smart_turn_attempts_by_end: HashMap<u64, u32>,
}

#[derive(Debug, Default)]
struct TuiModel {
    stream_session_id: Option<String>,
    vad_config: Option<String>,
    smart_turn_config: Option<String>,
    runtime_id: Option<String>,
    build_id: Option<String>,
    config_id: Option<String>,
    speech_out_ui: bool,
    turns: Vec<TuiTurn>,
    current: Option<usize>,
    last_closed: Option<usize>,
    next_turn_number: u64,
    notes: VecDeque<String>,
    live_vad_bar: String,
    live_fallback_bar: String,
    live_hold_bar: String,
    speech_out_params: Option<String>,
    /// Per-clock-domain baselines. The first observed monotonic timestamp in
    /// each domain seeds the baseline; subsequent timestamps in that domain
    /// are rendered as deltas from the baseline. Timestamps from different
    /// domains are never subtracted across domains.
    clock_baselines: HashMap<ClockDomainKey, u64>,
    transcript: TuiTranscriptState,
}

#[derive(Debug, Default, Clone)]
struct TuiTranscriptState {
    /// Last full transcript payload seen from the daemon. The daemon currently
    /// sends cumulative transcript text, not turn-local deltas.
    display: String,
    /// Last processed transcript revision. Older revisions are ignored so replay
    /// and live mode behave deterministically under out-of-order delivery.
    revision: Option<i64>,
    /// Authoritative owner for the current transcript stream, inferred from
    /// turn_id when present or conservatively from currently-open/last-closed
    /// turns when transcript_update lacks turn_id.
    owner: Option<usize>,
}

#[derive(Debug, Clone)]
struct TuiTurn {
    number: u64,
    turn_id: Option<String>,
    /// Full transcript prefix at the moment this TUI turn was opened. Used to
    /// derive a turn-local segment from cumulative transcript_update payloads.
    transcript_prefix: String,
    sections: Vec<TuiSection>,
    text: String,
    glyphs: Vec<String>,
    outputs: Vec<SpeechOutLine>,
    boundary: Option<BoundaryState>,
    paused: bool,
    closed: bool,
}

#[derive(Debug, Clone)]
struct TuiSection {
    text: String,
    glyphs: Vec<String>,
}

#[derive(Debug, Clone)]
struct SpeechOutLine {
    text: String,
    chunks: Vec<String>,
    glyphs: Vec<String>,
    /// When set, render [0..cut) dim grey (spoken/heard before barge) and
    /// [cut..) normal white (not spoken / cancelled remainder of intended).
    /// `text` remains the full intended assistant utterance.
    cut_prefix: Option<String>,
    cut_source: Option<String>,
}

impl TuiModel {
    fn new(speech_out_ui: bool) -> Self {
        Self {
            speech_out_ui,
            ..Self::default()
        }
    }

    fn handle(&mut self, value: &Value) {
        // Observe the event timestamp domain before dispatching so that
        // per-domain baselines are seeded even when the handler does not
        // call note_event (e.g. vad_speech_start uses self.note).
        self.observe_event_clock(value);

        if let Some(session_id) = value.get("stream_session_id").and_then(|v| v.as_str()) {
            self.stream_session_id
                .get_or_insert_with(|| session_id.to_owned());
        }
        let event = event_name(value);
        match event {
            "runtime_provenance" => {
                let runtime_id = value
                    .get("runtime_id")
                    .and_then(|v| v.as_str())
                    .map(str::to_owned);
                let build_id = value
                    .get("build_id")
                    .and_then(|v| v.as_str())
                    .map(str::to_owned);
                let config_id = value
                    .get("config_id")
                    .and_then(|v| v.as_str())
                    .map(str::to_owned);
                self.runtime_id = runtime_id;
                self.build_id = build_id;
                self.config_id = config_id;
                if let Some(id) = &self.runtime_id {
                    self.note(format!("runtime provenance: {id}"));
                }
            }
            "turn_session_start" | "vad_session_start" => {
                self.clear_live_timers();
                if event == "turn_session_start" {
                    self.note("turn session reset".to_owned());
                    return;
                }

                let frame_ms = value.get("frame_ms").and_then(|v| v.as_u64()).unwrap_or(20);
                let threshold = value
                    .get("threshold")
                    .and_then(|v| v.as_f64())
                    .unwrap_or_default();
                let onset = value
                    .get("onset_frames")
                    .and_then(|v| v.as_u64())
                    .unwrap_or_default();
                let hangover = value
                    .get("hangover_frames")
                    .and_then(|v| v.as_u64())
                    .unwrap_or_default();
                self.vad_config = Some(format!(
                    "silero frame={frame_ms}ms onset={onset} hangover={hangover} ({}) threshold={threshold:.2}",
                    format_ms(hangover.saturating_mul(frame_ms))
                ));
                self.note(format!(
                    "vad configured: hangover {}",
                    format_ms(hangover * frame_ms)
                ));
            }
            "smart_turn_session_start" => {
                let threshold = value
                    .get("threshold")
                    .and_then(|v| v.as_f64())
                    .unwrap_or_default();
                let timeout_ms = value
                    .get("timeout_ms")
                    .and_then(|v| v.as_u64())
                    .unwrap_or_default();
                let offsets = value
                    .get("recheck_offsets_ms")
                    .and_then(|v| v.as_array())
                    .map(|values| {
                        values
                            .iter()
                            .filter_map(|v| v.as_u64())
                            .map(format_ms)
                            .collect::<Vec<_>>()
                            .join("/")
                    })
                    .filter(|s| !s.is_empty())
                    .unwrap_or_else(|| "off".to_owned());
                self.smart_turn_config = Some(format!(
                    "smart-turn threshold={threshold:.2} budget={timeout_ms}ms rechecks={offsets}"
                ));
                self.note(format!("smart-turn ready: rechecks {offsets}"));
            }
            "transcript_token_committed" => {}
            "transcript_update" => self.handle_transcript_update(value),
            "transcript_committed" | "turn_transcript_committed" => {
                // Authoritative per-turn committed transcript event. Applies
                // only to a matching open turn; ignores closed turns and
                // orphan events.
                //
                // The event carries the full turn text, but the TUI renders
                // turn text as section deltas anchored at transcript_prefix.
                // Compute the delta so the current section shows only the
                // portion spoken since the last boundary/resume.
                let text = value.get("text").and_then(|v| v.as_str()).unwrap_or("");
                if text.is_empty() {
                    return;
                }
                if let Some(idx) = self
                    .turn_for_event(value)
                    .or_else(|| self.current_open_turn())
                    .filter(|idx| !self.turns[*idx].closed)
                {
                    self.set_turn_id(idx, value);
                    let prefix = &self.turns[idx].transcript_prefix;
                    let local = if text.starts_with(prefix) {
                        &text[prefix.len()..]
                    } else {
                        text
                    };
                    self.turns[idx].text = normalized_text(local);
                }
            }
            "turn_started" => {
                let start_sample = value
                    .get("start_sample")
                    .and_then(|v| v.as_u64())
                    .unwrap_or_default();
                let source = value
                    .get("source")
                    .and_then(|v| v.as_str())
                    .unwrap_or("turn");
                let idx = self
                    .turn_for_event(value)
                    .or_else(|| self.current_open_turn())
                    .unwrap_or_else(|| self.new_turn());
                self.set_turn_id(idx, value);
                self.note_event(
                    value,
                    format!(
                        "turn started by {source} at {}",
                        format_ms(samples_to_ms(start_sample))
                    ),
                );
            }
            "model_chunk_processed" if self.speech_out_ui => {
                self.handle_model_chunk_processed(value);
            }
            "vad_speech_start" => {
                self.clear_live_timers();
                if let Some(idx) = self.current_open_turn() {
                    if self.turns[idx].paused {
                        self.push_glyph(idx, "↺");
                        self.finalize_section(idx);
                        self.turns[idx].paused = false;
                        self.turns[idx].boundary = None;
                        // Anchor the transcript prefix so subsequent cumulative
                        // transcript_update payloads render only the new delta.
                        self.turns[idx].transcript_prefix = self.transcript.display.clone();
                        self.push_glyph(idx, "◖");
                        self.note("speech resumed; fresh transcript section started".to_owned());
                    } else if !self.turns[idx].glyphs.iter().any(|glyph| glyph == "◖") {
                        self.push_glyph(idx, "◖");
                        self.note(
                            "vad speech start attached to pending transcript text".to_owned(),
                        );
                    }
                } else {
                    let idx = self.new_turn();
                    self.push_glyph(idx, "◖");
                    self.note(format!(
                        "vad speech start at {}",
                        sample_field_ms(value, "start_sample")
                    ));
                }
            }
            "vad_speech_end" => {
                let idx = self.ensure_turn();
                let end_sample = value
                    .get("end_sample")
                    .and_then(|v| v.as_u64())
                    .unwrap_or_default();
                self.push_glyph(idx, "◗");
                self.turns[idx].paused = true;
                self.turns[idx].boundary = Some(BoundaryState {
                    end_sample,
                    check_count: 0,
                    pending_check_glyph: None,
                });
                self.note_event(
                    value,
                    format!(
                        "vad candidate boundary at {}; smart-turn probes begin",
                        sample_field_ms(value, "end_sample")
                    ),
                );
            }
            "smart_turn_candidate" => {
                let idx = self.ensure_turn();
                self.ensure_boundary_for_event(idx, value);
                let check_count = {
                    let boundary = self.turns[idx].boundary.as_mut().expect("boundary exists");
                    boundary.check_count = boundary.check_count.saturating_add(1);
                    boundary.check_count
                };
                let glyph = format!("{}…", circled(check_count));
                self.turns[idx].glyphs.push(glyph);
                let glyph_idx = self.turns[idx].glyphs.len() - 1;
                if let Some(boundary) = self.turns[idx].boundary.as_mut() {
                    boundary.pending_check_glyph = Some(glyph_idx);
                }
            }
            "smart_turn_decision" | "smart_turn_timeout" => {
                let idx = self.ensure_turn();
                self.ensure_boundary_for_event(idx, value);
                let probability = value
                    .get("probability")
                    .and_then(|v| v.as_f64())
                    .unwrap_or_default();
                let complete = value
                    .get("complete")
                    .and_then(|v| v.as_bool())
                    .unwrap_or(false);
                let pending = self.turns[idx]
                    .boundary
                    .as_ref()
                    .and_then(|boundary| boundary.pending_check_glyph);
                let (glyph_idx, check_count) = if let Some(glyph_idx) = pending {
                    let check_count = self.turns[idx]
                        .boundary
                        .as_ref()
                        .map(|boundary| boundary.check_count)
                        .unwrap_or(1);
                    (glyph_idx, check_count)
                } else {
                    let check_count = {
                        let boundary = self.turns[idx].boundary.as_mut().expect("boundary exists");
                        boundary.check_count = boundary.check_count.saturating_add(1);
                        boundary.check_count
                    };
                    self.turns[idx].glyphs.push(String::new());
                    (self.turns[idx].glyphs.len() - 1, check_count)
                };
                let rendered = format!(
                    "{}{}",
                    circled(check_count),
                    compact_probability(probability)
                );
                self.turns[idx].glyphs[glyph_idx] = rendered;
                if let Some(boundary) = self.turns[idx].boundary.as_mut() {
                    boundary.pending_check_glyph = None;
                }
                self.note_event(
                    value,
                    format!(
                        "smart-turn check {} -> {} p={probability:.3}",
                        check_count,
                        if complete { "complete" } else { "hold" }
                    ),
                );
            }
            "smart_turn_recheck_scheduled" => {
                let idx = self.ensure_turn();
                self.push_wait(idx);
                let remaining = value
                    .get("pending_decision_samples")
                    .and_then(|v| v.as_array())
                    .map(|a| a.len())
                    .unwrap_or_default();
                self.note_event(
                    value,
                    format!("semantic rechecks scheduled: {remaining} remaining"),
                );
            }
            "smart_turn_recheck_exhausted" => {
                let idx = self.ensure_turn();
                self.push_glyph(idx, "◇");
                self.turns[idx].paused = true;
                self.note("semantic probes exhausted without completion".to_owned());
            }
            "smart_turn_recheck_cancelled" => {
                let idx = self.ensure_turn();
                if self.turns[idx].paused {
                    self.push_glyph(idx, "↺");
                    self.finalize_section(idx);
                    self.push_glyph(idx, "◖");
                    // Anchor the transcript prefix so subsequent cumulative
                    // transcript_update payloads render only the new delta.
                    self.turns[idx].transcript_prefix = self.transcript.display.clone();
                }
                self.turns[idx].paused = false;
                self.turns[idx].boundary = None;
                self.note("semantic probes cancelled by resumed speech".to_owned());
            }
            "turn_eou_suppressed" => {
                let source = value.get("source").and_then(|v| v.as_str()).unwrap_or("?");
                let reason = value.get("reason").and_then(|v| v.as_str()).unwrap_or("?");
                self.note_event(value, format!("no eou: {source}/{reason}"));
            }
            "turn_closed" => {
                let idx = self
                    .turn_for_event(value)
                    .unwrap_or_else(|| self.ensure_turn());
                let source = value.get("source").and_then(|v| v.as_str()).unwrap_or("?");
                if matches!(source, "smart_turn" | "vad_acoustic_fallback") {
                    self.push_glyph(idx, "◆");
                } else {
                    self.push_glyph(idx, "◇");
                }
                self.turns[idx].closed = true;
                self.turns[idx].paused = false;
                self.set_turn_id(idx, value);
                self.last_closed = Some(idx);
                self.current = None;
                self.clear_live_timers();
                self.note_event(value, format!("turn closed by {source}"));
            }
            "speech_out_request_queued" if self.speech_out_ui => {
                let text = value.get("text").and_then(|v| v.as_str()).unwrap_or("");
                let idx = self.last_turn_for_output();
                self.turns[idx].outputs.push(SpeechOutLine {
                    text: text.trim().to_owned(),
                    chunks: Vec::new(),
                    glyphs: vec!["⏳".to_owned()],
                    cut_prefix: None,
                    cut_source: None,
                });
                self.note_event(value, "speech-out request queued".to_owned());
            }
            "speech_out_request_received" if self.speech_out_ui => {
                let text = value.get("text").and_then(|v| v.as_str()).unwrap_or("");
                let idx = self.last_turn_for_output();
                if !text.trim().is_empty() {
                    let needs_new_output = self.turns[idx].outputs.last().is_none_or(|output| {
                        !output.text.trim().is_empty() && output.text != text.trim()
                    });
                    if needs_new_output {
                        self.turns[idx].outputs.push(SpeechOutLine {
                            text: text.trim().to_owned(),
                            chunks: Vec::new(),
                            glyphs: Vec::new(),
                            cut_prefix: None,
                            cut_source: None,
                        });
                    } else if let Some(output) = self.turns[idx].outputs.last_mut() {
                        output.text = text.trim().to_owned();
                    }
                }
                self.push_output_glyph(idx, "⇢");
                let mut note = "speech-out request received".to_owned();
                if let Some(num_chunks) = value.get("num_chunks").and_then(|v| v.as_u64()) {
                    note.push_str(&format!(" — {num_chunks} text chunks"));
                }
                self.note_event(value, note);
            }
            "speech_out_text_chunks" if self.speech_out_ui => {
                let idx = self.last_turn_for_output();
                if let Some(chunks) = value.get("chunks").and_then(|v| v.as_array()) {
                    let rendered_chunks = chunks
                        .iter()
                        .filter_map(|c| c.as_str())
                        .map(|c| c.trim().to_owned())
                        .filter(|c| !c.is_empty())
                        .collect::<Vec<_>>();
                    let count = rendered_chunks.len();
                    let total_chars: usize = rendered_chunks.iter().map(|c| c.len()).sum();
                    if let Some(output) = self.turns[idx].outputs.last_mut() {
                        output.chunks = rendered_chunks;
                    }
                    self.note_event(
                        value,
                        format!("speech-out text chunked: {count} chunks, {total_chars} chars"),
                    );
                }
                self.push_output_glyph(idx, "✂");
            }
            "speech_out_synthesis_started" if self.speech_out_ui => {
                let idx = self.last_turn_for_output();
                self.push_output_glyph(idx, "⌁");
                let mut note = "speech-out synthesis started".to_owned();
                if let Some(ms) = value
                    .get("request_received_to_synthesis_started_ms")
                    .and_then(|v| v.as_f64())
                {
                    note.push_str(&format!(" ({ms:.1}ms setup)"));
                }
                self.note_event(value, note);
            }
            "speech_out_text_chunk_started" if self.speech_out_ui => {
                let idx = self.last_turn_for_output();
                let chunk = value
                    .get("text_chunk_index")
                    .and_then(|v| v.as_u64())
                    .unwrap_or_default()
                    + 1;
                let count = value
                    .get("text_chunk_count")
                    .and_then(|v| v.as_u64())
                    .unwrap_or_default();
                self.push_output_glyph(idx, "…");
                if count > 0 {
                    self.note_event(
                        value,
                        format!("speech-out text chunk {chunk}/{count} started"),
                    );
                } else {
                    self.note_event(value, format!("speech-out text chunk {chunk} started"));
                }
            }
            "speech_out_audio_chunk" if self.speech_out_ui => {
                let idx = self.last_turn_for_output();
                let seq = value
                    .get("seq")
                    .and_then(|v| v.as_u64())
                    .unwrap_or_default();
                if seq == 0 {
                    self.push_output_glyph(idx, "▣");
                    let bytes = value
                        .get("bytes")
                        .and_then(|v| v.as_u64())
                        .unwrap_or_default();
                    let mut note = format!("speech-out first audio chunk {bytes}b");
                    if let Some(ms) = value
                        .get("synthesis_started_to_first_audio_ms")
                        .and_then(|v| v.as_f64())
                    {
                        note.push_str(&format!(" (ttfb={ms:.1}ms)"));
                    }
                    self.note_event(value, note);
                }
            }
            "speech_out_text_chunk_completed" if self.speech_out_ui => {
                let chunk = value
                    .get("text_chunk_index")
                    .and_then(|v| v.as_u64())
                    .unwrap_or_default()
                    + 1;
                let count = value
                    .get("text_chunk_count")
                    .and_then(|v| v.as_u64())
                    .unwrap_or_default();
                let bytes = value
                    .get("bytes")
                    .and_then(|v| v.as_u64())
                    .unwrap_or_default();
                let mut note = if count > 0 {
                    format!("speech-out text chunk {chunk}/{count} completed: {bytes}b")
                } else {
                    format!("speech-out text chunk {chunk} completed: {bytes}b")
                };
                if let Some(ms) = value
                    .get("text_chunk_synthesis_duration_ms")
                    .and_then(|v| v.as_f64())
                {
                    note.push_str(&format!(" ({ms:.1}ms)"));
                }
                self.note_event(value, note);
            }
            "speech_out_completed" if self.speech_out_ui => {
                let idx = self.last_turn_for_output();
                self.push_output_glyph(idx, "✓");
                let bytes = value.get("bytes").and_then(|v| v.as_u64()).unwrap_or(0);
                let chunks = value
                    .get("chunk_count")
                    .and_then(|v| v.as_u64())
                    .unwrap_or(0);
                let mut note = format!("speech-out completed: {bytes}b in {chunks} chunks");
                if let Some(ms) = value
                    .get("total_synthesis_duration_ms")
                    .and_then(|v| v.as_f64())
                {
                    note.push_str(&format!(", {ms:.0}ms"));
                }
                if let Some(secs) = value.get("audio_duration_secs").and_then(|v| v.as_f64()) {
                    note.push_str(&format!(", audio={secs:.1}s"));
                }
                if let Some(rtf) = value.get("realtime_factor").and_then(|v| v.as_f64()) {
                    note.push_str(&format!(", rtf={rtf:.2}x"));
                }
                self.note_event(value, note);
            }
            "speech_out_diagnostic_terminal" if self.speech_out_ui => {
                let idx = self.last_turn_for_output();
                let outcome = value
                    .get("outcome")
                    .and_then(|v| v.as_str())
                    .unwrap_or("unknown");
                match outcome {
                    "completed" => self.push_output_glyph(idx, "✓"),
                    "cancelled" => self.push_output_glyph(idx, "⏹"),
                    "failed" => self.push_output_glyph(idx, "✗"),
                    _ => self.push_output_glyph(idx, "?"),
                }
                let mut note = format!("speech-out terminal: {outcome}");
                if let Some(ms) = value.get("e2e_ms").and_then(|v| v.as_f64()) {
                    note.push_str(&format!(" e2e={ms:.1}ms"));
                }
                if let Some(ms) = value.get("first_audio_ms").and_then(|v| v.as_f64()) {
                    note.push_str(&format!(" first-audio={ms:.1}ms"));
                }
                if let Some(ms) = value.get("cancel_latency_ms").and_then(|v| v.as_f64()) {
                    note.push_str(&format!(" cancel={ms:.1}ms"));
                }
                self.note_event(value, note);
            }

            "speech_out_skipped" if self.speech_out_ui => {
                let reason = value
                    .get("reason")
                    .and_then(|v| v.as_str())
                    .unwrap_or("unknown");
                self.note_event(value, format!("speech-out skipped: {reason}"));
            }
            "speech_out_echo_suppressed" if self.speech_out_ui => {
                let context = value
                    .get("context")
                    .and_then(|v| v.as_str())
                    .unwrap_or("transcript");
                self.note_event(value, format!("speech-out self-echo suppressed: {context}"));
            }
            "speech_out_barge_in" | "speech_out_cancel_requested" if self.speech_out_ui => {
                if let Some(idx) = self.turns.len().checked_sub(1) {
                    if !self.turns[idx].outputs.is_empty() {
                        self.push_output_glyph(idx, "⏹");
                    }
                }
                let trigger = value
                    .get("trigger")
                    .or_else(|| value.get("reason"))
                    .and_then(|v| v.as_str())
                    .unwrap_or("user_speech");
                self.note_event(value, format!("speech-out cancel requested: {trigger}"));
            }
            // Dual-Nemotron cut: spoken prefix (assistant self-ASR / pad fallback)
            // is dim grey; unsaid remainder of intended text stays normal white.
            "assistant_turn_truncated" if self.speech_out_ui => {
                let intended = value
                    .get("intended_text")
                    .or_else(|| value.get("text"))
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .trim()
                    .to_owned();
                let spoken = value
                    .get("spoken_prefix")
                    .or_else(|| value.get("cut_text"))
                    .or_else(|| value.get("text"))
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .trim()
                    .to_owned();
                let source = value
                    .get("primary_cut_source")
                    .and_then(|v| v.as_str())
                    .unwrap_or("fallback")
                    .to_owned();
                let idx = self.last_turn_for_output();
                let full_text = if intended.is_empty() {
                    spoken.clone()
                } else {
                    intended
                };
                if let Some(output) = self.turns[idx].outputs.last_mut() {
                    if !full_text.is_empty() {
                        output.text = full_text;
                    }
                    output.cut_prefix = if spoken.is_empty() {
                        None
                    } else {
                        Some(spoken.clone())
                    };
                    output.cut_source = Some(source.clone());
                    if output
                        .glyphs
                        .last()
                        .is_none_or(|g| g != "✂" && g != "⏹")
                    {
                        output.glyphs.push("✂".to_owned());
                    }
                } else if !full_text.is_empty() {
                    self.turns[idx].outputs.push(SpeechOutLine {
                        text: full_text,
                        chunks: Vec::new(),
                        glyphs: vec!["✂".to_owned()],
                        cut_prefix: if spoken.is_empty() {
                            None
                        } else {
                            Some(spoken.clone())
                        },
                        cut_source: Some(source.clone()),
                    });
                }
                let note = if spoken.is_empty() {
                    format!("assistant cut ({source}): empty")
                } else {
                    format!("assistant cut ({source}): {spoken}")
                };
                self.note_event(value, note);
            }
            "speech_out_cancel_ack" | "speech_out_cancel_acknowledged" if self.speech_out_ui => {
                let idx = self.last_turn_for_output();
                self.push_output_glyph(idx, "⏹");
                let mut note = "speech-out cancel acknowledged".to_owned();
                if let Some(ms) = value.get("cancel_latency_ms").and_then(|v| v.as_f64()) {
                    note.push_str(&format!(" ({ms:.1}ms)"));
                }
                self.note_event(value, note);
            }
            "speech_out_playback_started" if self.speech_out_ui => {
                let seq = value
                    .get("playback_seq")
                    .and_then(|v| v.as_u64())
                    .unwrap_or_default();
                self.note_event(value, format!("speech-out playback started: chunk {seq}"));
            }
            "speech_out_playback_ready" | "speech_out_playback_gate_released"
                if self.speech_out_ui =>
            {
                let seq = value
                    .get("playback_seq")
                    .or_else(|| value.get("text_chunk_index"))
                    .and_then(|v| v.as_u64())
                    .unwrap_or_default();
                self.note_event(value, format!("speech-out playback ready: chunk {seq}"));
            }
            "speech_out_playback_completed" if self.speech_out_ui => {
                let seq = value
                    .get("playback_seq")
                    .and_then(|v| v.as_u64())
                    .unwrap_or_default();
                let mut note = format!("speech-out playback completed: chunk {seq}");
                if let Some(ms) = value.get("playback_duration_ms").and_then(|v| v.as_f64()) {
                    note.push_str(&format!(" ({ms:.0}ms)"));
                }
                self.note_event(value, note);
            }
            "speech_out_playback_failed" if self.speech_out_ui => {
                let idx = self.last_turn_for_output();
                self.push_output_glyph(idx, "✗");
                let seq = value
                    .get("playback_seq")
                    .and_then(|v| v.as_u64())
                    .unwrap_or_default();
                let message = value
                    .get("message")
                    .and_then(|v| v.as_str())
                    .unwrap_or("unknown error");
                self.note_event(
                    value,
                    format!("speech-out playback failed: chunk {seq}: {message}"),
                );
            }
            "speech_out_failed" if self.speech_out_ui => {
                let idx = self.last_turn_for_output();
                self.push_output_glyph(idx, "✗");
                let message = value
                    .get("message")
                    .and_then(|v| v.as_str())
                    .unwrap_or("unknown error");
                self.note_event(value, format!("speech-out failed: {message}"));
            }
            "speech_out_params_updated" if self.speech_out_ui => {
                let selected = value
                    .get("selected")
                    .and_then(|v| v.as_str())
                    .unwrap_or("?");
                let speed = value
                    .get("speed")
                    .and_then(|v| v.as_f64())
                    .map(|v| format!("{v:.2}"))
                    .unwrap_or_else(|| "?".to_owned());
                let steps = value
                    .get("steps")
                    .and_then(|v| v.as_u64())
                    .map(|v| v.to_string())
                    .unwrap_or_else(|| "?".to_owned());
                let voice = value.get("voice").and_then(|v| v.as_str()).unwrap_or("?");
                self.speech_out_params = Some(format!(
                    "tuning  [j/k select, h/l adjust] selected={selected} speed={speed} steps={steps} voice={voice}"
                ));
                self.note_event(
                    value,
                    format!("speech-out tuning: selected={selected} speed={speed} steps={steps} voice={voice}"),
                );
            }
            "turn_session_end"
            | "vad_session_end"
            | "smart_turn_session_end"
            | "eou_session_end" => {
                self.clear_live_timers();
                if event == "turn_session_end" {
                    self.current = None;
                    self.note_event(value, "turn session ended".to_owned());
                }
            }
            "eou_state_reset" => {
                self.clear_live_timers();
                self.note_event(value, "endpointing state reset".to_owned());
            }
            "vad_state" | "vad_meter" | "vad_frame" => {
                let probability = value
                    .get("probability")
                    .and_then(|v| v.as_f64())
                    .unwrap_or_default();
                let smoothed = value
                    .get("smoothed_probability")
                    .and_then(|v| v.as_f64())
                    .unwrap_or(probability);
                let silence = value
                    .get("silence_counter")
                    .and_then(|v| v.as_u64())
                    .unwrap_or_default();
                let hangover = value
                    .get("hangover_frames")
                    .and_then(|v| v.as_u64())
                    .unwrap_or_default();
                let energy_rms = value.get("energy_rms").and_then(|v| v.as_f64());
                let energy_gated = value
                    .get("energy_gated")
                    .and_then(|v| v.as_bool())
                    .unwrap_or(false);
                self.push_vad_bar(0, probability, smoothed, silence, hangover, energy_rms, energy_gated);
                // Update fallback timer bar from vad_meter.
                let fallback_progress = value
                    .get("fallback_progress_ms")
                    .and_then(|v| v.as_u64())
                    .unwrap_or(0);
                let fallback_target = value
                    .get("fallback_target_ms")
                    .and_then(|v| v.as_u64())
                    .unwrap_or(3500);
                if self.current_open_turn().is_some() && fallback_progress > 0 {
                    self.push_fallback_bar(fallback_progress, fallback_target);
                } else {
                    self.live_fallback_bar.clear();
                }
            }
            "turn_hold" => {
                let hold_progress = value
                    .get("hold_progress_ms")
                    .and_then(|v| v.as_u64())
                    .unwrap_or(0);
                let hold_target = value
                    .get("hold_target_ms")
                    .and_then(|v| v.as_u64())
                    .unwrap_or(7500);
                if self.turn_for_event(value).is_some() && hold_progress > 0 {
                    self.push_hold_bar(hold_progress, hold_target);
                } else {
                    self.live_hold_bar.clear();
                }
            }
            "vad_acoustic_fallback" => {
                // Only attach to a matching *open* turn; ignore orphan/stale
                // fallback events when no turn is open or the matched turn
                // has already been closed.
                if let Some(idx) = self
                    .turn_for_event(value)
                    .filter(|idx| !self.turns[*idx].closed)
                {
                    self.push_wait(idx);
                    self.note_event(
                        value,
                        format!(
                            "acoustic fallback armed at {}; low smoothed vad",
                            sample_field_ms(value, "decision_sample")
                        ),
                    );
                }
            }
            "turn_human_hold" => {
                let ms_without_tokens = value
                    .get("ms_without_tokens")
                    .and_then(|v| v.as_u64())
                    .unwrap_or_default();
                self.note_event(
                    value,
                    format!(
                        "human hold: speech-like audio for {} without new transcript tokens",
                        format_ms(ms_without_tokens)
                    ),
                );
            }
            _ => {}
        }
    }

    fn render(&self, debug: bool) -> String {
        let mut out = String::new();
        out.push_str("speech-core turn tui\n");
        if let Some(session_id) = &self.stream_session_id {
            out.push_str(&format!("session {session_id}\n"));
        }
        if let Some(runtime_id) = &self.runtime_id {
            out.push_str(&format!("runtime {runtime_id}\n"));
        }
        if let Some(vad) = &self.vad_config {
            out.push_str(&format!("{vad}\n"));
        }
        if let Some(smart) = &self.smart_turn_config {
            out.push_str(&format!("{smart}\n"));
        }
        out.push_str("glyphs  ◖ speech  ◗ pause  ①②③④ semantic checks  ◆ close  ↺ resume  · wait  ◇ unresolved/fallback\n");
        if self.speech_out_ui {
            out.push_str(
                "output  ⏳ queued  ⇢ request  ✂ chunked  ⌁ synth  … text-chunk  ▣ first-audio  ⏹ cancel  ✓ done  ✗ failed\n",
            );
            if let Some(params) = &self.speech_out_params {
                out.push_str(params);
                out.push('\n');
            }
        }
        out.push('\n');

        let start = self.turns.len().saturating_sub(MAX_TUI_TURNS);
        for turn in &self.turns[start..] {
            out.push_str(&turn.render());
            out.push('\n');
        }
        if self.turns.is_empty() {
            out.push_str("waiting for speech…\n");
        }

        if debug {
            out.push_str("\nlast seam transitions\n");
            for note in &self.notes {
                out.push_str("  • ");
                out.push_str(note);
                out.push('\n');
            }
        }
        out.push_str("\nlive vad\n  ");
        if self.live_vad_bar.is_empty() {
            out.push_str("waiting…\n");
        } else {
            out.push_str(&self.live_vad_bar);
            out.push('\n');
            if !self.live_fallback_bar.is_empty() {
                out.push_str(&self.live_fallback_bar);
                out.push('\n');
            }
            if !self.live_hold_bar.is_empty() {
                out.push_str(&self.live_hold_bar);
                out.push('\n');
            }
        }
        out
    }

    fn ensure_turn(&mut self) -> usize {
        self.current_open_turn().unwrap_or_else(|| self.new_turn())
    }

    fn handle_transcript_update(&mut self, value: &Value) {
        let revision = value.get("revision").and_then(|v| v.as_i64());
        if let (Some(previous), Some(next)) = (self.transcript.revision, revision) {
            if next < previous {
                self.note_event(
                    value,
                    format!("ignored stale transcript revision {next} < {previous}"),
                );
                return;
            }
        }

        let committed = value
            .get("committed_text")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let tentative = value
            .get("tentative_text")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let display = format!("{committed}{tentative}");
        self.transcript.revision = revision.or(self.transcript.revision);
        self.transcript.display = display.clone();
        if display.is_empty() {
            return;
        }

        let idx = self.transcript_turn(value);
        // Closed visible turns are immutable; ordinary late transcript_update /
        // finalize events after close must not rewrite them.
        if self.turns[idx].closed {
            return;
        }

        let local = if display.starts_with(&self.turns[idx].transcript_prefix) {
            display[self.turns[idx].transcript_prefix.len()..].to_owned()
        } else {
            // Backward-compatible fallback for streams where the daemon/model resets
            // cumulative transcript text between turns.
            display.clone()
        };

        // Punctuation-only deltas (e.g. late "." from a prior closed turn)
        // must not render as the open turn's text, but must still advance the
        // section baseline so subsequent real deltas compute correctly.
        if is_punctuation_only_delta(&local) {
            self.turns[idx].transcript_prefix = display;
            return;
        }

        self.turns[idx].text = local;
    }

    fn transcript_turn(&mut self, value: &Value) -> usize {
        if let Some(turn_id) = value.get("turn_id").and_then(|v| v.as_str()) {
            if let Some(idx) = self.turns.iter().position(|turn| {
                turn.turn_id.as_deref() == Some(turn_id) || (turn.turn_id.is_none() && !turn.closed)
            }) {
                if self.turns[idx].turn_id.is_none() {
                    self.turns[idx].turn_id = Some(turn_id.to_owned());
                }
                self.transcript.owner = Some(idx);
                return idx;
            }
            let idx = self.new_turn();
            self.turns[idx].turn_id = Some(turn_id.to_owned());
            self.transcript.owner = Some(idx);
            return idx;
        }

        if let Some(idx) = self.current_open_turn() {
            self.transcript.owner = Some(idx);
            return idx;
        }
        if let Some(idx) = self
            .transcript
            .owner
            .filter(|idx| self.turns.get(*idx).is_some())
        {
            return idx;
        }
        if let Some(idx) = self
            .last_closed
            .filter(|idx| self.turns.get(*idx).is_some())
        {
            self.transcript.owner = Some(idx);
            return idx;
        }
        let idx = self.new_turn();
        self.transcript.owner = Some(idx);
        idx
    }

    fn current_open_turn(&self) -> Option<usize> {
        self.current
            .filter(|idx| self.turns.get(*idx).is_some_and(|turn| !turn.closed))
    }

    fn turn_for_event(&self, value: &Value) -> Option<usize> {
        if let Some(turn_id) = value.get("turn_id").and_then(|v| v.as_str()) {
            return self
                .turns
                .iter()
                .position(|turn| turn.turn_id.as_deref() == Some(turn_id));
        }
        self.current_open_turn()
    }

    fn last_turn_for_output(&mut self) -> usize {
        if let Some(idx) = self.current_open_turn() {
            return idx;
        }
        if !self.turns.is_empty() {
            return self.turns.len() - 1;
        }
        self.new_turn()
    }

    fn new_turn(&mut self) -> usize {
        self.next_turn_number = self.next_turn_number.saturating_add(1);
        self.turns.push(TuiTurn {
            number: self.next_turn_number,
            turn_id: None,
            transcript_prefix: self.transcript.display.clone(),
            sections: Vec::new(),
            text: String::new(),
            glyphs: Vec::new(),
            outputs: Vec::new(),
            boundary: None,
            paused: false,
            closed: false,
        });
        let idx = self.turns.len() - 1;
        self.current = Some(idx);
        self.transcript.owner = Some(idx);
        idx
    }

    fn push_glyph(&mut self, idx: usize, glyph: &str) {
        if self.turns[idx]
            .glyphs
            .last()
            .is_some_and(|existing| existing == glyph)
        {
            return;
        }
        self.turns[idx].glyphs.push(glyph.to_owned());
    }

    fn push_wait(&mut self, idx: usize) {
        self.push_glyph(idx, "·");
    }

    fn push_output_glyph(&mut self, idx: usize, glyph: &str) {
        if self.turns[idx].outputs.is_empty() {
            self.turns[idx].outputs.push(SpeechOutLine {
                text: "∅".to_owned(),
                chunks: Vec::new(),
                glyphs: Vec::new(),
                cut_prefix: None,
                cut_source: None,
            });
        }
        let output = self.turns[idx].outputs.last_mut().expect("output exists");
        if output
            .glyphs
            .last()
            .is_some_and(|existing| existing == glyph)
        {
            return;
        }
        output.glyphs.push(glyph.to_owned());
    }

    fn push_vad_bar(
        &mut self,
        _idx: usize,
        probability: f64,
        smoothed: f64,
        silence: u64,
        hangover: u64,
        energy_rms: Option<f64>,
        _energy_gated: bool,
    ) {
        let raw = bar8(probability);
        let smooth = bar8(smoothed);
        let quota = if hangover == 0 {
            "0/0".to_owned()
        } else {
            format!("{}/{}", silence.min(hangover), hangover)
        };
        let energy_part = if let Some(rms) = energy_rms {
            // Fixed 0..1 scale for the RMS bar so the display is stable across
            // different threshold settings. The gate threshold itself is a
            // daemon-side policy; the TUI just shows the measured energy.
            let energy_bar = bar8(rms);
            // Lightning marker when there is appreciable acoustic energy.
            // Single-space placeholder keeps alignment because ϟ is single-width.
            let marker = if rms >= 0.1 { "ϟ" } else { " " };
            format!("{marker}energy:{energy_bar} {rms:.3}  ")
        } else {
            String::new()
        };
        self.live_vad_bar =
            format!("{energy_part}raw:{raw} {probability:.2}  smooth:{smooth} {smoothed:.2}  stop:{quota}");
    }

    fn push_fallback_bar(&mut self, progress_ms: u64, target_ms: u64) {
        let fraction = if target_ms > 0 {
            (progress_ms as f64 / target_ms as f64).clamp(0.0, 1.0)
        } else {
            0.0
        };
        let bar = bar8(fraction);
        self.live_fallback_bar = format!(
            "  ⏲ fallback {bar} {:.1}/{:.1}s",
            progress_ms as f64 / 1000.0,
            target_ms as f64 / 1000.0
        );
    }

    fn push_hold_bar(&mut self, progress_ms: u64, target_ms: u64) {
        let fraction = if target_ms > 0 {
            (progress_ms as f64 / target_ms as f64).clamp(0.0, 1.0)
        } else {
            0.0
        };
        let bar = bar8(fraction);
        self.live_hold_bar = format!(
            "  ⏸ hold    {bar} {:.1}/{:.1}s",
            progress_ms as f64 / 1000.0,
            target_ms as f64 / 1000.0
        );
    }

    fn clear_live_timers(&mut self) {
        self.live_fallback_bar.clear();
        self.live_hold_bar.clear();
    }

    fn set_turn_id(&mut self, idx: usize, value: &Value) {
        if let Some(turn_id) = value.get("turn_id").and_then(|v| v.as_str()) {
            if self.turns[idx].turn_id.is_none() {
                self.turns[idx].turn_id = Some(turn_id.to_owned());
            }
        }
    }

    fn finalize_section(&mut self, idx: usize) {
        let text = std::mem::take(&mut self.turns[idx].text);
        let glyphs = std::mem::take(&mut self.turns[idx].glyphs);
        if text.is_empty() && glyphs.is_empty() {
            return;
        }
        self.turns[idx].sections.push(TuiSection { text, glyphs });
    }

    fn ensure_boundary_for_event(&mut self, idx: usize, value: &Value) {
        let end_sample = value
            .get("end_sample")
            .and_then(|v| v.as_u64())
            .unwrap_or_default();
        let needs_new_boundary = self.turns[idx]
            .boundary
            .as_ref()
            .is_none_or(|boundary| boundary.end_sample != end_sample);
        if needs_new_boundary {
            self.turns[idx].boundary = Some(BoundaryState {
                end_sample,
                check_count: 0,
                pending_check_glyph: None,
            });
        }
    }

    fn note_event(&mut self, value: &Value, note: String) {
        let note = if let Some((ms, domain)) = self.relative_event_ms(value) {
            let domain_tag = if self.clock_baselines.len() > 1 {
                domain_label(domain)
            } else {
                ""
            };
            format!("+{ms:07.1}ms{domain_tag} {note}")
        } else {
            note
        };
        self.note(note);
    }

    fn note(&mut self, note: String) {
        if self.notes.back() == Some(&note) {
            return;
        }
        self.notes.push_back(note);
        while self.notes.len() > MAX_TUI_NOTES {
            self.notes.pop_front();
        }
    }

    /// Observe the timestamp of every received event and seed the
    /// per-domain baseline, even if the event emits no note.
    fn observe_event_clock(&mut self, value: &Value) {
        if let Some((ns, domain)) = classify_timestamp(value) {
            self.clock_baselines.entry(domain).or_insert(ns);
        }
    }

    fn relative_event_ms(&mut self, value: &Value) -> Option<(f64, ClockDomainKey)> {
        let (ns, domain) = classify_timestamp(value)?;
        let first = *self.clock_baselines.entry(domain).or_insert(ns);
        let delta_ms = ns.saturating_sub(first) as f64 / 1_000_000.0;
        Some((delta_ms, domain))
    }

    fn handle_model_chunk_processed(&mut self, value: &Value) {
        let changed = value
            .get("result_changed")
            .and_then(|v| v.as_bool())
            .unwrap_or(false)
            || value
                .get("committed_changed")
                .and_then(|v| v.as_bool())
                .unwrap_or(false)
            || value
                .get("tentative_changed")
                .and_then(|v| v.as_bool())
                .unwrap_or(false);
        if !changed {
            return;
        }
        let start = value
            .get("chunk_source_sample_start")
            .and_then(|v| v.as_u64())
            .map(|s| format_ms(samples_to_ms(s)))
            .unwrap_or_else(|| "?".to_owned());
        let count = value
            .get("chunk_sample_count")
            .and_then(|v| v.as_u64())
            .map(|s| format_ms(samples_to_ms(s)))
            .unwrap_or_else(|| "?".to_owned());
        let committed = value
            .get("committed_tokens")
            .and_then(|v| v.as_u64())
            .unwrap_or_default();
        let total = value
            .get("total_tokens")
            .and_then(|v| v.as_u64())
            .unwrap_or_default();
        self.note_event(
            value,
            format!("↱ transcriber dispatch: audio @{start}+{count} tokens={committed}/{total}"),
        );
    }
}

impl TuiTurn {
    fn render(&self) -> String {
        let mut out = format!("#{:02}", self.number);
        for section in &self.sections {
            out.push('\n');
            out.push_str(&render_section(section));
        }
        let current = TuiSection {
            text: self.text.clone(),
            glyphs: self.glyphs.clone(),
        };
        if !current.text.is_empty() || !current.glyphs.is_empty() {
            out.push('\n');
            out.push_str(&render_section(&current));
        }
        for output in &self.outputs {
            out.push('\n');
            let glyphs = if output.glyphs.is_empty() {
                String::new()
            } else {
                format!("{} ", output.glyphs.join(" "))
            };
            out.push_str("  ");
            out.push_str(&glyphs);
            out.push('"');
            out.push_str(&render_speech_out_text(output));
            out.push('"');
        }
        out
    }
}

/// Dim/grey ANSI for the spoken assistant prefix (what was actually heard / cut).
const ANSI_DIM: &str = "\u{1b}[2m";
const ANSI_RESET: &str = "\u{1b}[0m";

fn normalized_speech_out_text(output: &SpeechOutLine) -> String {
    if output.chunks.is_empty() {
        return normalized_text(&output.text);
    }
    output
        .chunks
        .iter()
        .map(|chunk| normalized_text(chunk))
        .collect::<Vec<_>>()
        .join(" ✂ ")
}

/// Render assistant line: spoken/cut prefix dim grey, unsaid remainder normal.
fn render_speech_out_text(output: &SpeechOutLine) -> String {
    let full = normalized_speech_out_text(output);
    let Some(prefix_raw) = output.cut_prefix.as_ref() else {
        return full;
    };
    let prefix = normalized_text(prefix_raw);
    if prefix.is_empty() {
        return full;
    }
    // Match prefix as word-aligned start of full intended text when possible.
    let (spoken, rest) = split_spoken_remainder(&full, &prefix);
    if rest.is_empty() {
        // Entire line was spoken (or cut covers full intended).
        return format!("{ANSI_DIM}{spoken}{ANSI_RESET}");
    }
    if spoken.is_empty() {
        return full;
    }
    // Visible interrupt marker between heard (dim) and unsaid (normal).
    format!("{ANSI_DIM}{spoken}{ANSI_RESET} ✂{rest}")
}

fn split_spoken_remainder(full: &str, spoken_prefix: &str) -> (String, String) {
    let full_n = full.trim_start();
    let spoken_n = spoken_prefix.trim();
    if spoken_n.is_empty() {
        return (String::new(), full_n.to_owned());
    }
    // Exact prefix match (case-sensitive after normalize).
    if full_n.starts_with(spoken_n) {
        let rest = full_n[spoken_n.len()..].to_owned();
        return (spoken_n.to_owned(), rest);
    }
    // Case-insensitive word-prefix match against intended words.
    let full_words: Vec<&str> = full_n.split_whitespace().collect();
    let spoken_words: Vec<&str> = spoken_n.split_whitespace().collect();
    if spoken_words.is_empty() || full_words.is_empty() {
        return (spoken_n.to_owned(), format!(" {full_n}"));
    }
    let mut matched = 0usize;
    for (i, sw) in spoken_words.iter().enumerate() {
        if i >= full_words.len() {
            break;
        }
        if full_words[i].eq_ignore_ascii_case(sw) {
            matched += 1;
        } else {
            break;
        }
    }
    if matched == 0 {
        // Fallback: dim the ASR cut text and show full intended after a marker.
        return (spoken_n.to_owned(), format!(" | {full_n}"));
    }
    let spoken = full_words[..matched].join(" ");
    let rest = if matched < full_words.len() {
        format!(" {}", full_words[matched..].join(" "))
    } else {
        String::new()
    };
    (spoken, rest)
}

fn render_section(section: &TuiSection) -> String {
    let mut rendered_glyphs = section.glyphs.clone();
    if rendered_glyphs.first().is_none_or(|glyph| glyph != "◖") {
        rendered_glyphs.insert(0, "◖".to_owned());
    }
    let mut glyphs = rendered_glyphs.iter();
    let first = glyphs.next().map(String::as_str).unwrap_or("◖");
    let rest = glyphs.cloned().collect::<Vec<_>>().join(" ");
    let text = if section.text.trim().is_empty() {
        "∅".to_owned()
    } else {
        normalized_text(&section.text)
    };
    let line = if rest.is_empty() {
        format!("  {first} \"{text}\"")
    } else {
        format!("  {first} \"{text}\" {rest}")
    };
    line
}

fn write_stdout_text(text: &str) -> Result<()> {
    let stdout = io::stdout();
    let mut handle = stdout.lock();
    if let Err(err) = handle.write_all(text.as_bytes()) {
        if err.kind() == io::ErrorKind::BrokenPipe {
            std::process::exit(0);
        }
        return Err(err).context("writing to stdout");
    }
    if let Err(err) = handle.flush() {
        if err.kind() == io::ErrorKind::BrokenPipe {
            std::process::exit(0);
        }
        return Err(err).context("flushing stdout");
    }
    Ok(())
}

fn write_stdout_line(text: &str) -> Result<()> {
    write_stdout_text(&format!("{text}\n"))
}

#[tokio::main]
async fn main() -> Result<()> {
    let args = Args::parse();
    if let Some(path) = &args.replay_events {
        return replay_events(&args, path);
    }
    if args.stdin_events {
        let stdin = io::stdin();
        let reader = stdin.lock();
        return process_event_lines(&args, reader, true);
    }

    // ── Ready file: remove stale from previous run ────────────────────
    if let Some(ref ready_path) = args.ready_file {
        let _ = std::fs::remove_file(ready_path);
    }

    let (mut ws, _) = connect_async(&args.url)
        .await
        .with_context(|| format!("connecting to {}", args.url))?;

    let subscribe = ControlMessage::SubscribeEvents {
        stream_id: args.stream_id.clone(),
        stream_session_id: args.stream_session_id.clone(),
        event: None,
    };
    ws.send(Message::Text(serde_json::to_string(&subscribe)?))
        .await?;

    // ── Atomically signal readiness: create/write only after connect + subscribe ──
    if let Some(ref ready_path) = args.ready_file {
        std::fs::write(ready_path, "ready\n")
            .with_context(|| format!("writing ready file {}", ready_path.display()))?;
    }

    let mut transcript_state = TranscriptState::default();
    let mut tui_model = TuiModel::new(args.speech_out_ui);

    while let Some(msg) = ws.next().await {
        match msg? {
            Message::Text(text) => {
                handle_text_event(&args, &text, &mut transcript_state, &mut tui_model, true)?
            }
            Message::Close(_) => break,
            _ => {}
        }
    }

    if args.mode == Mode::Transcript && args.verbose && !transcript_state.final_text.is_empty() {
        diagnostic_line(
            &mut transcript_state.printed_transcript,
            format_args!("final transcript: {}", transcript_state.final_text),
        )?;
    }

    Ok(())
}

fn replay_events(args: &Args, path: &PathBuf) -> Result<()> {
    let file = File::open(path).with_context(|| format!("opening {}", path.display()))?;
    let reader = BufReader::new(file);
    process_event_lines(args, reader, false)
}

fn process_event_lines<R: BufRead>(args: &Args, reader: R, live_render: bool) -> Result<()> {
    let mut transcript_state = TranscriptState::default();
    let mut tui_model = TuiModel::new(args.speech_out_ui);
    for line in reader.lines() {
        let line = line?;
        handle_text_event(
            args,
            &line,
            &mut transcript_state,
            &mut tui_model,
            live_render,
        )?;
    }
    if args.mode.is_tui() && !live_render {
        write_stdout_text(&tui_model.render(args.mode.is_debug()))?;
    }
    Ok(())
}

fn handle_text_event(
    args: &Args,
    text: &str,
    transcript_state: &mut TranscriptState,
    tui_model: &mut TuiModel,
    live_render: bool,
) -> Result<()> {
    match args.mode {
        Mode::Jsonl => write_stdout_line(text)?,
        Mode::Inject => {
            let value: Value = match serde_json::from_str(text) {
                Ok(value) => value,
                Err(_) => return Ok(()),
            };
            handle_inject_value(&value, args, transcript_state)?;
        }
        Mode::Transcript => {
            let value: Value = match serde_json::from_str(text) {
                Ok(value) => value,
                Err(_) => return Ok(()),
            };
            handle_transcript_value(&value, args, transcript_state)?;
        }
        Mode::Tui | Mode::Debug => {
            let value: Value = match serde_json::from_str(text) {
                Ok(value) => value,
                Err(_) => return Ok(()),
            };
            tui_model.handle(&value);
            if live_render {
                write_stdout_text(&format!(
                    "\x1b[2J\x1b[H{}",
                    tui_model.render(args.mode.is_debug())
                ))?;
            }
        }
    }
    Ok(())
}

fn handle_inject_value(value: &Value, args: &Args, state: &mut TranscriptState) -> Result<()> {
    let event = event_name(value);
    match event {
        "transcript_token_committed" => {
            let token = value.get("text").and_then(|v| v.as_str()).unwrap_or("");
            if !token.is_empty() {
                inject_text(&args.inject_command, token)?;
                state.printed_transcript = true;
            }
        }
        "transcript_update" => {
            let committed = value
                .get("committed_text")
                .and_then(|v| v.as_str())
                .unwrap_or("");
            state.final_text = committed.to_owned();
            state.last_text = committed.to_owned();
        }
        _ => {}
    }
    Ok(())
}

fn inject_text(command: &str, text: &str) -> Result<()> {
    let status = Command::new(command)
        .arg(text)
        .status()
        .with_context(|| format!("running inject command {command}"))?;
    if !status.success() {
        anyhow::bail!("inject command {command} exited with {status}");
    }
    Ok(())
}

fn handle_transcript_value(value: &Value, args: &Args, state: &mut TranscriptState) -> Result<()> {
    let event = event_name(value);
    match event {
        "runtime_provenance" if args.verbose => {
            let runtime_id = value
                .get("runtime_id")
                .and_then(|v| v.as_str())
                .unwrap_or("unknown");
            let build_id = value
                .get("build_id")
                .and_then(|v| v.as_str())
                .unwrap_or("unknown");
            let config_id = value
                .get("config_id")
                .and_then(|v| v.as_str())
                .unwrap_or("unknown");
            diagnostic_line(
                &mut state.printed_transcript,
                format_args!("runtime {runtime_id} build={build_id} config={config_id}"),
            )?;
        }
        "transcript_update" => {
            let committed = value
                .get("committed_text")
                .and_then(|v| v.as_str())
                .unwrap_or("");
            let tentative = value
                .get("tentative_text")
                .and_then(|v| v.as_str())
                .unwrap_or("");
            let display = if tentative.is_empty() {
                committed.to_owned()
            } else {
                format!("{committed}{tentative}")
            };
            if display != state.last_text {
                if !display.is_empty() {
                    if let Some(delta) = display.strip_prefix(&state.last_text) {
                        if !delta.is_empty() {
                            print!("{delta}");
                            io::stdout().flush()?;
                            state.printed_transcript = true;
                        }
                    } else {
                        if state.printed_transcript {
                            println!();
                        }
                        print!("{display}");
                        io::stdout().flush()?;
                        state.printed_transcript = true;
                    }
                }
                state.last_text = display;
            }
            state.final_text = committed.to_owned();
        }
        "transcript_token_committed" if args.verbose && args.trace_tokens => {
            let token = value.get("text").and_then(|v| v.as_str()).unwrap_or("");
            let t0_ms = value
                .get("t0_ms")
                .and_then(|v| v.as_i64())
                .unwrap_or_default();
            let t1_ms = value
                .get("t1_ms")
                .and_then(|v| v.as_i64())
                .unwrap_or_default();
            let input_ms = value
                .get("input_received_ms")
                .and_then(|v| v.as_i64())
                .unwrap_or_default();
            let committed_ms = value
                .get("audio_committed_ms")
                .and_then(|v| v.as_i64())
                .unwrap_or_default();
            let probability = value
                .get("probability")
                .and_then(|v| v.as_f64())
                .unwrap_or_default();
            diagnostic_line(
                &mut state.printed_transcript,
                format_args!(
                    "asr  token t={t0_ms}-{t1_ms}ms committed={committed_ms}ms input={input_ms}ms p={probability:.2} text=\"{}\"",
                    token.escape_debug(),
                ),
            )?;
        }
        "model_chunk_processed" if args.verbose && args.trace_asr => {
            let input_ms = value
                .get("input_received_ms")
                .and_then(|v| v.as_i64())
                .unwrap_or_default();
            let committed_ms = value
                .get("audio_committed_ms")
                .and_then(|v| v.as_i64())
                .unwrap_or_default();
            let buffered_ms = value
                .get("buffered_ms")
                .and_then(|v| v.as_i64())
                .unwrap_or_default();
            let is_final = value
                .get("is_final")
                .and_then(|v| v.as_bool())
                .unwrap_or(false);
            diagnostic_line(
                &mut state.printed_transcript,
                format_args!(
                    "asr  chunk input={input_ms}ms committed={committed_ms}ms buffered={buffered_ms}ms final={is_final}"
                ),
            )?;
        }
        "vad_speech_start" if args.verbose => {
            diagnostic_line(
                &mut state.printed_transcript,
                format_args!(
                    "vad  start speech_start={} decision={} detect_delay={} p={:.3}",
                    sample_field_ms(value, "start_sample"),
                    sample_field_ms(value, "decision_sample"),
                    samples_to_ms(
                        value
                            .get("decision_sample")
                            .and_then(|v| v.as_u64())
                            .unwrap_or_default()
                            .saturating_sub(
                                value
                                    .get("start_sample")
                                    .and_then(|v| v.as_u64())
                                    .unwrap_or_default()
                            )
                    ),
                    value
                        .get("confidence")
                        .and_then(|v| v.as_f64())
                        .unwrap_or_default(),
                ),
            )?;
        }
        "vad_state" if args.verbose => {
            let sample_start = value
                .get("sample_start")
                .and_then(|v| v.as_u64())
                .unwrap_or_default();
            let sample_count = value
                .get("sample_count")
                .and_then(|v| v.as_u64())
                .unwrap_or_default();
            let probability = value
                .get("probability")
                .and_then(|v| v.as_f64())
                .unwrap_or_default();
            let threshold = value
                .get("threshold")
                .and_then(|v| v.as_f64())
                .unwrap_or_default();
            let raw_is_speech = value
                .get("raw_is_speech")
                .and_then(|v| v.as_bool())
                .unwrap_or(false);
            let smoothed_in_speech = value
                .get("smoothed_in_speech")
                .and_then(|v| v.as_bool())
                .unwrap_or(false);
            let silence_counter = value
                .get("silence_counter")
                .and_then(|v| v.as_u64())
                .unwrap_or_default();
            let hangover_frames = value
                .get("hangover_frames")
                .and_then(|v| v.as_u64())
                .unwrap_or_default();
            diagnostic_line(
                &mut state.printed_transcript,
                format_args!(
                    "vad  state t={}..{} p={probability:.3}/{threshold:.2} raw={} state={} silence={silence_counter}/{hangover_frames}",
                    samples_to_ms(sample_start),
                    samples_to_ms(sample_start.saturating_add(sample_count)),
                    if raw_is_speech { "speech" } else { "silence" },
                    if smoothed_in_speech { "in-speech" } else { "idle" },
                ),
            )?;
        }
        "vad_frame" if args.verbose && args.trace_vad => {
            let sample_start = value
                .get("sample_start")
                .and_then(|v| v.as_u64())
                .unwrap_or_default();
            let sample_count = value
                .get("sample_count")
                .and_then(|v| v.as_u64())
                .unwrap_or_default();
            let probability = value
                .get("probability")
                .and_then(|v| v.as_f64())
                .unwrap_or_default();
            let threshold = value
                .get("threshold")
                .and_then(|v| v.as_f64())
                .unwrap_or_default();
            let raw_is_speech = value
                .get("raw_is_speech")
                .and_then(|v| v.as_bool())
                .unwrap_or(false);
            let smoothed_in_speech = value
                .get("smoothed_in_speech")
                .and_then(|v| v.as_bool())
                .unwrap_or(false);
            diagnostic_line(
                &mut state.printed_transcript,
                format_args!(
                    "vad  frame t={}..{} p={probability:.3}/{threshold:.2} raw={} state={}",
                    samples_to_ms(sample_start),
                    samples_to_ms(sample_start.saturating_add(sample_count)),
                    if raw_is_speech { "speech" } else { "silence" },
                    if smoothed_in_speech {
                        "in-speech"
                    } else {
                        "idle"
                    },
                ),
            )?;
        }
        "vad_speech_end" if args.verbose => {
            let start_sample = value
                .get("start_sample")
                .and_then(|v| v.as_u64())
                .unwrap_or_default();
            let end_sample = value
                .get("end_sample")
                .and_then(|v| v.as_u64())
                .unwrap_or_default();
            let decision_sample = value
                .get("decision_sample")
                .and_then(|v| v.as_u64())
                .unwrap_or_default();
            state.last_vad_end = Some(VadEndObservation { end_sample });
            diagnostic_line(
                &mut state.printed_transcript,
                format_args!(
                    "──── vad stop  speech={}  silence={}  end={}  decision={}",
                    samples_to_ms(end_sample.saturating_sub(start_sample)),
                    samples_to_ms(decision_sample.saturating_sub(end_sample)),
                    samples_to_ms(end_sample),
                    samples_to_ms(decision_sample),
                ),
            )?;
        }
        "smart_turn_session_start" if args.verbose => {
            let threshold = value
                .get("threshold")
                .and_then(|v| v.as_f64())
                .unwrap_or_default();
            let timeout_ms = value
                .get("timeout_ms")
                .and_then(|v| v.as_u64())
                .unwrap_or_default();
            let model_path = value
                .get("model_path")
                .and_then(|v| v.as_str())
                .unwrap_or("unknown");
            diagnostic_line(
                &mut state.printed_transcript,
                format_args!(
                    "smart-turn ready  threshold={threshold:.2}  budget={timeout_ms}ms  model={model_path}"
                ),
            )?;
        }
        "smart_turn_candidate" if args.verbose && args.trace_asr => {
            diagnostic_line(
                &mut state.printed_transcript,
                format_args!(
                    "st   candidate end={} decision={} audio={}",
                    sample_field_ms(value, "end_sample"),
                    sample_field_ms(value, "decision_sample"),
                    samples_to_ms(
                        value
                            .get("audio_samples")
                            .and_then(|v| v.as_u64())
                            .unwrap_or_default()
                    ),
                ),
            )?;
        }
        "smart_turn_decision" | "smart_turn_timeout" if args.verbose => {
            let observation = SmartTurnObservation::from_value(value);
            let attempt = state
                .smart_turn_attempts_by_end
                .entry(observation.end_sample)
                .and_modify(|count| *count = count.saturating_add(1))
                .or_insert(1);
            let label = if observation.complete {
                "complete"
            } else {
                "hold"
            };
            let verdict = if observation.complete {
                "close"
            } else {
                "keep-open"
            };
            diagnostic_line(
                &mut state.printed_transcript,
                format_args!(
                    "     st#{attempt:<2} {label:<8} p={:.3}/{:.2}  {verdict:<9} cost={:.0}ms  feat={:.0} onnx={:.0}{}  decision={}",
                    observation.probability,
                    observation.threshold,
                    observation.inference_duration_ms,
                    observation.feature_duration_ms,
                    observation.model_duration_ms,
                    if observation.over_budget { "  OVER-BUDGET" } else { "" },
                    samples_to_ms(observation.decision_sample),
                ),
            )?;
            state.last_smart_turn = Some(observation);
        }
        "turn_eou_suppressed" if args.verbose => {
            let source = value
                .get("source")
                .and_then(|v| v.as_str())
                .unwrap_or("unknown");
            let detector = value
                .get("detector")
                .and_then(|v| v.as_str())
                .unwrap_or("unknown");
            let reason = value
                .get("reason")
                .and_then(|v| v.as_str())
                .unwrap_or("unknown");
            diagnostic_line(
                &mut state.printed_transcript,
                format_args!(
                    "     no eou   source={source} detector={detector} reason={reason} end={}",
                    sample_field_ms(value, "end_sample"),
                ),
            )?;
        }
        "turn_closed" => {
            let source = value
                .get("source")
                .and_then(|v| v.as_str())
                .unwrap_or("unknown");
            if matches!(
                source,
                "vad" | "model" | "smart_turn" | "vad_acoustic_fallback"
            ) {
                if state.printed_transcript {
                    println!();
                }
                println!("<EOU>");
                io::stdout().flush()?;
                state.printed_transcript = false;
            }
            if args.verbose {
                let degraded = value
                    .get("degraded")
                    .and_then(|v| v.as_bool())
                    .unwrap_or(false);
                let end_sample = value
                    .get("end_sample")
                    .and_then(|v| v.as_u64())
                    .unwrap_or_default();
                let decision_sample = value
                    .get("decision_sample")
                    .and_then(|v| v.as_u64())
                    .unwrap_or_default();
                let detector = value
                    .get("detector")
                    .and_then(|v| v.as_str())
                    .unwrap_or("unknown");
                let reason = value
                    .get("reason")
                    .and_then(|v| v.as_str())
                    .unwrap_or("unknown");
                let silence_ms = state
                    .last_vad_end
                    .filter(|vad| vad.end_sample == end_sample && decision_sample >= vad.end_sample)
                    .map(|vad| samples_to_ms(decision_sample - vad.end_sample));
                let smart = state.last_smart_turn.filter(|smart| {
                    smart.end_sample == end_sample && smart.decision_sample == decision_sample
                });
                if let Some(smart) = smart {
                    diagnostic_line(
                        &mut state.printed_transcript,
                        format_args!(
                            "     EOU     source={source} reason={reason} degraded={degraded} detector={detector} p={:.3} complete={} over_budget={} cost={:.0}ms feat={:.0} onnx={:.0} silence={}",
                            smart.probability,
                            smart.complete,
                            smart.over_budget,
                            smart.inference_duration_ms,
                            smart.feature_duration_ms,
                            smart.model_duration_ms,
                            format_ms(silence_ms.unwrap_or_default()),
                        ),
                    )?;
                } else {
                    diagnostic_line(
                        &mut state.printed_transcript,
                        format_args!(
                            "     EOU     source={source} reason={reason} degraded={degraded} detector={detector} end={} decision={} silence={}",
                            samples_to_ms(end_sample),
                            samples_to_ms(decision_sample),
                            format_ms(silence_ms.unwrap_or_default()),
                        ),
                    )?;
                }
            }
        }
        "runtime_provenance" => {}
        "model_error" => diagnostic_line(
            &mut state.printed_transcript,
            format_args!("model error: {value}"),
        )?,
        "audio_gap" | "audio_sample_gap" => {
            diagnostic_line(&mut state.printed_transcript, format_args!("gap: {value}"))?
        }
        _ => {}
    }
    Ok(())
}

fn diagnostic_line(printed_transcript: &mut bool, args: fmt::Arguments<'_>) -> Result<()> {
    if *printed_transcript {
        println!();
        *printed_transcript = false;
    }
    println!("[diag] {args}");
    io::stdout().flush()?;
    Ok(())
}

fn event_name(value: &Value) -> &str {
    value
        .get("event")
        .or_else(|| value.get("type"))
        .and_then(|v| v.as_str())
        .unwrap_or("")
}

fn sample_field_ms(value: &Value, field: &str) -> String {
    format_ms(samples_to_ms(
        value
            .get(field)
            .and_then(|v| v.as_u64())
            .unwrap_or_default(),
    ))
}

fn samples_to_ms(sample: u64) -> u64 {
    sample.saturating_mul(1_000) / SAMPLE_RATE
}

/// Classify an event's timestamp field into a clock domain.
///
/// Field-name heuristics:
/// - `client_mono_ns` → Client
/// - `daemon_mono_ns`, `ingress_receive_mono_ns`, `detector_end_mono_ns`,
///   `model_feed_end_mono_ns` → Daemon
/// - `diagnostic_mono_ns` → Harness (elapsed-since-harness-start diagnostic
///   clock; `diagnostic_clock_origin` does NOT reclassify this domain).
fn classify_timestamp(value: &Value) -> Option<(u64, ClockDomainKey)> {
    // Field-name heuristics, respecting priority order.
    if let Some(ns) = value.get("client_mono_ns").and_then(|v| v.as_u64()) {
        return Some((ns, ClockDomainKey::Client));
    }
    if let Some(ns) = value.get("daemon_mono_ns").and_then(|v| v.as_u64()) {
        return Some((ns, ClockDomainKey::Daemon));
    }
    if let Some(ns) = value
        .get("ingress_receive_mono_ns")
        .and_then(|v| v.as_u64())
    {
        return Some((ns, ClockDomainKey::Daemon));
    }
    if let Some(ns) = value.get("detector_end_mono_ns").and_then(|v| v.as_u64()) {
        return Some((ns, ClockDomainKey::Daemon));
    }
    if let Some(ns) = value.get("model_feed_end_mono_ns").and_then(|v| v.as_u64()) {
        return Some((ns, ClockDomainKey::Daemon));
    }
    if let Some(ns) = value.get("diagnostic_mono_ns").and_then(|v| v.as_u64()) {
        return Some((ns, ClockDomainKey::Harness));
    }
    None
}

fn format_ms(ms: u64) -> String {
    format!("{ms}ms")
}

fn domain_label(domain: ClockDomainKey) -> &'static str {
    match domain {
        ClockDomainKey::Harness => " h",
        ClockDomainKey::Client => " c",
        ClockDomainKey::Daemon => " d",
    }
}

fn circled(index: usize) -> String {
    match index {
        1 => '①',
        2 => '②',
        3 => '③',
        4 => '④',
        5 => '⑤',
        6 => '⑥',
        7 => '⑦',
        8 => '⑧',
        9 => '⑨',
        10 => '⑩',
        11 => '⑪',
        12 => '⑫',
        13 => '⑬',
        14 => '⑭',
        15 => '⑮',
        16 => '⑯',
        17 => '⑰',
        18 => '⑱',
        19 => '⑲',
        20 => '⑳',
        n => return format!("({n})"),
    }
    .to_string()
}

fn compact_probability(probability: f64) -> String {
    let probability = probability.clamp(0.0, 1.0);
    let rendered = format!("{probability:.2}");
    rendered.strip_prefix('0').unwrap_or(&rendered).to_owned()
}

fn normalized_text(text: &str) -> String {
    text.split_whitespace().collect::<Vec<_>>().join(" ")
}

/// True when a transcript delta contains only punctuation and whitespace
/// (no alphanumeric characters). Such deltas are late punctuation revisions
/// from a prior closed turn and must not render as the current turn's text.
fn is_punctuation_only_delta(text: &str) -> bool {
    !text.chars().any(|c| c.is_alphanumeric())
}

fn bar8(value: f64) -> String {
    let filled = ((value.clamp(0.0, 1.0) * 8.0).round() as usize).clamp(0, 8);
    format!("{}{}", "█".repeat(filled), "░".repeat(8 - filled))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn apply(model: &mut TuiModel, event: Value) {
        model.handle(&event);
    }

    #[test]
    fn tui_renders_probe_chain_accepting_on_third_check() {
        let mut model = TuiModel::default();
        apply(
            &mut model,
            json!({"event":"vad_speech_start","stream_session_id":"s","start_sample":0,"decision_sample":320}),
        );
        apply(
            &mut model,
            json!({"event":"transcript_update","stream_session_id":"s","committed_text":"hello there","tentative_text":""}),
        );
        apply(
            &mut model,
            json!({"event":"vad_speech_end","stream_session_id":"s","end_sample":16000,"decision_sample":21760}),
        );
        for (i, p) in [(0, 0.31), (1, 0.45), (2, 0.79)] {
            apply(
                &mut model,
                json!({"event":"smart_turn_candidate","stream_session_id":"s","end_sample":16000,"decision_sample":21760 + i * 4000}),
            );
            apply(
                &mut model,
                json!({"event":"smart_turn_decision","stream_session_id":"s","end_sample":16000,"decision_sample":21760 + i * 4000,"probability":p,"threshold":0.5,"complete":p > 0.5}),
            );
            if p <= 0.5 {
                apply(
                    &mut model,
                    json!({"event":"smart_turn_recheck_scheduled","stream_session_id":"s","pending_decision_samples":[1,2]}),
                );
            }
        }
        apply(
            &mut model,
            json!({"event":"turn_closed","stream_session_id":"s","source":"smart_turn"}),
        );
        let rendered = model.render(false);
        assert!(rendered.contains("#01\n  ◖ \"hello there\" ◗ ①.31 · ②.45 · ③.79 ◆"));
    }

    #[test]
    fn tui_renders_resumed_speech_as_continuation() {
        let mut model = TuiModel::default();
        apply(
            &mut model,
            json!({"event":"vad_speech_start","stream_session_id":"s","start_sample":0,"decision_sample":320}),
        );
        apply(
            &mut model,
            json!({"event":"transcript_update","stream_session_id":"s","committed_text":"not done","tentative_text":""}),
        );
        apply(
            &mut model,
            json!({"event":"vad_speech_end","stream_session_id":"s","end_sample":16000,"decision_sample":21760}),
        );
        apply(
            &mut model,
            json!({"event":"smart_turn_candidate","stream_session_id":"s"}),
        );
        apply(
            &mut model,
            json!({"event":"smart_turn_decision","stream_session_id":"s","probability":0.12,"complete":false}),
        );
        apply(
            &mut model,
            json!({"event":"smart_turn_recheck_scheduled","stream_session_id":"s","pending_decision_samples":[1,2,3]}),
        );
        apply(
            &mut model,
            json!({"event":"smart_turn_recheck_cancelled","stream_session_id":"s","reason":"speech_resumed"}),
        );
        let rendered = model.render(false);
        assert!(rendered.contains("#01\n  ◖ \"not done\" ◗ ①.12 · ↺"));
    }

    #[test]
    fn tui_renders_exhausted_probe_chain() {
        let mut model = TuiModel::default();
        apply(
            &mut model,
            json!({"event":"vad_speech_start","stream_session_id":"s","start_sample":0,"decision_sample":320}),
        );
        apply(
            &mut model,
            json!({"event":"transcript_update","stream_session_id":"s","committed_text":"maybe still going","tentative_text":""}),
        );
        apply(
            &mut model,
            json!({"event":"vad_speech_end","stream_session_id":"s","end_sample":16000,"decision_sample":21760}),
        );
        for p in [0.10, 0.20, 0.30, 0.40] {
            apply(
                &mut model,
                json!({"event":"smart_turn_candidate","stream_session_id":"s"}),
            );
            apply(
                &mut model,
                json!({"event":"smart_turn_decision","stream_session_id":"s","probability":p,"complete":false}),
            );
        }
        apply(
            &mut model,
            json!({"event":"smart_turn_recheck_exhausted","stream_session_id":"s"}),
        );
        let rendered = model.render(false);
        assert!(rendered.contains("①.10 ②.20 ③.30 ④.40 ◇"));
    }

    #[test]
    fn closed_turn_rejects_late_punctuation_revision() {
        // Regression: closed visible turns are immutable. Late transcript_update
        // must not rewrite the closed turn's text or create ghost turns.
        // Modeled on session 37c36a9d.
        let mut model = TuiModel::default();
        apply(
            &mut model,
            json!({"event":"vad_speech_start","stream_session_id":"s","start_sample":0,"decision_sample":320}),
        );
        apply(
            &mut model,
            json!({"event":"transcript_update","stream_session_id":"s","revision":1,"committed_text":"hello there","tentative_text":""}),
        );
        apply(
            &mut model,
            json!({"event":"turn_closed","stream_session_id":"s","source":"smart_turn","turn_id":"s:turn:0"}),
        );
        // Late punctuation arrives after close — must not rewrite the closed turn.
        apply(
            &mut model,
            json!({"event":"transcript_update","stream_session_id":"s","revision":2,"committed_text":"hello there.","tentative_text":""}),
        );

        assert_eq!(
            model.turns.len(),
            1,
            "late punctuation must not create a ghost turn"
        );
        // Closed turn text stays frozen at rev-1 text.
        assert_eq!(model.turns[0].text, "hello there");
        assert!(model.turns[0].closed);
        let rendered = model.render(false);
        assert!(
            rendered.contains("#01\n  ◖ \"hello there\" ◆"),
            "{rendered}"
        );
    }

    #[test]
    fn tui_replaces_non_prefix_transcript_revision_instead_of_appending() {
        let mut model = TuiModel::default();
        apply(
            &mut model,
            json!({"event":"vad_speech_start","stream_session_id":"s"}),
        );
        apply(
            &mut model,
            json!({"event":"transcript_update","stream_session_id":"s","revision":1,"committed_text":"i went there","tentative_text":""}),
        );
        apply(
            &mut model,
            json!({"event":"transcript_update","stream_session_id":"s","revision":2,"committed_text":"I went there","tentative_text":""}),
        );

        assert_eq!(model.turns.len(), 1);
        assert_eq!(model.turns[0].text, "I went there");
        assert!(!model.render(false).contains("i went thereI went there"));
    }

    #[test]
    fn disconnect_final_flush_keeps_closed_turn_immutable() {
        // Regression: a final transcript flush on disconnect must not rewrite
        // the already-closed turn text.
        // Modeled on session f62ee314.
        let mut model = TuiModel::default();
        apply(
            &mut model,
            json!({"event":"vad_speech_start","stream_session_id":"s"}),
        );
        apply(
            &mut model,
            json!({"event":"transcript_update","stream_session_id":"s","revision":1,"committed_text":"almost final","tentative_text":""}),
        );
        apply(
            &mut model,
            json!({"event":"turn_closed","stream_session_id":"s","source":"vad","turn_id":"s:turn:0"}),
        );
        // Disconnect final flush — must not rewrite the closed turn.
        apply(
            &mut model,
            json!({"event":"transcript_update","stream_session_id":"s","revision":2,"committed_text":"almost final now","tentative_text":""}),
        );

        assert_eq!(model.turns.len(), 1);
        // Closed turn is immutable; text stays at rev-1.
        assert_eq!(model.turns[0].text, "almost final");
        assert!(model.turns[0].closed);
    }

    #[test]
    fn punctuation_between_turns_routes_to_open_turn_only() {
        // Regression: when a new turn opens, late punctuation for a prior
        // closed turn must not rewrite it; the new turn must only get its own
        // transcript delta.
        // Modeled on session acc49911.
        let mut model = TuiModel::default();
        apply(
            &mut model,
            json!({"event":"vad_speech_start","stream_session_id":"s"}),
        );
        apply(
            &mut model,
            json!({"event":"transcript_update","stream_session_id":"s","revision":1,"committed_text":"first turn","tentative_text":""}),
        );
        apply(
            &mut model,
            json!({"event":"turn_closed","stream_session_id":"s","source":"smart_turn","turn_id":"s:turn:0"}),
        );
        // Late punctuation for the closed turn — must be ignored.
        apply(
            &mut model,
            json!({"event":"transcript_update","stream_session_id":"s","revision":2,"committed_text":"first turn.","tentative_text":""}),
        );
        // New speech starts a fresh turn.
        apply(
            &mut model,
            json!({"event":"vad_speech_start","stream_session_id":"s"}),
        );
        apply(
            &mut model,
            json!({"event":"transcript_update","stream_session_id":"s","revision":3,"committed_text":"first turn. second turn","tentative_text":""}),
        );

        assert_eq!(model.turns.len(), 2);
        // Closed turn stays frozen at its last-open revision.
        assert_eq!(model.turns[0].text, "first turn");
        // New turn gets only the delta after its transcript prefix.
        assert_eq!(model.turns[1].text, " second turn");
        let rendered = model.render(false);
        assert!(rendered.contains("#01\n  ◖ \"first turn\" ◆"), "{rendered}");
        assert!(rendered.contains("#02\n  ◖ \"second turn\""), "{rendered}");
    }

    #[test]
    fn tui_clears_hold_and_fallback_bars_on_close_and_session_reset() {
        let mut model = TuiModel::default();
        apply(
            &mut model,
            json!({"event":"vad_speech_start","stream_session_id":"s"}),
        );
        apply(
            &mut model,
            json!({"event":"turn_started","stream_session_id":"s","turn_id":"s:turn:0","source":"vad","start_sample":0}),
        );
        apply(
            &mut model,
            json!({"event":"vad_frame","stream_session_id":"s","probability":0.9,"smoothed_probability":0.8,"silence_counter":1,"hangover_frames":5,"fallback_progress_ms":1200,"fallback_target_ms":3500}),
        );
        apply(
            &mut model,
            json!({"event":"turn_hold","stream_session_id":"s","turn_id":"s:turn:0","hold_progress_ms":2200,"hold_target_ms":7000}),
        );
        assert!(!model.live_fallback_bar.is_empty());
        assert!(!model.live_hold_bar.is_empty());

        apply(
            &mut model,
            json!({"event":"turn_closed","stream_session_id":"s","source":"vad","turn_id":"s:turn:0"}),
        );
        assert!(model.live_fallback_bar.is_empty());
        assert!(model.live_hold_bar.is_empty());
        let rendered_after_close = model.render(false);
        assert!(
            !rendered_after_close.contains("⏲ fallback"),
            "{rendered_after_close}"
        );
        assert!(
            !rendered_after_close.contains("⏸ hold"),
            "{rendered_after_close}"
        );

        apply(
            &mut model,
            json!({"event":"vad_frame","stream_session_id":"s","probability":0.2,"smoothed_probability":0.2,"silence_counter":0,"hangover_frames":5,"fallback_progress_ms":700,"fallback_target_ms":3500}),
        );
        apply(
            &mut model,
            json!({"event":"turn_hold","stream_session_id":"s","turn_id":"s:turn:0","hold_progress_ms":800,"hold_target_ms":7000}),
        );
        apply(
            &mut model,
            json!({"event":"turn_session_start","stream_session_id":"s"}),
        );
        assert!(model.live_fallback_bar.is_empty());
        assert!(model.live_hold_bar.is_empty());
    }

    // ── ready-file argument parsing and lifecycle ────────────────────

    #[test]
    fn ready_file_arg_accepted_by_clap() {
        let args = Args::try_parse_from([
            "watch",
            "--mode",
            "jsonl",
            "--ready-file",
            "/tmp/test-ready",
        ]);
        assert!(args.is_ok(), "--ready-file should be accepted");
        let args = args.unwrap();
        assert_eq!(args.ready_file, Some(PathBuf::from("/tmp/test-ready")));
    }

    #[test]
    fn ready_file_not_provided_by_default() {
        let args = Args::try_parse_from(["watch", "--mode", "jsonl"]);
        assert!(args.is_ok());
        assert_eq!(args.unwrap().ready_file, None);
    }

    #[test]
    fn ready_file_write_is_after_subscribe_in_websocket_path() {
        // Verify code order: the ready file write appears after
        // ws.send(SubscribeEvents) in the main() function.
        // Use anchor comments to avoid matching test code.
        let source = include_str!("main.rs");
        let send_marker = "ws.send(Message::Text(serde_json::to_string(&subscribe)?))";
        let write_marker = "// ── Atomically signal readiness";
        let send_pos = source
            .find(send_marker)
            .expect("subscribe send must exist in source");
        let write_pos = source
            .find(write_marker)
            .expect("ready write comment must exist in source");
        assert!(
            write_pos > send_pos,
            "ready file write must occur AFTER subscribe send in source order. \
             write at {write_pos}, send at {send_pos}"
        );
    }

    #[test]
    fn ready_file_removed_before_connect_in_websocket_path() {
        // Verify code order: stale removal (remove_file) appears before connect_async.
        // We use anchor comments from the actual main() function to avoid matching
        // test code (include_str! includes the whole file).
        let source = include_str!("main.rs");
        let remove_marker = "// ── Ready file: remove stale from previous run";
        let connect_marker = "connect_async(&args.url)";
        let remove_pos = source
            .find(remove_marker)
            .expect("stale removal comment must exist in source");
        let connect_pos = source
            .find(connect_marker)
            .expect("connect_async must exist in source");
        assert!(
            remove_pos < connect_pos,
            "stale ready file removal must occur BEFORE connect_async. \
             remove at {remove_pos}, connect at {connect_pos}"
        );
    }

    #[test]
    fn speech_out_replay_renders_output_only_cancel_terminal() {
        let mut model = TuiModel::new(true);
        apply(
            &mut model,
            json!({"event":"speech_out_request_queued","utterance_id":"u","diagnostic_mono_ns":1_000_000_000_u64,"text":"hello output diagnostics"}),
        );
        apply(
            &mut model,
            json!({"event":"speech_out_request_received","utterance_id":"u","diagnostic_mono_ns":1_004_000_000_u64,"text":"hello output diagnostics","num_chunks":1}),
        );
        apply(
            &mut model,
            json!({"event":"speech_out_text_chunks","utterance_id":"u","diagnostic_mono_ns":1_005_000_000_u64,"chunks":["hello output diagnostics"]}),
        );
        apply(
            &mut model,
            json!({"event":"speech_out_synthesis_started","utterance_id":"u","diagnostic_mono_ns":1_020_000_000_u64}),
        );
        apply(
            &mut model,
            json!({"event":"speech_out_text_chunk_started","utterance_id":"u","diagnostic_mono_ns":1_030_000_000_u64,"text_chunk_index":0,"text_chunk_count":1}),
        );
        apply(
            &mut model,
            json!({"event":"speech_out_audio_chunk","utterance_id":"u","diagnostic_mono_ns":1_085_000_000_u64,"seq":0,"text_chunk_index":0,"bytes":2048}),
        );
        apply(
            &mut model,
            json!({"event":"speech_out_cancel_requested","utterance_id":"u","diagnostic_mono_ns":1_090_000_000_u64,"reason":"test"}),
        );
        apply(
            &mut model,
            json!({"event":"speech_out_cancel_acknowledged","utterance_id":"u","diagnostic_mono_ns":1_100_000_000_u64,"cancel_latency_ms":10.0}),
        );
        apply(
            &mut model,
            json!({"event":"speech_out_diagnostic_terminal","utterance_id":"u","diagnostic_mono_ns":1_101_000_000_u64,"outcome":"cancelled","e2e_ms":101.0,"first_audio_ms":85.0,"cancel_latency_ms":10.0}),
        );
        let rendered = model.render(true);
        assert!(rendered.contains("⏳ ⇢ ✂ ⌁ … ▣ ⏹ \"hello output diagnostics\""));
        assert!(rendered.contains(
            "speech-out terminal: cancelled e2e=101.0ms first-audio=85.0ms cancel=10.0ms"
        ));
    }

    #[test]
    fn assistant_turn_truncated_dims_spoken_prefix() {
        let mut model = TuiModel::new(true);
        apply(
            &mut model,
            json!({
                "event": "speech_out_request_received",
                "utterance_id": "u",
                "diagnostic_mono_ns": 1_000_000_000_u64,
                "text": "This is a long assistant reply so you can barge in",
                "num_chunks": 1
            }),
        );
        apply(
            &mut model,
            json!({
                "event": "assistant_turn_truncated",
                "diagnostic_mono_ns": 2_000_000_000_u64,
                "intended_text": "This is a long assistant reply so you can barge in",
                "spoken_prefix": "This is a long assistant reply",
                "cut_text": "This is a long assistant reply",
                "primary_cut_source": "drain",
                "played_chunks": 1
            }),
        );
        let rendered = model.render(true);
        assert!(
            rendered.contains(
                "\u{1b}[2mThis is a long assistant reply\u{1b}[0m ✂ so you can barge in"
            ),
            "spoken prefix must be dim + cut marker, remainder normal white; got:\n{rendered}"
        );
        assert!(
            rendered.contains("assistant cut (drain): This is a long assistant reply"),
            "seam note must include cut text; got:\n{rendered}"
        );
    }

    #[test]
    fn pause_resume_cumulative_delta_renders_only_new_section() {
        // Regression: when speech pauses and resumes, the resumed section must
        // only render the new transcript delta, not the entire cumulative text
        // accumulated before the pause.
        // Modeled on session 37c36a9d.
        let mut model = TuiModel::default();
        apply(
            &mut model,
            json!({"event":"vad_speech_start","stream_session_id":"s"}),
        );
        apply(
            &mut model,
            json!({"event":"transcript_update","stream_session_id":"s","revision":1,"committed_text":"hello","tentative_text":""}),
        );
        // Speech pauses; more transcript arrives before the pause registers.
        apply(
            &mut model,
            json!({"event":"vad_speech_end","stream_session_id":"s","end_sample":8000,"decision_sample":12000}),
        );
        apply(
            &mut model,
            json!({"event":"transcript_update","stream_session_id":"s","revision":2,"committed_text":"hello world","tentative_text":""}),
        );
        // Speech resumes — a fresh transcript section starts.
        apply(
            &mut model,
            json!({"event":"vad_speech_start","stream_session_id":"s"}),
        );
        apply(
            &mut model,
            json!({"event":"transcript_update","stream_session_id":"s","revision":3,"committed_text":"hello world and more","tentative_text":""}),
        );

        // The resumed section must only contain the delta added after resume.
        let rendered = model.render(false);
        assert!(
            rendered.contains("#01\n  ◖ \"hello world\" ◗"),
            "first section: {rendered}"
        );
        assert!(
            rendered.contains("◖ \"and more\""),
            "second section should only have delta: {rendered}"
        );
    }

    #[test]
    fn smart_turn_recheck_cancelled_cumulative_delta_renders_only_new_section() {
        // Regression: when smart-turn probes exhaust and then resume, the new
        // section must only render the transcript delta, not the entire cumulative
        // text. Modeled on session 2f900030.
        let mut model = TuiModel::default();
        apply(
            &mut model,
            json!({"event":"vad_speech_start","stream_session_id":"s"}),
        );
        apply(
            &mut model,
            json!({"event":"transcript_update","stream_session_id":"s","revision":1,"committed_text":"So as LoreMIPS are","tentative_text":""}),
        );
        // Probes exhaust (turn is considered paused), then speech resumes.
        apply(
            &mut model,
            json!({"event":"smart_turn_recheck_exhausted","stream_session_id":"s"}),
        );
        apply(
            &mut model,
            json!({"event":"smart_turn_recheck_cancelled","stream_session_id":"s","reason":"speech_resumed"}),
        );
        apply(
            &mut model,
            json!({"event":"transcript_update","stream_session_id":"s","revision":2,"committed_text":"So as LoreMIPS are way to find this","tentative_text":""}),
        );

        let rendered = model.render(false);
        assert!(
            rendered.contains("◖ \"So as LoreMIPS are\""),
            "first section should contain initial text: {rendered}"
        );
        assert!(
            rendered.contains("◖ \"way to find this\""),
            "second section should only have delta: {rendered}"
        );
        assert!(
            !rendered.contains("◖ \"So as LoreMIPS are way to find this\""),
            "must not render cumulative text in resumed section: {rendered}"
        );
    }

    #[test]
    fn stale_fallback_after_close_is_ignored() {
        // Regression: vad_acoustic_fallback arriving after the turn closed
        // must not create a ghost turn or attach to a closed turn.
        let mut model = TuiModel::default();
        apply(
            &mut model,
            json!({"event":"vad_speech_start","stream_session_id":"s"}),
        );
        apply(
            &mut model,
            json!({"event":"turn_started","stream_session_id":"s","turn_id":"s:turn:0","source":"vad"}),
        );
        apply(
            &mut model,
            json!({"event":"turn_closed","stream_session_id":"s","source":"smart_turn","turn_id":"s:turn:0"}),
        );
        // Stale fallback arrives after close — no open turn to attach to.
        apply(
            &mut model,
            json!({"event":"vad_acoustic_fallback","stream_session_id":"s","decision_sample":20000}),
        );

        // Must not have created a new ghost turn.
        assert_eq!(model.turns.len(), 1);
        assert!(model.turns[0].closed);
    }

    #[test]
    fn circled_glyph_beyond_four_renders_unicode_circled() {
        // The fifth semantic probe check must not render another ④.
        let glyph = circled(5);
        assert_eq!(glyph, "⑤");
        assert_ne!(glyph, circled(4));
        // Unicode circled numbers through ⑳, then parenthesized fallback.
        assert_eq!(circled(10), "⑩");
        assert_eq!(circled(20), "⑳");
        assert_eq!(circled(21), "(21)");
    }

    #[test]
    fn authoritative_per_turn_committed_transcript_overrides_cumulative_text() {
        // The forthcoming per-turn transcript_committed event must be used as
        // the authoritative turn text instead of deriving a delta from the
        // cumulative stream.
        let mut model = TuiModel::default();
        apply(
            &mut model,
            json!({"event":"vad_speech_start","stream_session_id":"s"}),
        );
        apply(
            &mut model,
            json!({"event":"transcript_update","stream_session_id":"s","revision":1,"committed_text":"hello","tentative_text":""}),
        );
        apply(
            &mut model,
            json!({"event":"transcript_committed","stream_session_id":"s","text":"hello world"}),
        );

        assert_eq!(model.turns[0].text, "hello world");
    }

    #[test]
    fn authoritative_committed_transcript_respects_closed_turn_immutability() {
        // Even the authoritative per-turn event must not rewrite a closed turn.
        let mut model = TuiModel::default();
        apply(
            &mut model,
            json!({"event":"vad_speech_start","stream_session_id":"s"}),
        );
        apply(
            &mut model,
            json!({"event":"transcript_update","stream_session_id":"s","revision":1,"committed_text":"first","tentative_text":""}),
        );
        apply(
            &mut model,
            json!({"event":"turn_closed","stream_session_id":"s","source":"smart_turn","turn_id":"s:turn:0"}),
        );
        // Late transcript_committed for closed turn must not rewrite.
        apply(
            &mut model,
            json!({"event":"turn_transcript_committed","stream_session_id":"s","text":"first final","turn_id":"s:turn:0"}),
        );

        assert_eq!(model.turns[0].text, "first");
        assert!(model.turns[0].closed);
    }

    #[test]
    fn fallback_bar_clamped_to_target_and_cleared_on_close() {
        // The fallback progress bar must never exceed the target bar width and
        // must clear when the turn closes.
        let mut model = TuiModel::default();
        apply(
            &mut model,
            json!({"event":"vad_speech_start","stream_session_id":"s"}),
        );
        apply(
            &mut model,
            json!({"event":"turn_started","stream_session_id":"s","turn_id":"s:turn:0","source":"vad"}),
        );
        // Progress exceeds target (5s > 3.5s) — bar must clamp, not overflow.
        apply(
            &mut model,
            json!({"event":"vad_frame","stream_session_id":"s","probability":0.5,"smoothed_probability":0.4,"silence_counter":2,"hangover_frames":5,"fallback_progress_ms":5000,"fallback_target_ms":3500}),
        );
        // Bar must exist and not be beyond full.
        assert!(model.live_fallback_bar.contains("████████"));
        assert!(!model.live_fallback_bar.contains("█████████"));

        apply(
            &mut model,
            json!({"event":"turn_closed","stream_session_id":"s","source":"vad","turn_id":"s:turn:0"}),
        );
        assert!(model.live_fallback_bar.is_empty());
    }

    #[test]
    fn punctuation_only_delta_suppressed_but_advances_baseline() {
        // Regression: when a late punctuation-only transcript_update (e.g. "."
        // for a prior closed turn) arrives after a new acoustic turn has opened,
        // the punctuation must not render as the new turn's text, but the
        // section baseline must advance so the next real update is correct.
        let mut model = TuiModel::default();
        // Turn 0: say something and close.
        apply(
            &mut model,
            json!({"event":"vad_speech_start","stream_session_id":"s"}),
        );
        apply(
            &mut model,
            json!({"event":"transcript_update","stream_session_id":"s","revision":1,"committed_text":"hello","tentative_text":""}),
        );
        apply(
            &mut model,
            json!({"event":"turn_closed","stream_session_id":"s","source":"smart_turn","turn_id":"s:turn:0"}),
        );
        // Turn 1: new speech starts.
        apply(
            &mut model,
            json!({"event":"vad_speech_start","stream_session_id":"s"}),
        );
        // Late punctuation for turn 0 arrives — cumulative text now "hello."
        apply(
            &mut model,
            json!({"event":"transcript_update","stream_session_id":"s","revision":2,"committed_text":"hello.","tentative_text":""}),
        );
        // Turn 1 must NOT show the punctuation.
        assert!(
            model.turns[1].text.is_empty(),
            "punctuation delta must not render: '{}'",
            model.turns[1].text
        );
        // Now real text for turn 1 arrives.
        apply(
            &mut model,
            json!({"event":"transcript_update","stream_session_id":"s","revision":3,"committed_text":"hello. world","tentative_text":""}),
        );
        // Turn 1 must get only its own delta (the leading space is part of the
        // cumulative-text delta since the prefix ended at "hello.").
        assert_eq!(model.turns[1].text, " world");
    }

    #[test]
    fn orphan_fallback_with_mismatched_turn_id_is_ignored() {
        // vad_acoustic_fallback with a turn_id that matches a closed turn
        // must not attach to it.
        let mut model = TuiModel::default();
        apply(
            &mut model,
            json!({"event":"vad_speech_start","stream_session_id":"s"}),
        );
        apply(
            &mut model,
            json!({"event":"turn_started","stream_session_id":"s","turn_id":"s:turn:0","source":"vad"}),
        );
        apply(
            &mut model,
            json!({"event":"turn_closed","stream_session_id":"s","source":"smart_turn","turn_id":"s:turn:0"}),
        );
        // Stale fallback with turn_id of the closed turn.
        apply(
            &mut model,
            json!({"event":"vad_acoustic_fallback","stream_session_id":"s","turn_id":"s:turn:0","decision_sample":20000}),
        );

        // Must not have created a ghost turn or modified the closed turn.
        assert_eq!(model.turns.len(), 1);
        assert!(model.turns[0].closed);
        // The closed turn must not carry a wait glyph from the stale fallback.
        assert!(
            !model.turns[0].glyphs.contains(&"·".to_owned()),
            "no wait glyph on closed turn: {:?}",
            model.turns[0].glyphs
        );
    }

    // ── clock-domain tests ───────────────────────────────────────────────

    #[test]
    fn single_domain_no_tag() {
        // When all events share the same domain (Harness), no domain tag is
        // rendered.
        let mut model = TuiModel::default();
        apply(
            &mut model,
            json!({"event":"vad_speech_start","stream_session_id":"s","start_sample":0,"decision_sample":320,"diagnostic_mono_ns":1_000_000_000_u64}),
        );
        apply(
            &mut model,
            json!({"event":"vad_speech_end","stream_session_id":"s","end_sample":16000,"decision_sample":21760,"diagnostic_mono_ns":1_500_000_000_u64}),
        );
        apply(
            &mut model,
            json!({"event":"turn_closed","stream_session_id":"s","source":"smart_turn","turn_id":"s:turn:0","diagnostic_mono_ns":2_000_000_000_u64}),
        );
        let rendered = model.render(true);
        // Notes must show harness-relative deltas without a domain tag.
        // Format is +{ms:07.1}ms, 7-char zero-padded.
        assert!(rendered.contains("+00500.0ms"), "{rendered}");
        assert!(rendered.contains("+01000.0ms"), "{rendered}");
        assert!(
            !rendered.contains("ms h"),
            "no per-note domain tag in single domain: {rendered}"
        );
        assert!(!rendered.contains("ms c"), "{rendered}");
        assert!(!rendered.contains("ms d"), "{rendered}");
    }

    #[test]
    fn domain_tag_appears_when_multiple_domains_interleave() {
        // Events from Harness and Daemon domains interleaved — domain tags
        // must appear on note_event calls after both domains are known.
        let mut model = TuiModel::default();
        // Harness event at t=0 (uses self.note, no timestamp tag).
        apply(
            &mut model,
            json!({"event":"vad_speech_start","stream_session_id":"s","start_sample":0,"decision_sample":320,"diagnostic_mono_ns":0_u64}),
        );
        // Daemon event at its own t=10_000_000_000 (different epoch).
        apply(
            &mut model,
            json!({"event":"vad_speech_end","stream_session_id":"s","end_sample":16000,"decision_sample":21760,"daemon_mono_ns":10_000_000_000_u64}),
        );
        // Harness event (smart_turn_candidate emits no note).
        apply(
            &mut model,
            json!({"event":"smart_turn_candidate","stream_session_id":"s","diagnostic_mono_ns":500_000_000_u64}),
        );
        // Daemon event at t=10_500_000_000.
        apply(
            &mut model,
            json!({"event":"smart_turn_decision","stream_session_id":"s","probability":0.8,"complete":true,"daemon_mono_ns":10_500_000_000_u64}),
        );
        // Harness event at t=700ms — first harness note_event call.
        apply(
            &mut model,
            json!({"event":"turn_closed","stream_session_id":"s","source":"smart_turn","turn_id":"s:turn:0","diagnostic_mono_ns":700_000_000_u64}),
        );
        let rendered = model.render(true);
        // Harness: only turn_closed emits a harness note_event.
        assert!(
            rendered.contains("+00700.0ms h"),
            "harness delta from baseline 0: {rendered}"
        );
        // Daemon deltas — zero-based for its own epoch.
        assert!(
            rendered.contains("+00000.0ms d"),
            "daemon baseline: {rendered}"
        );
        assert!(
            rendered.contains("+00500.0ms d"),
            "daemon delta: {rendered}"
        );
    }

    #[test]
    fn no_cross_domain_subtraction_prevents_zero_clamp() {
        // Harness epoch ~100ms, Daemon epoch 10_000_000_000_000.
        // Without per-domain baselines the harness delta would be enormous
        // or zero-clamped.
        let mut model = TuiModel::default();
        apply(
            &mut model,
            json!({"event":"vad_speech_start","stream_session_id":"s","diagnostic_mono_ns":100_000_000_u64}),
        );
        apply(
            &mut model,
            json!({"event":"vad_speech_end","stream_session_id":"s","daemon_mono_ns":10_000_000_000_000_u64}),
        );
        apply(
            &mut model,
            json!({"event":"turn_closed","stream_session_id":"s","source":"smart_turn","diagnostic_mono_ns":250_000_000_u64}),
        );
        let rendered = model.render(true);
        // Harness: 250ms - 100ms = 150ms delta.
        assert!(
            rendered.contains("+00150.0ms h"),
            "harness relative after multi-domain: {rendered}"
        );
        // Daemon: 0ms delta (its own baseline).
        assert!(
            rendered.contains("+00000.0ms d"),
            "daemon baseline: {rendered}"
        );
        // Must NOT contain absurd deltas.
        assert!(
            !rendered.contains("+9999"),
            "no enormous fabricated duration: {rendered}"
        );
        assert!(!rendered.contains("+10000"), "{rendered}");
    }

    #[test]
    fn client_domain_separate_from_daemon() {
        // Client timestamps have their own independent epoch.
        let mut model = TuiModel::new(true);
        apply(
            &mut model,
            json!({"event":"speech_out_request_queued","utterance_id":"u","client_mono_ns":7_000_000_000_u64,"text":"hello"}),
        );
        apply(
            &mut model,
            json!({"event":"speech_out_request_received","utterance_id":"u","daemon_mono_ns":50_000_000_000_u64,"text":"hello","num_chunks":1}),
        );
        apply(
            &mut model,
            json!({"event":"speech_out_completed","utterance_id":"u","client_mono_ns":7_200_000_000_u64}),
        );
        let rendered = model.render(true);
        // First client event: single domain, no tag.
        assert!(
            rendered.contains("+00000.0ms"),
            "client baseline (no tag): {rendered}"
        );
        // Daemon domain tag appears once second domain is known.
        assert!(
            rendered.contains("+00000.0ms d"),
            "daemon baseline: {rendered}"
        );
        // Subsequent client event gets tag (multi-domain).
        assert!(
            rendered.contains("+00200.0ms c"),
            "client delta: {rendered}"
        );
        // No cross-domain leakage.
        assert!(
            !rendered.contains("+43000"),
            "no cross-domain subtraction: {rendered}"
        );
    }

    #[test]
    fn diagnostic_clock_origin_does_not_reclassify() {
        // diagnostic_mono_ns is always Harness regardless of
        // diagnostic_clock_origin presence or value.
        let mut model = TuiModel::new(true);
        apply(
            &mut model,
            json!({"event":"speech_out_request_queued","utterance_id":"u","diagnostic_mono_ns":100_000_000_u64,"diagnostic_clock_origin":"harness_local_monotonic","text":"hi"}),
        );
        apply(
            &mut model,
            json!({"event":"speech_out_request_received","utterance_id":"u","diagnostic_mono_ns":150_000_000_u64,"diagnostic_clock_origin":"harness_local_monotonic","text":"hi","num_chunks":1}),
        );
        let rendered = model.render(true);
        // Both are single Harness domain, so no tag. Delta 150M-100M = 50ms.
        assert!(
            rendered.contains("+00050.0ms"),
            "harness delta, no tag: {rendered}"
        );
        // Should NOT show Client or Daemon tag.
        assert!(!rendered.contains("ms c"), "no client tag: {rendered}");
        assert!(!rendered.contains("ms d"), "no daemon tag: {rendered}");
    }

    #[test]
    fn non_note_seed_event_observes_domain() {
        // An event that uses self.note (not note_event) must still seed the
        // clock-domain baseline so that subsequent note_event calls in the
        // same domain compute correct relative deltas.
        let mut model = TuiModel::default();
        // vad_speech_start uses self.note — no note_event call — but the
        // domain baseline must still be seeded.
        apply(
            &mut model,
            json!({"event":"vad_speech_start","stream_session_id":"s","start_sample":0,"diagnostic_mono_ns":500_000_000_u64}),
        );
        apply(
            &mut model,
            json!({"event":"vad_speech_end","stream_session_id":"s","end_sample":16000,"diagnostic_mono_ns":700_000_000_u64}),
        );
        apply(
            &mut model,
            json!({"event":"turn_closed","stream_session_id":"s","source":"smart_turn","diagnostic_mono_ns":900_000_000_u64}),
        );
        let rendered = model.render(true);
        // Delta from 500M to 700M = 200ms.
        assert!(
            rendered.contains("+00200.0ms"),
            "correct seed-relative delta: {rendered}"
        );
        // Delta from 500M to 900M = 400ms.
        assert!(
            rendered.contains("+00400.0ms"),
            "correct relative delta: {rendered}"
        );
        // Single domain -> no tag.
        assert!(!rendered.contains("ms h"), "no tag: {rendered}");
    }

    #[test]
    fn equal_raw_timestamps_diff_domain_stay_zero() {
        // Two domains with equal raw timestamp values but separate baselines
        // must each render as zero in its own domain.
        let mut model = TuiModel::default();
        apply(
            &mut model,
            json!({"event":"vad_speech_start","stream_session_id":"s","diagnostic_mono_ns":42_u64}),
        );
        apply(
            &mut model,
            json!({"event":"vad_speech_end","stream_session_id":"s","daemon_mono_ns":42_u64}),
        );
        let rendered = model.render(true);
        // Daemon note_event shows its own zero delta.
        assert!(rendered.contains("+00000.0ms d"), "daemon zero: {rendered}");
        // Harness baseline was seeded by vad_speech_start (uses self.note).
        // No negative delta timestamps.
        assert!(!rendered.contains("+-"), "no negative delta: {rendered}");
    }

    #[test]
    fn plain_single_domain_unchanged_behavior() {
        // Existing test scenario (all diagnostic_mono_ns, same epoch) must
        // still work with no domain tags and correct deltas.
        let mut model = TuiModel::default();
        apply(
            &mut model,
            json!({"event":"vad_speech_start","stream_session_id":"s","start_sample":0,"decision_sample":320,"diagnostic_mono_ns":0_u64}),
        );
        apply(
            &mut model,
            json!({"event":"transcript_update","stream_session_id":"s","committed_text":"hello there","tentative_text":""}),
        );
        apply(
            &mut model,
            json!({"event":"vad_speech_end","stream_session_id":"s","end_sample":16000,"decision_sample":21760,"diagnostic_mono_ns":200_000_000_u64}),
        );
        for (i, p) in [(0, 0.31), (1, 0.45), (2, 0.79)] {
            apply(
                &mut model,
                json!({"event":"smart_turn_candidate","stream_session_id":"s","end_sample":16000,"decision_sample":21760 + i * 4000,"diagnostic_mono_ns":300_000_000_u64 + i * 100_000_000_u64}),
            );
            apply(
                &mut model,
                json!({"event":"smart_turn_decision","stream_session_id":"s","end_sample":16000,"decision_sample":21760 + i * 4000,"probability":p,"threshold":0.5,"complete":p > 0.5,"diagnostic_mono_ns":350_000_000_u64 + i * 100_000_000_u64}),
            );
            if p <= 0.5 {
                apply(
                    &mut model,
                    json!({"event":"smart_turn_recheck_scheduled","stream_session_id":"s","pending_decision_samples":[1,2]}),
                );
            }
        }
        apply(
            &mut model,
            json!({"event":"turn_closed","stream_session_id":"s","source":"smart_turn","turn_id":"s:turn:0","diagnostic_mono_ns":700_000_000_u64}),
        );
        let rendered = model.render(false);
        // Glyph rendering must still work as before.
        assert!(rendered.contains("#01\n  ◖ \"hello there\" ◗ ①.31 · ②.45 · ③.79 ◆"));
        // Debug notes must show deltas.
        let debug = model.render(true);
        assert!(debug.contains("+00200.0ms"), "vad end delta: {debug}");
        assert!(debug.contains("+00700.0ms"), "turn close delta: {debug}");
        // No domain tags for single-domain.
        assert!(!debug.contains("ms h"), "no domain tag: {debug}");
    }

    #[test]
    fn classify_timestamp_deterministic_ordering() {
        // When multiple timestamp fields exist on the same event, the
        // deterministic priority order must pick the right one.
        let v = json!({
            "event": "test",
            "diagnostic_mono_ns": 1,
            "client_mono_ns": 2,
            "daemon_mono_ns": 3
        });
        // classify_timestamp respects field-name priority.
        // client_mono_ns is checked before daemon_mono_ns and diagnostic_mono_ns.
        let (ns, domain) = classify_timestamp(&v).expect("must find a timestamp");
        assert_eq!(
            ns, 2,
            "client_mono_ns takes priority over diagnostic_mono_ns"
        );
        assert_eq!(domain, ClockDomainKey::Client);

        // When only diagnostic_mono_ns exists.
        let v2 = json!({"event": "test", "diagnostic_mono_ns": 42});
        let (ns2, domain2) = classify_timestamp(&v2).expect("must find a timestamp");
        assert_eq!(ns2, 42);
        assert_eq!(domain2, ClockDomainKey::Harness);

        // When only daemon_mono_ns exists.
        let v3 = json!({"event": "test", "daemon_mono_ns": 99});
        let (ns3, domain3) = classify_timestamp(&v3).expect("must find a timestamp");
        assert_eq!(ns3, 99);
        assert_eq!(domain3, ClockDomainKey::Daemon);

        // diagnostic_clock_origin does NOT reclassify diagnostic_mono_ns.
        let v4 = json!({"event": "test", "diagnostic_mono_ns": 77, "diagnostic_clock_origin": "harness_local_monotonic"});
        let (ns4, domain4) = classify_timestamp(&v4).expect("must find a timestamp");
        assert_eq!(ns4, 77);
        assert_eq!(
            domain4,
            ClockDomainKey::Harness,
            "diagnostic_clock_origin must not reclassify"
        );
    }
}
