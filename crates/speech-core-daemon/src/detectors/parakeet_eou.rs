use anyhow::{bail, Result};
use parakeet_rs::{ParakeetEOU, ParakeetEOUHandle};
use serde::Serialize;
use speech_core_protocol::{now_mono_ns, AudioFrame, PcmFormat};
use std::collections::{HashMap, VecDeque};
use std::path::PathBuf;

use super::{AudioDetector, DetectorAction, DetectorSignal, DetectorWriter, EouResetMode};
use crate::{AudioGapReset, HelloState};

const DETECTOR: &str = "parakeet_realtime_eou_120m_v1";
const MODEL_NAME: &str = "parakeet_realtime_eou_120m-v1";
const SAMPLE_RATE: u32 = 16_000;
const DEFAULT_CHUNK_MS: u32 = 160;
const MIN_BUFFER_SAMPLES: u64 = SAMPLE_RATE as u64;

#[derive(Debug, Clone)]
pub struct ParakeetEouConfig {
    pub model_dir: PathBuf,
    pub chunk_ms: u32,
    pub reset_on_eou: bool,
    pub emit_transcript: bool,
}

impl Default for ParakeetEouConfig {
    fn default() -> Self {
        Self {
            model_dir: PathBuf::new(),
            chunk_ms: DEFAULT_CHUNK_MS,
            reset_on_eou: false,
            emit_transcript: true,
        }
    }
}

pub struct ParakeetEouDetector {
    config: ParakeetEouConfig,
    handle: ParakeetEOUHandle,
    load_start_mono_ns: u64,
    load_end_mono_ns: u64,
    sessions: HashMap<String, EouSession>,
}

impl ParakeetEouDetector {
    pub fn new(config: ParakeetEouConfig) -> Result<Self> {
        if config.model_dir.as_os_str().is_empty() {
            bail!("Parakeet EOU model dir is empty");
        }
        for file in ["encoder.onnx", "decoder_joint.onnx", "tokenizer.json"] {
            let path = config.model_dir.join(file);
            if !path.exists() {
                bail!("missing Parakeet EOU artifact: {}", path.display());
            }
        }
        if config.chunk_ms == 0 {
            bail!("Parakeet EOU chunk_ms must be greater than zero");
        }
        let load_start_mono_ns = now_mono_ns();
        let handle = ParakeetEOUHandle::load(&config.model_dir, None)
            .map_err(|e| anyhow::anyhow!("failed to load Parakeet EOU shared model: {e}"))?;
        let load_end_mono_ns = now_mono_ns();
        Ok(Self {
            config,
            handle,
            load_start_mono_ns,
            load_end_mono_ns,
            sessions: HashMap::new(),
        })
    }
}

impl AudioDetector for ParakeetEouDetector {
    fn start_session(&mut self, hello: &HelloState, writer: &mut DetectorWriter<'_>) -> Result<()> {
        if hello.sample_rate_hz != SAMPLE_RATE || hello.channels != 1 {
            writer.write(&EouErrorEvent {
                event: "eou_error",
                stream_id: hello.stream_id.clone(),
                stream_session_id: hello.stream_session_id.clone(),
                adapter_id: hello.adapter_id.clone(),
                detector: DETECTOR,
                message: "Parakeet EOU requires 16 kHz mono PCM".to_owned(),
                daemon_mono_ns: now_mono_ns(),
            })?;
            bail!("Parakeet EOU requires 16 kHz mono PCM");
        }
        if !matches!(hello.format, PcmFormat::PcmF32Le | PcmFormat::PcmS16Le) {
            writer.write(&EouErrorEvent {
                event: "eou_error",
                stream_id: hello.stream_id.clone(),
                stream_session_id: hello.stream_session_id.clone(),
                adapter_id: hello.adapter_id.clone(),
                detector: DETECTOR,
                message: format!("unsupported PCM format for Parakeet EOU: {}", hello.format),
                daemon_mono_ns: now_mono_ns(),
            })?;
            bail!("unsupported PCM format for Parakeet EOU: {}", hello.format);
        }

        let open_start_mono_ns = now_mono_ns();
        let model = ParakeetEOU::from_shared(&self.handle);
        let open_end_mono_ns = now_mono_ns();
        let chunk_samples = chunk_samples(self.config.chunk_ms);
        self.sessions.insert(
            hello.stream_session_id.clone(),
            EouSession::new(hello.clone(), model, chunk_samples),
        );
        writer.write(&EouSessionStartEvent {
            event: "eou_session_start",
            stream_id: hello.stream_id.clone(),
            stream_session_id: hello.stream_session_id.clone(),
            adapter_id: hello.adapter_id.clone(),
            detector: DETECTOR,
            model_name: MODEL_NAME,
            model_dir: self.config.model_dir.display().to_string(),
            chunk_ms: self.config.chunk_ms,
            chunk_samples: chunk_samples as u32,
            reset_on_eou: self.config.reset_on_eou,
            reset_policy: if self.config.reset_on_eou {
                "raw_token"
            } else {
                "accepted_turn_action"
            },
            emit_transcript: self.config.emit_transcript,
            open_start_mono_ns,
            open_end_mono_ns,
            shared_load_start_mono_ns: self.load_start_mono_ns,
            shared_load_end_mono_ns: self.load_end_mono_ns,
            shared_load_duration_ms: ns_to_ms(
                self.load_end_mono_ns
                    .saturating_sub(self.load_start_mono_ns),
            ),
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
        while session.buffer.len() >= session.chunk_samples {
            let chunk: Vec<f32> = session.buffer.drain(..session.chunk_samples).collect();
            let chunk_start_sample = session.next_chunk_sample_start;
            session.next_chunk_sample_start = session
                .next_chunk_sample_start
                .saturating_add(session.chunk_samples as u64);
            signals.extend(session.process_chunk(
                &chunk,
                chunk_start_sample,
                ingress_receive_mono_ns,
                self.config.reset_on_eou,
                self.config.emit_transcript,
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
            let chunk_start_sample = session.next_chunk_sample_start;
            let mut chunk = std::mem::take(&mut session.buffer);
            chunk.resize(session.chunk_samples, 0.0);
            signals.extend(session.process_chunk(
                &chunk,
                chunk_start_sample,
                now_mono_ns(),
                self.config.reset_on_eou,
                self.config.emit_transcript,
                writer,
            )?);
        }
        // Like the parakeet-rs example, flush a few silence chunks. The model often emits final
        // symbols/EOU only after the trailing context arrives.
        for _ in 0..3 {
            let chunk_start_sample = session.next_chunk_sample_start;
            session.next_chunk_sample_start = session
                .next_chunk_sample_start
                .saturating_add(session.chunk_samples as u64);
            let silence = vec![0.0; session.chunk_samples];
            signals.extend(session.process_chunk(
                &silence,
                chunk_start_sample,
                now_mono_ns(),
                self.config.reset_on_eou,
                self.config.emit_transcript,
                writer,
            )?);
        }
        writer.write(&EouSessionEndEvent {
            event: "eou_session_end",
            stream_id: session.hello.stream_id,
            stream_session_id: session.hello.stream_session_id,
            adapter_id: session.hello.adapter_id,
            detector: DETECTOR,
            reason: reason.to_owned(),
            chunks_processed: session.chunks_processed,
            eou_tokens_detected: session.eou_tokens_detected,
            transcript_text: session.transcript,
            decoder_resets: session.decoder_resets,
            stream_resets: session.stream_resets,
            daemon_mono_ns: now_mono_ns(),
        })?;
        Ok(signals)
    }

    fn handle_action(
        &mut self,
        action: &DetectorAction,
        writer: &mut DetectorWriter<'_>,
    ) -> Result<()> {
        match action {
            DetectorAction::ResetEouState {
                stream_id,
                stream_session_id,
                adapter_id,
                mode,
                anchor_sample,
                source,
                reason,
                decision_sample,
            } => {
                let Some(session) = self.sessions.get_mut(stream_session_id) else {
                    return Ok(());
                };
                match mode {
                    EouResetMode::Stream => {
                        let mut replay = session.replay_from(*anchor_sample);
                        let replay_samples = std::mem::take(&mut replay.samples);
                        session.model.reset_stream_state();
                        session.buffer = replay_samples;
                        session.next_chunk_sample_start =
                            replay.start_sample.unwrap_or(*anchor_sample);
                        session.samples_since_stream_reset = 0;
                        session.stream_resets = session.stream_resets.saturating_add(1);
                        session.last_replay = Some(replay);
                    }
                    EouResetMode::Decoder => {
                        session.model.reset_decoder_state();
                        session.decoder_resets = session.decoder_resets.saturating_add(1);
                    }
                }
                let reset_index = session.decoder_resets.saturating_add(session.stream_resets);
                let replay = session.last_replay.take();
                writer.write(&EouStateResetEvent {
                    event: "eou_state_reset",
                    stream_id: stream_id.clone(),
                    stream_session_id: stream_session_id.clone(),
                    adapter_id: adapter_id.clone(),
                    detector: DETECTOR,
                    reset_index,
                    mode: mode.as_str(),
                    anchor_sample: *anchor_sample,
                    replay_start_sample: replay.as_ref().and_then(|replay| replay.start_sample),
                    replay_sample_count: replay.as_ref().map_or(0, |replay| replay.sample_count),
                    replay_anchor_available: replay
                        .as_ref()
                        .is_none_or(|replay| replay.anchor_available),
                    source,
                    reason,
                    decision_sample: *decision_sample,
                    daemon_mono_ns: now_mono_ns(),
                })?;
                Ok(())
            }
        }
    }

    fn audio_gap_reset(
        &mut self,
        gap: &AudioGapReset,
        writer: &mut DetectorWriter<'_>,
    ) -> Result<()> {
        let Some(session) = self.sessions.get_mut(&gap.stream_session_id) else {
            return Ok(());
        };
        session.model.reset_stream_state();
        session.buffer.clear();
        session.recent_audio.clear();
        session.recent_audio_start_sample = gap.observed_sample_start;
        session.last_replay = None;
        session.next_chunk_sample_start = gap.observed_sample_start;
        session.samples_since_stream_reset = 0;
        session.stream_resets = session.stream_resets.saturating_add(1);
        writer.write(&EouStateResetEvent {
            event: "eou_state_reset",
            stream_id: gap.stream_id.clone(),
            stream_session_id: gap.stream_session_id.clone(),
            adapter_id: gap.adapter_id.clone(),
            detector: DETECTOR,
            reset_index: session.decoder_resets.saturating_add(session.stream_resets),
            mode: EouResetMode::Stream.as_str(),
            anchor_sample: gap.observed_sample_start,
            replay_start_sample: None,
            replay_sample_count: 0,
            replay_anchor_available: false,
            source: "audio_gap",
            reason: "audio_gap",
            decision_sample: gap.observed_sample_start,
            daemon_mono_ns: now_mono_ns(),
        })
    }
}

struct ReplayAudio {
    samples: Vec<f32>,
    start_sample: Option<u64>,
    sample_count: u64,
    anchor_available: bool,
}

struct EouSession {
    hello: HelloState,
    model: ParakeetEOU,
    buffer: Vec<f32>,
    chunk_samples: usize,
    recent_audio: VecDeque<f32>,
    recent_audio_start_sample: u64,
    recent_audio_capacity: usize,
    last_replay: Option<ReplayAudio>,
    next_chunk_sample_start: u64,
    transcript: String,
    chunks_processed: u64,
    eou_tokens_detected: u32,
    decoder_resets: u32,
    stream_resets: u32,
    samples_since_stream_reset: u64,
}

impl EouSession {
    fn new(hello: HelloState, model: ParakeetEOU, chunk_samples: usize) -> Self {
        Self {
            hello,
            model,
            buffer: Vec::with_capacity(chunk_samples * 2),
            chunk_samples,
            recent_audio: VecDeque::with_capacity(SAMPLE_RATE as usize * 4),
            recent_audio_start_sample: 0,
            recent_audio_capacity: SAMPLE_RATE as usize * 4,
            last_replay: None,
            next_chunk_sample_start: 0,
            transcript: String::new(),
            chunks_processed: 0,
            eou_tokens_detected: 0,
            decoder_resets: 0,
            stream_resets: 0,
            samples_since_stream_reset: 0,
        }
    }

    fn push_samples(&mut self, frame: &AudioFrame, samples: &[f32]) {
        self.push_recent_audio(frame.header.source_sample_start, samples);
        if self.buffer.is_empty() {
            self.next_chunk_sample_start = frame.header.source_sample_start;
        }
        self.buffer.extend_from_slice(samples);
    }

    fn push_recent_audio(&mut self, source_sample_start: u64, samples: &[f32]) {
        if samples.is_empty() {
            return;
        }
        if self.recent_audio.is_empty() {
            self.recent_audio_start_sample = source_sample_start;
            self.recent_audio.extend(samples.iter().copied());
        } else {
            let recent_end = self
                .recent_audio_start_sample
                .saturating_add(self.recent_audio.len() as u64);
            if source_sample_start > recent_end {
                self.recent_audio.clear();
                self.recent_audio_start_sample = source_sample_start;
                self.recent_audio.extend(samples.iter().copied());
            } else if source_sample_start < recent_end {
                let overlap = recent_end.saturating_sub(source_sample_start) as usize;
                if overlap < samples.len() {
                    self.recent_audio.extend(samples[overlap..].iter().copied());
                }
            } else {
                self.recent_audio.extend(samples.iter().copied());
            }
        }
        while self.recent_audio.len() > self.recent_audio_capacity {
            self.recent_audio.pop_front();
            self.recent_audio_start_sample = self.recent_audio_start_sample.saturating_add(1);
        }
    }

    fn replay_from(&self, anchor_sample: u64) -> ReplayAudio {
        let recent_end = self
            .recent_audio_start_sample
            .saturating_add(self.recent_audio.len() as u64);
        if self.recent_audio.is_empty() || anchor_sample >= recent_end {
            return ReplayAudio {
                samples: Vec::new(),
                start_sample: None,
                sample_count: 0,
                anchor_available: anchor_sample == recent_end,
            };
        }
        let start_sample = anchor_sample.max(self.recent_audio_start_sample);
        let offset = start_sample.saturating_sub(self.recent_audio_start_sample) as usize;
        let samples: Vec<f32> = self.recent_audio.iter().skip(offset).copied().collect();
        ReplayAudio {
            sample_count: samples.len() as u64,
            samples,
            start_sample: Some(start_sample),
            anchor_available: start_sample == anchor_sample,
        }
    }

    fn process_chunk(
        &mut self,
        chunk: &[f32],
        chunk_start_sample: u64,
        ingress_receive_mono_ns: u64,
        reset_on_eou: bool,
        emit_transcript: bool,
        writer: &mut DetectorWriter<'_>,
    ) -> Result<Vec<DetectorSignal>> {
        let detector_start_mono_ns = now_mono_ns();
        let samples_since_stream_reset_before = self.samples_since_stream_reset;
        let samples_since_stream_reset_after = self
            .samples_since_stream_reset
            .saturating_add(chunk.len() as u64);
        // `reset_on_eou` is intentionally normally false in speech-core.  A raw `<EOU>` token is
        // only evidence; the turn manager decides whether that candidate is accepted.  Accepted
        // turn closures send a ResetEouState action back to this detector.
        let raw_text = self
            .model
            .transcribe(chunk, reset_on_eou)
            .map_err(|e| anyhow::anyhow!("Parakeet EOU inference failed: {e}"))?;
        let detector_end_mono_ns = now_mono_ns();
        self.samples_since_stream_reset = samples_since_stream_reset_after;
        self.chunks_processed = self.chunks_processed.saturating_add(1);
        let eou_detected = raw_text.contains("[EOU]") || raw_text.contains("<EOU>");
        let text_delta = raw_text
            .replace("[EOU]", "")
            .replace("<EOU>", "")
            .trim_end()
            .to_owned();
        if !text_delta.is_empty() {
            self.transcript.push_str(&text_delta);
        }
        let chunk_end_sample = chunk_start_sample.saturating_add(chunk.len() as u64);

        writer.write(&EouChunkProcessedEvent {
            event: "eou_chunk_processed",
            stream_id: self.hello.stream_id.clone(),
            stream_session_id: self.hello.stream_session_id.clone(),
            adapter_id: self.hello.adapter_id.clone(),
            detector: DETECTOR,
            chunk_index: self.chunks_processed.saturating_sub(1),
            chunk_start_sample,
            chunk_sample_count: chunk.len() as u32,
            chunk_end_sample,
            text_delta: text_delta.clone(),
            raw_text,
            transcript_text: if emit_transcript {
                Some(self.transcript.clone())
            } else {
                None
            },
            eou_detected,
            reset_on_eou,
            samples_since_stream_reset_before,
            samples_since_stream_reset_after,
            warmup_complete: samples_since_stream_reset_after >= MIN_BUFFER_SAMPLES,
            min_buffer_samples: MIN_BUFFER_SAMPLES,
            ingress_receive_mono_ns,
            detector_start_mono_ns,
            detector_end_mono_ns,
            detector_start_lag_ms: ns_to_ms(
                detector_start_mono_ns.saturating_sub(ingress_receive_mono_ns),
            ),
            detector_end_lag_ms: ns_to_ms(
                detector_end_mono_ns.saturating_sub(ingress_receive_mono_ns),
            ),
            detector_duration_ms: ns_to_ms(
                detector_end_mono_ns.saturating_sub(detector_start_mono_ns),
            ),
        })?;

        if eou_detected {
            self.eou_tokens_detected = self.eou_tokens_detected.saturating_add(1);
            writer.write(&EouTokenDetectedEvent {
                event: "eou_token_detected",
                stream_id: self.hello.stream_id.clone(),
                stream_session_id: self.hello.stream_session_id.clone(),
                adapter_id: self.hello.adapter_id.clone(),
                detector: DETECTOR,
                model_name: MODEL_NAME,
                eou_index: self.eou_tokens_detected,
                end_sample: chunk_end_sample,
                decision_sample: chunk_end_sample,
                text_delta: text_delta.clone(),
                confidence: None,
                detector_end_mono_ns,
            })?;
            return Ok(vec![DetectorSignal::ModelEou {
                detector: DETECTOR,
                stream_id: self.hello.stream_id.clone(),
                stream_session_id: self.hello.stream_session_id.clone(),
                adapter_id: self.hello.adapter_id.clone(),
                end_sample: chunk_end_sample,
                decision_sample: chunk_end_sample,
                text_delta,
                confidence: None,
            }]);
        }

        Ok(Vec::new())
    }
}

#[derive(Debug, Serialize)]
struct EouSessionStartEvent {
    event: &'static str,
    stream_id: String,
    stream_session_id: String,
    adapter_id: String,
    detector: &'static str,
    model_name: &'static str,
    model_dir: String,
    chunk_ms: u32,
    chunk_samples: u32,
    reset_on_eou: bool,
    reset_policy: &'static str,
    emit_transcript: bool,
    open_start_mono_ns: u64,
    open_end_mono_ns: u64,
    shared_load_start_mono_ns: u64,
    shared_load_end_mono_ns: u64,
    shared_load_duration_ms: f64,
    open_duration_ms: f64,
}

#[derive(Debug, Serialize)]
struct EouChunkProcessedEvent {
    event: &'static str,
    stream_id: String,
    stream_session_id: String,
    adapter_id: String,
    detector: &'static str,
    chunk_index: u64,
    chunk_start_sample: u64,
    chunk_sample_count: u32,
    chunk_end_sample: u64,
    text_delta: String,
    raw_text: String,
    transcript_text: Option<String>,
    eou_detected: bool,
    reset_on_eou: bool,
    samples_since_stream_reset_before: u64,
    samples_since_stream_reset_after: u64,
    warmup_complete: bool,
    min_buffer_samples: u64,
    ingress_receive_mono_ns: u64,
    detector_start_mono_ns: u64,
    detector_end_mono_ns: u64,
    detector_start_lag_ms: f64,
    detector_end_lag_ms: f64,
    detector_duration_ms: f64,
}

#[derive(Debug, Serialize)]
struct EouTokenDetectedEvent {
    event: &'static str,
    stream_id: String,
    stream_session_id: String,
    adapter_id: String,
    detector: &'static str,
    model_name: &'static str,
    eou_index: u32,
    end_sample: u64,
    decision_sample: u64,
    text_delta: String,
    confidence: Option<f32>,
    detector_end_mono_ns: u64,
}

#[derive(Debug, Serialize)]
struct EouSessionEndEvent {
    event: &'static str,
    stream_id: String,
    stream_session_id: String,
    adapter_id: String,
    detector: &'static str,
    reason: String,
    chunks_processed: u64,
    eou_tokens_detected: u32,
    transcript_text: String,
    decoder_resets: u32,
    stream_resets: u32,
    daemon_mono_ns: u64,
}

#[derive(Debug, Serialize)]
struct EouStateResetEvent {
    event: &'static str,
    stream_id: String,
    stream_session_id: String,
    adapter_id: String,
    detector: &'static str,
    reset_index: u32,
    mode: &'static str,
    anchor_sample: u64,
    replay_start_sample: Option<u64>,
    replay_sample_count: u64,
    replay_anchor_available: bool,
    source: &'static str,
    reason: &'static str,
    decision_sample: u64,
    daemon_mono_ns: u64,
}

#[derive(Debug, Serialize)]
struct EouErrorEvent {
    event: &'static str,
    stream_id: String,
    stream_session_id: String,
    adapter_id: String,
    detector: &'static str,
    message: String,
    daemon_mono_ns: u64,
}

fn chunk_samples(chunk_ms: u32) -> usize {
    (u64::from(chunk_ms).saturating_mul(u64::from(SAMPLE_RATE)) / 1_000).max(1) as usize
}

fn ns_to_ms(ns: u64) -> f64 {
    ns as f64 / 1_000_000.0
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_chunk_is_160ms() {
        assert_eq!(chunk_samples(160), 2560);
    }
}
