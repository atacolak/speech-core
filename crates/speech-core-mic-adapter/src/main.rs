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
use std::sync::mpsc::{self, SyncSender, TrySendError};
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
    let (sender, receiver) = mpsc::sync_channel::<CapturedBuffer>(128);
    let capture = start_capture_thread(
        args.device.clone(),
        args.sample_rate_hz,
        args.channels,
        sender,
        recorder.clone(),
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
            let dropped_samples = captured
                .total_dropped_samples
                .saturating_sub(last_total_dropped_samples)
                .saturating_add(buffered_to_discard);
            source_sample_start = source_sample_start.saturating_add(dropped_samples);
            pending_gap = Some(SourceSampleGap {
                dropped_samples,
                dropped_frames_estimate: dropped_samples.div_ceil(sample_count as u64),
                adapter_total_dropped_samples: captured.total_dropped_samples,
                adapter_total_dropped_buffers: captured.total_dropped_buffers,
                reason: "adapter_capture_channel_full".into(),
            });
            last_total_dropped_samples = captured.total_dropped_samples;
        }

        for (samples, source_capture_mono_ns, timestamp_quality) in chunker.push(
            captured.samples,
            captured.first_sample_capture_mono_ns,
            captured.timestamp_quality,
        ) {
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
    _thread: thread::JoinHandle<()>,
}

#[derive(Clone)]
struct WavRecorder {
    state: Arc<Mutex<Option<WavRecorderState>>>,
}

struct WavRecorderState {
    file: std::fs::File,
    sample_rate_hz: u32,
    channels: u16,
    data_bytes: u32,
}

impl WavRecorder {
    fn create(path: &PathBuf, sample_rate_hz: u32, channels: u16) -> Result<Self> {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)
                .with_context(|| format!("creating recorder directory {}", parent.display()))?;
        }
        let mut file = std::fs::File::create(path)
            .with_context(|| format!("creating recorder wav {}", path.display()))?;
        write_wav_header(&mut file, channels, sample_rate_hz, 0)?;
        eprintln!("recording captured audio to {}", path.display());
        Ok(Self {
            state: Arc::new(Mutex::new(Some(WavRecorderState {
                file,
                sample_rate_hz,
                channels,
                data_bytes: 0,
            }))),
        })
    }

    fn write_samples(&self, samples: &[f32]) {
        let Ok(mut guard) = self.state.lock() else {
            return;
        };
        let Some(state) = guard.as_mut() else {
            return;
        };
        let mut payload = Vec::with_capacity(samples.len() * 2);
        for sample in samples {
            let scaled = (sample.clamp(-1.0, 1.0) * i16::MAX as f32).round() as i16;
            payload.extend_from_slice(&scaled.to_le_bytes());
        }
        if let Err(err) = state.file.seek(SeekFrom::End(0)) {
            eprintln!("recording wav seek failed: {err}");
            return;
        }
        if let Err(err) = state.file.write_all(&payload) {
            eprintln!("recording wav sample failed: {err}");
            return;
        }
        state.data_bytes = state
            .data_bytes
            .saturating_add(payload.len().min(u32::MAX as usize) as u32);
        if let Err(err) = refresh_wav_header(state) {
            eprintln!("recording wav header refresh failed: {err}");
        }
    }
}

impl Drop for WavRecorder {
    fn drop(&mut self) {
        if Arc::strong_count(&self.state) != 1 {
            return;
        }
        let Ok(mut guard) = self.state.lock() else {
            return;
        };
        if let Some(mut state) = guard.take() {
            if let Err(err) = refresh_wav_header(&mut state) {
                eprintln!("recording wav finalize failed: {err}");
            }
            if let Err(err) = state.file.flush() {
                eprintln!("recording wav flush failed: {err}");
            }
        }
    }
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

#[derive(Default)]
struct DropCounters {
    samples: AtomicU64,
    buffers: AtomicU64,
}

fn start_capture_thread(
    device_filter: Option<String>,
    sample_rate_hz: u32,
    channels: u16,
    sender: SyncSender<CapturedBuffer>,
    recorder: Option<WavRecorder>,
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

    let drop_counters = Arc::new(DropCounters::default());
    let err_fn = |err| eprintln!("cpal stream error: {err}");
    let stream = match sample_format {
        SampleFormat::F32 => {
            let counters = Arc::clone(&drop_counters);
            let recorder = recorder.clone();
            device.build_input_stream(
                &config,
                move |data: &[f32], info| {
                    let samples = data.to_vec();
                    record_samples(&recorder, &samples);
                    send_captured(samples, info, channels, &sender, &counters)
                },
                err_fn,
                None,
            )?
        }
        SampleFormat::I16 => {
            let counters = Arc::clone(&drop_counters);
            let recorder = recorder.clone();
            device.build_input_stream(
                &config,
                move |data: &[i16], info| {
                    let samples: Vec<f32> =
                        data.iter().map(|s| *s as f32 / i16::MAX as f32).collect();
                    record_samples(&recorder, &samples);
                    send_captured(samples, info, channels, &sender, &counters)
                },
                err_fn,
                None,
            )?
        }
        SampleFormat::U16 => {
            let counters = Arc::clone(&drop_counters);
            let recorder = recorder.clone();
            device.build_input_stream(
                &config,
                move |data: &[u16], info| {
                    let samples: Vec<f32> = data
                        .iter()
                        .map(|s| (*s as f32 - 32768.0) / 32768.0)
                        .collect();
                    record_samples(&recorder, &samples);
                    send_captured(samples, info, channels, &sender, &counters)
                },
                err_fn,
                None,
            )?
        }
        other => bail!("unsupported cpal sample format: {other:?}"),
    };
    stream.play().context("starting cpal input stream")?;

    // Keep the stream owned by a handle and park a thread so dropping CaptureHandle stops capture.
    let thread = thread::spawn(|| loop {
        thread::park_timeout(Duration::from_secs(3600));
    });

    Ok(CaptureHandle {
        _stream: stream,
        _thread: thread,
    })
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

fn send_captured(
    samples: Vec<f32>,
    info: &cpal::InputCallbackInfo,
    channels: u16,
    sender: &SyncSender<CapturedBuffer>,
    drop_counters: &DropCounters,
) {
    let callback_receive_ns = now_mono_ns();
    let (first_sample_capture_mono_ns, timestamp_quality) =
        cpal_first_sample_timestamp(info, callback_receive_ns);
    let total_dropped_samples = drop_counters.samples.load(Ordering::Relaxed);
    let total_dropped_buffers = drop_counters.buffers.load(Ordering::Relaxed);
    let sample_frames = samples.len() as u64 / u64::from(channels);

    let captured = CapturedBuffer {
        samples,
        first_sample_capture_mono_ns,
        timestamp_quality,
        total_dropped_samples,
        total_dropped_buffers,
    };

    match sender.try_send(captured) {
        Ok(()) => {}
        Err(TrySendError::Full(_returned)) => {
            drop_counters
                .samples
                .fetch_add(sample_frames, Ordering::Relaxed);
            drop_counters.buffers.fetch_add(1, Ordering::Relaxed);
        }
        Err(TrySendError::Disconnected(_returned)) => {}
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
        samples: Vec<f32>,
        first_sample_ns: u64,
        timestamp_quality: TimestampQuality,
    ) -> Vec<(Vec<f32>, u64, TimestampQuality)> {
        if self.pending_first_sample_ns.is_none() {
            self.pending_first_sample_ns = Some(first_sample_ns);
            self.pending_quality = timestamp_quality;
        }
        self.buffer.extend(samples);
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
            .push(vec![0.0; 100], 1_000_000, TimestampQuality::SourceCapture)
            .is_empty());
        let frames = chunker.push(vec![0.0; 600], 99_000_000, TimestampQuality::SourceCapture);
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
            .push(vec![0.0; 100], 1_000_000, TimestampQuality::SourceCapture)
            .is_empty());
        assert_eq!(chunker.discard_buffered_sample_frames(), 100);
        assert!(
            chunker
                .push(vec![0.0; 320], 2_000_000, TimestampQuality::SourceCapture)
                .len()
                == 1
        );
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
