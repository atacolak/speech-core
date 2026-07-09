use anyhow::{anyhow, bail, Context, Result};
use clap::{Parser, Subcommand, ValueEnum};
use futures_util::{SinkExt, StreamExt};
use serde::{Deserialize, Serialize};
use serde_json::json;
use speech_core_protocol::now_mono_ns;
use std::net::SocketAddr;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::fs::File;
use tokio::io::{AsyncReadExt, AsyncWrite, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use tokio::process::{Child, Command};
use tokio::sync::Mutex;
use tokio_tungstenite::tungstenite::Message;
use tokio_tungstenite::{accept_async, connect_async, WebSocketStream};
use tracing::{debug, error, info, warn};
use uuid::Uuid;

const DEFAULT_SUPERTONIC_URL: &str = "http://127.0.0.1:7788/v1/tts";
const DEFAULT_SPEECH_OUT_WS_URL: &str = "ws://127.0.0.1:8788/ws/speech-out";
const DEFAULT_SPEECH_OUT_BIND: &str = "0.0.0.0:8788";
const DEFAULT_WARM_TTL_SECS: u64 = 20 * 60;
const DEFAULT_STEPS: u32 = 5;
const DEFAULT_SPEED: f32 = 1.30;
const DEFAULT_LANG: &str = "en";
const DEFAULT_VOICE: &str = "M1";

/// Pragmatic text chunker defaults.
const DEFAULT_CHUNK_MIN_CHARS: usize = 8;
const DEFAULT_CHUNK_MAX_CHARS: usize = 160;

/// WAV constants.
const WAV_HEADER_MIN_LEN: usize = 44;
const WAV_FMT_CHUNK_ID: [u8; 4] = *b"fmt ";
const WAV_DATA_CHUNK_ID: [u8; 4] = *b"data";

#[derive(Debug, Parser)]
#[command(
    author,
    version,
    about = "speech-out websocket TTS daemon and playback adapter"
)]
struct Cli {
    #[command(subcommand)]
    command: SpeechOutCommand,
}

#[derive(Debug, Subcommand)]
enum SpeechOutCommand {
    /// Synthesize or route one local utterance of text.
    Say(SayArgs),
    /// Run the desktop/sfub websocket TTS daemon. Inference happens here.
    Daemon(DaemonArgs),
    /// Connect to a speech-out daemon and play streamed audio chunks on this device.
    Play(PlayArgs),
}

#[derive(Debug, Parser)]
struct SayArgs {
    /// Text to synthesize. If omitted, pass --stdin to read text from stdin.
    text: Option<String>,

    /// Read utterance text from stdin.
    #[arg(long)]
    stdin: bool,

    /// TTS backend. mock is deterministic and has no audio dependencies; command runs an external program; supertonic-http calls a local supertonic serve endpoint.
    #[arg(long, value_enum, default_value_t = BackendKind::Mock, env = "SPEECH_OUT_BACKEND")]
    backend: BackendKind,

    /// Output WAV path for backends that produce audio.
    #[arg(long, env = "SPEECH_OUT_OUTPUT")]
    output: Option<PathBuf>,

    /// External command for --backend command. The text is passed as the final argv by default.
    #[arg(long, env = "SPEECH_OUT_COMMAND")]
    command: Option<String>,

    /// Additional argv for --backend command. Repeat for multiple args.
    #[arg(
        long = "command-arg",
        env = "SPEECH_OUT_COMMAND_ARGS",
        value_delimiter = ' '
    )]
    command_args: Vec<String>,

    /// Send utterance text to the command's stdin instead of appending it as argv.
    #[arg(long, env = "SPEECH_OUT_COMMAND_STDIN")]
    command_stdin: bool,

    /// Local supertonic serve URL. Defaults to the native endpoint so steps/lang are honored.
    #[arg(long, default_value = DEFAULT_SUPERTONIC_URL, env = "SPEECH_OUT_SUPERTONIC_URL")]
    supertonic_url: String,

    /// Supertonic/OpenAI-compatible voice id or preset voice name.
    #[arg(long, default_value = DEFAULT_VOICE, env = "SPEECH_OUT_VOICE")]
    voice: String,

    /// Language code for local backends that support it.
    #[arg(long, default_value = DEFAULT_LANG, env = "SPEECH_OUT_LANG")]
    lang: String,

    /// Supertonic diffusion steps. 5 is the current speed/quality default for interactive utterances.
    #[arg(long, default_value_t = DEFAULT_STEPS, env = "SPEECH_OUT_STEPS")]
    steps: u32,

    /// Speech speed multiplier.
    #[arg(long, default_value_t = DEFAULT_SPEED, env = "SPEECH_OUT_SPEED")]
    speed: f32,

    /// Optional Supertonic reference voice/audio/style handle. Passed through to native /v1/tts when set.
    #[arg(long, env = "SPEECH_OUT_REFERENCE")]
    reference: Option<String>,

    /// Optional Supertonic style prompt/preset. Passed through to native /v1/tts when set.
    #[arg(long, env = "SPEECH_OUT_STYLE")]
    style: Option<String>,

    /// Seconds to allow for one synthesis/playback command before terminating it.
    #[arg(long, default_value_t = 60, env = "SPEECH_OUT_TIMEOUT_SECS")]
    timeout_secs: u64,

    /// Playback command used after --backend supertonic-http when --output is omitted.
    #[arg(long, default_value = "pw-play", env = "SPEECH_OUT_PLAY_COMMAND")]
    play_command: String,

    /// Minimum chunk size in characters for pragmatic text chunking.
    #[arg(long, default_value_t = DEFAULT_CHUNK_MIN_CHARS, env = "SPEECH_OUT_CHUNK_MIN_CHARS")]
    chunk_min_chars: usize,

    /// Maximum chunk size in characters for pragmatic text chunking.
    #[arg(long, default_value_t = DEFAULT_CHUNK_MAX_CHARS, env = "SPEECH_OUT_CHUNK_MAX_CHARS")]
    chunk_max_chars: usize,
}

#[derive(Debug, Parser)]
struct DaemonArgs {
    /// Socket address for the speech-out websocket daemon.
    #[arg(long, default_value = DEFAULT_SPEECH_OUT_BIND, env = "SPEECH_OUT_DAEMON_BIND")]
    bind: SocketAddr,

    /// Local supertonic serve URL. Defaults to native /v1/tts so steps/speed/lang are honored.
    #[arg(long, default_value = DEFAULT_SUPERTONIC_URL, env = "SPEECH_OUT_SUPERTONIC_URL")]
    supertonic_url: String,

    /// curl binary used to call Supertonic HTTP and stream response bytes to websocket clients.
    #[arg(long, default_value = "curl", env = "SPEECH_OUT_CURL_COMMAND")]
    curl_command: String,

    /// Assume a separate Supertonic server is already running. By default the daemon starts it on demand.
    #[arg(long, action = clap::ArgAction::SetTrue, env = "SPEECH_OUT_EXTERNAL_SUPERTONIC")]
    external_supertonic: bool,

    /// Command used when managing Supertonic on demand.
    #[arg(
        long,
        default_value = "supertonic",
        env = "SPEECH_OUT_SUPERTONIC_COMMAND"
    )]
    supertonic_command: String,

    /// Args for the managed Supertonic command. Defaults to: serve --host 127.0.0.1 --port 7788.
    #[arg(
        long = "supertonic-arg",
        env = "SPEECH_OUT_SUPERTONIC_ARGS",
        value_delimiter = ' '
    )]
    supertonic_args: Vec<String>,

    /// Milliseconds to wait after starting a managed Supertonic server before the first request.
    #[arg(
        long,
        default_value_t = 1500,
        env = "SPEECH_OUT_SUPERTONIC_STARTUP_GRACE_MS"
    )]
    supertonic_startup_grace_ms: u64,

    /// Keep managed Supertonic warm this many seconds after the last request. Default is 20 minutes.
    #[arg(long, default_value_t = DEFAULT_WARM_TTL_SECS, env = "SPEECH_OUT_WARM_TTL_SECS")]
    warm_ttl_secs: u64,

    /// Seconds without response bytes before one Supertonic request is killed.
    #[arg(long, default_value_t = 90, env = "SPEECH_OUT_TIMEOUT_SECS")]
    timeout_secs: u64,

    /// Minimum chunk size in characters for pragmatic text chunking.
    #[arg(long, default_value_t = DEFAULT_CHUNK_MIN_CHARS, env = "SPEECH_OUT_CHUNK_MIN_CHARS")]
    chunk_min_chars: usize,

    /// Maximum chunk size in characters for pragmatic text chunking.
    #[arg(long, default_value_t = DEFAULT_CHUNK_MAX_CHARS, env = "SPEECH_OUT_CHUNK_MAX_CHARS")]
    chunk_max_chars: usize,
}

#[derive(Debug, Parser)]
struct PlayArgs {
    /// Text to synthesize on the desktop/sfub daemon.
    text: Option<String>,

    /// Read utterance text from stdin.
    #[arg(long)]
    stdin: bool,

    /// Websocket URL of the speech-out daemon.
    #[arg(long, default_value = DEFAULT_SPEECH_OUT_WS_URL, env = "SPEECH_OUT_WS_URL")]
    url: String,

    /// Utterance id to use. Defaults to a UUID.
    #[arg(long, env = "SPEECH_OUT_UTTERANCE_ID")]
    utterance_id: Option<String>,

    /// Supertonic voice id or preset voice name.
    #[arg(long, default_value = DEFAULT_VOICE, env = "SPEECH_OUT_VOICE")]
    voice: String,

    /// Language code.
    #[arg(long, default_value = DEFAULT_LANG, env = "SPEECH_OUT_LANG")]
    lang: String,

    /// Supertonic diffusion steps.
    #[arg(long, default_value_t = DEFAULT_STEPS, env = "SPEECH_OUT_STEPS")]
    steps: u32,

    /// Speech speed multiplier.
    #[arg(long, default_value_t = DEFAULT_SPEED, env = "SPEECH_OUT_SPEED")]
    speed: f32,

    /// Optional Supertonic reference voice/audio/style handle.
    #[arg(long, env = "SPEECH_OUT_REFERENCE")]
    reference: Option<String>,

    /// Optional Supertonic style prompt/preset.
    #[arg(long, env = "SPEECH_OUT_STYLE")]
    style: Option<String>,

    /// Output WAV path. When omitted, chunks are piped to the playback command.
    #[arg(long, env = "SPEECH_OUT_OUTPUT")]
    output: Option<PathBuf>,

    /// Playback command for streamed WAV chunks.
    #[arg(long, default_value = "pw-play", env = "SPEECH_OUT_PLAY_COMMAND")]
    play_command: String,

    /// Playback argv. Defaults to '-' so pw-play reads streamed WAV bytes from stdin.
    #[arg(long = "play-arg", env = "SPEECH_OUT_PLAY_ARGS", value_delimiter = ' ')]
    play_args: Vec<String>,

    /// Minimum chunk size in characters for pragmatic text chunking. Sent to the daemon for observability/chunk planning.
    #[arg(long, default_value_t = DEFAULT_CHUNK_MIN_CHARS, env = "SPEECH_OUT_CHUNK_MIN_CHARS")]
    chunk_min_chars: usize,

    /// Maximum chunk size in characters for pragmatic text chunking. Sent to the daemon for observability/chunk planning.
    #[arg(long, default_value_t = DEFAULT_CHUNK_MAX_CHARS, env = "SPEECH_OUT_CHUNK_MAX_CHARS")]
    chunk_max_chars: usize,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, ValueEnum)]
enum BackendKind {
    /// Print the utterance request as JSON. Useful for testing pi/profile wiring.
    Mock,
    /// Run an external command; by default the utterance text is appended as argv.
    Command,
    /// Call a local supertonic `serve` HTTP endpoint with curl.
    SupertonicHttp,
}

#[derive(Debug, Clone)]
struct UtteranceRequest {
    text: String,
    voice: String,
    lang: String,
    output: Option<PathBuf>,
}

#[derive(Debug, Serialize)]
struct MockEvent<'a> {
    event: &'static str,
    backend: &'static str,
    text: &'a str,
    voice: &'a str,
    lang: &'a str,
    output: Option<&'a Path>,
}

#[derive(Debug, Serialize)]
struct SupertonicSpeechRequest<'a> {
    text: &'a str,
    voice: &'a str,
    lang: &'a str,
    steps: u32,
    speed: f32,
    response_format: &'static str,
    #[serde(skip_serializing_if = "Option::is_none")]
    reference: Option<&'a str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    style: Option<&'a str>,
}

#[derive(Debug, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
enum ClientMessage {
    Speak(SpeakRequest),
    Ping {
        mono_ns: Option<u64>,
    },
    PlaybackReady {
        utterance_id: Option<String>,
        text_chunk_index: usize,
        client_mono_ns: Option<u64>,
    },
    Cancel {
        utterance_id: Option<String>,
        reason: Option<String>,
    },
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct SpeakRequest {
    #[serde(default)]
    utterance_id: Option<String>,
    text: String,
    #[serde(default)]
    voice: Option<String>,
    #[serde(default)]
    lang: Option<String>,
    #[serde(default)]
    steps: Option<u32>,
    #[serde(default)]
    speed: Option<f32>,
    #[serde(default)]
    reference: Option<String>,
    #[serde(default)]
    style: Option<String>,
    #[serde(default)]
    chunk_min_chars: Option<usize>,
    #[serde(default)]
    chunk_max_chars: Option<usize>,
    #[serde(default)]
    playback_flow_control: Option<bool>,
}

#[derive(Debug, Clone)]
struct EffectiveSpeakRequest {
    utterance_id: String,
    text: String,
    voice: String,
    lang: String,
    steps: u32,
    speed: f32,
    reference: Option<String>,
    style: Option<String>,
    /// Pragmatic text chunks derived from the input text.
    text_chunks: Vec<String>,
    /// If true, synthesize the next text chunk only after the client reports
    /// that playback of the previous chunk has completed. This prevents long
    /// monologues from using Supertonic compute while the mic/ASR path needs to
    /// hear barge-in.
    playback_flow_control: bool,
}

impl EffectiveSpeakRequest {
    fn from_request(request: SpeakRequest, chunk_min: usize, chunk_max: usize) -> Result<Self> {
        let text = normalize_text(&request.text)?;
        let chunk_min = request.chunk_min_chars.unwrap_or(chunk_min);
        let chunk_max = request.chunk_max_chars.unwrap_or(chunk_max);
        let text_chunks = TextChunker::new(chunk_min, chunk_max).chunk(&text);
        Ok(Self {
            utterance_id: request
                .utterance_id
                .unwrap_or_else(|| Uuid::new_v4().to_string()),
            text,
            voice: request.voice.unwrap_or_else(|| DEFAULT_VOICE.to_owned()),
            lang: request.lang.unwrap_or_else(|| DEFAULT_LANG.to_owned()),
            steps: request.steps.unwrap_or(DEFAULT_STEPS),
            speed: request.speed.unwrap_or(DEFAULT_SPEED),
            reference: request.reference.filter(|value| !value.trim().is_empty()),
            style: request.style.filter(|value| !value.trim().is_empty()),
            text_chunks,
            playback_flow_control: request.playback_flow_control.unwrap_or(false),
        })
    }
}

// ---------------------------------------------------------------------------
// Pragmatic text chunker
// ---------------------------------------------------------------------------

/// A pragmatic, sentence-aware text chunker suitable for future sentence-level
/// TTS pipelining. Splits on sentence-ending punctuation (. ! ?) when possible,
/// falling back to whitespace breaks within the max chunk bound.
#[derive(Debug, Clone)]
pub struct TextChunker {
    min_chars: usize,
    max_chars: usize,
}

impl TextChunker {
    pub fn new(min_chars: usize, max_chars: usize) -> Self {
        Self {
            min_chars,
            max_chars: max_chars.max(min_chars + 1),
        }
    }

    /// Split `text` into pragmatic chunks.
    pub fn chunk(&self, text: &str) -> Vec<String> {
        let text = text.trim();
        if text.is_empty() {
            return Vec::new();
        }

        let mut chunks: Vec<String> = Vec::new();
        let mut start = 0;
        let len = text.len();

        while start < len {
            let remaining = len - start;
            if remaining <= self.max_chars {
                chunks.push(text[start..].trim().to_owned());
                break;
            }

            // Try to find a sentence boundary within [start + min_chars, start + max_chars].
            let search_start = (start + self.min_chars).min(len);
            let search_end = (start + self.max_chars).min(len);
            let slice = &text[search_start..search_end];

            let boundary = slice
                .char_indices()
                .filter(|&(_, ch)| matches!(ch, '.' | '!' | '?'))
                .find(|&(offset, _ch)| {
                    let abs_pos = search_start + offset;
                    text[abs_pos..].chars().nth(1).is_none_or(|next| {
                        next.is_whitespace()
                            || next.is_uppercase()
                            || matches!(next, '"' | '\'' | ')' | ']' | '}')
                    })
                });

            if let Some((offset, _)) = boundary {
                let abs_pos = search_start + offset;
                let end = abs_pos + 1;
                chunks.push(text[start..end].trim().to_owned());
                start = end;
                continue;
            }

            // No sentence boundary found — fall back to last whitespace within max_chars.
            let fallback_end = (start + self.max_chars).min(len);
            let fallback_slice = &text[start..fallback_end];
            if let Some(last_space) = fallback_slice
                .char_indices()
                .rev()
                .find(|&(_, ch)| ch.is_whitespace())
                .map(|(i, _)| start + i)
            {
                chunks.push(text[start..last_space].trim().to_owned());
                start = last_space + 1;
            } else {
                chunks.push(text[start..fallback_end].trim().to_owned());
                start = fallback_end;
            }
        }

        chunks.retain(|c| !c.is_empty());
        chunks
    }

    pub fn min_chars(&self) -> usize {
        self.min_chars
    }

    pub fn max_chars(&self) -> usize {
        self.max_chars
    }
}

// ---------------------------------------------------------------------------
// WAV header parsing helpers
// ---------------------------------------------------------------------------

/// Minimal WAV duration extractor from raw waveform bytes.
#[derive(Debug, Clone, Copy, Default)]
pub(crate) struct WavMetadata {
    pub sample_rate: u32,
    pub channels: u16,
    pub bits_per_sample: u16,
    pub data_bytes: u32,
}

impl WavMetadata {
    /// Parse WAV header from the beginning of the audio data.
    /// Returns `None` if insufficient bytes are available or the header is invalid.
    pub fn from_bytes(bytes: &[u8]) -> Option<Self> {
        if bytes.len() < WAV_HEADER_MIN_LEN {
            return None;
        }
        if &bytes[0..4] != b"RIFF" || &bytes[8..12] != b"WAVE" {
            return None;
        }
        let mut offset = 12;
        let mut sample_rate = 0u32;
        let mut channels = 0u16;
        let mut bits_per_sample = 0u16;
        let mut data_bytes = 0u32;

        while offset + 8 <= bytes.len() {
            let chunk_id = &bytes[offset..offset + 4];
            let chunk_size =
                u32::from_le_bytes(bytes[offset + 4..offset + 8].try_into().unwrap()) as usize;
            if chunk_id == WAV_FMT_CHUNK_ID {
                if offset + 8 + chunk_size > bytes.len() {
                    return None;
                }
                let fmt_data = &bytes[offset + 8..offset + 8 + chunk_size];
                if fmt_data.len() >= 16 {
                    channels = u16::from_le_bytes(fmt_data[2..4].try_into().ok()?);
                    sample_rate = u32::from_le_bytes(fmt_data[4..8].try_into().ok()?);
                    bits_per_sample = u16::from_le_bytes(fmt_data[14..16].try_into().ok()?);
                }
            } else if chunk_id == WAV_DATA_CHUNK_ID {
                data_bytes = u32::from_le_bytes(bytes[offset + 4..offset + 8].try_into().ok()?);
                break;
            }
            offset += 8 + chunk_size;
            if !chunk_size.is_multiple_of(2) {
                offset += 1;
            }
        }

        if sample_rate == 0 || data_bytes == 0 {
            return None;
        }
        Some(Self {
            sample_rate,
            channels,
            bits_per_sample,
            data_bytes,
        })
    }

    /// Audio duration in seconds.
    pub fn duration_secs(&self) -> f64 {
        let bytes_per_second =
            self.sample_rate as u64 * self.channels as u64 * (self.bits_per_sample as u64 / 8);
        if bytes_per_second == 0 {
            return 0.0;
        }
        self.data_bytes as f64 / bytes_per_second as f64
    }
}

// ---------------------------------------------------------------------------
// Timing metrics accumulator
// ---------------------------------------------------------------------------

#[derive(Debug, Clone)]
pub(crate) struct SynthesisMetrics {
    pub request_received_mono_ns: u64,
    pub synthesis_started_mono_ns: Option<u64>,
    pub first_audio_mono_ns: Option<u64>,
    pub completed_mono_ns: Option<u64>,
    pub total_bytes: usize,
    pub chunk_count: u64,
    pub accumulated_audio: Vec<u8>,
}

impl SynthesisMetrics {
    pub fn new(request_received_mono_ns: u64) -> Self {
        Self {
            request_received_mono_ns,
            synthesis_started_mono_ns: None,
            first_audio_mono_ns: None,
            completed_mono_ns: None,
            total_bytes: 0,
            chunk_count: 0,
            accumulated_audio: Vec::new(),
        }
    }

    pub fn request_received_to_synthesis_started_ms(&self) -> Option<f64> {
        let started = self.synthesis_started_mono_ns?;
        Some(ns_to_ms(
            started.saturating_sub(self.request_received_mono_ns),
        ))
    }

    pub fn synthesis_started_to_first_audio_ms(&self) -> Option<f64> {
        let started = self.synthesis_started_mono_ns?;
        let first = self.first_audio_mono_ns?;
        Some(ns_to_ms(first.saturating_sub(started)))
    }

    pub fn total_synthesis_duration_ms(&self) -> Option<f64> {
        let started = self.synthesis_started_mono_ns?;
        let completed = self.completed_mono_ns?;
        Some(ns_to_ms(completed.saturating_sub(started)))
    }

    /// Compute audio duration from accumulated WAV bytes.
    pub fn audio_duration_secs(&self) -> Option<f64> {
        let meta = WavMetadata::from_bytes(&self.accumulated_audio)?;
        Some(meta.duration_secs())
    }

    /// Compute realtime factor: audio_duration / wall_clock_synthesis_duration.
    pub fn realtime_factor(&self) -> Option<f64> {
        let audio_secs = self.audio_duration_secs()?;
        let synth_ms = self.total_synthesis_duration_ms()?;
        if synth_ms <= 0.0 {
            return None;
        }
        Some(audio_secs / (synth_ms / 1000.0))
    }
}

fn ns_to_ms(ns: u64) -> f64 {
    ns as f64 / 1_000_000.0
}

struct SupertonicManager {
    external: bool,
    command: String,
    args: Vec<String>,
    startup_grace: Duration,
    warm_ttl: Duration,
    child: Option<Child>,
    last_used: Option<Instant>,
}

impl SupertonicManager {
    fn new(args: &DaemonArgs) -> Self {
        let supertonic_args = if args.supertonic_args.is_empty() {
            vec![
                "serve".to_owned(),
                "--host".to_owned(),
                "127.0.0.1".to_owned(),
                "--port".to_owned(),
                "7788".to_owned(),
            ]
        } else {
            args.supertonic_args.clone()
        };
        Self {
            external: args.external_supertonic,
            command: args.supertonic_command.clone(),
            args: supertonic_args,
            startup_grace: Duration::from_millis(args.supertonic_startup_grace_ms),
            warm_ttl: Duration::from_secs(args.warm_ttl_secs),
            child: None,
            last_used: None,
        }
    }

    async fn ensure_warm(&mut self) -> Result<()> {
        self.last_used = Some(Instant::now());
        if self.external {
            return Ok(());
        }
        if let Some(child) = &mut self.child {
            if child.try_wait()?.is_none() {
                debug!("managed supertonic is already warm");
                return Ok(());
            }
            warn!("managed supertonic exited; restarting for next request");
            self.child = None;
        }
        info!(command = %self.command, args = ?self.args, "starting managed supertonic server");
        let child = Command::new(&self.command)
            .args(&self.args)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .with_context(|| format!("starting managed Supertonic command `{}`", self.command))?;
        self.child = Some(child);
        tokio::time::sleep(self.startup_grace).await;
        Ok(())
    }

    async fn reap_if_idle(&mut self) {
        if self.external {
            return;
        }
        let Some(last_used) = self.last_used else {
            return;
        };
        if last_used.elapsed() < self.warm_ttl {
            return;
        }
        if let Some(mut child) = self.child.take() {
            info!(
                idle_secs = self.warm_ttl.as_secs(),
                "stopping idle managed supertonic server"
            );
            if let Err(err) = child.kill().await {
                warn!(error = ?err, "failed to kill idle managed supertonic server");
            }
            let _ = child.wait().await;
        }
        self.last_used = None;
    }
}

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "speech_out=info".into()),
        )
        .init();

    let cli = Cli::parse();
    match cli.command {
        SpeechOutCommand::Say(args) => run_say(args).await,
        SpeechOutCommand::Daemon(args) => run_daemon(args).await,
        SpeechOutCommand::Play(args) => run_play(args).await,
    }
}

async fn run_say(args: SayArgs) -> Result<()> {
    let text = read_text(args.text.as_deref(), args.stdin).await?;
    let request = UtteranceRequest {
        text,
        voice: args.voice.clone(),
        lang: args.lang.clone(),
        output: args.output.clone(),
    };

    match args.backend {
        BackendKind::Mock => run_mock(&request).await,
        BackendKind::Command => run_command(&args, &request).await,
        BackendKind::SupertonicHttp => run_supertonic_http(&args, &request).await,
    }
}

async fn read_text(text: Option<&str>, stdin: bool) -> Result<String> {
    match (text, stdin) {
        (Some(_), true) => bail!("pass either positional text or --stdin, not both"),
        (Some(text), false) => normalize_text(text),
        (None, true) => {
            let mut input = String::new();
            tokio::io::stdin()
                .read_to_string(&mut input)
                .await
                .context("reading utterance text from stdin")?;
            normalize_text(&input)
        }
        (None, false) => bail!("missing utterance text; pass text or --stdin"),
    }
}

fn normalize_text(text: &str) -> Result<String> {
    let text = text.trim().to_owned();
    if text.is_empty() {
        bail!("utterance text is empty");
    }
    Ok(text)
}

async fn run_mock(request: &UtteranceRequest) -> Result<()> {
    let event = MockEvent {
        event: "speech_out_utterance",
        backend: "mock",
        text: &request.text,
        voice: &request.voice,
        lang: &request.lang,
        output: request.output.as_deref(),
    };
    println!("{}", serde_json::to_string(&event)?);
    Ok(())
}

async fn run_command(args: &SayArgs, request: &UtteranceRequest) -> Result<()> {
    let command = args
        .command
        .as_deref()
        .ok_or_else(|| anyhow!("--backend command requires --command or SPEECH_OUT_COMMAND"))?;
    info!(%command, "running speech-out command backend");
    let mut cmd = Command::new(command);
    cmd.args(&args.command_args);
    if let Some(output) = &request.output {
        cmd.arg(output);
    }
    if args.command_stdin {
        cmd.stdin(Stdio::piped());
    } else {
        cmd.arg(&request.text);
    }

    let mut child = cmd
        .spawn()
        .with_context(|| format!("spawning speech-out command backend `{command}`"))?;
    if args.command_stdin {
        let mut stdin = child
            .stdin
            .take()
            .ok_or_else(|| anyhow!("failed to open stdin for command backend"))?;
        stdin
            .write_all(request.text.as_bytes())
            .await
            .context("writing utterance text to command backend stdin")?;
        stdin.shutdown().await.ok();
    }
    wait_with_timeout(child, args.timeout_secs, "command backend").await
}

async fn run_supertonic_http(args: &SayArgs, request: &UtteranceRequest) -> Result<()> {
    let body = supertonic_request_json(
        &request.text,
        &request.voice,
        &request.lang,
        args.steps,
        args.speed,
        args.reference.as_deref(),
        args.style.as_deref(),
    )?;

    let output = request.output.clone().unwrap_or_else(|| {
        std::env::temp_dir().join(format!("speech-out-{}.wav", std::process::id()))
    });

    info!(url = %args.supertonic_url, output = %output.display(), "calling local supertonic http backend");
    let mut curl = Command::new("curl");
    curl.args([
        "--fail",
        "--silent",
        "--show-error",
        "--location",
        "--connect-timeout",
        "1",
        "--retry",
        "20",
        "--retry-connrefused",
        "--retry-delay",
        "0",
        "--retry-max-time",
        "10",
        "--request",
        "POST",
        &args.supertonic_url,
        "--header",
        "Content-Type: application/json",
        "--data-binary",
        &body,
        "--output",
    ]);
    curl.arg(&output);
    wait_with_timeout(
        curl.spawn()
            .context("spawning curl for supertonic-http backend")?,
        args.timeout_secs,
        "supertonic http request",
    )
    .await?;

    if request.output.is_none() {
        let player = Command::new(&args.play_command)
            .arg(&output)
            .spawn()
            .with_context(|| format!("spawning playback command `{}`", args.play_command))?;
        wait_with_timeout(player, args.timeout_secs, "playback command").await?;
        tokio::fs::remove_file(&output).await.ok();
    } else {
        println!("{}", output.display());
    }
    Ok(())
}

fn supertonic_request_json(
    text: &str,
    voice: &str,
    lang: &str,
    steps: u32,
    speed: f32,
    reference: Option<&str>,
    style: Option<&str>,
) -> Result<String> {
    Ok(serde_json::to_string(&SupertonicSpeechRequest {
        text,
        voice,
        lang,
        steps,
        speed,
        response_format: "wav",
        reference,
        style,
    })?)
}

async fn run_daemon(args: DaemonArgs) -> Result<()> {
    let listener = TcpListener::bind(args.bind)
        .await
        .with_context(|| format!("binding speech-out daemon on {}", args.bind))?;
    let manager = Arc::new(Mutex::new(SupertonicManager::new(&args)));
    spawn_warm_reaper(Arc::clone(&manager));
    spawn_initial_warmup(Arc::clone(&manager));
    let args = Arc::new(args);
    info!(bind = %args.bind, warm_ttl_secs = args.warm_ttl_secs, external_supertonic = args.external_supertonic, "speech-out daemon listening");

    loop {
        let (stream, peer) = listener
            .accept()
            .await
            .context("accepting speech-out client")?;
        let args = Arc::clone(&args);
        let manager = Arc::clone(&manager);
        tokio::spawn(async move {
            if let Err(err) = handle_daemon_connection(stream, peer, args, manager).await {
                error!(peer = %peer, error = ?err, "speech-out connection failed");
            }
        });
    }
}

fn spawn_warm_reaper(manager: Arc<Mutex<SupertonicManager>>) {
    tokio::spawn(async move {
        loop {
            tokio::time::sleep(Duration::from_secs(30)).await;
            manager.lock().await.reap_if_idle().await;
        }
    });
}

fn spawn_initial_warmup(manager: Arc<Mutex<SupertonicManager>>) {
    tokio::spawn(async move {
        if let Err(err) = manager.lock().await.ensure_warm().await {
            warn!(error = ?err, "initial Supertonic warm-up failed");
        }
    });
}

async fn handle_daemon_connection(
    stream: TcpStream,
    peer: SocketAddr,
    args: Arc<DaemonArgs>,
    manager: Arc<Mutex<SupertonicManager>>,
) -> Result<()> {
    let mut ws = accept_async(stream)
        .await
        .with_context(|| format!("accepting websocket from {peer}"))?;
    info!(peer = %peer, "speech-out websocket connected");

    while let Some(message) = ws.next().await {
        match message? {
            Message::Text(text) => match serde_json::from_str::<ClientMessage>(&text) {
                Ok(ClientMessage::Speak(request)) => {
                    let request = EffectiveSpeakRequest::from_request(
                        request,
                        args.chunk_min_chars,
                        args.chunk_max_chars,
                    )?;
                    handle_speak_request(&mut ws, &args, &manager, request).await?;
                }
                Ok(ClientMessage::Ping { mono_ns }) => {
                    send_json(
                        &mut ws,
                        json!({
                            "event": "speech_out_pong",
                            "client_mono_ns": mono_ns,
                            "daemon_mono_ns": now_mono_ns(),
                        }),
                    )
                    .await?;
                }
                Ok(ClientMessage::PlaybackReady { .. }) => {
                    send_json(
                        &mut ws,
                        json!({
                            "event": "speech_out_error",
                            "daemon_mono_ns": now_mono_ns(),
                            "message": "playback_ready received without active speak request",
                        }),
                    )
                    .await?;
                }
                Ok(ClientMessage::Cancel { .. }) => {
                    send_json(
                        &mut ws,
                        json!({
                            "event": "speech_out_cancel_ack",
                            "daemon_mono_ns": now_mono_ns(),
                        }),
                    )
                    .await?;
                }
                Err(err) => {
                    send_json(
                        &mut ws,
                        json!({
                            "event": "speech_out_error",
                            "daemon_mono_ns": now_mono_ns(),
                            "message": format!("invalid client message: {err}"),
                        }),
                    )
                    .await?;
                }
            },
            Message::Close(_) => break,
            Message::Ping(bytes) => ws.send(Message::Pong(bytes)).await?,
            _ => {}
        }
    }
    Ok(())
}

async fn handle_speak_request(
    ws: &mut WebSocketStream<TcpStream>,
    args: &DaemonArgs,
    manager: &Arc<Mutex<SupertonicManager>>,
    request: EffectiveSpeakRequest,
) -> Result<()> {
    let request_received_mono_ns = now_mono_ns();

    let num_chunks = request.text_chunks.len();
    let chunk_sizes: Vec<usize> = request.text_chunks.iter().map(|c| c.len()).collect();

    send_json(
        ws,
        json!({
            "event": "speech_out_request_received",
            "utterance_id": request.utterance_id,
            "text": request.text,
            "voice": request.voice,
            "lang": request.lang,
            "steps": request.steps,
            "speed": request.speed,
            "reference": request.reference,
            "style": request.style,
            "daemon_mono_ns": request_received_mono_ns,
            "num_chunks": num_chunks,
            "chunk_sizes": chunk_sizes,
            "playback_flow_control": request.playback_flow_control,
        }),
    )
    .await?;

    // Emit a text_chunks event showing how pragmatically chunked for future pipelining.
    send_json(
        ws,
        json!({
            "event": "speech_out_text_chunks",
            "utterance_id": request.utterance_id,
            "num_chunks": num_chunks,
            "chunks": request.text_chunks,
            "daemon_mono_ns": now_mono_ns(),
        }),
    )
    .await?;

    manager.lock().await.ensure_warm().await?;

    let synthesis_started_mono_ns = now_mono_ns();
    let mut metrics = SynthesisMetrics::new(request_received_mono_ns);
    metrics.synthesis_started_mono_ns = Some(synthesis_started_mono_ns);

    send_json(
        ws,
        json!({
            "event": "speech_out_synthesis_started",
            "utterance_id": request.utterance_id,
            "backend": "supertonic-http",
            "supertonic_url": args.supertonic_url,
            "streaming_mode": "text_chunked_http_responses",
            "fallback_note": "Supertonic buffers each chunk internally, but speech-out now sends one Supertonic request per text chunk so first audio is gated by the first chunk, not the whole paragraph.",
            "daemon_mono_ns": synthesis_started_mono_ns,
            "request_received_to_synthesis_started_ms": metrics.request_received_to_synthesis_started_ms(),
        }),
    )
    .await?;

    match stream_supertonic_response(ws, args, &request, &mut metrics).await {
        Ok(total_bytes) => {
            let completed_mono_ns = now_mono_ns();
            metrics.completed_mono_ns = Some(completed_mono_ns);
            metrics.total_bytes = total_bytes;

            let mut completed = json!({
                "event": "speech_out_completed",
                "utterance_id": request.utterance_id,
                "bytes": total_bytes,
                "chunk_count": metrics.chunk_count,
                "daemon_mono_ns": completed_mono_ns,
                "total_synthesis_duration_ms": metrics.total_synthesis_duration_ms(),
            });

            if let Some(audio_duration) = metrics.audio_duration_secs() {
                let realtime = metrics.realtime_factor();
                completed["audio_duration_secs"] = json!(audio_duration);
                completed["realtime_factor"] = json!(realtime);
            }

            send_json(ws, completed).await?;
            manager.lock().await.last_used = Some(Instant::now());
            Ok(())
        }
        Err(err) => {
            let _ = send_json(
                ws,
                json!({
                    "event": "speech_out_failed",
                    "utterance_id": request.utterance_id,
                    "message": err.to_string(),
                    "daemon_mono_ns": now_mono_ns(),
                }),
            )
            .await;
            Err(err)
        }
    }
}

async fn wait_for_playback_ready(
    ws: &mut WebSocketStream<TcpStream>,
    request: &EffectiveSpeakRequest,
    text_chunk_index: usize,
    timeout_secs: u64,
) -> Result<()> {
    let started = now_mono_ns();
    send_json(
        ws,
        json!({
            "event": "speech_out_playback_gate_wait",
            "utterance_id": request.utterance_id,
            "text_chunk_index": text_chunk_index,
            "daemon_mono_ns": started,
        }),
    )
    .await?;
    let timeout = Duration::from_secs(timeout_secs);
    loop {
        let msg = tokio::time::timeout(timeout, ws.next()).await;
        let msg = match msg {
            Ok(Some(msg)) => msg?,
            Ok(None) => bail!("speech-out client disconnected while waiting for playback_ready"),
            Err(_) => {
                bail!("timed out waiting for playback_ready for text chunk {text_chunk_index}")
            }
        };
        match msg {
            Message::Text(text) => match serde_json::from_str::<ClientMessage>(&text) {
                Ok(ClientMessage::PlaybackReady {
                    utterance_id,
                    text_chunk_index: ready_index,
                    client_mono_ns,
                }) => {
                    if utterance_id
                        .as_deref()
                        .is_some_and(|id| id != request.utterance_id)
                    {
                        continue;
                    }
                    if ready_index != text_chunk_index {
                        continue;
                    }
                    send_json(
                        ws,
                        json!({
                            "event": "speech_out_playback_gate_released",
                            "utterance_id": request.utterance_id,
                            "text_chunk_index": text_chunk_index,
                            "client_mono_ns": client_mono_ns,
                            "daemon_mono_ns": now_mono_ns(),
                            "wait_ms": ns_to_ms(now_mono_ns().saturating_sub(started)),
                        }),
                    )
                    .await?;
                    return Ok(());
                }
                Ok(ClientMessage::Cancel {
                    utterance_id,
                    reason,
                }) => {
                    if utterance_id
                        .as_deref()
                        .is_none_or(|id| id == request.utterance_id)
                    {
                        bail!(
                            "speech-out utterance cancelled: {}",
                            reason.unwrap_or_else(|| "client_cancel".to_owned())
                        );
                    }
                }
                Ok(ClientMessage::Ping { mono_ns }) => {
                    send_json(
                        ws,
                        json!({
                            "event": "speech_out_pong",
                            "client_mono_ns": mono_ns,
                            "daemon_mono_ns": now_mono_ns(),
                        }),
                    )
                    .await?;
                }
                Ok(ClientMessage::Speak(_)) => {
                    bail!("received nested speak while waiting for playback_ready");
                }
                Err(err) => {
                    warn!(error = ?err, "ignoring invalid client message while waiting for playback_ready");
                }
            },
            Message::Close(_) => bail!("speech-out client closed while waiting for playback_ready"),
            Message::Ping(bytes) => ws.send(Message::Pong(bytes)).await?,
            _ => {}
        }
    }
}

async fn stream_supertonic_response(
    ws: &mut WebSocketStream<TcpStream>,
    args: &DaemonArgs,
    request: &EffectiveSpeakRequest,
    metrics: &mut SynthesisMetrics,
) -> Result<usize> {
    let chunks = if request.text_chunks.is_empty() {
        vec![request.text.clone()]
    } else {
        request.text_chunks.clone()
    };
    let mut total = 0usize;
    let mut global_seq = 0u64;
    for (text_chunk_index, text_chunk) in chunks.iter().enumerate() {
        let started = now_mono_ns();
        send_json(
            ws,
            json!({
                "event": "speech_out_text_chunk_started",
                "utterance_id": request.utterance_id,
                "text_chunk_index": text_chunk_index,
                "text_chunk_count": chunks.len(),
                "text": text_chunk,
                "chars": text_chunk.len(),
                "daemon_mono_ns": started,
            }),
        )
        .await?;

        let before = total;
        let chunk_bytes = stream_one_supertonic_text_chunk(
            ws,
            args,
            request,
            text_chunk,
            text_chunk_index,
            chunks.len(),
            &mut total,
            &mut global_seq,
            metrics,
        )
        .await?;

        send_json(
            ws,
            json!({
                "event": "speech_out_text_chunk_completed",
                "utterance_id": request.utterance_id,
                "text_chunk_index": text_chunk_index,
                "text_chunk_count": chunks.len(),
                "bytes": chunk_bytes,
                "total_bytes": total,
                "daemon_mono_ns": now_mono_ns(),
                "text_chunk_synthesis_duration_ms": ns_to_ms(now_mono_ns().saturating_sub(started)),
            }),
        )
        .await?;
        // Interactive playback uses one-chunk lookahead: while the client plays
        // chunk N, the daemon may synthesize and send chunk N+1, but it must not
        // start chunk N+2 until playback of N is acknowledged. This avoids the
        // no-prefetch gap between chunks while bounding Supertonic CPU pressure
        // so ASR/barge-in does not get hammered by a whole monologue being
        // synthesized ahead of time.
        if request.playback_flow_control && text_chunk_index > 0 {
            wait_for_playback_ready(ws, request, text_chunk_index - 1, args.timeout_secs).await?;
        }
        debug_assert_eq!(chunk_bytes, total.saturating_sub(before));
    }
    if total == 0 {
        bail!("Supertonic returned zero response bytes");
    }
    Ok(total)
}

#[allow(clippy::too_many_arguments)]
async fn stream_one_supertonic_text_chunk(
    ws: &mut WebSocketStream<TcpStream>,
    args: &DaemonArgs,
    request: &EffectiveSpeakRequest,
    text: &str,
    text_chunk_index: usize,
    text_chunk_count: usize,
    total: &mut usize,
    global_seq: &mut u64,
    metrics: &mut SynthesisMetrics,
) -> Result<usize> {
    let body = supertonic_request_json(
        text,
        &request.voice,
        &request.lang,
        request.steps,
        request.speed,
        request.reference.as_deref(),
        request.style.as_deref(),
    )?;

    let mut curl = Command::new(&args.curl_command);
    curl.args([
        "--fail",
        "--silent",
        "--show-error",
        "--location",
        "--connect-timeout",
        "1",
        "--retry",
        "20",
        "--retry-connrefused",
        "--retry-delay",
        "0",
        "--retry-max-time",
        "10",
        "--request",
        "POST",
        &args.supertonic_url,
        "--header",
        "Content-Type: application/json",
        "--data-binary",
        &body,
        "--output",
        "-",
    ])
    .stdin(Stdio::null())
    .stdout(Stdio::piped())
    .stderr(Stdio::piped());

    let mut child = curl
        .spawn()
        .with_context(|| format!("spawning `{}` for Supertonic request", args.curl_command))?;
    let mut stdout = child
        .stdout
        .take()
        .ok_or_else(|| anyhow!("failed to capture curl stdout"))?;
    let mut chunk = vec![0u8; 16 * 1024];
    let mut text_chunk_bytes = 0usize;
    let mut text_chunk_seq = 0u64;
    let timeout = Duration::from_secs(args.timeout_secs);

    loop {
        let read = tokio::time::timeout(timeout, stdout.read(&mut chunk)).await;
        let n = match read {
            Ok(Ok(n)) => n,
            Ok(Err(err)) => {
                let _ = child.kill().await;
                return Err(err).context("reading Supertonic response bytes");
            }
            Err(_) => {
                let _ = child.kill().await;
                bail!(
                    "Supertonic response timed out after {}s without bytes",
                    args.timeout_secs
                );
            }
        };
        if n == 0 {
            break;
        }
        *total += n;
        text_chunk_bytes += n;
        let now = now_mono_ns();
        metrics.total_bytes = *total;
        metrics.chunk_count = global_seq.saturating_add(1);
        if *global_seq == 0 {
            metrics.first_audio_mono_ns = Some(now);
        }
        if metrics.accumulated_audio.len() < 1024 * 1024 {
            metrics.accumulated_audio.extend_from_slice(&chunk[..n]);
        }

        let mut chunk_event = json!({
            "event": "speech_out_audio_chunk",
            "utterance_id": request.utterance_id,
            "seq": *global_seq,
            "text_chunk_index": text_chunk_index,
            "text_chunk_count": text_chunk_count,
            "text_chunk_seq": text_chunk_seq,
            "bytes": n,
            "text_chunk_bytes": text_chunk_bytes,
            "total_bytes": *total,
            "format": "wav",
            "daemon_mono_ns": now,
        });

        if *global_seq == 0 {
            if let Some(started_to_first) = metrics.synthesis_started_to_first_audio_ms() {
                chunk_event["synthesis_started_to_first_audio_ms"] = json!(started_to_first);
            }
        }

        send_json(ws, chunk_event).await?;
        ws.send(Message::Binary(chunk[..n].to_vec())).await?;
        *global_seq = global_seq.saturating_add(1);
        text_chunk_seq = text_chunk_seq.saturating_add(1);
    }

    let status = tokio::time::timeout(timeout, child.wait()).await;
    let status = match status {
        Ok(status) => status.context("waiting for Supertonic curl process")?,
        Err(_) => {
            let _ = child.kill().await;
            bail!("Supertonic curl process timed out after response stream ended");
        }
    };
    if !status.success() {
        let stderr = read_child_stderr(&mut child).await.unwrap_or_default();
        bail!("Supertonic curl exited with {status}: {stderr}");
    }
    if text_chunk_bytes == 0 {
        bail!("Supertonic returned zero response bytes for text chunk {text_chunk_index}");
    }
    Ok(text_chunk_bytes)
}

async fn read_child_stderr(child: &mut Child) -> Result<String> {
    let mut stderr_text = String::new();
    if let Some(mut stderr) = child.stderr.take() {
        stderr.read_to_string(&mut stderr_text).await.ok();
    }
    Ok(stderr_text)
}

async fn send_json(ws: &mut WebSocketStream<TcpStream>, value: serde_json::Value) -> Result<()> {
    ws.send(Message::Text(serde_json::to_string(&value)?))
        .await?;
    Ok(())
}

async fn run_play(args: PlayArgs) -> Result<()> {
    let text = read_text(args.text.as_deref(), args.stdin).await?;
    let request = SpeakRequest {
        utterance_id: Some(
            args.utterance_id
                .clone()
                .unwrap_or_else(|| Uuid::new_v4().to_string()),
        ),
        text,
        voice: Some(args.voice.clone()),
        lang: Some(args.lang.clone()),
        steps: Some(args.steps),
        speed: Some(args.speed),
        reference: args.reference.clone(),
        style: args.style.clone(),
        chunk_min_chars: Some(args.chunk_min_chars),
        chunk_max_chars: Some(args.chunk_max_chars),
        playback_flow_control: Some(args.output.is_none()),
    };
    let utterance_id = request.utterance_id.clone().unwrap_or_default();
    let (mut ws, _) = connect_async(&args.url)
        .await
        .with_context(|| format!("connecting to speech-out daemon {}", args.url))?;
    ws.send(Message::Text(serde_json::to_string(&json!({
        "type": "speak",
        "utterance_id": request.utterance_id,
        "text": request.text,
        "voice": request.voice,
        "lang": request.lang,
        "steps": request.steps,
        "speed": request.speed,
        "reference": request.reference,
        "style": request.style,
        "chunk_min_chars": request.chunk_min_chars,
        "chunk_max_chars": request.chunk_max_chars,
        "playback_flow_control": request.playback_flow_control,
    }))?))
    .await?;

    let mut output_file: Option<Box<dyn AsyncWrite + Unpin + Send>> =
        if let Some(output_path) = &args.output {
            Some(Box::new(File::create(output_path).await.with_context(
                || format!("creating {}", output_path.display()),
            )?))
        } else {
            None
        };
    let playback_config = PlaybackConfig {
        command: args.play_command.clone(),
        args: args.play_args.clone(),
    };
    let mut playback_chunk: Vec<u8> = Vec::new();

    while let Some(message) = ws.next().await {
        match message? {
            Message::Text(text) => {
                eprintln!("{text}");
                if let Ok(value) = serde_json::from_str::<serde_json::Value>(&text) {
                    match value.get("event").and_then(|v| v.as_str()) {
                        Some("speech_out_text_chunk_completed") if output_file.is_none() => {
                            let text_chunk_index = value
                                .get("text_chunk_index")
                                .and_then(|v| v.as_u64())
                                .unwrap_or(0)
                                as usize;
                            if !playback_chunk.is_empty() {
                                play_chunk_or_report(
                                    &playback_config,
                                    text_chunk_index as u64,
                                    std::mem::take(&mut playback_chunk),
                                )
                                .await?;
                            }
                            ws.send(Message::Text(serde_json::to_string(&json!({
                                "type": "playback_ready",
                                "utterance_id": utterance_id,
                                "text_chunk_index": text_chunk_index,
                                "client_mono_ns": now_mono_ns(),
                            }))?))
                            .await
                            .context("sending playback_ready to speech-out daemon")?;
                        }
                        Some("speech_out_completed") => {
                            if output_file.is_none() && !playback_chunk.is_empty() {
                                play_chunk_or_report(
                                    &playback_config,
                                    0,
                                    std::mem::take(&mut playback_chunk),
                                )
                                .await?;
                            }
                            break;
                        }
                        Some("speech_out_failed") => {
                            bail!(
                                "speech-out request failed: {}",
                                value
                                    .get("message")
                                    .and_then(|v| v.as_str())
                                    .unwrap_or("unknown error")
                            );
                        }
                        _ => {}
                    }
                }
            }
            Message::Binary(bytes) => {
                if let Some(output) = output_file.as_mut() {
                    output.write_all(&bytes).await.with_context(|| {
                        format!("writing audio chunk for utterance {utterance_id}")
                    })?;
                } else {
                    playback_chunk.extend_from_slice(&bytes);
                }
            }
            Message::Close(_) => break,
            _ => {}
        }
    }
    if let Some(mut output) = output_file {
        output.shutdown().await.ok();
    }
    if !playback_chunk.is_empty() {
        play_chunk_or_report(&playback_config, 0, playback_chunk).await?;
    }
    Ok(())
}

#[derive(Debug, Clone)]
struct PlaybackConfig {
    command: String,
    args: Vec<String>,
}

async fn play_chunk_or_report(
    config: &PlaybackConfig,
    playback_seq: u64,
    bytes: Vec<u8>,
) -> Result<()> {
    if let Err(err) = play_bytes_sequentially(config, playback_seq, bytes).await {
        eprintln!(
            "{}",
            serde_json::to_string(&json!({
                "event": "speech_out_playback_failed",
                "playback_seq": playback_seq,
                "message": err.to_string(),
                "client_mono_ns": now_mono_ns(),
            }))?
        );
        warn!(error = ?err, playback_seq, "speech-out playback failed");
    }
    Ok(())
}

async fn play_bytes_sequentially(
    config: &PlaybackConfig,
    playback_seq: u64,
    bytes: Vec<u8>,
) -> Result<()> {
    let utterance_temp = Uuid::new_v4().to_string();
    let path = std::env::temp_dir().join(format!("speech-out-{utterance_temp}.wav"));
    let byte_count = bytes.len();
    tokio::fs::write(&path, bytes)
        .await
        .with_context(|| format!("writing temporary speech-out wav {}", path.display()))?;
    eprintln!(
        "{}",
        serde_json::to_string(&json!({
            "event": "speech_out_playback_started",
            "playback_seq": playback_seq,
            "bytes": byte_count,
            "play_command": config.command,
            "path": path,
            "client_mono_ns": now_mono_ns(),
        }))?
    );
    let mut cmd = Command::new(&config.command);
    if config.args.is_empty() {
        cmd.arg(&path);
    } else {
        cmd.args(&config.args).arg(&path);
    }
    let started = now_mono_ns();
    let result = async {
        cmd.stderr(Stdio::piped());
        let output = cmd
            .output()
            .await
            .with_context(|| format!("running playback command `{}`", config.command))?;
        if !output.status.success() {
            let stderr = String::from_utf8_lossy(&output.stderr).trim().to_owned();
            if stderr.is_empty() {
                bail!("playback command exited with {}", output.status);
            } else {
                bail!("playback command exited with {}: {}", output.status, stderr);
            }
        }
        Ok::<_, anyhow::Error>(())
    }
    .await;
    let _ = tokio::fs::remove_file(&path).await;
    result?;
    eprintln!(
        "{}",
        serde_json::to_string(&json!({
            "event": "speech_out_playback_completed",
            "playback_seq": playback_seq,
            "bytes": byte_count,
            "playback_duration_ms": ns_to_ms(now_mono_ns().saturating_sub(started)),
            "client_mono_ns": now_mono_ns(),
        }))?
    );
    Ok(())
}

async fn wait_with_timeout(
    mut child: tokio::process::Child,
    timeout_secs: u64,
    label: &str,
) -> Result<()> {
    let status = match tokio::time::timeout(
        std::time::Duration::from_secs(timeout_secs),
        child.wait(),
    )
    .await
    {
        Ok(status) => status.with_context(|| format!("waiting for {label}"))?,
        Err(_) => {
            child.kill().await.ok();
            bail!("{label} timed out after {timeout_secs}s and was killed");
        }
    };
    if !status.success() {
        bail!("{label} exited with {status}");
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn normalize_text_trims_and_rejects_empty() {
        assert_eq!(normalize_text("  hello there  ").unwrap(), "hello there");
        assert!(normalize_text("  \n\t  ").is_err());
    }

    #[tokio::test]
    async fn positional_and_stdin_conflict() {
        assert!(read_text(Some("hello"), true).await.is_err());
    }

    #[test]
    fn supertonic_payload_uses_native_shape_with_controls() {
        let value: serde_json::Value = serde_json::from_str(
            &supertonic_request_json("hello", "M1", "en", 5, 1.15, Some("ref-a"), Some("calm"))
                .unwrap(),
        )
        .unwrap();
        assert_eq!(value["text"], "hello");
        assert_eq!(value["voice"], "M1");
        assert_eq!(value["lang"], "en");
        assert_eq!(value["steps"], 5);
        assert_eq!(value["speed"], 1.15);
        assert_eq!(value["response_format"], "wav");
        assert_eq!(value["reference"], "ref-a");
        assert_eq!(value["style"], "calm");
    }

    #[test]
    fn effective_speak_request_applies_interactive_defaults() {
        let request = EffectiveSpeakRequest::from_request(
            SpeakRequest {
                utterance_id: Some("u".into()),
                text: " heard you. ".into(),
                voice: None,
                lang: None,
                steps: None,
                speed: None,
                reference: None,
                style: None,
                chunk_min_chars: None,
                chunk_max_chars: None,
                playback_flow_control: None,
            },
            DEFAULT_CHUNK_MIN_CHARS,
            DEFAULT_CHUNK_MAX_CHARS,
        )
        .unwrap();
        assert_eq!(request.utterance_id, "u");
        assert_eq!(request.text, "heard you.");
        assert_eq!(request.voice, DEFAULT_VOICE);
        assert_eq!(request.lang, DEFAULT_LANG);
        assert_eq!(request.steps, DEFAULT_STEPS);
        assert_eq!(request.speed, DEFAULT_SPEED);
    }

    // ── TextChunker tests ─────────────────────────────────────────────

    #[test]
    fn text_chunker_empty_input_yields_no_chunks() {
        let chunker = TextChunker::new(20, 500);
        assert!(chunker.chunk("").is_empty());
        assert!(chunker.chunk("   ").is_empty());
    }

    #[test]
    fn text_chunker_single_short_sentence_stays_one_chunk() {
        let chunker = TextChunker::new(20, 500);
        let chunks = chunker.chunk("Hello world.");
        assert_eq!(chunks, vec!["Hello world."]);
    }

    #[test]
    fn text_chunker_splits_on_sentence_boundary() {
        // max_chars=32 forces splits: S1=30c, S2+S3=33c > 32.
        let chunker = TextChunker::new(5, 32);
        let text = "This is the first sentence. Here is another one. And a third.";
        let chunks = chunker.chunk(text);
        assert_eq!(chunks.len(), 3, "got {chunks:?}");
        assert_eq!(chunks[0], "This is the first sentence.");
        assert_eq!(chunks[1], "Here is another one.");
        assert_eq!(chunks[2], "And a third.");
    }

    #[test]
    fn text_chunker_falls_back_to_whitespace_when_no_sentence_boundary() {
        let chunker = TextChunker::new(5, 20);
        let text = "This is a long run of text that has no sentence boundaries within the limit";
        let chunks = chunker.chunk(text);
        assert!(chunks.len() >= 2);
        for chunk in &chunks {
            assert!(chunk.len() <= 25, "{chunk:?} is too long");
        }
    }

    #[test]
    fn text_chunker_respects_min_chunk() {
        // With a high min, short leading sentences merge into one chunk.
        let chunker = TextChunker::new(50, 200);
        let text = "Short. Another short. This is a much longer sentence that should trigger the split somewhere.";
        let chunks = chunker.chunk(text);
        // The first two short sentences should merge rather than split.
        assert!(chunks.len() < 4, "expected fewer chunks: {chunks:?}");
    }

    #[test]
    fn text_chunker_handles_multiple_punctuation_and_exclamation() {
        // max_chars=28 forces splits between the short clauses.
        let chunker = TextChunker::new(3, 28);
        let text = "Wow! That is amazing. Really? I can't believe it.";
        let chunks = chunker.chunk(text);
        assert!(chunks.len() >= 3, "expected at least 3 chunks: {chunks:?}");
        assert!(chunks[0].ends_with('!') || chunks[0].ends_with('.'));
    }

    #[test]
    fn text_chunker_reports_config() {
        let chunker = TextChunker::new(30, 400);
        assert_eq!(chunker.min_chars(), 30);
        assert_eq!(chunker.max_chars(), 400);
    }

    #[test]
    fn text_chunker_enforces_min_lt_max() {
        let chunker = TextChunker::new(100, 50);
        assert!(chunker.max_chars() > chunker.min_chars());
    }

    // ── WavMetadata tests ────────────────────────────────────────────

    fn make_minimal_wav(sample_rate: u32, channels: u16, bits: u16, data_samples: u32) -> Vec<u8> {
        let data_bytes = data_samples * channels as u32 * (bits as u32 / 8);
        let riff_size = 36 + data_bytes;
        let mut wav = Vec::new();
        wav.extend_from_slice(b"RIFF");
        wav.extend_from_slice(&(riff_size as u32).to_le_bytes());
        wav.extend_from_slice(b"WAVE");
        // fmt chunk
        wav.extend_from_slice(b"fmt ");
        wav.extend_from_slice(&(16u32).to_le_bytes()); // chunk size
        wav.extend_from_slice(&(1u16).to_le_bytes()); // PCM
        wav.extend_from_slice(&channels.to_le_bytes());
        wav.extend_from_slice(&sample_rate.to_le_bytes());
        let byte_rate = sample_rate * channels as u32 * (bits as u32 / 8);
        wav.extend_from_slice(&byte_rate.to_le_bytes());
        let block_align = channels * (bits / 8);
        wav.extend_from_slice(&block_align.to_le_bytes());
        wav.extend_from_slice(&bits.to_le_bytes());
        // data chunk
        wav.extend_from_slice(b"data");
        wav.extend_from_slice(&data_bytes.to_le_bytes());
        // padding
        wav.resize(wav.len() + data_bytes as usize, 0);
        wav
    }

    #[test]
    fn wav_metadata_parses_standard_pcm() {
        let wav = make_minimal_wav(24000, 1, 16, 24000); // 1 second of mono 16-bit
        let meta = WavMetadata::from_bytes(&wav).unwrap();
        assert_eq!(meta.sample_rate, 24000);
        assert_eq!(meta.channels, 1);
        assert_eq!(meta.bits_per_sample, 16);
        assert_eq!(meta.data_bytes, 48000); // 24000 samples * 2 bytes
        assert!((meta.duration_secs() - 1.0).abs() < 0.001);
    }

    #[test]
    fn wav_metadata_insufficient_bytes_returns_none() {
        assert!(WavMetadata::from_bytes(&[0u8; 10]).is_none());
    }

    #[test]
    fn wav_metadata_bad_magic_returns_none() {
        assert!(WavMetadata::from_bytes(&[0u8; 44]).is_none());
    }

    #[test]
    fn wav_metadata_duration_zero_for_missing_data() {
        let meta = WavMetadata::default();
        assert_eq!(meta.duration_secs(), 0.0);
    }

    #[test]
    fn wav_metadata_multi_channel_duration() {
        // 48000 sample-frames * 2 channels * 2 bytes = 192000 bytes at 48000 Hz => 1.0 sec.
        let wav = make_minimal_wav(48000, 2, 16, 48000);
        let meta = WavMetadata::from_bytes(&wav).unwrap();
        assert_eq!(meta.channels, 2);
        assert_eq!(meta.sample_rate, 48000);
        assert_eq!(meta.data_bytes, 192000);
        assert!((meta.duration_secs() - 1.0).abs() < 0.001);
    }

    // ── SynthesisMetrics tests ────────────────────────────────────────

    #[test]
    fn synthesis_metrics_new_has_defaults() {
        let m = SynthesisMetrics::new(1_000_000);
        assert_eq!(m.request_received_mono_ns, 1_000_000);
        assert!(m.synthesis_started_mono_ns.is_none());
        assert!(m.first_audio_mono_ns.is_none());
        assert!(m.completed_mono_ns.is_none());
        assert_eq!(m.total_bytes, 0);
        assert_eq!(m.chunk_count, 0);
    }

    #[test]
    fn synthesis_metrics_timing_returns_none_when_incomplete() {
        let m = SynthesisMetrics::new(1_000_000);
        assert!(m.request_received_to_synthesis_started_ms().is_none());
        assert!(m.synthesis_started_to_first_audio_ms().is_none());
        assert!(m.total_synthesis_duration_ms().is_none());
        assert!(m.audio_duration_secs().is_none());
        assert!(m.realtime_factor().is_none());
    }

    #[test]
    fn synthesis_metrics_computes_intervals() {
        let mut m = SynthesisMetrics::new(1_000_000);
        m.synthesis_started_mono_ns = Some(3_000_000);
        m.first_audio_mono_ns = Some(7_000_000);
        m.completed_mono_ns = Some(12_000_000);

        let r2s = m.request_received_to_synthesis_started_ms().unwrap();
        assert!((r2s - 2.0).abs() < 0.001); // 3_000_000 - 1_000_000 = 2ms

        let s2f = m.synthesis_started_to_first_audio_ms().unwrap();
        assert!((s2f - 4.0).abs() < 0.001); // 7_000_000 - 3_000_000 = 4ms

        let total = m.total_synthesis_duration_ms().unwrap();
        assert!((total - 9.0).abs() < 0.001); // 12_000_000 - 3_000_000 = 9ms
    }
}
