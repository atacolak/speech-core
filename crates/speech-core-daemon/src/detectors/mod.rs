use anyhow::{bail, Result};
use serde::Serialize;
use speech_core_protocol::{now_mono_ns, AudioFrame, PcmFormat};
use std::thread;
use tokio::runtime::Handle;
use tokio::sync::mpsc;
use tracing::{info, warn};

use crate::{HelloState, JsonlLogger};

pub mod parakeet_eou;
pub mod turn;
pub mod vad;

use parakeet_eou::{ParakeetEouConfig, ParakeetEouDetector};
use turn::{TurnManager, TurnManagerConfig};
use vad::{SileroVadConfig, SileroVadDetector};

#[derive(Debug, Clone, Default)]
pub struct DetectorConfig {
    pub queue_frames: usize,
    pub vad: Option<SileroVadConfig>,
    pub eou: Option<ParakeetEouConfig>,
    pub turn: TurnManagerConfig,
}

impl DetectorConfig {
    pub fn enabled(&self) -> bool {
        self.vad.is_some() || self.eou.is_some()
    }
}

#[derive(Clone)]
pub struct DetectorIngress {
    sender: mpsc::Sender<DetectorCommand>,
}

impl DetectorIngress {
    pub fn start(config: DetectorConfig, logger: JsonlLogger) -> Self {
        let (sender, mut receiver) = mpsc::channel(config.queue_frames.max(1));
        let runtime = Handle::current();
        thread::spawn(move || {
            info!(?config, "starting detector worker");
            let mut worker = DetectorWorker::new(config, logger, runtime);
            while let Some(command) = receiver.blocking_recv() {
                worker.handle(command);
            }
            worker.finalize_all("detector worker channel closed");
        });
        Self { sender }
    }

    pub async fn start_session(&self, hello: &HelloState, logger: &JsonlLogger) -> Result<()> {
        match self.sender.try_send(DetectorCommand::StartSession {
            hello: hello.clone(),
        }) {
            Ok(()) => Ok(()),
            Err(mpsc::error::TrySendError::Full(command)) => {
                log_detector_enqueue_error(
                    logger,
                    &command,
                    "detector queue full at session start",
                )
                .await?;
                bail!("detector queue full at session start")
            }
            Err(mpsc::error::TrySendError::Closed(_)) => bail!("detector channel closed"),
        }
    }

    pub async fn ingest_frame(
        &self,
        frame: &AudioFrame,
        logger: &JsonlLogger,
        ingress_receive_mono_ns: u64,
    ) -> Result<()> {
        match self.sender.try_send(DetectorCommand::AudioFrame {
            frame: frame.clone(),
            ingress_receive_mono_ns,
        }) {
            Ok(()) => Ok(()),
            Err(mpsc::error::TrySendError::Full(command)) => {
                log_detector_enqueue_error(logger, &command, "detector queue full; dropping frame")
                    .await?;
                Ok(())
            }
            Err(mpsc::error::TrySendError::Closed(_)) => bail!("detector channel closed"),
        }
    }

    pub async fn end_session(
        &self,
        hello: &HelloState,
        logger: &JsonlLogger,
        reason: impl Into<String>,
    ) -> Result<()> {
        let command = DetectorCommand::EndSession {
            stream_id: hello.stream_id.clone(),
            stream_session_id: hello.stream_session_id.clone(),
            adapter_id: hello.adapter_id.clone(),
            reason: reason.into(),
        };
        match self.sender.try_send(command) {
            Ok(()) => Ok(()),
            Err(mpsc::error::TrySendError::Full(command)) => {
                log_detector_enqueue_error(logger, &command, "detector queue full at session end")
                    .await?;
                Ok(())
            }
            Err(mpsc::error::TrySendError::Closed(_)) => Ok(()),
        }
    }
}

#[derive(Debug)]
enum DetectorCommand {
    StartSession {
        hello: HelloState,
    },
    AudioFrame {
        frame: AudioFrame,
        ingress_receive_mono_ns: u64,
    },
    EndSession {
        stream_id: String,
        stream_session_id: String,
        adapter_id: String,
        reason: String,
    },
}

impl DetectorCommand {
    fn ids(&self) -> (String, String, String) {
        match self {
            DetectorCommand::StartSession { hello } => (
                hello.stream_id.clone(),
                hello.stream_session_id.clone(),
                hello.adapter_id.clone(),
            ),
            DetectorCommand::AudioFrame { frame, .. } => (
                frame.header.stream_id.clone(),
                frame.header.stream_session_id.clone(),
                frame.header.adapter_id.clone(),
            ),
            DetectorCommand::EndSession {
                stream_id,
                stream_session_id,
                adapter_id,
                ..
            } => (
                stream_id.clone(),
                stream_session_id.clone(),
                adapter_id.clone(),
            ),
        }
    }
}

async fn log_detector_enqueue_error(
    logger: &JsonlLogger,
    command: &DetectorCommand,
    message: &str,
) -> Result<()> {
    let (stream_id, stream_session_id, adapter_id) = command.ids();
    logger
        .write(&DetectorErrorEvent {
            event: "detector_error",
            stream_id,
            stream_session_id,
            adapter_id,
            detector: "detector_worker",
            message: message.to_owned(),
            daemon_mono_ns: now_mono_ns(),
        })
        .await
}

struct DetectorWorker {
    logger: JsonlLogger,
    runtime: Handle,
    detectors: Vec<Box<dyn AudioDetector>>,
    turn_manager: TurnManager,
}

impl DetectorWorker {
    fn new(config: DetectorConfig, logger: JsonlLogger, runtime: Handle) -> Self {
        let mut detectors: Vec<Box<dyn AudioDetector>> = Vec::new();
        let mut writer = DetectorWriter::new(&logger, &runtime);

        if let Some(vad_config) = config.vad.clone() {
            match SileroVadDetector::new(vad_config) {
                Ok(detector) => detectors.push(Box::new(detector)),
                Err(err) => {
                    let _ = writer.write(&DetectorErrorEvent {
                        event: "detector_error",
                        stream_id: "".to_owned(),
                        stream_session_id: "".to_owned(),
                        adapter_id: "".to_owned(),
                        detector: "silero_vad",
                        message: format!("failed to initialize Silero VAD detector: {err}"),
                        daemon_mono_ns: now_mono_ns(),
                    });
                }
            }
        }

        if let Some(eou_config) = config.eou.clone() {
            match ParakeetEouDetector::new(eou_config) {
                Ok(detector) => detectors.push(Box::new(detector)),
                Err(err) => {
                    let _ = writer.write(&DetectorErrorEvent {
                        event: "detector_error",
                        stream_id: "".to_owned(),
                        stream_session_id: "".to_owned(),
                        adapter_id: "".to_owned(),
                        detector: "parakeet_realtime_eou_120m_v1",
                        message: format!("failed to initialize Parakeet EOU detector: {err}"),
                        daemon_mono_ns: now_mono_ns(),
                    });
                }
            }
        }

        Self {
            logger,
            runtime,
            detectors,
            turn_manager: TurnManager::new(config.turn),
        }
    }

    fn handle(&mut self, command: DetectorCommand) {
        let mut writer = DetectorWriter::new(&self.logger, &self.runtime);
        let result = (|| -> Result<()> {
            match command {
                DetectorCommand::StartSession { hello } => {
                    self.turn_manager.start_session(&hello, &mut writer)?;
                    for detector in &mut self.detectors {
                        detector.start_session(&hello, &mut writer)?;
                    }
                    Ok(())
                }
                DetectorCommand::AudioFrame {
                    frame,
                    ingress_receive_mono_ns,
                } => {
                    let samples = decode_frame_to_f32(&frame)?;
                    for detector in &mut self.detectors {
                        let signals = detector.ingest_frame(
                            &frame,
                            &samples,
                            ingress_receive_mono_ns,
                            &mut writer,
                        )?;
                        for signal in signals {
                            self.turn_manager.handle_signal(signal, &mut writer)?;
                        }
                    }
                    Ok(())
                }
                DetectorCommand::EndSession {
                    stream_session_id,
                    reason,
                    ..
                } => {
                    for detector in &mut self.detectors {
                        detector.end_session(&stream_session_id, &reason, &mut writer)?;
                    }
                    self.turn_manager
                        .end_session(&stream_session_id, &reason, &mut writer)
                }
            }
        })();
        if let Err(err) = result {
            warn!(error = ?err, "detector worker command failed");
        }
    }

    fn finalize_all(&mut self, reason: &str) {
        let mut writer = DetectorWriter::new(&self.logger, &self.runtime);
        self.turn_manager.finalize_all(reason, &mut writer).ok();
    }
}

pub struct DetectorWriter<'a> {
    logger: &'a JsonlLogger,
    runtime: &'a Handle,
}

impl<'a> DetectorWriter<'a> {
    fn new(logger: &'a JsonlLogger, runtime: &'a Handle) -> Self {
        Self { logger, runtime }
    }

    pub fn write<T: Serialize>(&mut self, event: &T) -> Result<()> {
        self.runtime.block_on(self.logger.write(event))
    }
}

/// Shared contract for streaming audio evidence producers.
///
/// Detectors are deliberately evidence-only: they emit VAD/EOU/model evidence. The turn manager
/// is the only component that promotes evidence into `turn_eou` / `turn_closed` events.
pub trait AudioDetector: Send {
    fn start_session(&mut self, hello: &HelloState, writer: &mut DetectorWriter<'_>) -> Result<()>;
    fn ingest_frame(
        &mut self,
        frame: &AudioFrame,
        samples: &[f32],
        ingress_receive_mono_ns: u64,
        writer: &mut DetectorWriter<'_>,
    ) -> Result<Vec<DetectorSignal>>;
    fn end_session(
        &mut self,
        stream_session_id: &str,
        reason: &str,
        writer: &mut DetectorWriter<'_>,
    ) -> Result<Vec<DetectorSignal>>;
}

#[derive(Debug, Clone)]
pub enum DetectorSignal {
    VadSegmentStart {
        detector: &'static str,
        stream_id: String,
        stream_session_id: String,
        adapter_id: String,
        start_sample: u64,
        decision_sample: u64,
        confidence: Option<f32>,
    },
    VadSegmentEnd {
        detector: &'static str,
        stream_id: String,
        stream_session_id: String,
        adapter_id: String,
        start_sample: u64,
        end_sample: u64,
        decision_sample: u64,
        confidence: Option<f32>,
    },
    ModelEou {
        detector: &'static str,
        stream_id: String,
        stream_session_id: String,
        adapter_id: String,
        end_sample: u64,
        decision_sample: u64,
        text_delta: String,
        confidence: Option<f32>,
    },
}

#[derive(Debug, Serialize)]
struct DetectorErrorEvent {
    event: &'static str,
    stream_id: String,
    stream_session_id: String,
    adapter_id: String,
    detector: &'static str,
    message: String,
    daemon_mono_ns: u64,
}

pub(crate) fn decode_frame_to_f32(frame: &AudioFrame) -> Result<Vec<f32>> {
    match frame.header.format {
        PcmFormat::PcmF32Le => {
            if frame.payload.len() % 4 != 0 {
                bail!("pcm_f32le payload length is not divisible by 4");
            }
            Ok(frame
                .payload
                .chunks_exact(4)
                .map(|bytes| f32::from_le_bytes(bytes.try_into().expect("4-byte chunk")))
                .collect())
        }
        PcmFormat::PcmS16Le => {
            if frame.payload.len() % 2 != 0 {
                bail!("pcm_s16le payload length is not divisible by 2");
            }
            Ok(frame
                .payload
                .chunks_exact(2)
                .map(|bytes| {
                    let sample = i16::from_le_bytes(bytes.try_into().expect("2-byte chunk"));
                    f32::from(sample) / 32768.0
                })
                .collect())
        }
    }
}
