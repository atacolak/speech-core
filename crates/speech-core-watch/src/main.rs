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
const MAX_TUI_NOTES: usize = 6;

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
    turns: Vec<TuiTurn>,
    current: Option<usize>,
    next_turn_number: u64,
    notes: VecDeque<String>,
    live_vad_bar: String,
    last_transcript_display: String,
}

#[derive(Debug, Clone)]
struct TuiTurn {
    number: u64,
    sections: Vec<TuiSection>,
    text: String,
    glyphs: Vec<String>,
    boundary: Option<BoundaryState>,
    paused: bool,
    closed: bool,
}

#[derive(Debug, Clone)]
struct TuiSection {
    text: String,
    glyphs: Vec<String>,
}

impl TuiModel {
    fn handle(&mut self, value: &Value) {
        if let Some(session_id) = value.get("stream_session_id").and_then(|v| v.as_str()) {
            self.stream_session_id
                .get_or_insert_with(|| session_id.to_owned());
        }
        let event = event_name(value);
        match event {
            "vad_session_start" => {
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
            "transcript_update" => {
                let committed = value
                    .get("committed_text")
                    .and_then(|v| v.as_str())
                    .unwrap_or("");
                let tentative = value
                    .get("tentative_text")
                    .and_then(|v| v.as_str())
                    .unwrap_or("");
                let display = format!("{committed}{tentative}");
                if !display.is_empty() {
                    let delta = display
                        .strip_prefix(&self.last_transcript_display)
                        .unwrap_or(&display);
                    if !delta.is_empty() {
                        let idx = self.turn_for_text();
                        self.turns[idx].text.push_str(delta);
                    }
                }
                self.last_transcript_display = display;
            }
            "vad_speech_start" => {
                if let Some(idx) = self.current_open_turn() {
                    if self.turns[idx].paused {
                        self.push_glyph(idx, "↺");
                        self.finalize_section(idx);
                        self.turns[idx].paused = false;
                        self.turns[idx].boundary = None;
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
                self.note(format!(
                    "vad candidate boundary at {}; smart-turn probes begin",
                    sample_field_ms(value, "end_sample")
                ));
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
                self.note(format!(
                    "smart-turn check {} -> {} p={probability:.3}",
                    check_count,
                    if complete { "complete" } else { "hold" }
                ));
            }
            "smart_turn_recheck_scheduled" => {
                let idx = self.ensure_turn();
                self.push_wait(idx);
                let remaining = value
                    .get("pending_decision_samples")
                    .and_then(|v| v.as_array())
                    .map(|a| a.len())
                    .unwrap_or_default();
                self.note(format!(
                    "semantic rechecks scheduled: {remaining} remaining"
                ));
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
                }
                self.turns[idx].paused = false;
                self.turns[idx].boundary = None;
                self.note("semantic probes cancelled by resumed speech".to_owned());
            }
            "turn_eou_suppressed" => {
                let source = value.get("source").and_then(|v| v.as_str()).unwrap_or("?");
                let reason = value.get("reason").and_then(|v| v.as_str()).unwrap_or("?");
                self.note(format!("no eou: {source}/{reason}"));
            }
            "turn_closed" => {
                let idx = self.ensure_turn();
                let source = value.get("source").and_then(|v| v.as_str()).unwrap_or("?");
                if matches!(source, "smart_turn" | "vad_acoustic_fallback") {
                    self.push_glyph(idx, "◆");
                } else {
                    self.push_glyph(idx, "◇");
                }
                self.turns[idx].closed = true;
                self.turns[idx].paused = false;
                self.current = None;
                self.note(format!("turn closed by {source}"));
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
                self.push_vad_bar(0, probability, smoothed, silence, hangover);
            }
            "vad_acoustic_fallback" => {
                let idx = self.ensure_turn();
                self.push_wait(idx);
                self.note(format!(
                    "acoustic fallback armed at {}; low smoothed vad",
                    sample_field_ms(value, "decision_sample")
                ));
            }
            "turn_human_hold" => {
                let ms_without_tokens = value
                    .get("ms_without_tokens")
                    .and_then(|v| v.as_u64())
                    .unwrap_or_default();
                self.note(format!(
                    "human hold: speech-like audio for {} without new transcript tokens",
                    format_ms(ms_without_tokens)
                ));
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
        if let Some(vad) = &self.vad_config {
            out.push_str(&format!("{vad}\n"));
        }
        if let Some(smart) = &self.smart_turn_config {
            out.push_str(&format!("{smart}\n"));
        }
        out.push_str("glyphs  ◖ speech  ◗ pause  ①②③④ semantic checks  ◆ close  ↺ resume  · wait  ◇ unresolved/fallback\n\n");

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
        }
        out
    }

    fn ensure_turn(&mut self) -> usize {
        self.current_open_turn().unwrap_or_else(|| self.new_turn())
    }

    fn turn_for_text(&mut self) -> usize {
        self.current_open_turn().unwrap_or_else(|| self.new_turn())
    }

    fn current_open_turn(&self) -> Option<usize> {
        self.current
            .filter(|idx| self.turns.get(*idx).is_some_and(|turn| !turn.closed))
    }

    fn new_turn(&mut self) -> usize {
        self.next_turn_number = self.next_turn_number.saturating_add(1);
        self.turns.push(TuiTurn {
            number: self.next_turn_number,
            sections: Vec::new(),
            text: String::new(),
            glyphs: Vec::new(),
            boundary: None,
            paused: false,
            closed: false,
        });
        let idx = self.turns.len() - 1;
        self.current = Some(idx);
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

    fn push_vad_bar(
        &mut self,
        _idx: usize,
        probability: f64,
        smoothed: f64,
        silence: u64,
        hangover: u64,
    ) {
        let raw = bar8(probability);
        let smooth = bar8(smoothed);
        let quota = if hangover == 0 {
            "0/0".to_owned()
        } else {
            format!("{}/{}", silence.min(hangover), hangover)
        };
        self.live_vad_bar =
            format!("raw:{raw} {probability:.2}  smooth:{smooth} {smoothed:.2}  stop:{quota}");
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

    fn note(&mut self, note: String) {
        if self.notes.back() == Some(&note) {
            return;
        }
        self.notes.push_back(note);
        while self.notes.len() > MAX_TUI_NOTES {
            self.notes.pop_front();
        }
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
        out
    }
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

#[tokio::main]
async fn main() -> Result<()> {
    let args = Args::parse();
    if let Some(path) = &args.replay_events {
        return replay_events(&args, path);
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

    let mut transcript_state = TranscriptState::default();
    let mut tui_model = TuiModel::default();

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
    let mut transcript_state = TranscriptState::default();
    let mut tui_model = TuiModel::default();
    for line in reader.lines() {
        let line = line?;
        handle_text_event(args, &line, &mut transcript_state, &mut tui_model, false)?;
    }
    if args.mode.is_tui() {
        print!("{}", tui_model.render(args.mode.is_debug()));
        io::stdout().flush()?;
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
        Mode::Jsonl => println!("{text}"),
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
                print!("\x1b[2J\x1b[H{}", tui_model.render(args.mode.is_debug()));
                io::stdout().flush()?;
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

fn format_ms(ms: u64) -> String {
    format!("{ms}ms")
}

fn circled(index: usize) -> &'static str {
    match index {
        1 => "①",
        2 => "②",
        3 => "③",
        4 => "④",
        _ => "④",
    }
}

fn compact_probability(probability: f64) -> String {
    let probability = probability.clamp(0.0, 1.0);
    let rendered = format!("{probability:.2}");
    rendered.strip_prefix('0').unwrap_or(&rendered).to_owned()
}

fn normalized_text(text: &str) -> String {
    text.split_whitespace().collect::<Vec<_>>().join(" ")
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
}
