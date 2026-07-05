use anyhow::{bail, Result};
use serde::Serialize;
use speech_core_protocol::{now_mono_ns, AudioFrame, PcmFormat};
use std::collections::HashMap;
use std::path::PathBuf;

use super::{AudioDetector, DetectorSignal, DetectorWriter};
use crate::HelloState;

const DETECTOR: &str = "silero_vad";
const SAMPLE_RATE: u32 = 16_000;
const FRAME_MS: u32 = 30;
const FRAME_SAMPLES: usize = (SAMPLE_RATE as usize * FRAME_MS as usize) / 1_000;

#[derive(Debug, Clone)]
pub struct SileroVadConfig {
    pub model_path: PathBuf,
    pub threshold: f32,
    pub onset_frames: usize,
    pub hangover_frames: usize,
    pub pre_speech_frames: usize,
    pub emit_frames: bool,
}

impl Default for SileroVadConfig {
    fn default() -> Self {
        Self {
            model_path: PathBuf::new(),
            threshold: 0.3,
            onset_frames: 2,
            hangover_frames: 8,
            pre_speech_frames: 5,
            emit_frames: false,
        }
    }
}

pub struct SileroVadDetector {
    config: SileroVadConfig,
    sessions: HashMap<String, VadSession>,
}

impl SileroVadDetector {
    pub fn new(config: SileroVadConfig) -> Result<Self> {
        if config.model_path.as_os_str().is_empty() {
            bail!("Silero VAD model path is empty");
        }
        if !config.model_path.exists() {
            bail!(
                "Silero VAD model not found: {}",
                config.model_path.display()
            );
        }
        if !(0.0..=1.0).contains(&config.threshold) {
            bail!("Silero VAD threshold must be between 0.0 and 1.0");
        }
        Ok(Self {
            config,
            sessions: HashMap::new(),
        })
    }
}

impl AudioDetector for SileroVadDetector {
    fn start_session(&mut self, hello: &HelloState, writer: &mut DetectorWriter<'_>) -> Result<()> {
        if hello.sample_rate_hz != SAMPLE_RATE || hello.channels != 1 {
            writer.write(&VadErrorEvent {
                event: "vad_error",
                stream_id: hello.stream_id.clone(),
                stream_session_id: hello.stream_session_id.clone(),
                adapter_id: hello.adapter_id.clone(),
                detector: DETECTOR,
                message: "Silero VAD requires 16 kHz mono PCM".to_owned(),
                daemon_mono_ns: now_mono_ns(),
            })?;
            return Ok(());
        }
        if !matches!(hello.format, PcmFormat::PcmF32Le | PcmFormat::PcmS16Le) {
            writer.write(&VadErrorEvent {
                event: "vad_error",
                stream_id: hello.stream_id.clone(),
                stream_session_id: hello.stream_session_id.clone(),
                adapter_id: hello.adapter_id.clone(),
                detector: DETECTOR,
                message: format!("unsupported PCM format for Silero VAD: {}", hello.format),
                daemon_mono_ns: now_mono_ns(),
            })?;
            return Ok(());
        }

        let open_start_mono_ns = now_mono_ns();
        let engine = vad_rs::Vad::new(&self.config.model_path, SAMPLE_RATE as usize)
            .map_err(|e| anyhow::anyhow!("failed to create Silero VAD: {e}"))?;
        let open_end_mono_ns = now_mono_ns();
        self.sessions.insert(
            hello.stream_session_id.clone(),
            VadSession::new(hello.clone(), engine, self.config.clone()),
        );
        writer.write(&VadSessionStartEvent {
            event: "vad_session_start",
            stream_id: hello.stream_id.clone(),
            stream_session_id: hello.stream_session_id.clone(),
            adapter_id: hello.adapter_id.clone(),
            detector: DETECTOR,
            model_path: self.config.model_path.display().to_string(),
            threshold: self.config.threshold,
            frame_ms: FRAME_MS,
            frame_samples: FRAME_SAMPLES as u32,
            onset_frames: self.config.onset_frames as u32,
            hangover_frames: self.config.hangover_frames as u32,
            pre_speech_frames: self.config.pre_speech_frames as u32,
            emit_frames: self.config.emit_frames,
            open_start_mono_ns,
            open_end_mono_ns,
            open_duration_ms: ns_to_ms(open_end_mono_ns.saturating_sub(open_start_mono_ns)),
        })?;
        Ok(())
    }

    fn ingest_frame(
        &mut self,
        frame: &AudioFrame,
        samples: &[f32],
        ingress_receive_mono_ns: u64,
        writer: &mut DetectorWriter<'_>,
    ) -> Result<Vec<DetectorSignal>> {
        let Some(session) = self.sessions.get_mut(&frame.header.stream_session_id) else {
            return Ok(Vec::new());
        };
        session.push_samples(frame, samples);
        let mut signals = Vec::new();
        while session.buffer.len() >= FRAME_SAMPLES {
            let vad_frame: Vec<f32> = session.buffer.drain(..FRAME_SAMPLES).collect();
            let frame_start_sample = session.next_vad_frame_sample_start;
            session.next_vad_frame_sample_start = session
                .next_vad_frame_sample_start
                .saturating_add(FRAME_SAMPLES as u64);
            signals.extend(session.process_vad_frame(
                &vad_frame,
                frame_start_sample,
                ingress_receive_mono_ns,
                writer,
            )?);
        }
        Ok(signals)
    }

    fn end_session(
        &mut self,
        stream_session_id: &str,
        reason: &str,
        writer: &mut DetectorWriter<'_>,
    ) -> Result<Vec<DetectorSignal>> {
        let Some(mut session) = self.sessions.remove(stream_session_id) else {
            return Ok(Vec::new());
        };
        let mut signals = Vec::new();
        if !session.buffer.is_empty() {
            let frame_start_sample = session.next_vad_frame_sample_start;
            let mut frame = std::mem::take(&mut session.buffer);
            frame.resize(FRAME_SAMPLES, 0.0);
            signals.extend(session.process_vad_frame(
                &frame,
                frame_start_sample,
                now_mono_ns(),
                writer,
            )?);
        }
        if session.in_speech {
            let end_sample = session
                .last_voice_sample_end
                .unwrap_or(session.next_vad_frame_sample_start);
            let decision_sample = session.next_vad_frame_sample_start;
            writer.write(&VadSpeechEndEvent {
                event: "vad_speech_end",
                stream_id: session.hello.stream_id.clone(),
                stream_session_id: session.hello.stream_session_id.clone(),
                adapter_id: session.hello.adapter_id.clone(),
                detector: DETECTOR,
                segment_index: session.segment_index,
                start_sample: session.current_segment_start_sample.unwrap_or(end_sample),
                end_sample,
                decision_sample,
                reason: reason.to_owned(),
                confidence: session.last_probability,
                daemon_mono_ns: now_mono_ns(),
            })?;
            signals.push(DetectorSignal::VadSegmentEnd {
                detector: DETECTOR,
                stream_id: session.hello.stream_id.clone(),
                stream_session_id: session.hello.stream_session_id.clone(),
                adapter_id: session.hello.adapter_id.clone(),
                start_sample: session.current_segment_start_sample.unwrap_or(end_sample),
                end_sample,
                decision_sample,
                confidence: session.last_probability,
            });
        }
        writer.write(&VadSessionEndEvent {
            event: "vad_session_end",
            stream_id: session.hello.stream_id,
            stream_session_id: session.hello.stream_session_id,
            adapter_id: session.hello.adapter_id,
            detector: DETECTOR,
            reason: reason.to_owned(),
            frames_processed: session.frames_processed,
            segments_detected: session.segment_index,
            daemon_mono_ns: now_mono_ns(),
        })?;
        Ok(signals)
    }
}

struct VadSession {
    hello: HelloState,
    engine: vad_rs::Vad,
    config: SileroVadConfig,
    buffer: Vec<f32>,
    next_vad_frame_sample_start: u64,
    in_speech: bool,
    onset_counter: usize,
    candidate_start_sample: Option<u64>,
    silence_start_sample: Option<u64>,
    silence_counter: usize,
    current_segment_start_sample: Option<u64>,
    last_voice_sample_end: Option<u64>,
    last_probability: Option<f32>,
    frames_processed: u64,
    segment_index: u32,
}

impl VadSession {
    fn new(hello: HelloState, engine: vad_rs::Vad, config: SileroVadConfig) -> Self {
        Self {
            hello,
            engine,
            config,
            buffer: Vec::with_capacity(FRAME_SAMPLES * 4),
            next_vad_frame_sample_start: 0,
            in_speech: false,
            onset_counter: 0,
            candidate_start_sample: None,
            silence_start_sample: None,
            silence_counter: 0,
            current_segment_start_sample: None,
            last_voice_sample_end: None,
            last_probability: None,
            frames_processed: 0,
            segment_index: 0,
        }
    }

    fn push_samples(&mut self, frame: &AudioFrame, samples: &[f32]) {
        if self.buffer.is_empty() {
            self.next_vad_frame_sample_start = frame.header.source_sample_start;
        }
        self.buffer.extend_from_slice(samples);
    }

    fn process_vad_frame(
        &mut self,
        frame: &[f32],
        frame_start_sample: u64,
        ingress_receive_mono_ns: u64,
        writer: &mut DetectorWriter<'_>,
    ) -> Result<Vec<DetectorSignal>> {
        let model_start_mono_ns = now_mono_ns();
        let result = self
            .engine
            .compute(frame)
            .map_err(|e| anyhow::anyhow!("Silero VAD inference failed: {e}"))?;
        let model_end_mono_ns = now_mono_ns();
        let probability = result.prob;
        self.last_probability = Some(probability);
        self.frames_processed = self.frames_processed.saturating_add(1);
        let raw_is_speech = probability > self.config.threshold;
        let frame_end_sample = frame_start_sample.saturating_add(FRAME_SAMPLES as u64);
        let mut signals = Vec::new();

        if raw_is_speech {
            self.last_voice_sample_end = Some(frame_end_sample);
            self.silence_counter = 0;
            self.silence_start_sample = None;
            if self.in_speech {
                // ongoing speech
            } else {
                if self.onset_counter == 0 {
                    let pre_roll = (self.config.pre_speech_frames * FRAME_SAMPLES) as u64;
                    self.candidate_start_sample = Some(frame_start_sample.saturating_sub(pre_roll));
                }
                self.onset_counter = self.onset_counter.saturating_add(1);
                if self.onset_counter >= self.config.onset_frames.max(1) {
                    self.in_speech = true;
                    self.segment_index = self.segment_index.saturating_add(1);
                    let start_sample = self.candidate_start_sample.unwrap_or(frame_start_sample);
                    self.current_segment_start_sample = Some(start_sample);
                    self.onset_counter = 0;
                    writer.write(&VadSpeechStartEvent {
                        event: "vad_speech_start",
                        stream_id: self.hello.stream_id.clone(),
                        stream_session_id: self.hello.stream_session_id.clone(),
                        adapter_id: self.hello.adapter_id.clone(),
                        detector: DETECTOR,
                        segment_index: self.segment_index,
                        start_sample,
                        decision_sample: frame_end_sample,
                        confidence: probability,
                        daemon_mono_ns: model_end_mono_ns,
                    })?;
                    signals.push(DetectorSignal::VadSegmentStart {
                        detector: DETECTOR,
                        stream_id: self.hello.stream_id.clone(),
                        stream_session_id: self.hello.stream_session_id.clone(),
                        adapter_id: self.hello.adapter_id.clone(),
                        start_sample,
                        decision_sample: frame_end_sample,
                        confidence: Some(probability),
                    });
                }
            }
        } else if self.in_speech {
            if self.silence_counter == 0 {
                self.silence_start_sample = Some(frame_start_sample);
            }
            self.silence_counter = self.silence_counter.saturating_add(1);
            if self.silence_counter >= self.config.hangover_frames.max(1) {
                self.in_speech = false;
                let end_sample = self
                    .silence_start_sample
                    .or(self.last_voice_sample_end)
                    .unwrap_or(frame_start_sample);
                let start_sample = self.current_segment_start_sample.unwrap_or(end_sample);
                writer.write(&VadSpeechEndEvent {
                    event: "vad_speech_end",
                    stream_id: self.hello.stream_id.clone(),
                    stream_session_id: self.hello.stream_session_id.clone(),
                    adapter_id: self.hello.adapter_id.clone(),
                    detector: DETECTOR,
                    segment_index: self.segment_index,
                    start_sample,
                    end_sample,
                    decision_sample: frame_end_sample,
                    reason: "silence_hangover_elapsed".to_owned(),
                    confidence: Some(probability),
                    daemon_mono_ns: model_end_mono_ns,
                })?;
                signals.push(DetectorSignal::VadSegmentEnd {
                    detector: DETECTOR,
                    stream_id: self.hello.stream_id.clone(),
                    stream_session_id: self.hello.stream_session_id.clone(),
                    adapter_id: self.hello.adapter_id.clone(),
                    start_sample,
                    end_sample,
                    decision_sample: frame_end_sample,
                    confidence: Some(probability),
                });
                self.current_segment_start_sample = None;
                self.silence_counter = 0;
                self.silence_start_sample = None;
            }
        } else {
            self.onset_counter = 0;
            self.candidate_start_sample = None;
        }

        if self.config.emit_frames {
            writer.write(&VadFrameEvent {
                event: "vad_frame",
                stream_id: self.hello.stream_id.clone(),
                stream_session_id: self.hello.stream_session_id.clone(),
                adapter_id: self.hello.adapter_id.clone(),
                detector: DETECTOR,
                frame_index: self.frames_processed.saturating_sub(1),
                sample_start: frame_start_sample,
                sample_count: FRAME_SAMPLES as u32,
                probability,
                threshold: self.config.threshold,
                raw_is_speech,
                smoothed_in_speech: self.in_speech,
                ingress_receive_mono_ns,
                detector_start_mono_ns: model_start_mono_ns,
                detector_end_mono_ns: model_end_mono_ns,
                detector_duration_ms: ns_to_ms(
                    model_end_mono_ns.saturating_sub(model_start_mono_ns),
                ),
            })?;
        }

        Ok(signals)
    }
}

#[derive(Debug, Serialize)]
struct VadSessionStartEvent {
    event: &'static str,
    stream_id: String,
    stream_session_id: String,
    adapter_id: String,
    detector: &'static str,
    model_path: String,
    threshold: f32,
    frame_ms: u32,
    frame_samples: u32,
    onset_frames: u32,
    hangover_frames: u32,
    pre_speech_frames: u32,
    emit_frames: bool,
    open_start_mono_ns: u64,
    open_end_mono_ns: u64,
    open_duration_ms: f64,
}

#[derive(Debug, Serialize)]
struct VadFrameEvent {
    event: &'static str,
    stream_id: String,
    stream_session_id: String,
    adapter_id: String,
    detector: &'static str,
    frame_index: u64,
    sample_start: u64,
    sample_count: u32,
    probability: f32,
    threshold: f32,
    raw_is_speech: bool,
    smoothed_in_speech: bool,
    ingress_receive_mono_ns: u64,
    detector_start_mono_ns: u64,
    detector_end_mono_ns: u64,
    detector_duration_ms: f64,
}

#[derive(Debug, Serialize)]
struct VadSpeechStartEvent {
    event: &'static str,
    stream_id: String,
    stream_session_id: String,
    adapter_id: String,
    detector: &'static str,
    segment_index: u32,
    start_sample: u64,
    decision_sample: u64,
    confidence: f32,
    daemon_mono_ns: u64,
}

#[derive(Debug, Serialize)]
struct VadSpeechEndEvent {
    event: &'static str,
    stream_id: String,
    stream_session_id: String,
    adapter_id: String,
    detector: &'static str,
    segment_index: u32,
    start_sample: u64,
    end_sample: u64,
    decision_sample: u64,
    reason: String,
    confidence: Option<f32>,
    daemon_mono_ns: u64,
}

#[derive(Debug, Serialize)]
struct VadSessionEndEvent {
    event: &'static str,
    stream_id: String,
    stream_session_id: String,
    adapter_id: String,
    detector: &'static str,
    reason: String,
    frames_processed: u64,
    segments_detected: u32,
    daemon_mono_ns: u64,
}

#[derive(Debug, Serialize)]
struct VadErrorEvent {
    event: &'static str,
    stream_id: String,
    stream_session_id: String,
    adapter_id: String,
    detector: &'static str,
    message: String,
    daemon_mono_ns: u64,
}

fn ns_to_ms(ns: u64) -> f64 {
    ns as f64 / 1_000_000.0
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn frame_size_is_30ms_at_16khz() {
        assert_eq!(FRAME_SAMPLES, 480);
    }

    #[test]
    fn default_hangover_is_240ms() {
        assert_eq!(
            SileroVadConfig::default().hangover_frames * FRAME_MS as usize,
            240
        );
    }
}
