//! speech-in mic adapter — captures system microphone audio via CPAL and streams
//! timestamped PCM frames to the daemon over websocket.

use anyhow::{anyhow, bail, Context, Result};
use clap::{Parser, ValueEnum};
use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
use cpal::{SampleFormat, StreamConfig};
use futures_util::{SinkExt, StreamExt};
use speech_core_protocol::{
    frame_duration_samples, now_mono_ns, AudioFrame, AudioFrameHeader, ClockComparability,
    ClockDomain, ControlMessage, PcmFormat, SourceKind, SourceSampleGap, TimestampProvenance,
    TimestampQuality,
};
use std::f32::consts::TAU;
use std::io::{Seek, SeekFrom, Write};
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::mpsc::{self, Receiver, SyncSender, TrySendError};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;
use tokio::net::TcpStream;
use tokio::time::{interval, MissedTickBehavior};
use tokio_tungstenite::tungstenite::Message;
use tokio_tungstenite::{connect_async, MaybeTlsStream, WebSocketStream};
use uuid::Uuid;

const DEFAULT_FRAME_MS: u32 = 20;

#[derive(Debug, Parser)]
#[command(
    author,
    version,
    about = "speech-core cpal/synthetic microphone adapter"
)]
struct Args {
    /// Websocket URL for daemon audio ingress.
    #[arg(
        long,
        default_value = "ws://127.0.0.1:8765/ws/audio-ingress",
        env = "SPEECH_CORE_WS_URL"
    )]
    url: String,

    /// Stable adapter id to place in frame headers.
    #[arg(long, env = "SPEECH_CORE_ADAPTER_ID")]
    adapter_id: Option<String>,

    /// Stream id to place in frame headers.
    #[arg(long, default_value = "default.mic", env = "SPEECH_CORE_STREAM_ID")]
    stream_id: String,

    /// Stream session id. Generated on startup by default so adapter restarts are new sessions.
    #[arg(long, env = "SPEECH_CORE_STREAM_SESSION_ID")]
    stream_session_id: Option<String>,

    /// Desired sample rate sent on the wire. Real capture currently requires the selected device to support this rate.
    #[arg(long, default_value_t = 16_000)]
    sample_rate_hz: u32,

    /// Desired channel count sent on the wire. Real capture currently requires the selected device to support this channel count.
    #[arg(long, default_value_t = 1)]
    channels: u16,

    /// Wire PCM format.
    #[arg(long, value_enum, default_value_t = WireFormat::PcmS16Le)]
    format: WireFormat,

    /// Frame duration in milliseconds.
    #[arg(long, default_value_t = DEFAULT_FRAME_MS)]
    frame_ms: u32,

    /// List input devices and exit.
    #[arg(long)]
    list_devices: bool,

    /// Run without cpal, generating synthetic silence or tone frames.
    #[arg(long)]
    synthetic: bool,

    /// Alias for --synthetic that also avoids connecting to the daemon unless --connect is set.
    #[arg(long)]
    dry_run: bool,

    /// Connect even when --dry-run is used.
    #[arg(long)]
    connect: bool,

    /// Synthetic signal to generate.
    #[arg(long, value_enum, default_value_t = SyntheticSignal::Silence)]
    synthetic_signal: SyntheticSignal,

    /// Tone frequency for --synthetic-signal tone.
    #[arg(long, default_value_t = 440.0)]
    tone_hz: f32,

    /// Optional cpal input device name substring.
    #[arg(long)]
    device: Option<String>,

    /// Number of frames to send before exiting. Unlimited by default.
    #[arg(long)]
    frames: Option<u64>,

    /// Also write captured 16 kHz mono audio to this wav file while streaming.
    #[arg(long, env = "SPEECH_CORE_RECORD_WAV")]
    record_wav: Option<PathBuf>,
}

#[derive(Debug, Clone, Copy, ValueEnum)]
enum WireFormat {
    PcmS16Le,
    PcmF32Le,
}

impl From<WireFormat> for PcmFormat {
    fn from(value: WireFormat) -> Self {
        match value {
            WireFormat::PcmS16Le => PcmFormat::PcmS16Le,
            WireFormat::PcmF32Le => PcmFormat::PcmF32Le,
        }
    }
}

#[derive(Debug, Clone, Copy, ValueEnum)]
enum SyntheticSignal {
    Silence,
    Tone,
}

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "speech_core_mic_adapter=info".into()),
        )
        .init();

    let args = Args::parse();
    if args.list_devices {
        list_devices()?;
        return Ok(());
    }

    validate_args(&args)?;

    let adapter_id = args
        .adapter_id
        .clone()
        .unwrap_or_else(|| format!("{}-{}", hostname_fallback(), Uuid::new_v4().simple()));
    let stream_session_id = args
        .stream_session_id
        .clone()
        .unwrap_or_else(|| Uuid::new_v4().to_string());
    let clock_id = format!("host:{}:monotonic", hostname_fallback());

    if args.synthetic || args.dry_run {
        run_synthetic(args, adapter_id, stream_session_id, clock_id).await
    } else {
        run_cpal(args, adapter_id, stream_session_id, clock_id).await
    }
}

fn validate_args(args: &Args) -> Result<()> {
    if args.frame_ms == 0 {
        bail!("--frame-ms must be greater than zero");
    }
    if frame_duration_samples(args.sample_rate_hz, args.frame_ms) == 0 {
        bail!("frame duration produced zero samples; increase --frame-ms or --sample-rate-hz");
    }
    if args.channels == 0 {
        bail!("--channels must be greater than zero");
    }
    Ok(())
}

async fn run_synthetic(
    args: Args,
    adapter_id: String,
    stream_session_id: String,
    clock_id: String,
) -> Result<()> {
    let connect = !args.dry_run || args.connect;
    let provenance = provenance(&clock_id, TimestampQuality::SyntheticScheduled);
    if connect {
        let mut ws = connect_ws(&args.url).await?;
        send_hello(
            &mut ws,
            &args,
            &adapter_id,
            &stream_session_id,
            SourceKind::Synthetic,
            &provenance,
        )
        .await?;
        send_synthetic_frames(args, adapter_id, stream_session_id, clock_id, Some(&mut ws)).await
    } else {
        eprintln!("dry-run synthetic mode: generating frames without websocket connection");
        send_synthetic_frames(args, adapter_id, stream_session_id, clock_id, None).await
    }
}

async fn send_synthetic_frames(
    args: Args,
    adapter_id: String,
    stream_session_id: String,
    clock_id: String,
    mut ws: Option<&mut WebSocketStream<MaybeTlsStream<TcpStream>>>,
) -> Result<()> {
    let recorder = args
        .record_wav
        .as_ref()
        .map(|path| WavRecorder::create(path, args.sample_rate_hz, args.channels))
        .transpose()?;
    let sample_count = frame_duration_samples(args.sample_rate_hz, args.frame_ms);
    let frame_period = Duration::from_millis(args.frame_ms as u64);
    let mut ticker = interval(frame_period);
    ticker.set_missed_tick_behavior(MissedTickBehavior::Delay);

    let mut seq = 0_u64;
    let mut source_sample_start = 0_u64;
    loop {
        if args.frames.is_some_and(|max| seq >= max) {
            break;
        }
        ticker.tick().await;
        let source_capture_mono_ns = now_mono_ns();
        let samples = synthetic_samples(
            args.synthetic_signal,
            source_sample_start,
            sample_count,
            args.sample_rate_hz,
            args.channels,
            args.tone_hz,
        );
        record_samples(&recorder, &samples);
        let payload = encode_payload(&samples, args.format.into());
        let frame = build_frame(
            &args.stream_id,
            &stream_session_id,
            &adapter_id,
            SourceKind::Synthetic,
            seq,
            args.format.into(),
            args.sample_rate_hz,
            args.channels,
            source_sample_start,
            sample_count,
            source_capture_mono_ns,
            provenance(&clock_id, TimestampQuality::SyntheticScheduled),
            None,
            payload,
        )?;

        if let Some(socket) = ws.as_deref_mut() {
            socket.send(Message::Binary(frame.encode()?)).await?;
            drain_available_events(socket).await?;
        } else {
            eprintln!(
                "frame session={} seq={} sample_start={} sample_count={} first_sample_mono_ns={}",
                stream_session_id, seq, source_sample_start, sample_count, source_capture_mono_ns
            );
        }

        seq = seq.saturating_add(1);
        source_sample_start = source_sample_start.saturating_add(u64::from(sample_count));
    }
    drop(recorder);
    Ok(())
}

async fn run_cpal(
    args: Args,
    adapter_id: String,
    stream_session_id: String,
    clock_id: String,
) -> Result<()> {
    let recorder = args
        .record_wav
        .as_ref()
        .map(|path| WavRecorder::create(path, args.sample_rate_hz, args.channels))
        .transpose()?;
    let (sender, receiver) = mpsc::sync_channel::<CapturedBuffer>(CAPTURE_CHANNEL_CAPACITY);
    let buffer_pool = AudioBufferPool::default();
    let capture = start_capture(
        args.device.clone(),
        args.sample_rate_hz,
        args.channels,
        sender,
        buffer_pool.clone(),
    )?;

    let mut ws = connect_ws(&args.url).await?;
    send_hello(
        &mut ws,
        &args,
        &adapter_id,
        &stream_session_id,
        SourceKind::Mic,
        &provenance(&clock_id, TimestampQuality::Unknown),
    )
    .await?;

    let sample_count = frame_duration_samples(args.sample_rate_hz, args.frame_ms) as usize;
    let mut chunker = FrameChunker::new(sample_count, args.channels as usize, args.sample_rate_hz);
    let mut seq = 0_u64;
    let mut source_sample_start = 0_u64;
    let mut last_total_dropped_samples = 0_u64;
    let mut pending_gap: Option<SourceSampleGap> = None;

    loop {
        if args.frames.is_some_and(|max| seq >= max) {
            break;
        }

        let captured = receiver
            .recv()
            .map_err(|_| anyhow!("cpal capture channel closed"))?;

        if captured.total_dropped_samples > last_total_dropped_samples {
            let buffered_to_discard = chunker.discard_buffered_sample_frames() as u64;
            if let Some(gap) = source_gap_from_drop_totals(
                captured.total_dropped_samples,
                captured.total_dropped_buffers,
                &mut last_total_dropped_samples,
                buffered_to_discard,
                sample_count as u64,
            ) {
                source_sample_start = source_sample_start.saturating_add(gap.dropped_samples);
                pending_gap = Some(match pending_gap.take() {
                    Some(existing) => merge_source_gaps(existing, gap, sample_count as u64),
                    None => gap,
                });
            }
        }

        record_samples(&recorder, &captured.samples);
        let frame_chunks = chunker.push(
            &captured.samples,
            captured.first_sample_capture_mono_ns,
            captured.timestamp_quality,
        );
        buffer_pool.recycle(captured.samples);

        for (samples, source_capture_mono_ns, timestamp_quality) in frame_chunks {
            let payload = encode_payload(&samples, args.format.into());
            let frame = build_frame(
                &args.stream_id,
                &stream_session_id,
                &adapter_id,
                SourceKind::Mic,
                seq,
                args.format.into(),
                args.sample_rate_hz,
                args.channels,
                source_sample_start,
                sample_count as u32,
                source_capture_mono_ns,
                provenance(&clock_id, timestamp_quality),
                pending_gap.take(),
                payload,
            )?;
            ws.send(Message::Binary(frame.encode()?)).await?;
            drain_available_events(&mut ws).await?;
            seq = seq.saturating_add(1);
            source_sample_start = source_sample_start.saturating_add(sample_count as u64);
            if args.frames.is_some_and(|max| seq >= max) {
                break;
            }
        }
    }

    drop(capture);
    drop(recorder);
    Ok(())
}

async fn connect_ws(url: &str) -> Result<WebSocketStream<MaybeTlsStream<TcpStream>>> {
    let (ws, _) = connect_async(url)
        .await
        .with_context(|| format!("connecting to {url}"))?;
    Ok(ws)
}

async fn send_hello(
    ws: &mut WebSocketStream<MaybeTlsStream<TcpStream>>,
    args: &Args,
    adapter_id: &str,
    stream_session_id: &str,
    source_kind: SourceKind,
    timestamp_provenance: &TimestampProvenance,
) -> Result<()> {
    let hello = ControlMessage::Hello {
        adapter_id: adapter_id.to_owned(),
        stream_id: args.stream_id.clone(),
        stream_session_id: stream_session_id.to_owned(),
        source_kind,
        sample_rate_hz: args.sample_rate_hz,
        channels: args.channels,
        format: args.format.into(),
        timestamp_provenance: timestamp_provenance.clone(),
        adapter_hello_send_mono_ns: now_mono_ns(),
    };
    ws.send(Message::Text(serde_json::to_string(&hello)?))
        .await?;
    Ok(())
}

async fn drain_available_events(ws: &mut WebSocketStream<MaybeTlsStream<TcpStream>>) -> Result<()> {
    while let Ok(Some(msg)) = tokio::time::timeout(Duration::from_millis(1), ws.next()).await {
        match msg? {
            Message::Text(text) => eprintln!("daemon event: {text}"),
            Message::Close(close) => bail!("daemon closed websocket: {close:?}"),
            _ => {}
        }
    }
    Ok(())
}

#[allow(clippy::too_many_arguments)]
fn build_frame(
    stream_id: &str,
    stream_session_id: &str,
    adapter_id: &str,
    source_kind: SourceKind,
    seq: u64,
    format: PcmFormat,
    sample_rate_hz: u32,
    channels: u16,
    source_sample_start: u64,
    sample_count: u32,
    source_capture_mono_ns: u64,
    timestamp_provenance: TimestampProvenance,
    preceding_source_gap: Option<SourceSampleGap>,
    payload: Vec<u8>,
) -> Result<AudioFrame> {
    AudioFrame::new(
        AudioFrameHeader {
            stream_id: stream_id.to_owned(),
            stream_session_id: stream_session_id.to_owned(),
            adapter_id: adapter_id.to_owned(),
            source_kind,
            seq,
            format,
            sample_rate_hz,
            channels,
            source_sample_start,
            sample_count,
            source_capture_mono_ns,
            adapter_send_mono_ns: now_mono_ns(),
            timestamp_provenance,
            preceding_source_gap,
        },
        payload,
    )
    .map_err(Into::into)
}

fn provenance(clock_id: &str, timestamp_quality: TimestampQuality) -> TimestampProvenance {
    TimestampProvenance {
        adapter_clock_id: clock_id.to_owned(),
        adapter_clock_domain: ClockDomain::HostMonotonic,
        timestamp_quality,
        timestamp_semantics: speech_core_protocol::TimestampSemantics::FirstSample,
        clock_comparability: ClockComparability::Uncalibrated,
        estimated_daemon_offset_ns: None,
        estimated_offset_uncertainty_ns: None,
    }
}

fn synthetic_samples(
    signal: SyntheticSignal,
    source_sample_start: u64,
    sample_count: u32,
    sample_rate_hz: u32,
    channels: u16,
    tone_hz: f32,
) -> Vec<f32> {
    let mut samples = Vec::with_capacity(sample_count as usize * channels as usize);
    for i in 0..sample_count {
        let mono = match signal {
            SyntheticSignal::Silence => 0.0,
            SyntheticSignal::Tone => {
                let t = (source_sample_start + u64::from(i)) as f32 / sample_rate_hz as f32;
                (t * tone_hz * TAU).sin() * 0.2
            }
        };
        for _ in 0..channels {
            samples.push(mono);
        }
    }
    samples
}

fn encode_payload(samples: &[f32], format: PcmFormat) -> Vec<u8> {
    match format {
        PcmFormat::PcmS16Le => {
            let mut out = Vec::with_capacity(samples.len() * 2);
            for sample in samples {
                let scaled = (sample.clamp(-1.0, 1.0) * i16::MAX as f32).round() as i16;
                out.extend_from_slice(&scaled.to_le_bytes());
            }
            out
        }
        PcmFormat::PcmF32Le => {
            let mut out = Vec::with_capacity(samples.len() * 4);
            for sample in samples {
                out.extend_from_slice(&sample.to_le_bytes());
            }
            out
        }
    }
}

struct CaptureHandle {
    _stream: cpal::Stream,
}

struct WavRecorder {
    sender: Option<SyncSender<Vec<u8>>>,
    worker: Option<thread::JoinHandle<()>>,
    dropped_chunks: Arc<AtomicU64>,
}

struct WavRecorderState {
    file: std::fs::File,
    sample_rate_hz: u32,
    channels: u16,
    data_bytes: u32,
}

impl WavRecorder {
    const CHANNEL_CAPACITY: usize = 128;

    fn create(path: &PathBuf, sample_rate_hz: u32, channels: u16) -> Result<Self> {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)
                .with_context(|| format!("creating recorder directory {}", parent.display()))?;
        }
        let mut file = std::fs::File::create(path)
            .with_context(|| format!("creating recorder wav {}", path.display()))?;
        write_wav_header(&mut file, channels, sample_rate_hz, 0)?;
        eprintln!("recording captured audio to {}", path.display());

        let (sender, receiver) = mpsc::sync_channel(Self::CHANNEL_CAPACITY);
        let dropped_chunks = Arc::new(AtomicU64::new(0));
        let worker_dropped_chunks = Arc::clone(&dropped_chunks);
        let worker = thread::Builder::new()
            .name("speech-core-wav-recorder".to_owned())
            .spawn(move || {
                let mut state = WavRecorderState {
                    file,
                    sample_rate_hz,
                    channels,
                    data_bytes: 0,
                };
                if let Err(err) = run_wav_recorder_worker(&mut state, receiver) {
                    eprintln!("recording wav worker failed: {err}");
                }
                let dropped = worker_dropped_chunks.load(Ordering::Relaxed);
                if dropped > 0 {
                    eprintln!(
                        "recording wav dropped {dropped} chunks because recorder queue was full"
                    );
                }
            })
            .context("starting wav recorder worker")?;

        Ok(Self {
            sender: Some(sender),
            worker: Some(worker),
            dropped_chunks,
        })
    }

    fn write_samples(&self, samples: &[f32]) {
        let Some(sender) = self.sender.as_ref() else {
            return;
        };
        let payload = encode_wav_payload(samples);
        match sender.try_send(payload) {
            Ok(()) => {}
            Err(TrySendError::Full(_returned)) => {
                self.dropped_chunks.fetch_add(1, Ordering::Relaxed);
            }
            Err(TrySendError::Disconnected(_returned)) => {}
        }
    }

    #[cfg(test)]
    fn dropped_chunks(&self) -> u64 {
        self.dropped_chunks.load(Ordering::Relaxed)
    }
}

impl Drop for WavRecorder {
    fn drop(&mut self) {
        drop(self.sender.take());
        if let Some(worker) = self.worker.take() {
            if worker.join().is_err() {
                eprintln!("recording wav worker panicked");
            }
        }
    }
}

fn encode_wav_payload(samples: &[f32]) -> Vec<u8> {
    let mut payload = Vec::with_capacity(samples.len() * 2);
    for sample in samples {
        let scaled = (sample.clamp(-1.0, 1.0) * i16::MAX as f32).round() as i16;
        payload.extend_from_slice(&scaled.to_le_bytes());
    }
    payload
}

fn run_wav_recorder_worker(
    state: &mut WavRecorderState,
    receiver: Receiver<Vec<u8>>,
) -> Result<()> {
    for payload in receiver {
        state.file.write_all(&payload)?;
        state.data_bytes = state
            .data_bytes
            .saturating_add(payload.len().min(u32::MAX as usize) as u32);
    }
    refresh_wav_header(state)?;
    state.file.flush()?;
    Ok(())
}

fn refresh_wav_header(state: &mut WavRecorderState) -> Result<()> {
    let pos = state.file.stream_position()?;
    state.file.seek(SeekFrom::Start(0))?;
    write_wav_header(
        &mut state.file,
        state.channels,
        state.sample_rate_hz,
        state.data_bytes,
    )?;
    state.file.seek(SeekFrom::Start(pos))?;
    state.file.flush()?;
    Ok(())
}

fn write_wav_header(
    file: &mut std::fs::File,
    channels: u16,
    sample_rate_hz: u32,
    data_bytes: u32,
) -> Result<()> {
    let bits_per_sample = 16_u16;
    let block_align = channels.saturating_mul(bits_per_sample / 8);
    let byte_rate = sample_rate_hz.saturating_mul(u32::from(block_align));
    let riff_size = 36_u32.saturating_add(data_bytes);
    file.write_all(b"RIFF")?;
    file.write_all(&riff_size.to_le_bytes())?;
    file.write_all(b"WAVE")?;
    file.write_all(b"fmt ")?;
    file.write_all(&16_u32.to_le_bytes())?;
    file.write_all(&1_u16.to_le_bytes())?;
    file.write_all(&channels.to_le_bytes())?;
    file.write_all(&sample_rate_hz.to_le_bytes())?;
    file.write_all(&byte_rate.to_le_bytes())?;
    file.write_all(&block_align.to_le_bytes())?;
    file.write_all(&bits_per_sample.to_le_bytes())?;
    file.write_all(b"data")?;
    file.write_all(&data_bytes.to_le_bytes())?;
    Ok(())
}

fn record_samples(recorder: &Option<WavRecorder>, samples: &[f32]) {
    if let Some(recorder) = recorder {
        recorder.write_samples(samples);
    }
}

struct CapturedBuffer {
    samples: Vec<f32>,
    first_sample_capture_mono_ns: u64,
    timestamp_quality: TimestampQuality,
    total_dropped_samples: u64,
    total_dropped_buffers: u64,
}

const CAPTURE_CHANNEL_CAPACITY: usize = 128;

#[derive(Default)]
struct DropCounters {
    samples: AtomicU64,
    buffers: AtomicU64,
}

#[cfg(test)]
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct DropTotals {
    samples: u64,
    buffers: u64,
}

#[derive(Clone, Default)]
struct AudioBufferPool {
    buffers: Arc<Mutex<Vec<Vec<f32>>>>,
}

impl AudioBufferPool {
    const MAX_RETAINED_BUFFERS: usize = CAPTURE_CHANNEL_CAPACITY;

    fn take(&self, minimum_capacity: usize) -> Vec<f32> {
        let Ok(mut buffers) = self.buffers.try_lock() else {
            return Vec::with_capacity(minimum_capacity);
        };
        let Some(mut buffer) = buffers.pop() else {
            return Vec::with_capacity(minimum_capacity);
        };
        buffer.clear();
        if buffer.capacity() < minimum_capacity {
            buffer.reserve(minimum_capacity - buffer.capacity());
        }
        buffer
    }

    fn recycle(&self, buffer: Vec<f32>) {
        self.recycle_with(buffer, LockMode::MayBlock);
    }

    fn try_recycle(&self, buffer: Vec<f32>) {
        self.recycle_with(buffer, LockMode::NonBlocking);
    }

    fn recycle_with(&self, mut buffer: Vec<f32>, lock_mode: LockMode) {
        buffer.clear();
        match lock_mode {
            LockMode::MayBlock => {
                let Ok(mut buffers) = self.buffers.lock() else {
                    return;
                };
                if buffers.len() < Self::MAX_RETAINED_BUFFERS {
                    buffers.push(buffer);
                }
            }
            LockMode::NonBlocking => {
                let Ok(mut buffers) = self.buffers.try_lock() else {
                    return;
                };
                if buffers.len() < Self::MAX_RETAINED_BUFFERS {
                    buffers.push(buffer);
                }
            }
        }
    }
}

#[derive(Clone, Copy)]
enum LockMode {
    MayBlock,
    NonBlocking,
}

#[derive(Clone)]
struct CaptureBridge {
    sender: SyncSender<CapturedBuffer>,
    channels: u16,
    drop_counters: Arc<DropCounters>,
    buffer_pool: AudioBufferPool,
}

impl CaptureBridge {
    fn new(
        sender: SyncSender<CapturedBuffer>,
        channels: u16,
        buffer_pool: AudioBufferPool,
    ) -> Self {
        Self {
            sender,
            channels,
            drop_counters: Arc::new(DropCounters::default()),
            buffer_pool,
        }
    }

    fn take_buffer(&self, minimum_capacity: usize) -> Vec<f32> {
        self.buffer_pool.take(minimum_capacity)
    }

    fn send(&self, samples: Vec<f32>, info: &cpal::InputCallbackInfo) {
        let callback_receive_ns = now_mono_ns();
        let (first_sample_capture_mono_ns, timestamp_quality) =
            cpal_first_sample_timestamp(info, callback_receive_ns);
        self.send_with_metadata(samples, first_sample_capture_mono_ns, timestamp_quality);
    }

    fn send_with_metadata(
        &self,
        samples: Vec<f32>,
        first_sample_capture_mono_ns: u64,
        timestamp_quality: TimestampQuality,
    ) {
        let total_dropped_samples = self.drop_counters.samples.load(Ordering::Relaxed);
        let total_dropped_buffers = self.drop_counters.buffers.load(Ordering::Relaxed);
        let sample_frames = samples.len() as u64 / u64::from(self.channels);

        let captured = CapturedBuffer {
            samples,
            first_sample_capture_mono_ns,
            timestamp_quality,
            total_dropped_samples,
            total_dropped_buffers,
        };

        match self.sender.try_send(captured) {
            Ok(()) => {}
            Err(TrySendError::Full(returned)) => {
                self.drop_counters
                    .samples
                    .fetch_add(sample_frames, Ordering::Relaxed);
                self.drop_counters.buffers.fetch_add(1, Ordering::Relaxed);
                self.buffer_pool.try_recycle(returned.samples);
            }
            Err(TrySendError::Disconnected(returned)) => {
                self.buffer_pool.try_recycle(returned.samples);
            }
        }
    }

    #[cfg(test)]
    fn drop_totals(&self) -> DropTotals {
        DropTotals {
            samples: self.drop_counters.samples.load(Ordering::Relaxed),
            buffers: self.drop_counters.buffers.load(Ordering::Relaxed),
        }
    }
}

fn start_capture(
    device_filter: Option<String>,
    sample_rate_hz: u32,
    channels: u16,
    sender: SyncSender<CapturedBuffer>,
    buffer_pool: AudioBufferPool,
) -> Result<CaptureHandle> {
    let host = cpal::default_host();
    let device = find_input_device(&host, device_filter.as_deref())?;
    let device_name = device.name().unwrap_or_else(|_| "<unknown>".to_owned());
    let supported = device
        .supported_input_configs()
        .context("querying supported input configs")?
        .filter(|config| {
            config.channels() == channels
                && config.min_sample_rate().0 <= sample_rate_hz
                && config.max_sample_rate().0 >= sample_rate_hz
                && is_capture_sample_format_supported(config.sample_format())
        })
        .min_by_key(|config| capture_sample_format_rank(config.sample_format()))
        .with_context(|| {
            format!(
                "no supported input config for {sample_rate_hz} Hz / {channels} channels on {device_name}; v1 adapter does not resample yet and currently supports only f32/i16/u16 capture sample formats"
            )
        })?;

    let sample_format = supported.sample_format();
    let config = StreamConfig {
        channels,
        sample_rate: cpal::SampleRate(sample_rate_hz),
        buffer_size: cpal::BufferSize::Default,
    };

    eprintln!(
        "capturing from '{}' at {} Hz / {} channels / {:?}",
        device_name, sample_rate_hz, channels, sample_format
    );

    let bridge = CaptureBridge::new(sender, channels, buffer_pool);
    let err_fn = |err| eprintln!("cpal stream error: {err}");
    let stream = match sample_format {
        SampleFormat::F32 => {
            let bridge = bridge.clone();
            device.build_input_stream(
                &config,
                move |data: &[f32], info| {
                    let mut samples = bridge.take_buffer(data.len());
                    append_f32_samples(&mut samples, data);
                    bridge.send(samples, info)
                },
                err_fn,
                None,
            )?
        }
        SampleFormat::I16 => {
            let bridge = bridge.clone();
            device.build_input_stream(
                &config,
                move |data: &[i16], info| {
                    let mut samples = bridge.take_buffer(data.len());
                    append_i16_samples(&mut samples, data);
                    bridge.send(samples, info)
                },
                err_fn,
                None,
            )?
        }
        SampleFormat::U16 => {
            let bridge = bridge.clone();
            device.build_input_stream(
                &config,
                move |data: &[u16], info| {
                    let mut samples = bridge.take_buffer(data.len());
                    append_u16_samples(&mut samples, data);
                    bridge.send(samples, info)
                },
                err_fn,
                None,
            )?
        }
        other => bail!("unsupported cpal sample format: {other:?}"),
    };
    stream.play().context("starting cpal input stream")?;

    Ok(CaptureHandle { _stream: stream })
}

fn is_capture_sample_format_supported(format: SampleFormat) -> bool {
    matches!(
        format,
        SampleFormat::F32 | SampleFormat::I16 | SampleFormat::U16
    )
}

fn capture_sample_format_rank(format: SampleFormat) -> u8 {
    match format {
        SampleFormat::F32 => 0,
        SampleFormat::I16 => 1,
        SampleFormat::U16 => 2,
        _ => 100,
    }
}

fn append_f32_samples(out: &mut Vec<f32>, data: &[f32]) {
    out.extend_from_slice(data);
}

fn append_i16_samples(out: &mut Vec<f32>, data: &[i16]) {
    out.extend(data.iter().map(|sample| *sample as f32 / i16::MAX as f32));
}

fn append_u16_samples(out: &mut Vec<f32>, data: &[u16]) {
    out.extend(
        data.iter()
            .map(|sample| (*sample as f32 - 32768.0) / 32768.0),
    );
}

fn source_gap_from_drop_totals(
    total_dropped_samples: u64,
    total_dropped_buffers: u64,
    last_total_dropped_samples: &mut u64,
    buffered_discarded_samples: u64,
    frame_sample_count: u64,
) -> Option<SourceSampleGap> {
    if total_dropped_samples <= *last_total_dropped_samples {
        return None;
    }

    let dropped_samples = total_dropped_samples
        .saturating_sub(*last_total_dropped_samples)
        .saturating_add(buffered_discarded_samples);
    *last_total_dropped_samples = total_dropped_samples;

    Some(SourceSampleGap {
        dropped_samples,
        dropped_frames_estimate: dropped_samples.div_ceil(frame_sample_count),
        adapter_total_dropped_samples: total_dropped_samples,
        adapter_total_dropped_buffers: total_dropped_buffers,
        reason: "adapter_capture_channel_full".into(),
    })
}

fn merge_source_gaps(
    previous: SourceSampleGap,
    next: SourceSampleGap,
    frame_sample_count: u64,
) -> SourceSampleGap {
    let dropped_samples = previous
        .dropped_samples
        .saturating_add(next.dropped_samples);
    SourceSampleGap {
        dropped_samples,
        dropped_frames_estimate: dropped_samples.div_ceil(frame_sample_count),
        adapter_total_dropped_samples: next.adapter_total_dropped_samples,
        adapter_total_dropped_buffers: next.adapter_total_dropped_buffers,
        reason: next.reason,
    }
}

fn cpal_first_sample_timestamp(
    info: &cpal::InputCallbackInfo,
    callback_receive_ns: u64,
) -> (u64, TimestampQuality) {
    let timestamp = info.timestamp();
    match timestamp.callback.duration_since(&timestamp.capture) {
        Some(capture_to_callback) => (
            callback_receive_ns.saturating_sub(duration_ns_u64(capture_to_callback)),
            TimestampQuality::SourceCapture,
        ),
        None => (callback_receive_ns, TimestampQuality::CallbackReceive),
    }
}

fn duration_ns_u64(duration: Duration) -> u64 {
    duration.as_nanos().min(u128::from(u64::MAX)) as u64
}

fn find_input_device(host: &cpal::Host, filter: Option<&str>) -> Result<cpal::Device> {
    if let Some(filter) = filter {
        let filter_lower = filter.to_lowercase();
        for device in host.input_devices().context("listing input devices")? {
            let name = device.name().unwrap_or_default();
            if name.to_lowercase().contains(&filter_lower) {
                return Ok(device);
            }
        }
        bail!("no input device matched '{filter}'");
    }

    host.default_input_device()
        .ok_or_else(|| anyhow!("no default input device available"))
}

fn list_devices() -> Result<()> {
    let host = cpal::default_host();
    println!("host: {:?}", host.id());
    for (idx, device) in host.input_devices()?.enumerate() {
        let name = device.name().unwrap_or_else(|_| "<unknown>".into());
        println!("[{idx}] {name}");
        match device.supported_input_configs() {
            Ok(configs) => {
                for config in configs {
                    println!(
                        "    channels={} sample_rate={}..{} format={:?}",
                        config.channels(),
                        config.min_sample_rate().0,
                        config.max_sample_rate().0,
                        config.sample_format()
                    );
                }
            }
            Err(err) => println!("    error reading configs: {err}"),
        }
    }
    Ok(())
}

struct FrameChunker {
    samples_per_frame: usize,
    channels: usize,
    sample_rate_hz: u32,
    buffer: Vec<f32>,
    pending_first_sample_ns: Option<u64>,
    pending_quality: TimestampQuality,
}

impl FrameChunker {
    fn new(sample_count: usize, channels: usize, sample_rate_hz: u32) -> Self {
        Self {
            samples_per_frame: sample_count * channels,
            channels,
            sample_rate_hz,
            buffer: Vec::with_capacity(sample_count * channels * 2),
            pending_first_sample_ns: None,
            pending_quality: TimestampQuality::Unknown,
        }
    }

    fn push(
        &mut self,
        samples: &[f32],
        first_sample_ns: u64,
        timestamp_quality: TimestampQuality,
    ) -> Vec<(Vec<f32>, u64, TimestampQuality)> {
        if self.pending_first_sample_ns.is_none() {
            self.pending_first_sample_ns = Some(first_sample_ns);
            self.pending_quality = timestamp_quality;
        }
        self.buffer.extend_from_slice(samples);
        let mut frames = Vec::new();
        while self.buffer.len() >= self.samples_per_frame {
            let frame_samples: Vec<f32> = self.buffer.drain(0..self.samples_per_frame).collect();
            let frame_first_sample_ns = self.pending_first_sample_ns.unwrap_or(first_sample_ns);
            let quality = self.pending_quality;
            frames.push((frame_samples, frame_first_sample_ns, quality));

            if self.buffer.is_empty() {
                self.pending_first_sample_ns = None;
                self.pending_quality = TimestampQuality::Unknown;
            } else {
                self.pending_first_sample_ns =
                    Some(frame_first_sample_ns.saturating_add(sample_frames_to_ns(
                        self.samples_per_frame / self.channels,
                        self.sample_rate_hz,
                    )));
            }
        }
        frames
    }

    fn discard_buffered_sample_frames(&mut self) -> usize {
        let frames = self.buffer.len() / self.channels;
        self.buffer.clear();
        self.pending_first_sample_ns = None;
        self.pending_quality = TimestampQuality::Unknown;
        frames
    }
}

fn sample_frames_to_ns(sample_frames: usize, sample_rate_hz: u32) -> u64 {
    ((sample_frames as u128) * 1_000_000_000_u128 / u128::from(sample_rate_hz)) as u64
}

fn hostname_fallback() -> String {
    std::env::var("HOSTNAME")
        .ok()
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "adapter".to_owned())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn synthetic_silence_payload_has_expected_size() {
        let samples = synthetic_samples(SyntheticSignal::Silence, 0, 320, 16_000, 1, 440.0);
        let payload = encode_payload(&samples, PcmFormat::PcmS16Le);
        assert_eq!(payload.len(), 640);
        assert!(payload.iter().all(|b| *b == 0));
    }

    #[test]
    fn chunker_derives_frame_timestamps_from_sample_offsets() {
        let mut chunker = FrameChunker::new(320, 1, 16_000);
        assert!(chunker
            .push(&vec![0.0; 100], 1_000_000, TimestampQuality::SourceCapture)
            .is_empty());
        let frames = chunker.push(&vec![0.0; 600], 99_000_000, TimestampQuality::SourceCapture);
        assert_eq!(frames.len(), 2);
        assert_eq!(frames[0].0.len(), 320);
        assert_eq!(frames[0].1, 1_000_000);
        assert_eq!(frames[1].0.len(), 320);
        assert_eq!(frames[1].1, 21_000_000);
    }

    #[test]
    fn chunker_can_discard_partial_buffer_for_drop_gap_accounting() {
        let mut chunker = FrameChunker::new(320, 1, 16_000);
        assert!(chunker
            .push(&vec![0.0; 100], 1_000_000, TimestampQuality::SourceCapture)
            .is_empty());
        assert_eq!(chunker.discard_buffered_sample_frames(), 100);
        assert!(
            chunker
                .push(&vec![0.0; 320], 2_000_000, TimestampQuality::SourceCapture)
                .len()
                == 1
        );
    }

    #[test]
    fn capture_bridge_drop_counters_are_reported_on_next_delivered_buffer() {
        let (sender, receiver) = mpsc::sync_channel(1);
        let pool = AudioBufferPool::default();
        let bridge = CaptureBridge::new(sender, 1, pool);

        bridge.send_with_metadata(vec![0.0; 320], 1_000, TimestampQuality::SourceCapture);
        bridge.send_with_metadata(vec![0.0; 160], 2_000, TimestampQuality::SourceCapture);
        assert_eq!(
            bridge.drop_totals(),
            DropTotals {
                samples: 160,
                buffers: 1
            }
        );

        let first = receiver.recv().unwrap();
        assert_eq!(first.total_dropped_samples, 0);
        assert_eq!(first.total_dropped_buffers, 0);

        bridge.send_with_metadata(vec![0.0; 80], 3_000, TimestampQuality::CallbackReceive);
        let after_drop = receiver.recv().unwrap();
        assert_eq!(after_drop.total_dropped_samples, 160);
        assert_eq!(after_drop.total_dropped_buffers, 1);
        assert_eq!(after_drop.samples.len(), 80);
        assert_eq!(
            after_drop.timestamp_quality,
            TimestampQuality::CallbackReceive
        );
    }

    #[test]
    fn source_gap_metadata_includes_callback_drops_and_discarded_partial_frame() {
        let mut last_total = 320;
        let gap = source_gap_from_drop_totals(960, 3, &mut last_total, 100, 320).unwrap();

        assert_eq!(gap.dropped_samples, 740);
        assert_eq!(gap.dropped_frames_estimate, 3);
        assert_eq!(gap.adapter_total_dropped_samples, 960);
        assert_eq!(gap.adapter_total_dropped_buffers, 3);
        assert_eq!(gap.reason, "adapter_capture_channel_full");
        assert_eq!(last_total, 960);
    }

    #[test]
    fn pending_source_gaps_merge_without_losing_earlier_evidence() {
        let previous = SourceSampleGap {
            dropped_samples: 100,
            dropped_frames_estimate: 1,
            adapter_total_dropped_samples: 100,
            adapter_total_dropped_buffers: 1,
            reason: "adapter_capture_channel_full".into(),
        };
        let next = SourceSampleGap {
            dropped_samples: 700,
            dropped_frames_estimate: 3,
            adapter_total_dropped_samples: 720,
            adapter_total_dropped_buffers: 4,
            reason: "adapter_capture_channel_full".into(),
        };

        let merged = merge_source_gaps(previous, next, 320);

        assert_eq!(merged.dropped_samples, 800);
        assert_eq!(merged.dropped_frames_estimate, 3);
        assert_eq!(merged.adapter_total_dropped_samples, 720);
        assert_eq!(merged.adapter_total_dropped_buffers, 4);
        assert_eq!(merged.reason, "adapter_capture_channel_full");
    }

    #[test]
    fn capture_sample_conversion_preserves_supported_formats() {
        let mut f32_samples = Vec::new();
        append_f32_samples(&mut f32_samples, &[-1.0, 0.0, 1.0]);
        assert_eq!(f32_samples, vec![-1.0, 0.0, 1.0]);

        let mut i16_samples = Vec::new();
        append_i16_samples(&mut i16_samples, &[i16::MIN, 0, i16::MAX]);
        assert!((i16_samples[0] - (i16::MIN as f32 / i16::MAX as f32)).abs() < f32::EPSILON);
        assert_eq!(i16_samples[1], 0.0);
        assert_eq!(i16_samples[2], 1.0);

        let mut u16_samples = Vec::new();
        append_u16_samples(&mut u16_samples, &[0, 32768, u16::MAX]);
        assert_eq!(u16_samples[0], -1.0);
        assert_eq!(u16_samples[1], 0.0);
        assert!((u16_samples[2] - 0.9999695).abs() < 0.000001);
    }

    #[test]
    fn wav_recorder_offloads_writes_and_finalizes_header_on_drop() {
        let mut path = std::env::temp_dir();
        path.push(format!(
            "speech-core-recorder-offload-{}.wav",
            Uuid::new_v4().simple()
        ));

        {
            let recorder = WavRecorder::create(&path, 16_000, 1).unwrap();
            recorder.write_samples(&[0.0; 320]);
            assert_eq!(recorder.dropped_chunks(), 0);
        }

        let bytes = std::fs::read(&path).unwrap();
        let _ = std::fs::remove_file(&path);
        assert_eq!(&bytes[0..4], b"RIFF");
        assert_eq!(&bytes[8..12], b"WAVE");
        assert_eq!(u32::from_le_bytes(bytes[40..44].try_into().unwrap()), 640);
        assert_eq!(bytes.len(), 44 + 640);
    }

    #[test]
    fn build_frame_carries_session_and_gap_metadata() {
        let frame = build_frame(
            "stream",
            "session",
            "adapter",
            SourceKind::Synthetic,
            0,
            PcmFormat::PcmS16Le,
            16_000,
            1,
            320,
            320,
            1_000,
            provenance("host:test:monotonic", TimestampQuality::SyntheticScheduled),
            Some(SourceSampleGap {
                dropped_samples: 320,
                dropped_frames_estimate: 1,
                adapter_total_dropped_samples: 320,
                adapter_total_dropped_buffers: 1,
                reason: "test".into(),
            }),
            vec![0; 640],
        )
        .unwrap();
        assert_eq!(frame.header.stream_session_id, "session");
        assert_eq!(
            frame.header.preceding_source_gap.unwrap().dropped_samples,
            320
        );
    }
}
