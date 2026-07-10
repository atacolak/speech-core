use anyhow::{bail, Result};
use serde::Serialize;
use speech_core_protocol::{now_mono_ns, AudioFrame, PcmFormat};
use std::collections::HashMap;
use std::path::PathBuf;

use super::{AudioDetector, DetectorAction, DetectorSignal, DetectorWriter};
use crate::{AudioGapReset, HelloState};

const DETECTOR: &str = "silero_vad";
const SAMPLE_RATE: u32 = 16_000;
// Silero v4 at 16 kHz is calibrated for 512-sample (~32 ms) recurrent
// windows. Feeding it 320-sample / 20 ms windows does not error, but in live
// captures it produced near-zero probabilities for obvious speech. Keep the
// adapter/audio transport at 20 ms if desired; the detector window must stay at
// the model's native size.
const FRAME_SAMPLES: usize = 512;
const FRAME_MS: u32 = (FRAME_SAMPLES as u32 * 1_000) / SAMPLE_RATE;

#[derive(Debug, Clone)]
pub struct SileroVadConfig {
    pub model_path: PathBuf,
    pub threshold: f32,
    pub onset_frames: usize,
    pub hangover_frames: usize,
    pub pre_speech_frames: usize,
    pub emit_frames: bool,
    pub smoothing_alpha: f32,
    pub stop_threshold: f32,
    pub fallback_threshold: f32,
    pub acoustic_fallback_silence_ms: u32,
    /// Pre-VAD energy gate: skip inference when RMS < energy_threshold.
    pub energy_enabled: bool,
    /// Normalized RMS floor; audio below this is treated as silence.
    pub energy_threshold: f32,
}

impl SileroVadConfig {
    pub fn speech_end_silence_ms(&self) -> u32 {
        self.hangover_frames.max(1) as u32 * FRAME_MS
    }
}

impl Default for SileroVadConfig {
    fn default() -> Self {
        Self {
            model_path: PathBuf::new(),
            threshold: 0.5,
            onset_frames: 2,
            hangover_frames: 3,
            pre_speech_frames: 5,
            emit_frames: false,
            smoothing_alpha: 0.1,
            stop_threshold: 0.2,
            fallback_threshold: 0.1,
            acoustic_fallback_silence_ms: u32::MAX,
            energy_enabled: false,
            energy_threshold: 0.01,
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
        if !(0.0..=1.0).contains(&config.smoothing_alpha) || config.smoothing_alpha == 0.0 {
            bail!("Silero VAD smoothing_alpha must be in (0.0, 1.0]");
        }
        if !(0.0..=1.0).contains(&config.stop_threshold) {
            bail!("Silero VAD stop_threshold must be between 0.0 and 1.0");
        }
        if !(0.0..=1.0).contains(&config.fallback_threshold) {
            bail!("Silero VAD fallback_threshold must be between 0.0 and 1.0");
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
            bail!("Silero VAD requires 16 kHz mono PCM");
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
            bail!("unsupported PCM format for Silero VAD: {}", hello.format);
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
            speech_end_silence_ms: self.config.speech_end_silence_ms(),
            emit_frames: self.config.emit_frames,
            smoothing_alpha: self.config.smoothing_alpha,
            stop_threshold: self.config.stop_threshold,
            fallback_threshold: self
                .config
                .fallback_threshold
                .max(self.config.stop_threshold),
            configured_fallback_threshold: self.config.fallback_threshold,
            acoustic_fallback_silence_ms: self.config.acoustic_fallback_silence_ms,
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
                confidence: session.smoothed_probability.or(session.last_probability),
                raw_probability: session.last_probability.unwrap_or_default(),
                smoothed_probability: session.smoothed_probability.unwrap_or_default(),
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
                confidence: session.smoothed_probability.or(session.last_probability),
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

    fn handle_action(
        &mut self,
        action: &DetectorAction,
        _writer: &mut DetectorWriter<'_>,
    ) -> Result<()> {
        let DetectorAction::ResetEouState {
            stream_session_id,
            reason,
            ..
        } = action;
        if *reason == "vad_speech_start" {
            return Ok(());
        }
        if let Some(session) = self.sessions.get_mut(stream_session_id) {
            session.acoustic_fallback_emitted = true;
            session.last_segment_start_sample = None;
            session.last_segment_end_sample = None;
            session.last_segment_end_confidence = None;
            if reset_requires_low_release(reason) {
                // Forced closes happen while VAD may still be in speech/noise. Clear the
                // old segment and require an actual low-probability release before onset
                // can arm again, otherwise continuous above-threshold noise immediately
                // resurrects the just-closed segment.
                session.reset_after_forced_close();
            }
        }
        Ok(())
    }

    fn audio_gap_reset(
        &mut self,
        gap: &AudioGapReset,
        _writer: &mut DetectorWriter<'_>,
    ) -> Result<()> {
        if let Some(session) = self.sessions.get_mut(&gap.stream_session_id) {
            session.buffer.clear();
            session.next_vad_frame_sample_start = gap.observed_sample_start;
            session.in_speech = false;
            session.onset_counter = 0;
            session.candidate_start_sample = None;
            session.silence_start_sample = None;
            session.silence_counter = 0;
            session.current_segment_start_sample = None;
            session.last_voice_sample_end = None;
            session.last_raw_is_speech = None;
            session.last_smoothed_in_speech = None;
            session.low_silence_start_sample = None;
            session.acoustic_fallback_emitted = true;
        }
        Ok(())
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
    smoothed_probability: Option<f32>,
    last_raw_is_speech: Option<bool>,
    last_smoothed_in_speech: Option<bool>,
    last_segment_start_sample: Option<u64>,
    last_segment_end_sample: Option<u64>,
    last_segment_end_confidence: Option<f32>,
    acoustic_fallback_emitted: bool,
    low_silence_start_sample: Option<u64>,
    forced_close_rearm_required: bool,
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
            smoothed_probability: None,
            last_raw_is_speech: None,
            last_smoothed_in_speech: None,
            last_segment_start_sample: None,
            last_segment_end_sample: None,
            last_segment_end_confidence: None,
            acoustic_fallback_emitted: true,
            low_silence_start_sample: None,
            forced_close_rearm_required: false,
            frames_processed: 0,
            segment_index: 0,
        }
    }

    fn reset_after_forced_close(&mut self) {
        self.in_speech = false;
        self.onset_counter = 0;
        self.silence_counter = 0;
        self.silence_start_sample = None;
        self.candidate_start_sample = None;
        self.current_segment_start_sample = None;
        self.last_voice_sample_end = None;
        self.low_silence_start_sample = None;
        self.forced_close_rearm_required = true;
    }

    fn release_forced_close_rearm(&mut self) {
        self.forced_close_rearm_required = false;
        self.onset_counter = 0;
        self.candidate_start_sample = None;
    }

    fn onset_can_arm(&self) -> bool {
        !self.forced_close_rearm_required
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
        // Energy gate: skip VAD inference on near-silent frames.
        let energy_rms = if self.config.energy_enabled {
            let sum_sq: f32 = frame.iter().map(|s| s * s).sum();
            (sum_sq / frame.len() as f32).sqrt()
        } else {
            f32::NAN
        };
        let energy_gated = self.config.energy_enabled && energy_rms < self.config.energy_threshold;
        let probability = if energy_gated {
            0.0_f32
        } else {
            let result = self
                .engine
                .compute(frame)
                .map_err(|e| anyhow::anyhow!("Silero VAD inference failed: {e}"))?;
            result.prob
        };
        let model_end_mono_ns = now_mono_ns();
        self.last_probability = Some(probability);
        let previous_smoothed_probability = self.smoothed_probability.unwrap_or(probability);
        let alpha = self.config.smoothing_alpha.clamp(0.000_001, 1.0);
        let smoothed_probability =
            alpha.mul_add(probability, (1.0 - alpha) * previous_smoothed_probability);
        self.smoothed_probability = Some(smoothed_probability);
        self.frames_processed = self.frames_processed.saturating_add(1);
        let raw_is_speech = probability > self.config.threshold;
        let start_threshold = self.config.threshold;
        let stop_threshold = self.config.stop_threshold.min(start_threshold);
        let previous_raw_is_speech = self.last_raw_is_speech;
        let previous_smoothed_in_speech = self.last_smoothed_in_speech;
        let frame_end_sample = frame_start_sample.saturating_add(FRAME_SAMPLES as u64);
        let mut signals = Vec::new();
        let rearm_released =
            self.forced_close_rearm_required && smoothed_probability < stop_threshold;
        if rearm_released {
            self.release_forced_close_rearm();
        }

        if self.in_speech {
            if smoothed_probability >= stop_threshold {
                self.last_voice_sample_end = Some(frame_end_sample);
                self.low_silence_start_sample = None;
                self.silence_counter = 0;
                self.silence_start_sample = None;
                signals.push(DetectorSignal::VadSpeechPresence {
                    detector: DETECTOR,
                    stream_id: self.hello.stream_id.clone(),
                    stream_session_id: self.hello.stream_session_id.clone(),
                    adapter_id: self.hello.adapter_id.clone(),
                    start_sample: self
                        .current_segment_start_sample
                        .unwrap_or(frame_start_sample),
                    decision_sample: frame_end_sample,
                    confidence: Some(smoothed_probability),
                });
            } else {
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
                        reason: "smoothed_hangover_elapsed".to_owned(),
                        confidence: Some(smoothed_probability),
                        raw_probability: probability,
                        smoothed_probability,
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
                        confidence: Some(smoothed_probability),
                    });
                    self.last_segment_start_sample = Some(start_sample);
                    self.last_segment_end_sample = Some(end_sample);
                    self.last_segment_end_confidence = Some(smoothed_probability);
                    self.acoustic_fallback_emitted = false;
                    self.current_segment_start_sample = None;
                    self.silence_counter = 0;
                    self.silence_start_sample = None;
                }
            }
        } else if self.onset_can_arm() && (raw_is_speech || smoothed_probability >= start_threshold)
        {
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
                self.last_voice_sample_end = Some(frame_end_sample);
                self.low_silence_start_sample = None;
                self.acoustic_fallback_emitted = true;
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
                    confidence: smoothed_probability,
                    raw_probability: probability,
                    smoothed_probability,
                    daemon_mono_ns: model_end_mono_ns,
                })?;
                signals.push(DetectorSignal::VadSegmentStart {
                    detector: DETECTOR,
                    stream_id: self.hello.stream_id.clone(),
                    stream_session_id: self.hello.stream_session_id.clone(),
                    adapter_id: self.hello.adapter_id.clone(),
                    start_sample,
                    decision_sample: frame_end_sample,
                    confidence: Some(smoothed_probability),
                });
            }
        } else {
            self.onset_counter = 0;
            self.candidate_start_sample = None;
        }

        if !self.in_speech && smoothed_probability < stop_threshold {
            let low_start = *self
                .low_silence_start_sample
                .get_or_insert(frame_start_sample);
            signals.push(DetectorSignal::VadLowSilence {
                detector: DETECTOR,
                stream_id: self.hello.stream_id.clone(),
                stream_session_id: self.hello.stream_session_id.clone(),
                adapter_id: self.hello.adapter_id.clone(),
                start_sample: low_start,
                decision_sample: frame_end_sample,
                silence_samples: frame_end_sample.saturating_sub(low_start),
                confidence: Some(smoothed_probability),
            });
        } else if !self.in_speech {
            self.low_silence_start_sample = None;
        }

        let effective_fallback_threshold = self
            .config
            .fallback_threshold
            .max(self.config.stop_threshold);
        if !self.in_speech
            && !self.acoustic_fallback_emitted
            && smoothed_probability <= effective_fallback_threshold
        {
            if let (Some(start_sample), Some(end_sample)) =
                (self.last_segment_start_sample, self.last_segment_end_sample)
            {
                let required_silence_samples =
                    ms_to_samples(self.config.acoustic_fallback_silence_ms);
                let silence_samples = frame_end_sample.saturating_sub(end_sample);
                if silence_samples >= required_silence_samples {
                    self.acoustic_fallback_emitted = true;
                    writer.write(&VadAcousticFallbackEvent {
                        event: "vad_acoustic_fallback",
                        stream_id: self.hello.stream_id.clone(),
                        stream_session_id: self.hello.stream_session_id.clone(),
                        adapter_id: self.hello.adapter_id.clone(),
                        detector: DETECTOR,
                        segment_index: self.segment_index,
                        start_sample,
                        end_sample,
                        decision_sample: frame_end_sample,
                        silence_samples,
                        confidence: Some(smoothed_probability),
                        raw_probability: probability,
                        smoothed_probability,
                        fallback_threshold: effective_fallback_threshold,
                        configured_fallback_threshold: self.config.fallback_threshold,
                        acoustic_fallback_silence_ms: self.config.acoustic_fallback_silence_ms,
                        daemon_mono_ns: model_end_mono_ns,
                    })?;
                    signals.push(DetectorSignal::VadAcousticFallback {
                        detector: DETECTOR,
                        stream_id: self.hello.stream_id.clone(),
                        stream_session_id: self.hello.stream_session_id.clone(),
                        adapter_id: self.hello.adapter_id.clone(),
                        start_sample,
                        end_sample,
                        decision_sample: frame_end_sample,
                        silence_samples,
                        confidence: self
                            .last_segment_end_confidence
                            .or(Some(smoothed_probability)),
                    });
                }
            }
        }

        let raw_changed = previous_raw_is_speech != Some(raw_is_speech);
        let state_changed = previous_smoothed_in_speech != Some(self.in_speech);
        let hangover_progress =
            self.in_speech && smoothed_probability < stop_threshold && self.silence_counter > 0;
        self.last_raw_is_speech = Some(raw_is_speech);
        self.last_smoothed_in_speech = Some(self.in_speech);
        if raw_changed || state_changed || hangover_progress {
            writer.write(&VadStateEvent {
                event: "vad_state",
                stream_id: self.hello.stream_id.clone(),
                stream_session_id: self.hello.stream_session_id.clone(),
                adapter_id: self.hello.adapter_id.clone(),
                detector: DETECTOR,
                frame_index: self.frames_processed.saturating_sub(1),
                sample_start: frame_start_sample,
                sample_count: FRAME_SAMPLES as u32,
                sample_time_ms: samples_to_ms(frame_start_sample),
                probability,
                smoothed_probability,
                threshold: self.config.threshold,
                stop_threshold,
                fallback_threshold: self.config.fallback_threshold,
                raw_is_speech,
                smoothed_in_speech: self.in_speech,
                raw_changed,
                state_changed,
                hangover_progress,
                silence_counter: self.silence_counter as u32,
                hangover_frames: self.config.hangover_frames as u32,
                ingress_receive_mono_ns,
                detector_start_mono_ns: model_start_mono_ns,
                detector_end_mono_ns: model_end_mono_ns,
                detector_duration_ms: ns_to_ms(
                    model_end_mono_ns.saturating_sub(model_start_mono_ns),
                ),
            })?;
        }

        let effective_fallback_threshold = self
            .config
            .fallback_threshold
            .max(self.config.stop_threshold);
        let fallback_progress_ms = if !self.in_speech
            && smoothed_probability <= effective_fallback_threshold
            && self.last_segment_end_sample.is_some()
        {
            let silence_samples = frame_end_sample
                .saturating_sub(self.last_segment_end_sample.unwrap_or(frame_end_sample));
            samples_to_ms(silence_samples) as u32
        } else {
            0u32
        };

        writer.write(&VadMeterEvent {
            event: "vad_meter",
            stream_id: self.hello.stream_id.clone(),
            stream_session_id: self.hello.stream_session_id.clone(),
            adapter_id: self.hello.adapter_id.clone(),
            detector: DETECTOR,
            frame_index: self.frames_processed.saturating_sub(1),
            sample_start: frame_start_sample,
            sample_count: FRAME_SAMPLES as u32,
            sample_time_ms: samples_to_ms(frame_start_sample),
            probability,
            smoothed_probability,
            threshold: self.config.threshold,
            stop_threshold,
            fallback_threshold: self.config.fallback_threshold,
            fallback_progress_ms,
            fallback_target_ms: self.config.acoustic_fallback_silence_ms,
            raw_is_speech,
            smoothed_in_speech: self.in_speech,
            silence_counter: self.silence_counter as u32,
            hangover_frames: self.config.hangover_frames as u32,
            energy_rms: if self.config.energy_enabled {
                Some(energy_rms)
            } else {
                None
            },
            energy_gated,
            ingress_receive_mono_ns,
            detector_start_mono_ns: model_start_mono_ns,
            detector_end_mono_ns: model_end_mono_ns,
            detector_duration_ms: ns_to_ms(model_end_mono_ns.saturating_sub(model_start_mono_ns)),
        })?;

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
                sample_time_ms: samples_to_ms(frame_start_sample),
                probability,
                smoothed_probability,
                threshold: self.config.threshold,
                stop_threshold,
                fallback_threshold: self.config.fallback_threshold,
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
    speech_end_silence_ms: u32,
    emit_frames: bool,
    smoothing_alpha: f32,
    stop_threshold: f32,
    fallback_threshold: f32,
    configured_fallback_threshold: f32,
    acoustic_fallback_silence_ms: u32,
    open_start_mono_ns: u64,
    open_end_mono_ns: u64,
    open_duration_ms: f64,
}

#[derive(Debug, Serialize)]
struct VadStateEvent {
    event: &'static str,
    stream_id: String,
    stream_session_id: String,
    adapter_id: String,
    detector: &'static str,
    frame_index: u64,
    sample_start: u64,
    sample_count: u32,
    sample_time_ms: u64,
    probability: f32,
    smoothed_probability: f32,
    threshold: f32,
    stop_threshold: f32,
    fallback_threshold: f32,
    raw_is_speech: bool,
    smoothed_in_speech: bool,
    raw_changed: bool,
    state_changed: bool,
    hangover_progress: bool,
    silence_counter: u32,
    hangover_frames: u32,
    ingress_receive_mono_ns: u64,
    detector_start_mono_ns: u64,
    detector_end_mono_ns: u64,
    detector_duration_ms: f64,
}

#[derive(Debug, Serialize)]
struct VadMeterEvent {
    event: &'static str,
    stream_id: String,
    stream_session_id: String,
    adapter_id: String,
    detector: &'static str,
    frame_index: u64,
    sample_start: u64,
    sample_count: u32,
    sample_time_ms: u64,
    probability: f32,
    smoothed_probability: f32,
    threshold: f32,
    stop_threshold: f32,
    fallback_threshold: f32,
    raw_is_speech: bool,
    smoothed_in_speech: bool,
    silence_counter: u32,
    hangover_frames: u32,
    fallback_progress_ms: u32,
    fallback_target_ms: u32,
    energy_rms: Option<f32>,
    energy_gated: bool,
    ingress_receive_mono_ns: u64,
    detector_start_mono_ns: u64,
    detector_end_mono_ns: u64,
    detector_duration_ms: f64,
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
    sample_time_ms: u64,
    probability: f32,
    smoothed_probability: f32,
    threshold: f32,
    stop_threshold: f32,
    fallback_threshold: f32,
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
    raw_probability: f32,
    smoothed_probability: f32,
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
    raw_probability: f32,
    smoothed_probability: f32,
    daemon_mono_ns: u64,
}

#[derive(Debug, Serialize)]
struct VadAcousticFallbackEvent {
    event: &'static str,
    stream_id: String,
    stream_session_id: String,
    adapter_id: String,
    detector: &'static str,
    segment_index: u32,
    start_sample: u64,
    end_sample: u64,
    decision_sample: u64,
    silence_samples: u64,
    confidence: Option<f32>,
    raw_probability: f32,
    smoothed_probability: f32,
    fallback_threshold: f32,
    configured_fallback_threshold: f32,
    acoustic_fallback_silence_ms: u32,
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

fn samples_to_ms(samples: u64) -> u64 {
    samples.saturating_mul(1_000) / SAMPLE_RATE as u64
}

fn ms_to_samples(ms: u32) -> u64 {
    u64::from(ms).saturating_mul(u64::from(SAMPLE_RATE)) / 1_000
}

fn ns_to_ms(ns: u64) -> f64 {
    ns as f64 / 1_000_000.0
}

fn reset_requires_low_release(reason: &str) -> bool {
    matches!(
        reason,
        "smart_turn_complete_direct"
            | "human_hold_speech_like_audio_without_tokens"
            | "vad_acoustic_fallback_low_probability_silence"
            | "transcript_backed_turn_low_vad_silence"
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use speech_core_protocol::{ClockDomain, SourceKind, TimestampProvenance, TimestampQuality};
    use tempfile::tempdir;
    use tokio::runtime::Runtime;
    use tokio::sync::broadcast;

    const SESSION_ID: &str = "test.session";

    fn test_hello() -> HelloState {
        HelloState {
            adapter_id: "test.adapter".to_owned(),
            stream_id: "test.stream".to_owned(),
            stream_session_id: SESSION_ID.to_owned(),
            source_kind: SourceKind::Synthetic,
            sample_rate_hz: SAMPLE_RATE,
            channels: 1,
            format: PcmFormat::PcmF32Le,
            timestamp_provenance: TimestampProvenance::uncalibrated(
                "test-clock",
                ClockDomain::HostMonotonic,
                TimestampQuality::SyntheticScheduled,
            ),
            generation: 0,
        }
    }

    fn test_action(reason: &'static str, decision_sample: u64) -> DetectorAction {
        DetectorAction::ResetEouState {
            stream_id: "test.stream".to_owned(),
            stream_session_id: SESSION_ID.to_owned(),
            adapter_id: "test.adapter".to_owned(),
            mode: super::super::EouResetMode::Decoder,
            anchor_sample: decision_sample,
            source: "test",
            reason,
            decision_sample,
        }
    }

    fn test_session() -> VadSession {
        VadSession::new(
            test_hello(),
            vad_rs::Vad::new(
                &PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../models/silero_vad_v4.onnx"),
                SAMPLE_RATE as usize,
            )
            .expect("test VAD model should load"),
            SileroVadConfig::default(),
        )
    }

    fn writer_fixture() -> (
        Runtime,
        crate::JsonlLogger,
        tempfile::TempDir,
        broadcast::Receiver<String>,
    ) {
        let runtime = Runtime::new().expect("test runtime should start");
        let dir = tempdir().expect("temp log dir should be created");
        let (event_tx, events) = broadcast::channel(256);
        let logger = runtime
            .block_on(crate::JsonlLogger::open(dir.path().to_path_buf(), event_tx))
            .expect("test logger should open");
        (runtime, logger, dir, events)
    }

    #[test]
    fn frame_size_is_native_silero_window_at_16khz() {
        assert_eq!(FRAME_SAMPLES, 512);
        assert_eq!(FRAME_MS, 32);
    }

    #[test]
    fn default_hangover_is_about_100ms() {
        assert_eq!(
            SileroVadConfig::default().hangover_frames * FRAME_MS as usize,
            96
        );
    }

    #[test]
    fn vad_close_gap_timeout_uses_configured_hangover_duration() {
        let config = SileroVadConfig {
            hangover_frames: 5,
            acoustic_fallback_silence_ms: 250,
            ..Default::default()
        };
        assert_eq!(config.speech_end_silence_ms(), 160);
        assert_eq!(
            SileroVadConfig::default().acoustic_fallback_silence_ms,
            u32::MAX,
            "the separate acoustic fallback timer is disabled by default so speech_end hangover is the close-gap source of truth"
        );
    }
    #[test]
    fn forced_close_reset_clears_vad_state_and_requires_release() {
        let mut session = test_session();
        session.in_speech = true;
        session.onset_counter = 2;
        session.silence_counter = 1;
        session.silence_start_sample = Some(10);
        session.candidate_start_sample = Some(20);
        session.current_segment_start_sample = Some(30);
        session.last_voice_sample_end = Some(40);
        session.low_silence_start_sample = Some(50);

        session.reset_after_forced_close();

        assert!(!session.in_speech);
        assert_eq!(session.onset_counter, 0);
        assert_eq!(session.silence_counter, 0);
        assert_eq!(session.silence_start_sample, None);
        assert_eq!(session.candidate_start_sample, None);
        assert_eq!(session.current_segment_start_sample, None);
        assert_eq!(session.last_voice_sample_end, None);
        assert_eq!(session.low_silence_start_sample, None);
        assert!(!session.onset_can_arm());
    }

    #[test]
    fn forced_close_continuous_noise_does_not_rearm_without_low_release() {
        let mut session = test_session();
        session.reset_after_forced_close();

        assert!(!session.onset_can_arm());
        assert!(session.forced_close_rearm_required);
        // Above-threshold frames are intentionally ignored while rearm is required.
        assert!(!session.onset_can_arm());
    }

    #[test]
    fn normal_new_speech_can_start_after_release() {
        let mut session = test_session();
        session.reset_after_forced_close();
        session.release_forced_close_rearm();

        assert!(session.onset_can_arm());
        assert!(!session.forced_close_rearm_required);
    }

    #[test]
    fn normal_accepted_boundary_reset_does_not_require_release() {
        let mut detector = SileroVadDetector {
            config: SileroVadConfig::default(),
            sessions: HashMap::from([(SESSION_ID.to_owned(), test_session())]),
        };
        let (runtime, logger, _dir, _events) = writer_fixture();
        let mut writer = DetectorWriter::new(&logger, runtime.handle());

        detector
            .handle_action(&test_action("vad_speech_end", 16_000), &mut writer)
            .expect("normal reset should succeed");

        let session = detector.sessions.get(SESSION_ID).expect("session remains");
        assert!(session.onset_can_arm());
        assert!(!session.forced_close_rearm_required);
    }

    #[test]
    fn forced_close_action_requires_low_release() {
        let mut detector = SileroVadDetector {
            config: SileroVadConfig::default(),
            sessions: HashMap::from([(SESSION_ID.to_owned(), test_session())]),
        };
        {
            let session = detector.sessions.get_mut(SESSION_ID).unwrap();
            session.in_speech = true;
            session.current_segment_start_sample = Some(0);
            session.last_voice_sample_end = Some(16_000);
        }
        let (runtime, logger, _dir, _events) = writer_fixture();
        let mut writer = DetectorWriter::new(&logger, runtime.handle());

        detector
            .handle_action(
                &test_action("human_hold_speech_like_audio_without_tokens", 16_000),
                &mut writer,
            )
            .expect("forced close reset should succeed");

        let session = detector.sessions.get(SESSION_ID).expect("session remains");
        assert!(!session.in_speech);
        assert_eq!(session.current_segment_start_sample, None);
        assert!(!session.onset_can_arm());
    }
}
