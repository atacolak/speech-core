mod detectors;
mod model;

use anyhow::{bail, Context, Result};
use clap::Parser;
use detectors::parakeet_eou::ParakeetEouConfig;
use detectors::turn::TurnManagerConfig;
use detectors::vad::SileroVadConfig;
use detectors::{DetectorConfig, DetectorIngress};
use futures_util::{SinkExt, StreamExt};
use model::{ModelConfig, ModelIngress};
use serde::Serialize;
use speech_core_protocol::{
    adapter_send_to_ingress_latency, capture_to_ingress_latency, now_mono_ns, AudioFrame,
    AudioFrameIngested, AudioGap, AudioSampleGap, ControlMessage, HelloAck, PcmFormat, ServerEvent,
    SourceKind, StreamStart, TimestampProvenance,
};
use std::collections::HashMap;
use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::Arc;
use tokio::fs::{create_dir_all, OpenOptions};
use tokio::io::{AsyncWriteExt, BufWriter};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::{broadcast, Mutex};
use tokio_tungstenite::tungstenite::Message;
use tracing::{debug, error, info, warn};

#[derive(Debug, Parser)]
#[command(author, version, about = "speech-core websocket audio ingress daemon")]
struct Args {
    /// Socket address to listen on.
    #[arg(
        long,
        default_value = "127.0.0.1:8765",
        env = "SPEECH_CORE_DAEMON_BIND"
    )]
    bind: SocketAddr,

    /// Directory for jsonl event logs.
    #[arg(long, default_value = "./logs", env = "SPEECH_CORE_LOG_DIR")]
    log_dir: PathBuf,

    /// Optional transcribe.cpp GGUF model path. When omitted, daemon stays transport-only.
    #[arg(long, env = "SPEECH_CORE_MODEL_PATH")]
    model_path: Option<PathBuf>,

    /// Nemotron streaming feed chunk size in milliseconds.
    #[arg(long, default_value_t = 160, env = "SPEECH_CORE_STREAM_CHUNK_MS")]
    stream_chunk_ms: u32,

    /// Nemotron/parakeet attention right context selector.
    #[arg(long, default_value_t = 1, env = "SPEECH_CORE_ATT_CONTEXT_RIGHT")]
    att_context_right: i32,

    /// Bounded model-worker input queue depth in audio frames.
    #[arg(long, default_value_t = 256, env = "SPEECH_CORE_MODEL_QUEUE_FRAMES")]
    model_queue_frames: usize,

    /// Optional Silero VAD ONNX model path. Enables VAD events when set.
    #[arg(long, env = "SPEECH_CORE_VAD_MODEL_PATH")]
    vad_model_path: Option<PathBuf>,

    /// Silero VAD speech probability threshold.
    #[arg(long, default_value_t = 0.3, env = "SPEECH_CORE_VAD_THRESHOLD")]
    vad_threshold: f32,

    /// Consecutive 30ms speech frames required before speech_start.
    #[arg(long, default_value_t = 2, env = "SPEECH_CORE_VAD_ONSET_FRAMES")]
    vad_onset_frames: usize,

    /// Consecutive 30ms non-speech frames required before speech_end.
    #[arg(long, default_value_t = 15, env = "SPEECH_CORE_VAD_HANGOVER_FRAMES")]
    vad_hangover_frames: usize,

    /// Pre-roll 30ms frames included in reported VAD speech start sample.
    #[arg(long, default_value_t = 5, env = "SPEECH_CORE_VAD_PRE_SPEECH_FRAMES")]
    vad_pre_speech_frames: usize,

    /// Emit every Silero VAD frame probability event. Verbose, useful for tuning.
    #[arg(
        long,
        default_value_t = false,
        action = clap::ArgAction::Set,
        env = "SPEECH_CORE_VAD_EMIT_FRAMES"
    )]
    vad_emit_frames: bool,

    /// Optional Parakeet realtime EOU ONNX directory. Enables model EOU events when set.
    #[arg(long, env = "SPEECH_CORE_EOU_MODEL_DIR")]
    eou_model_dir: Option<PathBuf>,

    /// Parakeet EOU chunk size in milliseconds.
    #[arg(long, default_value_t = 160, env = "SPEECH_CORE_EOU_CHUNK_MS")]
    eou_chunk_ms: u32,

    /// Reset Parakeet EOU decoder state immediately when a raw EOU token is detected.
    ///
    /// Default false: speech-core treats raw model EOU as evidence and resets only when the
    /// turn manager accepts a boundary. This prevents suppressed startup/silence EOU candidates
    /// from poisoning the decoder state.
    #[arg(
        long,
        default_value_t = false,
        action = clap::ArgAction::Set,
        env = "SPEECH_CORE_EOU_RESET_ON_TOKEN"
    )]
    eou_reset_on_token: bool,

    /// Emit accumulated Parakeet EOU transcript text in eou_chunk_processed events.
    #[arg(
        long,
        default_value_t = true,
        action = clap::ArgAction::Set,
        env = "SPEECH_CORE_EOU_EMIT_TRANSCRIPT"
    )]
    eou_emit_transcript: bool,

    /// Bounded detector-worker input queue depth in audio frames.
    #[arg(long, default_value_t = 512, env = "SPEECH_CORE_DETECTOR_QUEUE_FRAMES")]
    detector_queue_frames: usize,

    /// Allow VAD speech_end to close turns when model EOU is unavailable or for comparison.
    #[arg(
        long,
        default_value_t = false,
        action = clap::ArgAction::Set,
        env = "SPEECH_CORE_TURN_VAD_CLOSE_ENABLED"
    )]
    turn_vad_close_enabled: bool,

    /// Allow Parakeet EOU tokens to close turns as non-degraded model EOU.
    #[arg(
        long,
        default_value_t = true,
        action = clap::ArgAction::Set,
        env = "SPEECH_CORE_TURN_MODEL_EOU_CLOSE_ENABLED"
    )]
    turn_model_eou_close_enabled: bool,

    /// Minimum observed speech before accepting a model EOU token.
    #[arg(
        long,
        default_value_t = 300,
        env = "SPEECH_CORE_TURN_MIN_MODEL_EOU_SPEECH_MS"
    )]
    turn_min_model_eou_speech_ms: u32,

    /// Minimum gap after a turn close before accepting another model EOU token.
    #[arg(
        long,
        default_value_t = 700,
        env = "SPEECH_CORE_TURN_MODEL_EOU_REFRACTORY_MS"
    )]
    turn_model_eou_refractory_ms: u32,
}

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "speech_core_daemon=info".into()),
        )
        .init();

    let args = Args::parse();
    if args.stream_chunk_ms == 0 {
        bail!("--stream-chunk-ms must be greater than zero");
    }
    if args.eou_chunk_ms == 0 {
        bail!("--eou-chunk-ms must be greater than zero");
    }
    if !(0.0..=1.0).contains(&args.vad_threshold) {
        bail!("--vad-threshold must be between 0.0 and 1.0");
    }
    create_dir_all(&args.log_dir)
        .await
        .with_context(|| format!("creating log directory {}", args.log_dir.display()))?;

    let (event_tx, _) = broadcast::channel(1024);
    let logger = JsonlLogger::open(args.log_dir, event_tx.clone()).await?;
    let model_ingress = args.model_path.clone().map(|model_path| {
        ModelIngress::start(
            ModelConfig {
                model_path,
                stream_chunk_ms: args.stream_chunk_ms,
                att_context_right: args.att_context_right,
                queue_frames: args.model_queue_frames,
            },
            logger.clone(),
        )
    });
    let detector_config = DetectorConfig {
        queue_frames: args.detector_queue_frames,
        vad: args
            .vad_model_path
            .clone()
            .map(|model_path| SileroVadConfig {
                model_path,
                threshold: args.vad_threshold,
                onset_frames: args.vad_onset_frames,
                hangover_frames: args.vad_hangover_frames,
                pre_speech_frames: args.vad_pre_speech_frames,
                emit_frames: args.vad_emit_frames,
            }),
        eou: args
            .eou_model_dir
            .clone()
            .map(|model_dir| ParakeetEouConfig {
                model_dir,
                chunk_ms: args.eou_chunk_ms,
                reset_on_eou: args.eou_reset_on_token,
                emit_transcript: args.eou_emit_transcript,
            }),
        turn: TurnManagerConfig {
            model_eou_close_enabled: args.turn_model_eou_close_enabled,
            vad_close_enabled: args.turn_vad_close_enabled,
            min_model_eou_speech_ms: args.turn_min_model_eou_speech_ms,
            model_eou_refractory_ms: args.turn_model_eou_refractory_ms,
        },
    };
    let detector_ingress = detector_config
        .enabled()
        .then(|| DetectorIngress::start(detector_config, logger.clone()));
    let state = Arc::new(DaemonState::new(logger, model_ingress, detector_ingress));
    let listener = TcpListener::bind(args.bind)
        .await
        .with_context(|| format!("binding {}", args.bind))?;
    info!(bind = %args.bind, "speech-core daemon listening");

    let mut shutdown = Box::pin(shutdown_signal());
    loop {
        tokio::select! {
            biased;
            signal = &mut shutdown => {
                if let Err(err) = signal {
                    warn!(error = ?err, "shutdown signal handler failed");
                }
                info!("shutdown requested");
                break;
            }
            accepted = listener.accept() => {
                let (stream, peer) = accepted.context("accepting tcp connection")?;
                let state = Arc::clone(&state);
                tokio::spawn(async move {
                    if let Err(err) = handle_connection(stream, peer, state).await {
                        error!(peer = %peer, error = ?err, "connection failed");
                    }
                });
            }
        }
    }

    Ok(())
}

async fn shutdown_signal() -> Result<()> {
    #[cfg(unix)]
    {
        let mut terminate =
            tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
                .context("installing sigterm handler")?;
        tokio::select! {
            result = tokio::signal::ctrl_c() => {
                result.context("waiting for ctrl-c")?;
            }
            _ = terminate.recv() => {}
        }
    }
    #[cfg(not(unix))]
    {
        tokio::signal::ctrl_c()
            .await
            .context("waiting for ctrl-c")?;
    }
    Ok(())
}

struct DaemonState {
    sessions: Mutex<HashMap<String, StreamState>>,
    logger: JsonlLogger,
    model_ingress: Option<ModelIngress>,
    detector_ingress: Option<DetectorIngress>,
}

impl DaemonState {
    fn new(
        logger: JsonlLogger,
        model_ingress: Option<ModelIngress>,
        detector_ingress: Option<DetectorIngress>,
    ) -> Self {
        Self {
            sessions: Mutex::new(HashMap::new()),
            logger,
            model_ingress,
            detector_ingress,
        }
    }

    async fn start_session(&self, hello: HelloState, daemon_mono_ns: u64) -> Result<ServerEvent> {
        let mut sessions = self.sessions.lock().await;
        sessions.insert(
            hello.stream_session_id.clone(),
            StreamState::new(hello.clone()),
        );
        drop(sessions);
        if let Some(model) = &self.model_ingress {
            model.start_session(&hello, &self.logger).await?;
        }
        if let Some(detector) = &self.detector_ingress {
            detector.start_session(&hello, &self.logger).await?;
        }
        Ok(ServerEvent::StreamStart(StreamStart {
            stream_id: hello.stream_id,
            stream_session_id: hello.stream_session_id,
            adapter_id: hello.adapter_id,
            source_kind: hello.source_kind,
            sample_rate_hz: hello.sample_rate_hz,
            channels: hello.channels,
            format: hello.format,
            timestamp_provenance: hello.timestamp_provenance,
            daemon_mono_ns,
        }))
    }

    async fn ingest(
        &self,
        hello: &HelloState,
        frame: AudioFrame,
        ingress_receive_mono_ns: u64,
    ) -> Result<IngestOutcome> {
        validate_frame_against_hello(hello, &frame)?;

        let ingress_queue_enter_mono_ns = now_mono_ns();
        // Transport v1 does not include a downstream queue yet. Keep the timestamp fields so later
        // queues can fill them, but report depth truthfully as zero.
        let ingress_queue_exit_mono_ns = now_mono_ns();

        let mut sessions = self.sessions.lock().await;
        let stream = sessions
            .entry(frame.header.stream_session_id.clone())
            .or_insert_with(|| StreamState::new(hello.clone()));

        let sequence_gap = match stream.next_seq {
            Some(expected) if frame.header.seq == expected => None,
            Some(expected) => Some(frame.header.seq as i64 - expected as i64),
            None => None,
        };

        let seq_gap = match stream.next_seq {
            Some(expected) if frame.header.seq > expected => Some(AudioGap {
                stream_id: frame.header.stream_id.clone(),
                stream_session_id: frame.header.stream_session_id.clone(),
                adapter_id: frame.header.adapter_id.clone(),
                expected_seq: expected,
                observed_seq: frame.header.seq,
                missing_frames: frame.header.seq - expected,
            }),
            _ => None,
        };

        let sample_gap = match stream.next_sample_start {
            Some(expected) if frame.header.source_sample_start != expected => {
                Some(AudioSampleGap {
                    stream_id: frame.header.stream_id.clone(),
                    stream_session_id: frame.header.stream_session_id.clone(),
                    adapter_id: frame.header.adapter_id.clone(),
                    expected_sample_start: expected,
                    observed_sample_start: frame.header.source_sample_start,
                    delta_samples: frame.header.source_sample_start as i64 - expected as i64,
                    declared_source_gap: frame.header.preceding_source_gap.clone(),
                })
            }
            Some(expected) if frame.header.preceding_source_gap.is_some() => Some(AudioSampleGap {
                stream_id: frame.header.stream_id.clone(),
                stream_session_id: frame.header.stream_session_id.clone(),
                adapter_id: frame.header.adapter_id.clone(),
                expected_sample_start: expected,
                observed_sample_start: frame.header.source_sample_start,
                delta_samples: 0,
                declared_source_gap: frame.header.preceding_source_gap.clone(),
            }),
            None if frame.header.preceding_source_gap.is_some() => Some(AudioSampleGap {
                stream_id: frame.header.stream_id.clone(),
                stream_session_id: frame.header.stream_session_id.clone(),
                adapter_id: frame.header.adapter_id.clone(),
                expected_sample_start: frame.header.source_sample_start,
                observed_sample_start: frame.header.source_sample_start,
                delta_samples: 0,
                declared_source_gap: frame.header.preceding_source_gap.clone(),
            }),
            _ => None,
        };

        stream.next_seq = Some(frame.header.seq.saturating_add(1));
        stream.next_sample_start = Some(
            frame
                .header
                .source_sample_start
                .saturating_add(u64::from(frame.header.sample_count)),
        );
        stream.frames_seen = stream.frames_seen.saturating_add(1);

        let capture_to_ingress = capture_to_ingress_latency(
            ingress_receive_mono_ns,
            frame.header.source_capture_mono_ns,
            &frame.header.timestamp_provenance,
        );
        let adapter_send_to_ingress = adapter_send_to_ingress_latency(
            ingress_receive_mono_ns,
            frame.header.adapter_send_mono_ns,
            &frame.header.timestamp_provenance,
        );

        let event = AudioFrameIngested {
            stream_id: frame.header.stream_id.clone(),
            stream_session_id: frame.header.stream_session_id.clone(),
            adapter_id: frame.header.adapter_id.clone(),
            source_kind: frame.header.source_kind,
            seq: frame.header.seq,
            sample_start: frame.header.source_sample_start,
            sample_count: frame.header.sample_count,
            source_capture_mono_ns: frame.header.source_capture_mono_ns,
            adapter_send_mono_ns: frame.header.adapter_send_mono_ns,
            timestamp_provenance: frame.header.timestamp_provenance.clone(),
            ingress_receive_mono_ns,
            ingress_queue_enter_mono_ns,
            ingress_queue_exit_mono_ns,
            ingress_queue_depth_frames: 0,
            frames_seen_in_session: stream.frames_seen,
            capture_to_ingress,
            adapter_send_to_ingress,
            sequence_gap,
            sample_gap: sample_gap.clone(),
        };
        drop(sessions);

        if let Some(model) = &self.model_ingress {
            model
                .ingest_frame(&frame, &self.logger, ingress_receive_mono_ns)
                .await?;
        }
        if let Some(detector) = &self.detector_ingress {
            detector
                .ingest_frame(&frame, &self.logger, ingress_receive_mono_ns)
                .await?;
        }

        Ok(IngestOutcome {
            event,
            seq_gap,
            sample_gap,
        })
    }
}

#[derive(Debug, Clone)]
struct HelloState {
    adapter_id: String,
    stream_id: String,
    stream_session_id: String,
    source_kind: SourceKind,
    sample_rate_hz: u32,
    channels: u16,
    format: PcmFormat,
    timestamp_provenance: TimestampProvenance,
}

#[derive(Clone)]
struct StreamState {
    _hello: HelloState,
    next_seq: Option<u64>,
    next_sample_start: Option<u64>,
    frames_seen: u64,
}

impl StreamState {
    fn new(hello: HelloState) -> Self {
        Self {
            _hello: hello,
            next_seq: None,
            next_sample_start: None,
            frames_seen: 0,
        }
    }
}

struct IngestOutcome {
    event: AudioFrameIngested,
    seq_gap: Option<AudioGap>,
    sample_gap: Option<AudioSampleGap>,
}

#[derive(Clone)]
struct JsonlLogger {
    inner: Arc<Mutex<BufWriter<tokio::fs::File>>>,
    event_tx: broadcast::Sender<String>,
}

impl JsonlLogger {
    async fn open(log_dir: PathBuf, event_tx: broadcast::Sender<String>) -> Result<Self> {
        let path = log_dir.join("events.jsonl");
        let file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&path)
            .await
            .with_context(|| format!("opening jsonl log {}", path.display()))?;
        info!(path = %path.display(), "writing jsonl events");
        Ok(Self {
            inner: Arc::new(Mutex::new(BufWriter::new(file))),
            event_tx,
        })
    }

    async fn write<T: Serialize>(&self, event: &T) -> Result<()> {
        let mut line = serde_json::to_vec(event).context("serializing jsonl event")?;
        let text = String::from_utf8(line.clone()).context("json event should be utf-8")?;
        line.push(b'\n');
        let mut writer = self.inner.lock().await;
        writer.write_all(&line).await?;
        writer.flush().await?;
        let _ = self.event_tx.send(text);
        Ok(())
    }

    fn subscribe(&self) -> broadcast::Receiver<String> {
        self.event_tx.subscribe()
    }
}

async fn handle_connection(
    stream: TcpStream,
    peer: SocketAddr,
    state: Arc<DaemonState>,
) -> Result<()> {
    stream.set_nodelay(true).ok();
    let ws = tokio_tungstenite::accept_async(stream)
        .await
        .context("websocket handshake")?;
    let (mut sink, mut source) = ws.split();
    let mut hello: Option<HelloState> = None;
    info!(peer = %peer, "audio ingress websocket connected");

    while let Some(msg) = source.next().await {
        let msg = match msg {
            Ok(msg) => msg,
            Err(err) => {
                warn!(peer = %peer, error = ?err, "websocket receive failed; finalizing session");
                break;
            }
        };

        match msg {
            Message::Binary(bytes) => {
                let ingress_receive_mono_ns = now_mono_ns();
                let Some(hello_state) = hello.as_ref() else {
                    let event = ServerEvent::Error {
                        message: "binary audio received before hello".to_owned(),
                    };
                    state.logger.write(&event).await?;
                    sink.send(Message::Text(serde_json::to_string(&event)?))
                        .await?;
                    continue;
                };

                match AudioFrame::decode(&bytes) {
                    Ok(frame) => match state
                        .ingest(hello_state, frame, ingress_receive_mono_ns)
                        .await
                    {
                        Ok(outcome) => {
                            if let Some(gap) = &outcome.seq_gap {
                                warn!(stream_id = %gap.stream_id, session_id = %gap.stream_session_id, expected_seq = gap.expected_seq, observed_seq = gap.observed_seq, "sequence gap detected");
                                let event = ServerEvent::AudioGap(gap.clone());
                                state.logger.write(&event).await?;
                                sink.send(Message::Text(serde_json::to_string(&event)?))
                                    .await?;
                            }
                            if let Some(gap) = &outcome.sample_gap {
                                warn!(stream_id = %gap.stream_id, session_id = %gap.stream_session_id, expected_sample_start = gap.expected_sample_start, observed_sample_start = gap.observed_sample_start, "sample-clock discontinuity detected");
                                let event = ServerEvent::AudioSampleGap(gap.clone());
                                state.logger.write(&event).await?;
                                sink.send(Message::Text(serde_json::to_string(&event)?))
                                    .await?;
                            }

                            debug!(stream_id = %outcome.event.stream_id, session_id = %outcome.event.stream_session_id, seq = outcome.event.seq, "frame ingested");
                            let event = ServerEvent::AudioFrameIngested(outcome.event);
                            state.logger.write(&event).await?;
                            sink.send(Message::Text(serde_json::to_string(&event)?))
                                .await?;
                        }
                        Err(err) => {
                            warn!(peer = %peer, error = ?err, "invalid audio frame metadata");
                            let event = ServerEvent::Error {
                                message: format!("invalid audio frame metadata: {err}"),
                            };
                            state.logger.write(&event).await?;
                            sink.send(Message::Text(serde_json::to_string(&event)?))
                                .await?;
                        }
                    },
                    Err(err) => {
                        warn!(peer = %peer, error = ?err, "invalid audio frame");
                        let event = ServerEvent::Error {
                            message: format!("invalid audio frame: {err}"),
                        };
                        state.logger.write(&event).await?;
                        sink.send(Message::Text(serde_json::to_string(&event)?))
                            .await?;
                    }
                }
            }
            Message::Text(text) => match serde_json::from_str::<ControlMessage>(&text) {
                Ok(ControlMessage::Hello {
                    adapter_id,
                    stream_id,
                    stream_session_id,
                    source_kind,
                    sample_rate_hz,
                    channels,
                    format,
                    timestamp_provenance,
                    adapter_hello_send_mono_ns,
                }) => {
                    let daemon_receive_mono_ns = now_mono_ns();
                    let hello_state = HelloState {
                        adapter_id,
                        stream_id,
                        stream_session_id,
                        source_kind,
                        sample_rate_hz,
                        channels,
                        format,
                        timestamp_provenance,
                    };
                    info!(peer = %peer, adapter_id = %hello_state.adapter_id, stream_id = %hello_state.stream_id, session_id = %hello_state.stream_session_id, source_kind = %hello_state.source_kind, sample_rate_hz, channels, format = %hello_state.format, "adapter hello");

                    let stream_start = state
                        .start_session(hello_state.clone(), daemon_receive_mono_ns)
                        .await?;
                    state.logger.write(&stream_start).await?;
                    sink.send(Message::Text(serde_json::to_string(&stream_start)?))
                        .await?;

                    let daemon_send_mono_ns = now_mono_ns();
                    let ack = ServerEvent::HelloAck(HelloAck {
                        stream_id: hello_state.stream_id.clone(),
                        stream_session_id: hello_state.stream_session_id.clone(),
                        adapter_id: hello_state.adapter_id.clone(),
                        daemon_receive_mono_ns,
                        daemon_send_mono_ns,
                        adapter_hello_send_mono_ns,
                        clock_comparability: hello_state.timestamp_provenance.clock_comparability,
                        estimated_offset_uncertainty_ns: hello_state
                            .timestamp_provenance
                            .estimated_offset_uncertainty_ns,
                    });
                    state.logger.write(&ack).await?;
                    sink.send(Message::Text(serde_json::to_string(&ack)?))
                        .await?;
                    hello = Some(hello_state);
                }
                Ok(ControlMessage::SubscribeEvents {
                    stream_id,
                    stream_session_id,
                    event,
                }) => {
                    info!(peer = %peer, ?stream_id, ?stream_session_id, ?event, "event subscriber connected");
                    stream_events_to_subscriber(
                        &mut sink,
                        state.logger.subscribe(),
                        stream_id,
                        stream_session_id,
                        event,
                    )
                    .await?;
                    break;
                }
                Ok(control) => {
                    debug!(?control, "control message");
                }
                Err(err) => {
                    warn!(peer = %peer, error = ?err, "invalid control json");
                    let event = ServerEvent::Error {
                        message: format!("invalid control json: {err}"),
                    };
                    state.logger.write(&event).await?;
                    sink.send(Message::Text(serde_json::to_string(&event)?))
                        .await?;
                }
            },
            Message::Ping(payload) => sink.send(Message::Pong(payload)).await?,
            Message::Pong(_) => {}
            Message::Close(close) => {
                info!(peer = %peer, ?close, "websocket closed");
                break;
            }
            Message::Frame(_) => {}
        }
    }

    if let (Some(model), Some(hello_state)) = (&state.model_ingress, hello.as_ref()) {
        model
            .end_session(hello_state, &state.logger, "websocket connection ended")
            .await?;
    }
    if let (Some(detector), Some(hello_state)) = (&state.detector_ingress, hello.as_ref()) {
        detector
            .end_session(hello_state, &state.logger, "websocket connection ended")
            .await?;
    }

    Ok(())
}

async fn stream_events_to_subscriber(
    sink: &mut futures_util::stream::SplitSink<
        tokio_tungstenite::WebSocketStream<TcpStream>,
        Message,
    >,
    mut rx: broadcast::Receiver<String>,
    stream_id: Option<String>,
    stream_session_id: Option<String>,
    event: Option<String>,
) -> Result<()> {
    loop {
        let text = match rx.recv().await {
            Ok(text) => text,
            Err(broadcast::error::RecvError::Lagged(skipped)) => {
                warn!(skipped, "event subscriber lagged");
                continue;
            }
            Err(broadcast::error::RecvError::Closed) => break,
        };
        if event_matches(
            &text,
            stream_id.as_deref(),
            stream_session_id.as_deref(),
            event.as_deref(),
        ) {
            sink.send(Message::Text(text)).await?;
        }
    }
    Ok(())
}

fn event_matches(
    text: &str,
    stream_id: Option<&str>,
    stream_session_id: Option<&str>,
    event: Option<&str>,
) -> bool {
    let Ok(value) = serde_json::from_str::<serde_json::Value>(text) else {
        return false;
    };
    if let Some(expected) = event {
        let observed = value
            .get("event")
            .or_else(|| value.get("type"))
            .and_then(|v| v.as_str());
        if observed != Some(expected) {
            return false;
        }
    }
    if let Some(expected) = stream_id {
        if value.get("stream_id").and_then(|v| v.as_str()) != Some(expected) {
            return false;
        }
    }
    if let Some(expected) = stream_session_id {
        if value.get("stream_session_id").and_then(|v| v.as_str()) != Some(expected) {
            return false;
        }
    }
    true
}

fn validate_frame_against_hello(hello: &HelloState, frame: &AudioFrame) -> Result<()> {
    let h = &frame.header;
    if h.adapter_id != hello.adapter_id {
        bail!(
            "adapter_id mismatch: frame={} hello={}",
            h.adapter_id,
            hello.adapter_id
        );
    }
    if h.stream_id != hello.stream_id {
        bail!(
            "stream_id mismatch: frame={} hello={}",
            h.stream_id,
            hello.stream_id
        );
    }
    if h.stream_session_id != hello.stream_session_id {
        bail!(
            "stream_session_id mismatch: frame={} hello={}",
            h.stream_session_id,
            hello.stream_session_id
        );
    }
    if h.source_kind != hello.source_kind {
        bail!("source_kind mismatch");
    }
    if h.format != hello.format {
        bail!("format mismatch");
    }
    if h.sample_rate_hz != hello.sample_rate_hz {
        bail!("sample_rate_hz mismatch");
    }
    if h.channels != hello.channels {
        bail!("channels mismatch");
    }
    if h.timestamp_provenance.adapter_clock_id != hello.timestamp_provenance.adapter_clock_id
        || h.timestamp_provenance.adapter_clock_domain
            != hello.timestamp_provenance.adapter_clock_domain
        || h.timestamp_provenance.timestamp_semantics
            != hello.timestamp_provenance.timestamp_semantics
        || h.timestamp_provenance.clock_comparability
            != hello.timestamp_provenance.clock_comparability
        || h.timestamp_provenance.estimated_daemon_offset_ns
            != hello.timestamp_provenance.estimated_daemon_offset_ns
        || h.timestamp_provenance.estimated_offset_uncertainty_ns
            != hello.timestamp_provenance.estimated_offset_uncertainty_ns
    {
        bail!("timestamp clock provenance mismatch");
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use speech_core_protocol::{
        ClockComparability, ClockDomain, PcmFormat, SourceKind, SourceSampleGap, TimestampQuality,
    };
    use tempfile::tempdir;

    fn provenance() -> TimestampProvenance {
        TimestampProvenance::uncalibrated(
            "host:test:monotonic",
            ClockDomain::HostMonotonic,
            TimestampQuality::CallbackReceive,
        )
    }

    fn hello(session: &str) -> HelloState {
        HelloState {
            adapter_id: "test.adapter".into(),
            stream_id: "test.stream".into(),
            stream_session_id: session.into(),
            source_kind: SourceKind::Synthetic,
            sample_rate_hz: 16_000,
            channels: 1,
            format: PcmFormat::PcmS16Le,
            timestamp_provenance: provenance(),
        }
    }

    async fn logger(dir: &tempfile::TempDir) -> JsonlLogger {
        let (event_tx, _) = broadcast::channel(16);
        JsonlLogger::open(dir.path().to_path_buf(), event_tx)
            .await
            .unwrap()
    }

    fn frame(session: &str, seq: u64, sample_start: u64) -> AudioFrame {
        AudioFrame::new(
            speech_core_protocol::AudioFrameHeader {
                stream_id: "test.stream".into(),
                stream_session_id: session.into(),
                adapter_id: "test.adapter".into(),
                source_kind: SourceKind::Synthetic,
                seq,
                format: PcmFormat::PcmS16Le,
                sample_rate_hz: 16_000,
                channels: 1,
                source_sample_start: sample_start,
                sample_count: 320,
                source_capture_mono_ns: 1_000,
                adapter_send_mono_ns: 2_000,
                timestamp_provenance: provenance(),
                preceding_source_gap: None,
            },
            vec![0; 640],
        )
        .unwrap()
    }

    #[tokio::test]
    async fn ingest_reports_uncalibrated_latency_and_zero_queue_depth() {
        let dir = tempdir().unwrap();
        let state = DaemonState::new(logger(&dir).await, None, None);
        let hello = hello("session-a");
        state.start_session(hello.clone(), 1).await.unwrap();

        let outcome = state
            .ingest(&hello, frame("session-a", 0, 0), 3_000)
            .await
            .unwrap();
        assert!(outcome.seq_gap.is_none());
        assert_eq!(outcome.event.sequence_gap, None);
        assert_eq!(outcome.event.ingress_queue_depth_frames, 0);
        assert_eq!(outcome.event.capture_to_ingress.value_ms, None);
        assert_eq!(
            outcome.event.capture_to_ingress.status,
            speech_core_protocol::LatencyStatus::Uncalibrated
        );
    }

    #[tokio::test]
    async fn session_reset_does_not_create_negative_gap() {
        let dir = tempdir().unwrap();
        let state = DaemonState::new(logger(&dir).await, None, None);
        let first = hello("session-a");
        let second = hello("session-b");
        state.start_session(first.clone(), 1).await.unwrap();
        state
            .ingest(&first, frame("session-a", 10, 3200), 3_000)
            .await
            .unwrap();
        state.start_session(second.clone(), 2).await.unwrap();

        let outcome = state
            .ingest(&second, frame("session-b", 0, 0), 4_000)
            .await
            .unwrap();
        assert_eq!(outcome.event.sequence_gap, None);
        assert!(outcome.seq_gap.is_none());
    }

    #[tokio::test]
    async fn sample_clock_gap_is_reported() {
        let dir = tempdir().unwrap();
        let state = DaemonState::new(logger(&dir).await, None, None);
        let hello = hello("session-a");
        state.start_session(hello.clone(), 1).await.unwrap();
        state
            .ingest(&hello, frame("session-a", 0, 0), 3_000)
            .await
            .unwrap();

        let mut second = frame("session-a", 1, 640);
        second.header.preceding_source_gap = Some(SourceSampleGap {
            dropped_samples: 320,
            dropped_frames_estimate: 1,
            adapter_total_dropped_samples: 320,
            adapter_total_dropped_buffers: 1,
            reason: "adapter_capture_channel_full".into(),
        });
        let outcome = state.ingest(&hello, second, 4_000).await.unwrap();
        let gap = outcome.sample_gap.unwrap();
        assert_eq!(gap.expected_sample_start, 320);
        assert_eq!(gap.observed_sample_start, 640);
        assert_eq!(gap.delta_samples, 320);
        assert_eq!(
            outcome
                .event
                .sample_gap
                .unwrap()
                .declared_source_gap
                .unwrap()
                .dropped_samples,
            320
        );
    }

    #[test]
    fn rejects_metadata_that_differs_from_hello() {
        let hello = hello("session-a");
        let mut frame = frame("session-a", 0, 0);
        frame.header.sample_rate_hz = 48_000;
        assert!(validate_frame_against_hello(&hello, &frame).is_err());
    }

    #[tokio::test]
    async fn same_clock_callback_receive_does_not_become_capture_latency() {
        let dir = tempdir().unwrap();
        let state = DaemonState::new(logger(&dir).await, None, None);
        let mut hello = hello("session-a");
        hello.timestamp_provenance.clock_comparability = ClockComparability::SameClock;
        let mut frame = frame("session-a", 0, 0);
        frame.header.timestamp_provenance = hello.timestamp_provenance.clone();
        assert!(validate_frame_against_hello(&hello, &frame).is_ok());
        state.start_session(hello.clone(), 1).await.unwrap();

        let outcome = state.ingest(&hello, frame, 11_000_000).await.unwrap();
        assert_eq!(outcome.event.capture_to_ingress.value_ms, None);
        assert_eq!(
            outcome.event.capture_to_ingress.status,
            speech_core_protocol::LatencyStatus::TimestampQualityInsufficient
        );
        assert_eq!(outcome.event.adapter_send_to_ingress.value_ms, Some(10.998));
        assert_eq!(
            outcome.event.adapter_send_to_ingress.status,
            speech_core_protocol::LatencyStatus::SameClock
        );
    }
}
