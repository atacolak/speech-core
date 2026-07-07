use anyhow::{bail, Result};
use serde::Serialize;
use speech_core_protocol::{now_mono_ns, AudioFrame, PcmFormat};
use std::collections::VecDeque;
use std::thread;
use tokio::runtime::Handle;
use tokio::sync::mpsc;
use tracing::{info, warn};

use crate::{HelloState, JsonlLogger};

pub mod parakeet_eou;
pub mod smart_turn;
pub mod turn;
pub mod vad;

use parakeet_eou::{ParakeetEouConfig, ParakeetEouDetector};
use smart_turn::{SmartTurnConfig, SmartTurnDetector};
use turn::{TurnManager, TurnManagerConfig};
use vad::{SileroVadConfig, SileroVadDetector};

#[derive(Debug, Clone, Default)]
pub struct DetectorConfig {
    pub queue_frames: usize,
    pub vad: Option<SileroVadConfig>,
    pub eou: Option<ParakeetEouConfig>,
    pub smart_turn: Option<SmartTurnConfig>,
    pub turn: TurnManagerConfig,
}

impl DetectorConfig {
    pub fn enabled(&self) -> bool {
        self.vad.is_some() || self.eou.is_some() || self.smart_turn.is_some()
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
    smart_turn: Option<SmartTurnDetector>,
    turn_manager: TurnManager,
    semantic_rechecks: Vec<SemanticRecheckState>,
    min_vad_speech_samples: u64,
}

#[derive(Debug, Clone)]
struct SemanticRecheckState {
    stream_id: String,
    stream_session_id: String,
    adapter_id: String,
    start_sample: u64,
    end_sample: u64,
    initial_decision_sample: u64,
    pending_decision_samples: VecDeque<u64>,
    confidence: Option<f32>,
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

        let smart_turn = config.smart_turn.clone().and_then(|smart_turn_config| {
            match SmartTurnDetector::new(smart_turn_config) {
                Ok(detector) => Some(detector),
                Err(err) => {
                    let _ = writer.write(&DetectorErrorEvent {
                        event: "detector_error",
                        stream_id: "".to_owned(),
                        stream_session_id: "".to_owned(),
                        adapter_id: "".to_owned(),
                        detector: smart_turn::DETECTOR,
                        message: format!("failed to initialize Smart Turn detector: {err}"),
                        daemon_mono_ns: now_mono_ns(),
                    });
                    None
                }
            }
        });

        Self {
            logger,
            runtime,
            detectors,
            smart_turn,
            min_vad_speech_samples: ms_to_samples(config.turn.min_vad_speech_ms),
            turn_manager: TurnManager::new(config.turn),
            semantic_rechecks: Vec::new(),
        }
    }

    fn handle(&mut self, command: DetectorCommand) {
        let logger = self.logger.clone();
        let runtime = self.runtime.clone();
        let mut writer = DetectorWriter::new(&logger, &runtime);
        let result = (|| -> Result<()> {
            match command {
                DetectorCommand::StartSession { hello } => {
                    self.semantic_rechecks
                        .retain(|state| state.stream_session_id != hello.stream_session_id);
                    self.turn_manager.start_session(&hello, &mut writer)?;
                    if let Some(smart_turn) = &mut self.smart_turn {
                        smart_turn.start_session(&hello, &mut writer)?;
                    }
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
                    let frame_end_sample = frame
                        .header
                        .source_sample_start
                        .saturating_add(frame.header.sample_count as u64);
                    if let Some(smart_turn) = &mut self.smart_turn {
                        smart_turn.ingest_frame(
                            &frame,
                            &samples,
                            ingress_receive_mono_ns,
                            &mut writer,
                        )?;
                    }
                    for detector_idx in 0..self.detectors.len() {
                        let signals = {
                            let detector = &mut self.detectors[detector_idx];
                            detector.ingest_frame(
                                &frame,
                                &samples,
                                ingress_receive_mono_ns,
                                &mut writer,
                            )?
                        };
                        self.handle_signals(signals, &mut writer)?;
                    }
                    self.process_due_semantic_rechecks(
                        &frame.header.stream_session_id,
                        frame_end_sample,
                        &mut writer,
                    )?;
                    Ok(())
                }
                DetectorCommand::EndSession {
                    stream_session_id,
                    reason,
                    ..
                } => {
                    self.semantic_rechecks
                        .retain(|state| state.stream_session_id != stream_session_id);
                    for detector_idx in 0..self.detectors.len() {
                        let signals = {
                            let detector = &mut self.detectors[detector_idx];
                            detector.end_session(&stream_session_id, &reason, &mut writer)?
                        };
                        self.handle_signals(signals, &mut writer)?;
                    }
                    if let Some(smart_turn) = &mut self.smart_turn {
                        smart_turn.end_session(&stream_session_id, &reason, &mut writer)?;
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

    fn upsert_semantic_recheck(
        &mut self,
        recheck: SemanticRecheckState,
        writer: &mut DetectorWriter<'_>,
    ) -> Result<()> {
        self.semantic_rechecks.retain(|state| {
            !(state.stream_session_id == recheck.stream_session_id
                && state.end_sample == recheck.end_sample)
        });
        writer.write(&SemanticRecheckScheduledEvent {
            event: "smart_turn_recheck_scheduled",
            stream_id: recheck.stream_id.clone(),
            stream_session_id: recheck.stream_session_id.clone(),
            adapter_id: recheck.adapter_id.clone(),
            detector: smart_turn::DETECTOR,
            start_sample: recheck.start_sample,
            end_sample: recheck.end_sample,
            initial_decision_sample: recheck.initial_decision_sample,
            next_decision_sample: recheck
                .pending_decision_samples
                .front()
                .copied()
                .unwrap_or(recheck.initial_decision_sample),
            pending_decision_samples: recheck.pending_decision_samples.iter().copied().collect(),
            daemon_mono_ns: now_mono_ns(),
        })?;
        self.semantic_rechecks.push(recheck);
        Ok(())
    }

    fn cancel_semantic_rechecks_for_resumed_speech(
        &mut self,
        stream_session_id: &str,
        writer: &mut DetectorWriter<'_>,
    ) -> Result<()> {
        let mut remaining = Vec::new();
        let mut cancelled = Vec::new();
        for state in self.semantic_rechecks.drain(..) {
            if state.stream_session_id == stream_session_id {
                cancelled.push(state);
            } else {
                remaining.push(state);
            }
        }
        self.semantic_rechecks = remaining;
        for state in cancelled {
            writer.write(&SemanticRecheckCancelledEvent {
                event: "smart_turn_recheck_cancelled",
                stream_id: state.stream_id,
                stream_session_id: state.stream_session_id,
                adapter_id: state.adapter_id,
                detector: smart_turn::DETECTOR,
                start_sample: state.start_sample,
                end_sample: state.end_sample,
                initial_decision_sample: state.initial_decision_sample,
                reason: "speech_resumed",
                daemon_mono_ns: now_mono_ns(),
            })?;
        }
        Ok(())
    }

    fn process_due_semantic_rechecks(
        &mut self,
        stream_session_id: &str,
        audio_end_sample: u64,
        writer: &mut DetectorWriter<'_>,
    ) -> Result<()> {
        let Some(smart_turn) = &mut self.smart_turn else {
            return Ok(());
        };
        let mut pending = std::mem::take(&mut self.semantic_rechecks);
        let mut keep = Vec::new();
        for mut recheck in pending.drain(..) {
            let Some(next_decision_sample) = recheck.pending_decision_samples.front().copied()
            else {
                continue;
            };
            if recheck.stream_session_id != stream_session_id
                || audio_end_sample < next_decision_sample
            {
                keep.push(recheck);
                continue;
            }
            let decision_sample = recheck
                .pending_decision_samples
                .pop_front()
                .unwrap_or(next_decision_sample);
            let decision = match smart_turn.predict_for_vad_end(
                &recheck.stream_session_id,
                recheck.end_sample,
                decision_sample,
                writer,
            ) {
                Ok(decision) => decision,
                Err(err) => {
                    writer.write(&DetectorErrorEvent {
                        event: "detector_error",
                        stream_id: recheck.stream_id.clone(),
                        stream_session_id: recheck.stream_session_id.clone(),
                        adapter_id: recheck.adapter_id.clone(),
                        detector: smart_turn::DETECTOR,
                        message: format!("Smart Turn recheck failed; keeping turn open: {err}"),
                        daemon_mono_ns: now_mono_ns(),
                    })?;
                    smart_turn::SmartTurnDecision::unavailable("smart_turn_error")
                }
            };
            self.turn_manager.handle_signal(
                DetectorSignal::SemanticTurnDecision {
                    detector: smart_turn::DETECTOR,
                    stream_id: recheck.stream_id.clone(),
                    stream_session_id: recheck.stream_session_id.clone(),
                    adapter_id: recheck.adapter_id.clone(),
                    end_sample: recheck.end_sample,
                    decision_sample,
                    complete: decision.complete,
                    probability: decision.probability,
                    threshold: decision.threshold,
                    timed_out: decision.timed_out,
                    available: decision.available,
                    reason: decision.reason,
                    duration_ms: decision.duration_ms,
                },
                writer,
            )?;
            if decision.available && decision.complete {
                let actions = self.turn_manager.handle_signal(
                    DetectorSignal::VadSegmentEnd {
                        detector: "semantic_recheck",
                        stream_id: recheck.stream_id.clone(),
                        stream_session_id: recheck.stream_session_id.clone(),
                        adapter_id: recheck.adapter_id.clone(),
                        start_sample: recheck.start_sample,
                        end_sample: recheck.end_sample,
                        decision_sample,
                        confidence: recheck.confidence,
                    },
                    writer,
                )?;
                for action in actions {
                    let DetectorAction::ResetEouState {
                        stream_session_id,
                        reason,
                        ..
                    } = &action;
                    if *reason != "vad_speech_start" {
                        self.semantic_rechecks
                            .retain(|state| state.stream_session_id != *stream_session_id);
                    }
                    smart_turn.handle_action(&action, writer)?;
                    for detector in &mut self.detectors {
                        detector.handle_action(&action, writer)?;
                    }
                }
            } else if decision.available && !decision.complete {
                if recheck.pending_decision_samples.is_empty() {
                    writer.write(&SemanticRecheckExhaustedEvent {
                        event: "smart_turn_recheck_exhausted",
                        stream_id: recheck.stream_id.clone(),
                        stream_session_id: recheck.stream_session_id.clone(),
                        adapter_id: recheck.adapter_id.clone(),
                        detector: smart_turn::DETECTOR,
                        start_sample: recheck.start_sample,
                        end_sample: recheck.end_sample,
                        initial_decision_sample: recheck.initial_decision_sample,
                        final_decision_sample: decision_sample,
                        confidence: recheck.confidence,
                        daemon_mono_ns: now_mono_ns(),
                    })?;
                } else {
                    writer.write(&SemanticRecheckScheduledEvent {
                        event: "smart_turn_recheck_scheduled",
                        stream_id: recheck.stream_id.clone(),
                        stream_session_id: recheck.stream_session_id.clone(),
                        adapter_id: recheck.adapter_id.clone(),
                        detector: smart_turn::DETECTOR,
                        start_sample: recheck.start_sample,
                        end_sample: recheck.end_sample,
                        initial_decision_sample: recheck.initial_decision_sample,
                        next_decision_sample: recheck
                            .pending_decision_samples
                            .front()
                            .copied()
                            .unwrap_or(decision_sample),
                        pending_decision_samples: recheck
                            .pending_decision_samples
                            .iter()
                            .copied()
                            .collect(),
                        daemon_mono_ns: now_mono_ns(),
                    })?;
                    keep.push(recheck);
                }
            }
        }
        self.semantic_rechecks = keep;
        Ok(())
    }

    fn handle_signals(
        &mut self,
        signals: Vec<DetectorSignal>,
        writer: &mut DetectorWriter<'_>,
    ) -> Result<()> {
        for signal in signals {
            if let DetectorSignal::VadSegmentStart {
                stream_session_id, ..
            } = &signal
            {
                self.cancel_semantic_rechecks_for_resumed_speech(stream_session_id, writer)?;
            }
            if let DetectorSignal::VadSegmentEnd {
                stream_id,
                stream_session_id,
                adapter_id,
                start_sample,
                end_sample,
                decision_sample,
                confidence,
                ..
            } = &signal
            {
                let segment_samples = end_sample.saturating_sub(*start_sample);
                if let Some(smart_turn) = &mut self.smart_turn {
                    if segment_samples < self.min_vad_speech_samples {
                        writer.write(&SmartTurnSkippedEvent {
                            event: "smart_turn_skipped",
                            stream_id: stream_id.clone(),
                            stream_session_id: stream_session_id.clone(),
                            adapter_id: adapter_id.clone(),
                            detector: smart_turn::DETECTOR,
                            start_sample: *start_sample,
                            end_sample: *end_sample,
                            decision_sample: *decision_sample,
                            reason: "vad_segment_too_short",
                            observed_speech_samples: segment_samples,
                            min_required_samples: self.min_vad_speech_samples,
                            daemon_mono_ns: now_mono_ns(),
                        })?;
                    } else {
                        let decision = match smart_turn.predict_for_vad_end(
                            stream_session_id,
                            *end_sample,
                            *decision_sample,
                            writer,
                        ) {
                            Ok(decision) => decision,
                            Err(err) => {
                                writer.write(&DetectorErrorEvent {
                                    event: "detector_error",
                                    stream_id: stream_id.clone(),
                                    stream_session_id: stream_session_id.clone(),
                                    adapter_id: adapter_id.clone(),
                                    detector: smart_turn::DETECTOR,
                                    message: format!(
                                        "Smart Turn prediction failed; falling back to VAD: {err}"
                                    ),
                                    daemon_mono_ns: now_mono_ns(),
                                })?;
                                smart_turn::SmartTurnDecision::unavailable("smart_turn_error")
                            }
                        };
                        let recheck = (decision.available
                            && !decision.complete
                            && smart_turn.recheck_enabled())
                        .then(|| SemanticRecheckState {
                            stream_id: stream_id.clone(),
                            stream_session_id: stream_session_id.clone(),
                            adapter_id: adapter_id.clone(),
                            start_sample: *start_sample,
                            end_sample: *end_sample,
                            initial_decision_sample: *decision_sample,
                            pending_decision_samples: smart_turn
                                .recheck_offsets_samples()
                                .into_iter()
                                .map(|offset| end_sample.saturating_add(offset))
                                .filter(|sample| *sample > *decision_sample)
                                .collect(),
                            confidence: *confidence,
                        });
                        let semantic_signal = DetectorSignal::SemanticTurnDecision {
                            detector: smart_turn::DETECTOR,
                            stream_id: stream_id.clone(),
                            stream_session_id: stream_session_id.clone(),
                            adapter_id: adapter_id.clone(),
                            end_sample: *end_sample,
                            decision_sample: *decision_sample,
                            complete: decision.complete,
                            probability: decision.probability,
                            threshold: decision.threshold,
                            timed_out: decision.timed_out,
                            available: decision.available,
                            reason: decision.reason,
                            duration_ms: decision.duration_ms,
                        };
                        self.turn_manager.handle_signal(semantic_signal, writer)?;
                        if let Some(recheck) = recheck {
                            self.upsert_semantic_recheck(recheck, writer)?;
                        }
                    }
                }
            }
            let actions = self.turn_manager.handle_signal(signal, writer)?;
            for action in actions {
                let DetectorAction::ResetEouState {
                    stream_session_id,
                    reason,
                    ..
                } = &action;
                if *reason != "vad_speech_start" {
                    self.semantic_rechecks
                        .retain(|state| state.stream_session_id != *stream_session_id);
                }
                if let Some(smart_turn) = &mut self.smart_turn {
                    smart_turn.handle_action(&action, writer)?;
                }
                for detector in &mut self.detectors {
                    detector.handle_action(&action, writer)?;
                }
            }
        }
        Ok(())
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
    fn handle_action(
        &mut self,
        _action: &DetectorAction,
        _writer: &mut DetectorWriter<'_>,
    ) -> Result<()> {
        Ok(())
    }
}

#[derive(Debug, Clone)]
pub enum DetectorAction {
    ResetEouState {
        stream_id: String,
        stream_session_id: String,
        adapter_id: String,
        mode: EouResetMode,
        anchor_sample: u64,
        source: &'static str,
        reason: &'static str,
        decision_sample: u64,
    },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EouResetMode {
    /// Reset encoder cache, decoder state, and detector-local queued audio.
    Stream,
    /// Reset only the RNNT prediction-network state; keep encoder/audio context flowing.
    Decoder,
}

impl EouResetMode {
    pub fn as_str(self) -> &'static str {
        match self {
            EouResetMode::Stream => "stream",
            EouResetMode::Decoder => "decoder",
        }
    }
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
    VadSpeechPresence {
        detector: &'static str,
        stream_id: String,
        stream_session_id: String,
        adapter_id: String,
        start_sample: u64,
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
    SemanticTurnDecision {
        detector: &'static str,
        stream_id: String,
        stream_session_id: String,
        adapter_id: String,
        end_sample: u64,
        decision_sample: u64,
        complete: bool,
        probability: Option<f32>,
        threshold: Option<f32>,
        timed_out: bool,
        available: bool,
        reason: &'static str,
        duration_ms: Option<f64>,
    },
    VadAcousticFallback {
        detector: &'static str,
        stream_id: String,
        stream_session_id: String,
        adapter_id: String,
        start_sample: u64,
        end_sample: u64,
        decision_sample: u64,
        silence_samples: u64,
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

#[derive(Debug, Serialize)]
struct SmartTurnSkippedEvent {
    event: &'static str,
    stream_id: String,
    stream_session_id: String,
    adapter_id: String,
    detector: &'static str,
    start_sample: u64,
    end_sample: u64,
    decision_sample: u64,
    reason: &'static str,
    observed_speech_samples: u64,
    min_required_samples: u64,
    daemon_mono_ns: u64,
}

#[derive(Debug, Serialize)]
struct SemanticRecheckScheduledEvent {
    event: &'static str,
    stream_id: String,
    stream_session_id: String,
    adapter_id: String,
    detector: &'static str,
    start_sample: u64,
    end_sample: u64,
    initial_decision_sample: u64,
    next_decision_sample: u64,
    pending_decision_samples: Vec<u64>,
    daemon_mono_ns: u64,
}

#[derive(Debug, Serialize)]
struct SemanticRecheckCancelledEvent {
    event: &'static str,
    stream_id: String,
    stream_session_id: String,
    adapter_id: String,
    detector: &'static str,
    start_sample: u64,
    end_sample: u64,
    initial_decision_sample: u64,
    reason: &'static str,
    daemon_mono_ns: u64,
}

#[derive(Debug, Serialize)]
struct SemanticRecheckExhaustedEvent {
    event: &'static str,
    stream_id: String,
    stream_session_id: String,
    adapter_id: String,
    detector: &'static str,
    start_sample: u64,
    end_sample: u64,
    initial_decision_sample: u64,
    final_decision_sample: u64,
    confidence: Option<f32>,
    daemon_mono_ns: u64,
}

fn ms_to_samples(ms: u32) -> u64 {
    u64::from(ms).saturating_mul(16_000) / 1_000
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

#[cfg(test)]
mod tests {
    use super::*;
    use anyhow::Result;
    use std::sync::{Arc, Mutex};
    use tempfile::tempdir;
    use tokio::runtime::Runtime;
    use tokio::sync::broadcast;

    #[derive(Debug, Default)]
    struct MockDetector {
        signal_batches: Arc<Mutex<Vec<Vec<DetectorSignal>>>>,
        actions: Arc<Mutex<Vec<DetectorAction>>>,
    }

    impl AudioDetector for MockDetector {
        fn start_session(
            &mut self,
            _hello: &HelloState,
            _writer: &mut DetectorWriter<'_>,
        ) -> Result<()> {
            Ok(())
        }

        fn ingest_frame(
            &mut self,
            _frame: &AudioFrame,
            _samples: &[f32],
            _ingress_receive_mono_ns: u64,
            _writer: &mut DetectorWriter<'_>,
        ) -> Result<Vec<DetectorSignal>> {
            Ok(self
                .signal_batches
                .lock()
                .unwrap()
                .pop()
                .unwrap_or_default())
        }

        fn end_session(
            &mut self,
            _stream_session_id: &str,
            _reason: &str,
            _writer: &mut DetectorWriter<'_>,
        ) -> Result<Vec<DetectorSignal>> {
            Ok(Vec::new())
        }

        fn handle_action(
            &mut self,
            action: &DetectorAction,
            _writer: &mut DetectorWriter<'_>,
        ) -> Result<()> {
            self.actions.lock().unwrap().push(action.clone());
            Ok(())
        }
    }

    fn test_frame() -> AudioFrame {
        let provenance = speech_core_protocol::TimestampProvenance::uncalibrated(
            "test-clock",
            speech_core_protocol::ClockDomain::HostMonotonic,
            speech_core_protocol::TimestampQuality::SyntheticScheduled,
        );
        AudioFrame::new(
            speech_core_protocol::AudioFrameHeader {
                stream_id: "test.stream".into(),
                stream_session_id: "test.session".into(),
                adapter_id: "test.adapter".into(),
                source_kind: speech_core_protocol::SourceKind::Synthetic,
                seq: 0,
                format: PcmFormat::PcmS16Le,
                sample_rate_hz: 16_000,
                channels: 1,
                source_sample_start: 0,
                sample_count: 320,
                source_capture_mono_ns: 1,
                adapter_send_mono_ns: 2,
                timestamp_provenance: provenance,
                preceding_source_gap: None,
            },
            vec![0_u8; 640],
        )
        .unwrap()
    }

    fn with_worker<
        F: FnOnce(
            &mut DetectorWorker,
            &Runtime,
            Arc<Mutex<Vec<DetectorAction>>>,
            Arc<Mutex<Vec<Vec<DetectorSignal>>>>,
        ),
    >(
        turn_config: TurnManagerConfig,
        f: F,
    ) {
        let min_vad_speech_samples = ms_to_samples(turn_config.min_vad_speech_ms);
        let runtime = Runtime::new().unwrap();
        let dir = tempdir().unwrap();
        let logger = runtime.block_on(async {
            let (event_tx, _) = broadcast::channel(16);
            JsonlLogger::open(dir.path().to_path_buf(), event_tx)
                .await
                .unwrap()
        });
        let actions = Arc::new(Mutex::new(Vec::new()));
        let signal_batches = Arc::new(Mutex::new(Vec::new()));
        let detector = MockDetector {
            signal_batches: Arc::clone(&signal_batches),
            actions: Arc::clone(&actions),
        };
        let mut worker = DetectorWorker {
            logger,
            runtime: runtime.handle().clone(),
            detectors: vec![Box::new(detector)],
            smart_turn: None,
            turn_manager: TurnManager::new(turn_config),
            semantic_rechecks: Vec::new(),
            min_vad_speech_samples,
        };
        f(&mut worker, &runtime, actions, signal_batches);
    }

    #[test]
    fn vad_start_dispatches_stream_reset_anchored_to_speech_start() {
        with_worker(
            TurnManagerConfig {
                vad_close_enabled: true,
                model_eou_close_enabled: true,
                ..Default::default()
            },
            |worker, runtime, actions, _signal_batches| {
                let logger = worker.logger.clone();
                let mut writer = DetectorWriter::new(&logger, runtime.handle());
                let signal = DetectorSignal::VadSegmentStart {
                    detector: "silero_vad",
                    stream_id: "test.stream".into(),
                    stream_session_id: "test.session".into(),
                    adapter_id: "test.adapter".into(),
                    start_sample: 1_600,
                    decision_sample: 4_800,
                    confidence: Some(0.9),
                };
                worker.handle_signals(vec![signal], &mut writer).unwrap();
                let actions = actions.lock().unwrap();
                assert_eq!(actions.len(), 1);
                match &actions[0] {
                    DetectorAction::ResetEouState {
                        mode,
                        anchor_sample,
                        decision_sample,
                        reason,
                        ..
                    } => {
                        assert_eq!(*mode, EouResetMode::Stream);
                        assert_eq!(*anchor_sample, 1_600);
                        assert_eq!(*decision_sample, 4_800);
                        assert_eq!(*reason, "vad_speech_start");
                    }
                }
            },
        );
    }

    #[test]
    fn suppressed_model_eou_does_not_dispatch_reset() {
        with_worker(
            TurnManagerConfig {
                vad_close_enabled: true,
                model_eou_close_enabled: true,
                ..Default::default()
            },
            |worker, runtime, actions, _signal_batches| {
                let logger = worker.logger.clone();
                let mut writer = DetectorWriter::new(&logger, runtime.handle());
                let signals = vec![
                    DetectorSignal::VadSegmentStart {
                        detector: "silero_vad",
                        stream_id: "test.stream".into(),
                        stream_session_id: "test.session".into(),
                        adapter_id: "test.adapter".into(),
                        start_sample: 0,
                        decision_sample: 3_200,
                        confidence: Some(0.9),
                    },
                    DetectorSignal::ModelEou {
                        detector: "parakeet_realtime_eou_120m_v1",
                        stream_id: "test.stream".into(),
                        stream_session_id: "test.session".into(),
                        adapter_id: "test.adapter".into(),
                        end_sample: 8_000,
                        decision_sample: 8_000,
                        text_delta: String::new(),
                        confidence: None,
                    },
                ];
                worker.handle_signals(signals, &mut writer).unwrap();
                let actions = actions.lock().unwrap();
                assert_eq!(actions.len(), 1);
                match &actions[0] {
                    DetectorAction::ResetEouState { reason, .. } => {
                        assert_eq!(*reason, "vad_speech_start");
                    }
                }
            },
        );
    }

    #[test]
    fn accepted_model_eou_dispatches_decoder_reset() {
        with_worker(
            TurnManagerConfig {
                vad_close_enabled: false,
                model_eou_close_enabled: true,
                ..Default::default()
            },
            |worker, runtime, actions, _signal_batches| {
                let logger = worker.logger.clone();
                let mut writer = DetectorWriter::new(&logger, runtime.handle());
                let signals = vec![
                    DetectorSignal::VadSegmentStart {
                        detector: "silero_vad",
                        stream_id: "test.stream".into(),
                        stream_session_id: "test.session".into(),
                        adapter_id: "test.adapter".into(),
                        start_sample: 0,
                        decision_sample: 3_200,
                        confidence: Some(0.9),
                    },
                    DetectorSignal::VadSegmentEnd {
                        detector: "silero_vad",
                        stream_id: "test.stream".into(),
                        stream_session_id: "test.session".into(),
                        adapter_id: "test.adapter".into(),
                        start_sample: 0,
                        end_sample: 16_000,
                        decision_sample: 17_920,
                        confidence: Some(0.1),
                    },
                    DetectorSignal::ModelEou {
                        detector: "parakeet_realtime_eou_120m_v1",
                        stream_id: "test.stream".into(),
                        stream_session_id: "test.session".into(),
                        adapter_id: "test.adapter".into(),
                        end_sample: 24_000,
                        decision_sample: 24_000,
                        text_delta: String::new(),
                        confidence: None,
                    },
                ];
                worker.handle_signals(signals, &mut writer).unwrap();
                let actions = actions.lock().unwrap();
                assert!(actions.iter().any(|action| matches!(
                    action,
                    DetectorAction::ResetEouState {
                        mode: EouResetMode::Decoder,
                        reason: "eou_token_detected",
                        anchor_sample: 24_000,
                        ..
                    }
                )));
            },
        );
    }

    #[test]
    fn semantic_incomplete_suppresses_vad_close_without_reset() {
        with_worker(
            TurnManagerConfig {
                vad_close_enabled: true,
                semantic_gate_enabled: true,
                semantic_gate_close_enabled: true,
                ..Default::default()
            },
            |worker, runtime, actions, _signal_batches| {
                let logger = worker.logger.clone();
                let mut writer = DetectorWriter::new(&logger, runtime.handle());
                let signals = vec![
                    DetectorSignal::VadSegmentStart {
                        detector: "silero_vad",
                        stream_id: "test.stream".into(),
                        stream_session_id: "test.session".into(),
                        adapter_id: "test.adapter".into(),
                        start_sample: 0,
                        decision_sample: 3_200,
                        confidence: Some(0.9),
                    },
                    DetectorSignal::SemanticTurnDecision {
                        detector: smart_turn::DETECTOR,
                        stream_id: "test.stream".into(),
                        stream_session_id: "test.session".into(),
                        adapter_id: "test.adapter".into(),
                        end_sample: 16_000,
                        decision_sample: 17_920,
                        complete: false,
                        probability: Some(0.2),
                        threshold: Some(0.5),
                        timed_out: false,
                        available: true,
                        reason: "smart_turn_incomplete",
                        duration_ms: Some(10.0),
                    },
                    DetectorSignal::VadSegmentEnd {
                        detector: "silero_vad",
                        stream_id: "test.stream".into(),
                        stream_session_id: "test.session".into(),
                        adapter_id: "test.adapter".into(),
                        start_sample: 0,
                        end_sample: 16_000,
                        decision_sample: 17_920,
                        confidence: Some(0.1),
                    },
                ];
                worker.handle_signals(signals, &mut writer).unwrap();
                let actions = actions.lock().unwrap();
                assert_eq!(actions.len(), 1);
                assert!(matches!(
                    &actions[0],
                    DetectorAction::ResetEouState {
                        reason: "vad_speech_start",
                        ..
                    }
                ));
            },
        );
    }

    #[test]
    fn semantic_complete_closes_with_smart_turn_source() {
        with_worker(
            TurnManagerConfig {
                vad_close_enabled: true,
                semantic_gate_enabled: true,
                semantic_gate_close_enabled: true,
                ..Default::default()
            },
            |worker, runtime, actions, _signal_batches| {
                let logger = worker.logger.clone();
                let mut writer = DetectorWriter::new(&logger, runtime.handle());
                let signals = vec![
                    DetectorSignal::VadSegmentStart {
                        detector: "silero_vad",
                        stream_id: "test.stream".into(),
                        stream_session_id: "test.session".into(),
                        adapter_id: "test.adapter".into(),
                        start_sample: 0,
                        decision_sample: 3_200,
                        confidence: Some(0.9),
                    },
                    DetectorSignal::SemanticTurnDecision {
                        detector: smart_turn::DETECTOR,
                        stream_id: "test.stream".into(),
                        stream_session_id: "test.session".into(),
                        adapter_id: "test.adapter".into(),
                        end_sample: 16_000,
                        decision_sample: 17_920,
                        complete: true,
                        probability: Some(0.8),
                        threshold: Some(0.5),
                        timed_out: false,
                        available: true,
                        reason: "smart_turn_complete",
                        duration_ms: Some(10.0),
                    },
                    DetectorSignal::VadSegmentEnd {
                        detector: "silero_vad",
                        stream_id: "test.stream".into(),
                        stream_session_id: "test.session".into(),
                        adapter_id: "test.adapter".into(),
                        start_sample: 0,
                        end_sample: 16_000,
                        decision_sample: 17_920,
                        confidence: Some(0.1),
                    },
                ];
                worker.handle_signals(signals, &mut writer).unwrap();
                let actions = actions.lock().unwrap();
                assert!(actions.iter().any(|action| matches!(
                    action,
                    DetectorAction::ResetEouState {
                        source: "smart_turn",
                        reason: "smart_turn_complete_after_vad_speech_end",
                        mode: EouResetMode::Decoder,
                        anchor_sample: 17_920,
                        ..
                    }
                )));
            },
        );
    }

    #[test]
    fn worker_routes_actions_to_later_detectors_in_same_audio_frame() {
        with_worker(
            TurnManagerConfig {
                vad_close_enabled: true,
                model_eou_close_enabled: true,
                ..Default::default()
            },
            |worker, _runtime, actions, signal_batches| {
                signal_batches
                    .lock()
                    .unwrap()
                    .push(vec![DetectorSignal::VadSegmentStart {
                        detector: "silero_vad",
                        stream_id: "test.stream".into(),
                        stream_session_id: "test.session".into(),
                        adapter_id: "test.adapter".into(),
                        start_sample: 960,
                        decision_sample: 4_320,
                        confidence: Some(0.9),
                    }]);
                worker.handle(DetectorCommand::AudioFrame {
                    frame: test_frame(),
                    ingress_receive_mono_ns: 42,
                });
                let actions = actions.lock().unwrap();
                assert_eq!(actions.len(), 1);
            },
        );
    }
}
