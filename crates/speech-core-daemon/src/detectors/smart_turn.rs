use anyhow::{bail, Context, Result};
use ort::session::{builder::GraphOptimizationLevel, Session};
use ort::value::Tensor;
use rustfft::num_complex::Complex;
use rustfft::num_traits::Zero;
use rustfft::FftPlanner;
use serde::Serialize;
use speech_core_protocol::{now_mono_ns, AudioFrame, PcmFormat};
use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::Arc;

use super::{AudioDetector, DetectorAction, DetectorSignal, DetectorWriter};
use crate::HelloState;

pub const DETECTOR: &str = "pipecat_smart_turn_v3";
const MODEL_NAME: &str = "pipecat-ai/smart-turn-v3.2-cpu";
const SAMPLE_RATE: u32 = 16_000;
const WINDOW_SECONDS: u64 = 8;
const WINDOW_SAMPLES: usize = SAMPLE_RATE as usize * WINDOW_SECONDS as usize;
const N_FFT: usize = 400;
const HOP_LENGTH: usize = 160;
const N_MELS: usize = 80;
const MEL_FLOOR: f32 = 1e-10;
const NORM_VARIANCE_EPS: f32 = 1e-7;
const DEBUG_DUMP_ENV: &str = "SPEECH_CORE_SMART_TURN_DEBUG_DUMP_DIR";
const EXPECTED_INPUT_NAME: &str = "input_features";
const EXPECTED_OUTPUT_COUNT: usize = 1;

#[derive(Debug, Clone)]
pub struct SmartTurnConfig {
    pub model_path: PathBuf,
    pub threshold: f32,
    pub timeout_ms: u32,
    pub cpu_count: usize,
    pub max_audio_secs: u32,
    pub pre_speech_ms: u32,
    pub recheck_interval_ms: u32,
    pub recheck_max_attempts: u32,
    pub recheck_offsets_ms: Vec<u32>,
}

impl Default for SmartTurnConfig {
    fn default() -> Self {
        Self {
            model_path: PathBuf::new(),
            threshold: 0.5,
            timeout_ms: 250,
            cpu_count: 1,
            max_audio_secs: WINDOW_SECONDS as u32,
            pre_speech_ms: 500,
            recheck_interval_ms: 0,
            recheck_max_attempts: 0,
            recheck_offsets_ms: vec![96, 192, 384, 768, 1536],
        }
    }
}

impl SmartTurnConfig {
    pub fn effective_recheck_offsets_ms(&self) -> Vec<u32> {
        let mut offsets = if !self.recheck_offsets_ms.is_empty() {
            self.recheck_offsets_ms.clone()
        } else if self.recheck_interval_ms > 0 && self.recheck_max_attempts > 0 {
            (1..=self.recheck_max_attempts)
                .map(|attempt| self.recheck_interval_ms.saturating_mul(attempt))
                .collect()
        } else {
            Vec::new()
        };
        offsets.sort_unstable();
        offsets.dedup();
        offsets.retain(|offset| *offset > 0);
        offsets
    }
}

pub struct SmartTurnDetector {
    config: SmartTurnConfig,
    session: Option<Session>,
    features: WhisperFeatureExtractor,
    load_start_mono_ns: u64,
    load_end_mono_ns: u64,
    input_name: String,
    output_name: String,
    sessions: HashMap<String, SmartTurnSession>,
}

impl SmartTurnDetector {
    pub fn new(config: SmartTurnConfig) -> Result<Self> {
        if config.model_path.as_os_str().is_empty() {
            bail!("Smart Turn model path is empty");
        }
        if !config.model_path.exists() {
            bail!(
                "Smart Turn model not found: {}",
                config.model_path.display()
            );
        }
        if !(0.0..=1.0).contains(&config.threshold) {
            bail!("Smart Turn threshold must be between 0.0 and 1.0");
        }
        if config.timeout_ms == 0 {
            bail!("Smart Turn timeout_ms must be greater than zero");
        }
        if config.cpu_count == 0 {
            bail!("Smart Turn cpu_count must be greater than zero");
        }
        if config.max_audio_secs == 0 || config.max_audio_secs > WINDOW_SECONDS as u32 {
            bail!(
                "Smart Turn max_audio_secs must be between 1 and {}",
                WINDOW_SECONDS
            );
        }
        for offset in config.effective_recheck_offsets_ms() {
            if offset == 0 {
                bail!("Smart Turn recheck offsets must be greater than zero");
            }
        }

        let load_start_mono_ns = now_mono_ns();
        let session = Session::builder()
            .map_err(|err| anyhow::anyhow!("creating Smart Turn ONNX session builder: {err}"))?
            .with_parallel_execution(false)
            .map_err(|err| {
                anyhow::anyhow!("configuring Smart Turn ONNX sequential execution: {err}")
            })?
            .with_inter_threads(1)
            .map_err(|err| anyhow::anyhow!("configuring Smart Turn ONNX inter-op threads: {err}"))?
            .with_intra_threads(config.cpu_count)
            .map_err(|err| anyhow::anyhow!("configuring Smart Turn ONNX intra-op threads: {err}"))?
            .with_optimization_level(GraphOptimizationLevel::All)
            .map_err(|err| anyhow::anyhow!("configuring Smart Turn ONNX optimizations: {err}"))?
            .commit_from_file(&config.model_path)
            .map_err(|err| {
                anyhow::anyhow!(
                    "loading Smart Turn model {}: {err}",
                    config.model_path.display()
                )
            })?;
        let load_end_mono_ns = now_mono_ns();

        validate_model_contract(&session)?;
        let input_name = session.inputs()[0].name().to_owned();
        let output_name = session.outputs()[0].name().to_owned();

        Ok(Self {
            config,
            session: Some(session),
            features: WhisperFeatureExtractor::new(),
            load_start_mono_ns,
            load_end_mono_ns,
            input_name,
            output_name,
            sessions: HashMap::new(),
        })
    }
}

impl AudioDetector for SmartTurnDetector {
    fn start_session(&mut self, hello: &HelloState, writer: &mut DetectorWriter<'_>) -> Result<()> {
        if hello.sample_rate_hz != SAMPLE_RATE || hello.channels != 1 {
            writer.write(&SmartTurnErrorEvent {
                event: "smart_turn_error",
                stream_id: hello.stream_id.clone(),
                stream_session_id: hello.stream_session_id.clone(),
                adapter_id: hello.adapter_id.clone(),
                detector: DETECTOR,
                message: "Smart Turn v3 requires 16 kHz mono PCM".to_owned(),
                daemon_mono_ns: now_mono_ns(),
            })?;
            return Ok(());
        }
        if !matches!(hello.format, PcmFormat::PcmF32Le | PcmFormat::PcmS16Le) {
            writer.write(&SmartTurnErrorEvent {
                event: "smart_turn_error",
                stream_id: hello.stream_id.clone(),
                stream_session_id: hello.stream_session_id.clone(),
                adapter_id: hello.adapter_id.clone(),
                detector: DETECTOR,
                message: format!("unsupported PCM format for Smart Turn v3: {}", hello.format),
                daemon_mono_ns: now_mono_ns(),
            })?;
            return Ok(());
        }

        self.sessions.insert(
            hello.stream_session_id.clone(),
            SmartTurnSession::new(
                hello.clone(),
                self.config.max_audio_secs,
                self.config.pre_speech_ms,
            ),
        );
        writer.write(&SmartTurnSessionStartEvent {
            event: "smart_turn_session_start",
            stream_id: hello.stream_id.clone(),
            stream_session_id: hello.stream_session_id.clone(),
            adapter_id: hello.adapter_id.clone(),
            detector: DETECTOR,
            model_name: MODEL_NAME,
            model_path: self.config.model_path.display().to_string(),
            input_name: self.input_name.clone(),
            output_name: self.output_name.clone(),
            threshold: self.config.threshold,
            timeout_ms: self.config.timeout_ms,
            cpu_count: self.config.cpu_count,
            max_audio_secs: self.config.max_audio_secs,
            pre_speech_ms: self.config.pre_speech_ms,
            recheck_interval_ms: self.config.recheck_interval_ms,
            recheck_max_attempts: self.config.recheck_max_attempts,
            recheck_offsets_ms: self.config.effective_recheck_offsets_ms(),
            shared_load_start_mono_ns: self.load_start_mono_ns,
            shared_load_end_mono_ns: self.load_end_mono_ns,
            shared_load_duration_ms: ns_to_ms(
                self.load_end_mono_ns
                    .saturating_sub(self.load_start_mono_ns),
            ),
            daemon_mono_ns: now_mono_ns(),
        })
    }

    fn ingest_frame(
        &mut self,
        frame: &AudioFrame,
        samples: &[f32],
        _ingress_receive_mono_ns: u64,
        _writer: &mut DetectorWriter<'_>,
    ) -> Result<Vec<DetectorSignal>> {
        let Some(session) = self.sessions.get_mut(&frame.header.stream_session_id) else {
            return Ok(Vec::new());
        };
        session.push_samples(frame, samples);
        Ok(Vec::new())
    }

    fn end_session(
        &mut self,
        stream_session_id: &str,
        reason: &str,
        writer: &mut DetectorWriter<'_>,
    ) -> Result<Vec<DetectorSignal>> {
        let Some(session) = self.sessions.remove(stream_session_id) else {
            return Ok(Vec::new());
        };
        writer.write(&SmartTurnSessionEndEvent {
            event: "smart_turn_session_end",
            stream_id: session.hello.stream_id,
            stream_session_id: session.hello.stream_session_id,
            adapter_id: session.hello.adapter_id,
            detector: DETECTOR,
            reason: reason.to_owned(),
            samples_buffered: session.audio.len() as u64,
            predictions: session.predictions,
            timeouts: session.timeouts,
            errors: session.errors,
            daemon_mono_ns: now_mono_ns(),
        })?;
        Ok(Vec::new())
    }
}

impl SmartTurnDetector {
    pub fn recheck_offsets_samples(&self) -> Vec<u64> {
        self.config
            .effective_recheck_offsets_ms()
            .into_iter()
            .map(|offset_ms| u64::from(offset_ms).saturating_mul(u64::from(SAMPLE_RATE)) / 1_000)
            .collect()
    }

    pub fn recheck_enabled(&self) -> bool {
        !self.recheck_offsets_samples().is_empty()
    }

    pub fn predict_for_vad_end(
        &mut self,
        stream_session_id: &str,
        end_sample: u64,
        decision_sample: u64,
        writer: &mut DetectorWriter<'_>,
    ) -> Result<SmartTurnDecision> {
        let Some(mut session) = self.sessions.remove(stream_session_id) else {
            return Ok(SmartTurnDecision::unavailable("missing_smart_turn_session"));
        };

        let predict_start_mono_ns = now_mono_ns();
        let audio = session.segment_for_prediction(decision_sample);
        if let Err(err) = maybe_dump_debug_audio(
            &session.hello.stream_session_id,
            end_sample,
            decision_sample,
            &audio,
        ) {
            writer.write(&SmartTurnErrorEvent {
                event: "smart_turn_error",
                stream_id: session.hello.stream_id.clone(),
                stream_session_id: session.hello.stream_session_id.clone(),
                adapter_id: session.hello.adapter_id.clone(),
                detector: DETECTOR,
                message: format!("failed to dump Smart Turn debug audio: {err}"),
                daemon_mono_ns: now_mono_ns(),
            })?;
        }
        writer.write(&SmartTurnCandidateEvent {
            event: "smart_turn_candidate",
            stream_id: session.hello.stream_id.clone(),
            stream_session_id: session.hello.stream_session_id.clone(),
            adapter_id: session.hello.adapter_id.clone(),
            detector: DETECTOR,
            end_sample,
            decision_sample,
            audio_samples: audio.len() as u64,
            threshold: self.config.threshold,
            timeout_ms: self.config.timeout_ms,
            daemon_mono_ns: now_mono_ns(),
        })?;
        if audio.is_empty() {
            session.errors = session.errors.saturating_add(1);
            let decision = SmartTurnDecision::unavailable("empty_audio_segment");
            self.sessions.insert(stream_session_id.to_owned(), session);
            return Ok(decision);
        }

        let feature_start_mono_ns = now_mono_ns();
        let features = self.features.compute(&audio);
        let feature_end_mono_ns = now_mono_ns();
        let input =
            Tensor::<f32>::from_array(([1_usize, N_MELS, WINDOW_SAMPLES / HOP_LENGTH], features))
                .context("creating Smart Turn input tensor")?;

        let Some(session_model) = self.session.as_mut() else {
            session.errors = session.errors.saturating_add(1);
            let decision = SmartTurnDecision::unavailable("smart_turn_model_unavailable");
            self.sessions.insert(stream_session_id.to_owned(), session);
            return Ok(decision);
        };
        let model_start_mono_ns = now_mono_ns();
        let outputs = session_model
            .run(ort::inputs![input])
            .map_err(|err| anyhow::anyhow!("running Smart Turn ONNX inference: {err}"))?;
        let model_end_mono_ns = now_mono_ns();
        let (_, probabilities) = outputs[0]
            .try_extract_tensor::<f32>()
            .map_err(|err| anyhow::anyhow!("extracting Smart Turn output tensor: {err}"))?;
        let Some(probability) = probabilities.first().copied() else {
            session.errors = session.errors.saturating_add(1);
            let decision = SmartTurnDecision::unavailable("empty_model_output");
            self.sessions.insert(stream_session_id.to_owned(), session);
            return Ok(decision);
        };
        let predict_end_mono_ns = now_mono_ns();
        let duration_ms = ns_to_ms(predict_end_mono_ns.saturating_sub(predict_start_mono_ns));
        session.predictions = session.predictions.saturating_add(1);

        let complete = probability > self.config.threshold;
        let over_budget = duration_ms > f64::from(self.config.timeout_ms);
        if over_budget {
            session.timeouts = session.timeouts.saturating_add(1);
        }
        let decision = SmartTurnDecision {
            available: true,
            complete,
            probability: Some(probability),
            threshold: Some(self.config.threshold),
            timed_out: over_budget,
            reason: if complete {
                "smart_turn_complete"
            } else {
                "smart_turn_incomplete"
            },
            duration_ms: Some(duration_ms),
        };

        writer.write(&SmartTurnDecisionEvent {
            event: "smart_turn_decision",
            stream_id: session.hello.stream_id.clone(),
            stream_session_id: session.hello.stream_session_id.clone(),
            adapter_id: session.hello.adapter_id.clone(),
            detector: DETECTOR,
            end_sample,
            decision_sample,
            probability,
            threshold: self.config.threshold,
            complete,
            timed_out: over_budget,
            inference_start_mono_ns: predict_start_mono_ns,
            inference_end_mono_ns: predict_end_mono_ns,
            inference_duration_ms: duration_ms,
            feature_duration_ms: ns_to_ms(
                feature_end_mono_ns.saturating_sub(feature_start_mono_ns),
            ),
            model_duration_ms: ns_to_ms(model_end_mono_ns.saturating_sub(model_start_mono_ns)),
            audio_samples: audio.len() as u64,
            daemon_mono_ns: now_mono_ns(),
        })?;

        self.sessions.insert(stream_session_id.to_owned(), session);
        Ok(decision)
    }

    pub fn handle_action(
        &mut self,
        action: &DetectorAction,
        _writer: &mut DetectorWriter<'_>,
    ) -> Result<()> {
        let DetectorAction::ResetEouState {
            stream_session_id,
            decision_sample,
            reason,
            ..
        } = action;
        if *reason != "vad_speech_start" {
            if let Some(session) = self.sessions.get_mut(stream_session_id) {
                session.clear_through(*decision_sample);
            }
        }
        Ok(())
    }
}

#[derive(Debug, Clone)]
pub struct SmartTurnDecision {
    pub available: bool,
    pub complete: bool,
    pub probability: Option<f32>,
    pub threshold: Option<f32>,
    pub timed_out: bool,
    pub reason: &'static str,
    pub duration_ms: Option<f64>,
}

impl SmartTurnDecision {
    pub fn unavailable(reason: &'static str) -> Self {
        Self {
            available: false,
            complete: true,
            probability: None,
            threshold: None,
            timed_out: false,
            reason,
            duration_ms: None,
        }
    }
}

struct SmartTurnSession {
    hello: HelloState,
    audio: Vec<f32>,
    first_sample: u64,
    max_audio_samples: usize,
    pre_speech_samples: usize,
    predictions: u64,
    timeouts: u64,
    errors: u64,
}

impl SmartTurnSession {
    fn new(hello: HelloState, max_audio_secs: u32, pre_speech_ms: u32) -> Self {
        let max_audio_samples =
            (u64::from(max_audio_secs).saturating_mul(u64::from(SAMPLE_RATE))) as usize;
        let pre_speech_samples =
            (u64::from(pre_speech_ms).saturating_mul(u64::from(SAMPLE_RATE)) / 1_000) as usize;
        Self {
            hello,
            audio: Vec::with_capacity(max_audio_samples),
            first_sample: 0,
            max_audio_samples,
            pre_speech_samples,
            predictions: 0,
            timeouts: 0,
            errors: 0,
        }
    }

    fn push_samples(&mut self, frame: &AudioFrame, samples: &[f32]) {
        if self.audio.is_empty() {
            self.first_sample = frame.header.source_sample_start;
        }
        let expected_end = frame
            .header
            .source_sample_start
            .saturating_add(samples.len() as u64);
        let current_end = self.first_sample.saturating_add(self.audio.len() as u64);
        if frame.header.source_sample_start > current_end {
            let gap = frame.header.source_sample_start.saturating_sub(current_end) as usize;
            self.audio.extend(std::iter::repeat(0.0).take(gap));
        } else if frame.header.source_sample_start < current_end {
            let overlap = current_end.saturating_sub(frame.header.source_sample_start) as usize;
            if overlap >= samples.len() {
                self.trim_to_limit();
                return;
            }
            self.audio.extend_from_slice(&samples[overlap..]);
            self.trim_to_limit();
            return;
        }
        self.audio.extend_from_slice(samples);
        let new_end = self.first_sample.saturating_add(self.audio.len() as u64);
        if new_end < expected_end {
            self.audio
                .extend(std::iter::repeat(0.0).take(expected_end.saturating_sub(new_end) as usize));
        }
        self.trim_to_limit();
    }

    fn segment_for_prediction(&self, end_sample: u64) -> Vec<f32> {
        let audio_end = self.first_sample.saturating_add(self.audio.len() as u64);
        let clamped_end = end_sample.min(audio_end).max(self.first_sample);
        let end_index = clamped_end.saturating_sub(self.first_sample) as usize;
        let max_samples = self.max_audio_samples.min(WINDOW_SAMPLES);
        let wanted_start = end_index.saturating_sub(max_samples);
        let start_index = wanted_start.saturating_sub(self.pre_speech_samples.min(wanted_start));
        self.audio[start_index..end_index].to_vec()
    }

    fn clear_through(&mut self, end_sample: u64) {
        if end_sample <= self.first_sample {
            return;
        }
        let drain = end_sample
            .saturating_sub(self.first_sample)
            .min(self.audio.len() as u64) as usize;
        self.audio.drain(..drain);
        self.first_sample = self.first_sample.saturating_add(drain as u64);
    }

    fn trim_to_limit(&mut self) {
        let max = self.max_audio_samples.min(WINDOW_SAMPLES);
        if self.audio.len() > max {
            let drain = self.audio.len() - max;
            self.audio.drain(..drain);
            self.first_sample = self.first_sample.saturating_add(drain as u64);
        }
    }
}

struct WhisperFeatureExtractor {
    hann: Vec<f32>,
    mel_filters_t: Vec<Vec<f32>>,
    fft: Arc<dyn rustfft::Fft<f32>>,
}

impl WhisperFeatureExtractor {
    fn new() -> Self {
        let hann = periodic_hann_window(N_FFT);
        let mel_filters =
            build_mel_filterbank(N_FFT / 2 + 1, N_MELS, 0.0, SAMPLE_RATE as f32 / 2.0);
        let mut mel_filters_t = vec![vec![0.0; N_FFT / 2 + 1]; N_MELS];
        for (freq_idx, row) in mel_filters.iter().enumerate() {
            for mel_idx in 0..N_MELS {
                mel_filters_t[mel_idx][freq_idx] = row[mel_idx];
            }
        }
        let fft = FftPlanner::<f32>::new().plan_fft_forward(N_FFT);
        Self {
            hann,
            mel_filters_t,
            fft,
        }
    }

    fn compute(&self, audio: &[f32]) -> Vec<f32> {
        let mut x = vec![0.0_f32; WINDOW_SAMPLES];
        if audio.len() >= WINDOW_SAMPLES {
            x.copy_from_slice(&audio[audio.len() - WINDOW_SAMPLES..]);
        } else {
            let offset = WINDOW_SAMPLES - audio.len();
            x[offset..].copy_from_slice(audio);
        }

        normalize_waveform(&mut x);
        let power = self.power_spectrogram(&x);
        let mut log_spec = vec![0.0_f32; N_MELS * (WINDOW_SAMPLES / HOP_LENGTH)];
        let mut max_log = f32::NEG_INFINITY;

        for mel_idx in 0..N_MELS {
            for frame_idx in 0..(WINDOW_SAMPLES / HOP_LENGTH) {
                let mut mel = 0.0_f32;
                for freq_idx in 0..(N_FFT / 2 + 1) {
                    mel += self.mel_filters_t[mel_idx][freq_idx] * power[freq_idx][frame_idx];
                }
                let value = mel.max(MEL_FLOOR).log10();
                max_log = max_log.max(value);
                log_spec[mel_idx * (WINDOW_SAMPLES / HOP_LENGTH) + frame_idx] = value;
            }
        }

        let floor = max_log - 8.0;
        for value in &mut log_spec {
            let v = (*value).max(floor);
            *value = (v + 4.0) / 4.0;
        }
        log_spec
    }

    fn power_spectrogram(&self, waveform: &[f32]) -> Vec<Vec<f32>> {
        let pad = N_FFT / 2;
        let padded = reflect_pad(waveform, pad);
        let frames = WINDOW_SAMPLES / HOP_LENGTH + 1;
        let mut spec = vec![vec![0.0_f32; frames]; N_FFT / 2 + 1];
        let mut buffer = vec![Complex::<f32>::zero(); N_FFT];

        for frame_idx in 0..frames {
            let start = frame_idx * HOP_LENGTH;
            for i in 0..N_FFT {
                buffer[i] = Complex::new(padded[start + i] * self.hann[i], 0.0);
            }
            self.fft.process(&mut buffer);
            for freq_idx in 0..(N_FFT / 2 + 1) {
                spec[freq_idx][frame_idx] = buffer[freq_idx].norm_sqr();
            }
        }
        spec
    }
}

fn validate_model_contract(session: &Session) -> Result<()> {
    if session.inputs().len() != 1 {
        bail!(
            "unsupported Smart Turn ONNX contract: expected 1 input named {EXPECTED_INPUT_NAME}, got {} inputs",
            session.inputs().len()
        );
    }
    if session.outputs().len() != EXPECTED_OUTPUT_COUNT {
        bail!(
            "unsupported Smart Turn ONNX contract: expected {EXPECTED_OUTPUT_COUNT} output, got {} outputs",
            session.outputs().len()
        );
    }
    if session.inputs()[0].name() != EXPECTED_INPUT_NAME {
        bail!(
            "unsupported Smart Turn ONNX input name: expected {EXPECTED_INPUT_NAME}, got {}",
            session.inputs()[0].name()
        );
    }
    Ok(())
}

fn normalize_waveform(x: &mut [f32]) {
    let mean = x.iter().copied().sum::<f32>() / x.len() as f32;
    let variance = x
        .iter()
        .map(|sample| {
            let centered = *sample - mean;
            centered * centered
        })
        .sum::<f32>()
        / x.len() as f32;
    let denom = (variance + NORM_VARIANCE_EPS).sqrt();
    for sample in x {
        *sample = (*sample - mean) / denom;
    }
}

fn reflect_pad(waveform: &[f32], pad: usize) -> Vec<f32> {
    let mut padded = Vec::with_capacity(waveform.len() + 2 * pad);
    for i in (1..=pad).rev() {
        padded.push(waveform[i.min(waveform.len() - 1)]);
    }
    padded.extend_from_slice(waveform);
    for i in 0..pad {
        let idx = waveform.len().saturating_sub(2 + i);
        padded.push(waveform[idx]);
    }
    padded
}

fn periodic_hann_window(window_length: usize) -> Vec<f32> {
    (0..window_length)
        .map(|i| 0.5 - 0.5 * (2.0 * std::f32::consts::PI * i as f32 / window_length as f32).cos())
        .collect()
}

fn hertz_to_mel_slaney(freq: f32) -> f32 {
    let min_log_hertz = 1000.0;
    let min_log_mel = 15.0;
    let logstep = 27.0 / 6.4_f32.ln();
    if freq >= min_log_hertz {
        min_log_mel + (freq / min_log_hertz).ln() * logstep
    } else {
        3.0 * freq / 200.0
    }
}

fn mel_to_hertz_slaney(mel: f32) -> f32 {
    let min_log_hertz = 1000.0;
    let min_log_mel = 15.0;
    let logstep = 6.4_f32.ln() / 27.0;
    if mel >= min_log_mel {
        min_log_hertz * ((mel - min_log_mel) * logstep).exp()
    } else {
        200.0 * mel / 3.0
    }
}

fn build_mel_filterbank(
    num_frequency_bins: usize,
    num_mel_filters: usize,
    min_frequency: f32,
    max_frequency: f32,
) -> Vec<Vec<f32>> {
    let mel_min = hertz_to_mel_slaney(min_frequency);
    let mel_max = hertz_to_mel_slaney(max_frequency);
    let mel_freqs: Vec<f32> = (0..num_mel_filters + 2)
        .map(|i| mel_min + (mel_max - mel_min) * i as f32 / (num_mel_filters + 1) as f32)
        .collect();
    let filter_freqs: Vec<f32> = mel_freqs.into_iter().map(mel_to_hertz_slaney).collect();
    let fft_freqs: Vec<f32> = (0..num_frequency_bins)
        .map(|i| i as f32 * (SAMPLE_RATE / 2) as f32 / (num_frequency_bins - 1) as f32)
        .collect();

    let mut filters = vec![vec![0.0; num_mel_filters]; num_frequency_bins];
    for mel_idx in 0..num_mel_filters {
        let left = filter_freqs[mel_idx];
        let center = filter_freqs[mel_idx + 1];
        let right = filter_freqs[mel_idx + 2];
        let enorm = 2.0 / (right - left);
        for (freq_idx, freq) in fft_freqs.iter().enumerate() {
            let down = if center > left {
                (*freq - left) / (center - left)
            } else {
                0.0
            };
            let up = if right > center {
                (right - *freq) / (right - center)
            } else {
                0.0
            };
            filters[freq_idx][mel_idx] = down.min(up).max(0.0) * enorm;
        }
    }
    filters
}

fn maybe_dump_debug_audio(
    stream_session_id: &str,
    end_sample: u64,
    decision_sample: u64,
    audio: &[f32],
) -> Result<()> {
    let Some(dir) = std::env::var_os(DEBUG_DUMP_ENV) else {
        return Ok(());
    };
    let dir = PathBuf::from(dir);
    std::fs::create_dir_all(&dir)
        .with_context(|| format!("creating Smart Turn debug dump dir {}", dir.display()))?;
    let stem = format!(
        "{}_end{}_decision{}_audio{}",
        sanitize_filename_component(stream_session_id),
        end_sample,
        decision_sample,
        audio.len()
    );
    let wav_path = dir.join(format!("{stem}.wav"));
    let json_path = dir.join(format!("{stem}.json"));
    let spec = hound::WavSpec {
        channels: 1,
        sample_rate: SAMPLE_RATE,
        bits_per_sample: 32,
        sample_format: hound::SampleFormat::Float,
    };
    let mut writer = hound::WavWriter::create(&wav_path, spec)
        .with_context(|| format!("creating {}", wav_path.display()))?;
    for sample in audio {
        writer.write_sample(sample.clamp(-1.0, 1.0))?;
    }
    writer.finalize()?;
    std::fs::write(
        &json_path,
        format!(
            "{{\n  \"stream_session_id\": \"{}\",\n  \"end_sample\": {},\n  \"decision_sample\": {},\n  \"audio_samples\": {},\n  \"sample_rate\": {}\n}}\n",
            stream_session_id,
            end_sample,
            decision_sample,
            audio.len(),
            SAMPLE_RATE,
        ),
    )
    .with_context(|| format!("writing {}", json_path.display()))?;
    Ok(())
}

fn sanitize_filename_component(value: &str) -> String {
    value
        .chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() || matches!(c, '-' | '_' | '.') {
                c
            } else {
                '_'
            }
        })
        .collect()
}

fn ns_to_ms(ns: u64) -> f64 {
    ns as f64 / 1_000_000.0
}

#[derive(Debug, Serialize)]
struct SmartTurnSessionStartEvent {
    event: &'static str,
    stream_id: String,
    stream_session_id: String,
    adapter_id: String,
    detector: &'static str,
    model_name: &'static str,
    model_path: String,
    input_name: String,
    output_name: String,
    threshold: f32,
    timeout_ms: u32,
    cpu_count: usize,
    max_audio_secs: u32,
    pre_speech_ms: u32,
    recheck_interval_ms: u32,
    recheck_max_attempts: u32,
    recheck_offsets_ms: Vec<u32>,
    shared_load_start_mono_ns: u64,
    shared_load_end_mono_ns: u64,
    shared_load_duration_ms: f64,
    daemon_mono_ns: u64,
}

#[derive(Debug, Serialize)]
struct SmartTurnCandidateEvent {
    event: &'static str,
    stream_id: String,
    stream_session_id: String,
    adapter_id: String,
    detector: &'static str,
    end_sample: u64,
    decision_sample: u64,
    audio_samples: u64,
    threshold: f32,
    timeout_ms: u32,
    daemon_mono_ns: u64,
}

#[derive(Debug, Serialize)]
struct SmartTurnDecisionEvent {
    event: &'static str,
    stream_id: String,
    stream_session_id: String,
    adapter_id: String,
    detector: &'static str,
    end_sample: u64,
    decision_sample: u64,
    probability: f32,
    threshold: f32,
    complete: bool,
    timed_out: bool,
    inference_start_mono_ns: u64,
    inference_end_mono_ns: u64,
    inference_duration_ms: f64,
    feature_duration_ms: f64,
    model_duration_ms: f64,
    audio_samples: u64,
    daemon_mono_ns: u64,
}

#[derive(Debug, Serialize)]
struct SmartTurnSessionEndEvent {
    event: &'static str,
    stream_id: String,
    stream_session_id: String,
    adapter_id: String,
    detector: &'static str,
    reason: String,
    samples_buffered: u64,
    predictions: u64,
    timeouts: u64,
    errors: u64,
    daemon_mono_ns: u64,
}

#[derive(Debug, Serialize)]
struct SmartTurnErrorEvent {
    event: &'static str,
    stream_id: String,
    stream_session_id: String,
    adapter_id: String,
    detector: &'static str,
    message: String,
    daemon_mono_ns: u64,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn feature_shape_matches_smart_turn_contract() {
        let extractor = WhisperFeatureExtractor::new();
        let audio = vec![0.0_f32; WINDOW_SAMPLES];
        let features = extractor.compute(&audio);
        assert_eq!(features.len(), N_MELS * (WINDOW_SAMPLES / HOP_LENGTH));
        assert_eq!(features.len(), 80 * 800);
    }

    #[test]
    fn hann_window_matches_periodic_definition() {
        let window = periodic_hann_window(4);
        assert!((window[0] - 0.0).abs() < 1e-12);
        assert!((window[1] - 0.5).abs() < 1e-12);
        assert!((window[2] - 1.0).abs() < 1e-12);
        assert!((window[3] - 0.5).abs() < 1e-12);
    }

    #[test]
    fn segment_keeps_recent_audio_before_vad_end() {
        let hello = test_hello();
        let mut session = SmartTurnSession::new(hello, 8, 500);
        let frame = test_frame(0, 160_000);
        session.push_samples(&frame, &vec![0.25; 160_000]);
        assert_eq!(session.audio.len(), WINDOW_SAMPLES);
        let segment = session.segment_for_prediction(160_000);
        assert_eq!(segment.len(), WINDOW_SAMPLES);
    }

    #[test]
    fn real_model_smoke_when_env_set() -> Result<()> {
        let Some(model_path) = std::env::var_os("SPEECH_CORE_SMART_TURN_MODEL_PATH") else {
            return Ok(());
        };
        let mut detector = SmartTurnDetector::new(SmartTurnConfig {
            model_path: model_path.into(),
            ..Default::default()
        })?;
        let runtime = tokio::runtime::Runtime::new()?;
        let dir = tempfile::tempdir()?;
        let logger = runtime.block_on(async {
            let (event_tx, _) = tokio::sync::broadcast::channel(16);
            crate::JsonlLogger::open(dir.path().to_path_buf(), event_tx).await
        })?;
        let mut writer = DetectorWriter::new(&logger, runtime.handle());
        let hello = test_hello();
        detector.start_session(&hello, &mut writer)?;
        let frame = test_frame(0, 16_000);
        detector.ingest_frame(&frame, &vec![0.0; 16_000], 0, &mut writer)?;
        let decision = detector.predict_for_vad_end("ss", 16_000, 16_000, &mut writer)?;
        assert!(decision.available);
        assert!(decision.probability.is_some_and(f32::is_finite));
        Ok(())
    }

    #[test]
    #[ignore = "requires SPEECH_CORE_SMART_TURN_MODEL_PATH and prints local timing"]
    fn real_model_timing_when_env_set() -> Result<()> {
        let Some(model_path) = std::env::var_os("SPEECH_CORE_SMART_TURN_MODEL_PATH") else {
            return Ok(());
        };
        let mut detector = SmartTurnDetector::new(SmartTurnConfig {
            model_path: model_path.into(),
            ..Default::default()
        })?;
        let runtime = tokio::runtime::Runtime::new()?;
        let dir = tempfile::tempdir()?;
        let logger = runtime.block_on(async {
            let (event_tx, _) = tokio::sync::broadcast::channel(64);
            crate::JsonlLogger::open(dir.path().to_path_buf(), event_tx).await
        })?;
        let mut writer = DetectorWriter::new(&logger, runtime.handle());
        let hello = test_hello();
        detector.start_session(&hello, &mut writer)?;
        let audio: Vec<f32> = (0..WINDOW_SAMPLES)
            .map(|i| ((i as f32 * 0.017).sin() * 0.05).clamp(-1.0, 1.0))
            .collect();
        let frame = test_frame(0, WINDOW_SAMPLES as u32);
        detector.ingest_frame(&frame, &audio, 0, &mut writer)?;

        let mut durations = Vec::new();
        for _ in 0..6 {
            let decision = detector.predict_for_vad_end(
                "ss",
                WINDOW_SAMPLES as u64,
                WINDOW_SAMPLES as u64,
                &mut writer,
            )?;
            assert!(decision.available);
            if let Some(duration_ms) = decision.duration_ms {
                durations.push(duration_ms);
            }
        }
        durations.sort_by(|a, b| a.total_cmp(b));
        eprintln!(
            "smart_turn timing total_ms min={:.2} median={:.2} max={:.2}",
            durations.first().copied().unwrap_or_default(),
            durations[durations.len() / 2],
            durations.last().copied().unwrap_or_default(),
        );
        Ok(())
    }

    fn test_hello() -> HelloState {
        HelloState {
            adapter_id: "a".to_owned(),
            stream_id: "s".to_owned(),
            stream_session_id: "ss".to_owned(),
            source_kind: speech_core_protocol::SourceKind::Synthetic,
            sample_rate_hz: SAMPLE_RATE,
            channels: 1,
            format: PcmFormat::PcmS16Le,
            timestamp_provenance: speech_core_protocol::TimestampProvenance::uncalibrated(
                "test",
                speech_core_protocol::ClockDomain::HostMonotonic,
                speech_core_protocol::TimestampQuality::SyntheticScheduled,
            ),
        }
    }

    fn test_frame(source_sample_start: u64, sample_count: u32) -> AudioFrame {
        let provenance = speech_core_protocol::TimestampProvenance::uncalibrated(
            "test",
            speech_core_protocol::ClockDomain::HostMonotonic,
            speech_core_protocol::TimestampQuality::SyntheticScheduled,
        );
        AudioFrame::new(
            speech_core_protocol::AudioFrameHeader {
                stream_id: "s".into(),
                stream_session_id: "ss".into(),
                adapter_id: "a".into(),
                source_kind: speech_core_protocol::SourceKind::Synthetic,
                seq: 0,
                format: PcmFormat::PcmS16Le,
                sample_rate_hz: SAMPLE_RATE,
                channels: 1,
                source_sample_start,
                sample_count,
                source_capture_mono_ns: 0,
                adapter_send_mono_ns: 0,
                timestamp_provenance: provenance,
                preceding_source_gap: None,
            },
            vec![0_u8; sample_count as usize * 2],
        )
        .unwrap()
    }
}
