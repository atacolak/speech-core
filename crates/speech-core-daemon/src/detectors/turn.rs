use anyhow::Result;
use serde::Serialize;
use speech_core_protocol::now_mono_ns;
use std::collections::HashMap;
use std::time::{Duration, Instant};

use super::{DetectorAction, DetectorSignal, DetectorWriter, EouResetMode};
use crate::model::ModelProgressMap;
use crate::HelloState;

#[derive(Debug, Clone)]
pub struct TurnManagerConfig {
    /// Promote Parakeet EOU tokens into authoritative non-degraded turn closure.
    pub model_eou_close_enabled: bool,
    /// Promote VAD speech-end into degraded turn closure. Useful as fallback or comparison mode.
    pub vad_close_enabled: bool,
    /// Ignore VAD speech-end for segments shorter than this. This filters laptop mic clicks/noise.
    pub min_vad_speech_ms: u32,
    /// Ignore model EOU tokens before at least this much speech has been observed.
    pub min_model_eou_speech_ms: u32,
    /// Ignore repeated EOU tokens this close to the previous close.
    pub model_eou_refractory_ms: u32,
    /// Shared model progress tracker so we can wait for model catch-up before closing a VAD turn.
    pub model_progress: Option<ModelProgressMap>,
    /// Max wait for the model to catch up before emitting a degraded VAD turn closure.
    pub model_alignment_timeout_ms: u32,
    /// Emit a non-closing human-presence event after this much speech-like audio without committed tokens.
    pub human_hold_silence_ms: u32,
    /// Require Smart Turn semantic completeness before accepting VAD speech_end.
    pub semantic_gate_enabled: bool,
    /// If true, Smart Turn incomplete decisions suppress VAD closure; if false, decisions are logged only.
    pub semantic_gate_close_enabled: bool,
}

impl Default for TurnManagerConfig {
    fn default() -> Self {
        Self {
            model_eou_close_enabled: false,
            vad_close_enabled: false,
            min_vad_speech_ms: 300,
            min_model_eou_speech_ms: 300,
            model_eou_refractory_ms: 700,
            model_progress: None,
            model_alignment_timeout_ms: 3000,
            human_hold_silence_ms: 12000,
            semantic_gate_enabled: false,
            semantic_gate_close_enabled: false,
        }
    }
}

pub struct TurnManager {
    config: TurnManagerConfig,
    sessions: HashMap<String, TurnSession>,
}

impl TurnManager {
    pub fn new(config: TurnManagerConfig) -> Self {
        Self {
            config,
            sessions: HashMap::new(),
        }
    }

    pub fn start_session(
        &mut self,
        hello: &HelloState,
        writer: &mut DetectorWriter<'_>,
    ) -> Result<()> {
        self.sessions.insert(
            hello.stream_session_id.clone(),
            TurnSession::new(hello.clone()),
        );
        writer.write(&TurnSessionStartEvent {
            event: "turn_session_start",
            stream_id: hello.stream_id.clone(),
            stream_session_id: hello.stream_session_id.clone(),
            adapter_id: hello.adapter_id.clone(),
            model_eou_close_enabled: self.config.model_eou_close_enabled,
            vad_close_enabled: self.config.vad_close_enabled,
            min_vad_speech_ms: self.config.min_vad_speech_ms,
            min_model_eou_speech_ms: self.config.min_model_eou_speech_ms,
            model_eou_refractory_ms: self.config.model_eou_refractory_ms,
            semantic_gate_enabled: self.config.semantic_gate_enabled,
            semantic_gate_close_enabled: self.config.semantic_gate_close_enabled,
            human_hold_silence_ms: self.config.human_hold_silence_ms,
            daemon_mono_ns: now_mono_ns(),
        })
    }

    pub fn handle_signal(
        &mut self,
        signal: DetectorSignal,
        writer: &mut DetectorWriter<'_>,
    ) -> Result<Vec<DetectorAction>> {
        match signal {
            DetectorSignal::VadSegmentStart {
                detector,
                stream_id,
                stream_session_id,
                adapter_id,
                start_sample,
                decision_sample,
                confidence,
            } => {
                let session = self.session_mut(&stream_id, &stream_session_id, &adapter_id);
                let started_new_turn = session.open_turn.is_none();
                if started_new_turn {
                    session.start_turn(start_sample, "vad", writer)?;
                }
                session.saw_vad_signal = true;
                session.last_vad_start_sample = Some(start_sample);
                session.in_speech = true;
                writer.write(&TurnSignalObservedEvent {
                    event: "turn_signal_observed",
                    stream_id: stream_id.clone(),
                    stream_session_id: stream_session_id.clone(),
                    adapter_id: adapter_id.clone(),
                    detector,
                    signal: "vad_speech_start",
                    sample: start_sample,
                    decision_sample,
                    confidence,
                    daemon_mono_ns: now_mono_ns(),
                })?;
                if started_new_turn {
                    Ok(vec![DetectorAction::ResetEouState {
                        stream_id,
                        stream_session_id,
                        adapter_id,
                        mode: EouResetMode::Stream,
                        anchor_sample: start_sample,
                        source: "vad",
                        reason: "vad_speech_start",
                        decision_sample,
                    }])
                } else {
                    Ok(Vec::new())
                }
            }
            DetectorSignal::VadSegmentEnd {
                detector,
                stream_id,
                stream_session_id,
                adapter_id,
                start_sample,
                end_sample,
                decision_sample,
                confidence,
            } => {
                let vad_close_enabled = self.config.vad_close_enabled;
                let semantic_gate_blocks_vad =
                    self.config.semantic_gate_enabled && self.config.semantic_gate_close_enabled;
                let min_vad_speech_samples = ms_to_samples(self.config.min_vad_speech_ms);
                let human_hold_silence_samples = ms_to_samples(self.config.human_hold_silence_ms);
                // Snapshot model alignment config before mutable session borrow.
                let model_progress = self.config.model_progress.clone();
                let model_alignment_timeout_ms = self.config.model_alignment_timeout_ms;
                let session = self.session_mut(&stream_id, &stream_session_id, &adapter_id);
                if session.open_turn.is_none() {
                    session.start_turn(start_sample, "vad", writer)?;
                }
                session.saw_vad_signal = true;
                session.in_speech = false;
                session.last_vad_end_sample = Some(end_sample);
                session.last_vad_end_decision_sample = Some(decision_sample);
                let turn_id = session
                    .open_turn
                    .as_ref()
                    .map(|turn| turn.turn_id.clone())
                    .unwrap_or_else(|| session.next_turn_id());
                writer.write(&TurnEouCandidateEvent {
                    event: "turn_eou_candidate",
                    stream_id: stream_id.clone(),
                    stream_session_id: stream_session_id.clone(),
                    adapter_id: adapter_id.clone(),
                    turn_id: turn_id.clone(),
                    source: "vad",
                    degraded: true,
                    detector,
                    confidence,
                    start_sample: Some(start_sample),
                    end_sample,
                    decision_sample,
                    text_delta: None,
                    vad_end_to_model_eou_ms: None,
                    vad_decision_to_model_eou_ms: None,
                    daemon_mono_ns: now_mono_ns(),
                })?;
                let observed_speech_samples = end_sample.saturating_sub(start_sample);
                if observed_speech_samples < min_vad_speech_samples {
                    writer.write(&TurnEouSuppressedEvent {
                        event: "turn_eou_suppressed",
                        stream_id,
                        stream_session_id,
                        adapter_id,
                        source: "vad",
                        detector,
                        reason: "vad_too_short",
                        end_sample,
                        decision_sample,
                        observed_speech_samples,
                        min_required_samples: min_vad_speech_samples,
                        daemon_mono_ns: now_mono_ns(),
                    })?;
                    if session
                        .open_turn
                        .as_ref()
                        .is_some_and(|turn| turn.start_sample == start_sample)
                    {
                        session.open_turn.take();
                    }
                    return Ok(Vec::new());
                }
                let mut actions = Vec::new();
                let mut close_source = "vad";
                let mut close_detector = detector;
                let mut close_confidence = confidence;
                let mut close_degraded = true;
                let mut close_reason = "vad_speech_end";
                if semantic_gate_blocks_vad {
                    match session.last_semantic_decision.as_ref() {
                        Some(decision)
                            if decision.end_sample == end_sample
                                && decision.decision_sample == decision_sample
                                && decision.available
                                && decision.complete =>
                        {
                            close_source = "smart_turn";
                            close_detector = decision.detector;
                            close_confidence = decision.probability;
                            close_degraded = false;
                            close_reason = "smart_turn_complete_after_vad_speech_end";
                        }
                        Some(decision)
                            if decision.end_sample == end_sample
                                && decision.decision_sample == decision_sample
                                && decision.available =>
                        {
                            writer.write(&TurnEouSuppressedEvent {
                                event: "turn_eou_suppressed",
                                stream_id: stream_id.clone(),
                                stream_session_id: stream_session_id.clone(),
                                adapter_id: adapter_id.clone(),
                                source: "semantic",
                                detector: decision.detector,
                                reason: "semantic_incomplete",
                                end_sample,
                                decision_sample,
                                observed_speech_samples,
                                min_required_samples: min_vad_speech_samples,
                                daemon_mono_ns: now_mono_ns(),
                            })?;
                            if human_hold_silence_samples > 0 {
                                let token_anchor = model_progress
                                    .as_ref()
                                    .and_then(|progress| {
                                        progress.last_token_end_sample(&stream_session_id)
                                    })
                                    .or_else(|| {
                                        session.open_turn.as_ref().map(|turn| turn.start_sample)
                                    })
                                    .unwrap_or(start_sample);
                                let samples_without_tokens =
                                    decision_sample.saturating_sub(token_anchor);
                                if samples_without_tokens >= human_hold_silence_samples
                                    && session.last_human_hold_token_anchor != Some(token_anchor)
                                {
                                    session.last_human_hold_token_anchor = Some(token_anchor);
                                    writer.write(&TurnHumanHoldEvent {
                                        event: "turn_human_hold",
                                        stream_id,
                                        stream_session_id,
                                        adapter_id,
                                        turn_id: turn_id.clone(),
                                        detector: decision.detector,
                                        reason: "speech_like_audio_without_tokens",
                                        start_sample,
                                        end_sample,
                                        decision_sample,
                                        last_token_end_sample: token_anchor,
                                        samples_without_tokens,
                                        ms_without_tokens: samples_to_ms(samples_without_tokens),
                                        probability: decision.probability,
                                        threshold: None,
                                        daemon_mono_ns: now_mono_ns(),
                                    })?;
                                }
                            }
                            return Ok(Vec::new());
                        }
                        _ => {
                            // Fail open: if Smart Turn is unavailable or did not produce a decision
                            // for this VAD boundary, preserve the current VAD behavior.
                            close_reason = "smart_turn_unavailable_vad_fallback";
                        }
                    }
                }
                if vad_close_enabled {
                    // Wait for the model to catch up to end_sample before closing.
                    // This fixes the event-ordering race where turn_closed lands before
                    // the model's final transcript updates for the same audio.
                    if let Some(ref model_progress) = model_progress {
                        let timeout = Duration::from_millis(model_alignment_timeout_ms as u64);
                        let deadline = Instant::now() + timeout;
                        loop {
                            let committed = model_progress.get(&stream_session_id).unwrap_or(0);
                            if committed >= end_sample {
                                break;
                            }
                            if Instant::now() >= deadline {
                                break;
                            }
                            std::thread::sleep(Duration::from_millis(1));
                        }
                    }
                    session.close_turn(
                        turn_id,
                        close_source,
                        close_degraded,
                        close_detector,
                        close_confidence,
                        end_sample,
                        decision_sample,
                        close_reason,
                        writer,
                    )?;
                    actions.push(DetectorAction::ResetEouState {
                        stream_id,
                        stream_session_id,
                        adapter_id,
                        mode: EouResetMode::Decoder,
                        anchor_sample: decision_sample,
                        source: close_source,
                        reason: close_reason,
                        decision_sample,
                    });
                }
                Ok(actions)
            }
            DetectorSignal::VadSpeechPresence {
                detector,
                stream_id,
                stream_session_id,
                adapter_id,
                start_sample,
                decision_sample,
                confidence,
            } => {
                let human_hold_silence_samples = ms_to_samples(self.config.human_hold_silence_ms);
                let model_progress = self.config.model_progress.clone();
                let session = self.session_mut(&stream_id, &stream_session_id, &adapter_id);
                if human_hold_silence_samples == 0 || session.open_turn.is_none() {
                    return Ok(Vec::new());
                }
                let token_anchor = model_progress
                    .as_ref()
                    .and_then(|progress| progress.last_token_end_sample(&stream_session_id))
                    .or_else(|| session.open_turn.as_ref().map(|turn| turn.start_sample))
                    .unwrap_or(start_sample);
                let samples_without_tokens = decision_sample.saturating_sub(token_anchor);
                if samples_without_tokens >= human_hold_silence_samples
                    && session.last_human_hold_token_anchor != Some(token_anchor)
                {
                    session.last_human_hold_token_anchor = Some(token_anchor);
                    let turn_id = session
                        .open_turn
                        .as_ref()
                        .map(|turn| turn.turn_id.clone())
                        .unwrap_or_else(|| session.next_turn_id());
                    writer.write(&TurnHumanHoldEvent {
                        event: "turn_human_hold",
                        stream_id,
                        stream_session_id,
                        adapter_id,
                        turn_id,
                        detector,
                        reason: "speech_like_audio_without_tokens",
                        start_sample,
                        end_sample: decision_sample,
                        decision_sample,
                        last_token_end_sample: token_anchor,
                        samples_without_tokens,
                        ms_without_tokens: samples_to_ms(samples_without_tokens),
                        probability: confidence,
                        threshold: None,
                        daemon_mono_ns: now_mono_ns(),
                    })?;
                }
                Ok(Vec::new())
            }
            DetectorSignal::ModelEou {
                detector,
                stream_id,
                stream_session_id,
                adapter_id,
                end_sample,
                decision_sample,
                text_delta,
                confidence,
            } => {
                let model_eou_close_enabled = self.config.model_eou_close_enabled;
                let min_model_eou_speech_samples =
                    ms_to_samples(self.config.min_model_eou_speech_ms);
                let model_eou_refractory_samples =
                    ms_to_samples(self.config.model_eou_refractory_ms);
                let session = self.session_mut(&stream_id, &stream_session_id, &adapter_id);
                let provisional_start = session
                    .last_vad_start_sample
                    .or(session.last_closed_end_sample)
                    .unwrap_or(0);
                let observed_speech_samples = decision_sample.saturating_sub(provisional_start);
                let vad_still_in_speech = session.saw_vad_signal && session.in_speech;
                let no_new_vad_speech_since_close = session.saw_vad_signal
                    && session.last_closed_decision_sample.is_some_and(|last| {
                        !session
                            .last_vad_start_sample
                            .is_some_and(|start| start > last)
                    });
                let refractory = session.last_closed_decision_sample.is_some_and(|last| {
                    decision_sample.saturating_sub(last) < model_eou_refractory_samples
                });
                let too_early = observed_speech_samples < min_model_eou_speech_samples;
                if too_early || vad_still_in_speech || refractory || no_new_vad_speech_since_close {
                    writer.write(&TurnEouSuppressedEvent {
                        event: "turn_eou_suppressed",
                        stream_id,
                        stream_session_id,
                        adapter_id,
                        source: "model",
                        detector,
                        reason: if too_early {
                            "too_early"
                        } else if vad_still_in_speech {
                            "vad_still_in_speech"
                        } else if no_new_vad_speech_since_close {
                            "no_new_vad_speech_since_close"
                        } else {
                            "refractory"
                        },
                        end_sample,
                        decision_sample,
                        observed_speech_samples,
                        min_required_samples: min_model_eou_speech_samples,
                        daemon_mono_ns: now_mono_ns(),
                    })?;
                    return Ok(Vec::new());
                }
                if session.open_turn.is_none() {
                    session.start_turn(provisional_start, "model", writer)?;
                }
                let effective_end_sample = session
                    .last_vad_end_sample
                    .filter(|sample| *sample <= decision_sample)
                    .unwrap_or(end_sample);
                let vad_end_to_model_eou_ms = session
                    .last_vad_end_sample
                    .filter(|sample| *sample <= decision_sample)
                    .map(|sample| samples_to_ms(decision_sample.saturating_sub(sample)));
                let vad_decision_to_model_eou_ms = session
                    .last_vad_end_decision_sample
                    .filter(|sample| *sample <= decision_sample)
                    .map(|sample| samples_to_ms(decision_sample.saturating_sub(sample)));
                let turn_id = session
                    .open_turn
                    .as_ref()
                    .map(|turn| turn.turn_id.clone())
                    .unwrap_or_else(|| session.next_turn_id());
                writer.write(&TurnEouCandidateEvent {
                    event: "turn_eou_candidate",
                    stream_id: stream_id.clone(),
                    stream_session_id: stream_session_id.clone(),
                    adapter_id: adapter_id.clone(),
                    turn_id: turn_id.clone(),
                    source: "model",
                    degraded: false,
                    detector,
                    confidence,
                    start_sample: session.open_turn.as_ref().map(|turn| turn.start_sample),
                    end_sample: effective_end_sample,
                    decision_sample,
                    text_delta: Some(text_delta),
                    vad_end_to_model_eou_ms,
                    vad_decision_to_model_eou_ms,
                    daemon_mono_ns: now_mono_ns(),
                })?;
                let mut actions = Vec::new();
                if model_eou_close_enabled {
                    session.close_turn(
                        turn_id,
                        "model",
                        false,
                        detector,
                        confidence,
                        effective_end_sample,
                        decision_sample,
                        "eou_token_detected",
                        writer,
                    )?;
                    actions.push(DetectorAction::ResetEouState {
                        stream_id,
                        stream_session_id,
                        adapter_id,
                        mode: EouResetMode::Decoder,
                        anchor_sample: decision_sample,
                        source: "model",
                        reason: "eou_token_detected",
                        decision_sample,
                    });
                }
                Ok(actions)
            }
            DetectorSignal::SemanticTurnDecision {
                detector,
                stream_id,
                stream_session_id,
                adapter_id,
                end_sample,
                decision_sample,
                complete,
                probability,
                threshold,
                timed_out,
                available,
                reason,
                duration_ms,
            } => {
                let session = self.session_mut(&stream_id, &stream_session_id, &adapter_id);
                session.last_semantic_decision = Some(SemanticDecisionState {
                    detector,
                    end_sample,
                    decision_sample,
                    complete,
                    available,
                    probability,
                });
                writer.write(&TurnSemanticDecisionEvent {
                    event: "turn_semantic_decision",
                    stream_id,
                    stream_session_id,
                    adapter_id,
                    detector,
                    end_sample,
                    decision_sample,
                    complete,
                    available,
                    timed_out,
                    probability,
                    threshold,
                    reason,
                    duration_ms,
                    daemon_mono_ns: now_mono_ns(),
                })?;
                Ok(Vec::new())
            }
            DetectorSignal::VadAcousticFallback {
                detector,
                stream_id,
                stream_session_id,
                adapter_id,
                start_sample,
                end_sample,
                decision_sample,
                silence_samples,
                confidence,
            } => {
                let vad_close_enabled = self.config.vad_close_enabled;
                let min_vad_speech_samples = ms_to_samples(self.config.min_vad_speech_ms);
                let model_progress = self.config.model_progress.clone();
                let model_alignment_timeout_ms = self.config.model_alignment_timeout_ms;
                let session = self.session_mut(&stream_id, &stream_session_id, &adapter_id);
                let observed_speech_samples = end_sample.saturating_sub(start_sample);
                if session.open_turn.is_none() {
                    return Ok(Vec::new());
                }
                let turn_id = session
                    .open_turn
                    .as_ref()
                    .map(|turn| turn.turn_id.clone())
                    .unwrap_or_else(|| session.next_turn_id());
                if observed_speech_samples < min_vad_speech_samples {
                    writer.write(&TurnEouSuppressedEvent {
                        event: "turn_eou_suppressed",
                        stream_id,
                        stream_session_id,
                        adapter_id,
                        source: "vad",
                        detector,
                        reason: "acoustic_fallback_vad_too_short",
                        end_sample,
                        decision_sample,
                        observed_speech_samples,
                        min_required_samples: min_vad_speech_samples,
                        daemon_mono_ns: now_mono_ns(),
                    })?;
                    return Ok(Vec::new());
                }
                writer.write(&TurnEouCandidateEvent {
                    event: "turn_eou_candidate",
                    stream_id: stream_id.clone(),
                    stream_session_id: stream_session_id.clone(),
                    adapter_id: adapter_id.clone(),
                    turn_id: turn_id.clone(),
                    source: "vad_acoustic_fallback",
                    degraded: true,
                    detector,
                    confidence,
                    start_sample: Some(start_sample),
                    end_sample,
                    decision_sample,
                    text_delta: None,
                    vad_end_to_model_eou_ms: Some(samples_to_ms(silence_samples)),
                    vad_decision_to_model_eou_ms: None,
                    daemon_mono_ns: now_mono_ns(),
                })?;
                if vad_close_enabled {
                    if let Some(ref model_progress) = model_progress {
                        let timeout = Duration::from_millis(model_alignment_timeout_ms as u64);
                        let deadline = Instant::now() + timeout;
                        loop {
                            let committed = model_progress.get(&stream_session_id).unwrap_or(0);
                            if committed >= end_sample {
                                break;
                            }
                            if Instant::now() >= deadline {
                                break;
                            }
                            std::thread::sleep(Duration::from_millis(1));
                        }
                    }
                    session.close_turn(
                        turn_id,
                        "vad_acoustic_fallback",
                        true,
                        detector,
                        confidence,
                        end_sample,
                        decision_sample,
                        "vad_acoustic_fallback_low_probability_silence",
                        writer,
                    )?;
                    Ok(vec![DetectorAction::ResetEouState {
                        stream_id,
                        stream_session_id,
                        adapter_id,
                        mode: EouResetMode::Decoder,
                        anchor_sample: decision_sample,
                        source: "vad_acoustic_fallback",
                        reason: "vad_acoustic_fallback_low_probability_silence",
                        decision_sample,
                    }])
                } else {
                    Ok(Vec::new())
                }
            }
        }
    }

    pub fn end_session(
        &mut self,
        stream_session_id: &str,
        reason: &str,
        writer: &mut DetectorWriter<'_>,
    ) -> Result<()> {
        if let Some(mut session) = self.sessions.remove(stream_session_id) {
            if let Some(turn) = session.open_turn.take() {
                let end_sample = session.last_vad_end_sample.unwrap_or(turn.start_sample);
                session.close_specific_turn(
                    turn.turn_id,
                    "session_end",
                    true,
                    "session_end",
                    None,
                    end_sample,
                    end_sample,
                    reason,
                    writer,
                )?;
            }
            writer.write(&TurnSessionEndEvent {
                event: "turn_session_end",
                stream_id: session.hello.stream_id,
                stream_session_id: session.hello.stream_session_id,
                adapter_id: session.hello.adapter_id,
                reason: reason.to_owned(),
                turns_started: session.turns_started,
                turns_closed: session.turns_closed,
                daemon_mono_ns: now_mono_ns(),
            })?;
        }
        Ok(())
    }

    pub fn finalize_all(&mut self, reason: &str, writer: &mut DetectorWriter<'_>) -> Result<()> {
        let ids: Vec<String> = self.sessions.keys().cloned().collect();
        for id in ids {
            self.end_session(&id, reason, writer)?;
        }
        Ok(())
    }

    fn session_mut(
        &mut self,
        stream_id: &str,
        stream_session_id: &str,
        adapter_id: &str,
    ) -> &mut TurnSession {
        self.sessions
            .entry(stream_session_id.to_owned())
            .or_insert_with(|| {
                TurnSession::new(HelloState {
                    adapter_id: adapter_id.to_owned(),
                    stream_id: stream_id.to_owned(),
                    stream_session_id: stream_session_id.to_owned(),
                    source_kind: speech_core_protocol::SourceKind::Other,
                    sample_rate_hz: 16_000,
                    channels: 1,
                    format: speech_core_protocol::PcmFormat::PcmF32Le,
                    timestamp_provenance: speech_core_protocol::TimestampProvenance::uncalibrated(
                        "unknown",
                        speech_core_protocol::ClockDomain::Unknown,
                        speech_core_protocol::TimestampQuality::Unknown,
                    ),
                })
            })
    }
}

struct TurnSession {
    hello: HelloState,
    open_turn: Option<OpenTurn>,
    next_turn_index: u64,
    turns_started: u64,
    turns_closed: u64,
    in_speech: bool,
    saw_vad_signal: bool,
    last_vad_start_sample: Option<u64>,
    last_vad_end_sample: Option<u64>,
    last_vad_end_decision_sample: Option<u64>,
    last_closed_end_sample: Option<u64>,
    last_closed_decision_sample: Option<u64>,
    last_semantic_decision: Option<SemanticDecisionState>,
    last_human_hold_token_anchor: Option<u64>,
}

impl TurnSession {
    fn new(hello: HelloState) -> Self {
        Self {
            hello,
            open_turn: None,
            next_turn_index: 0,
            turns_started: 0,
            turns_closed: 0,
            in_speech: false,
            saw_vad_signal: false,
            last_vad_start_sample: None,
            last_vad_end_sample: None,
            last_vad_end_decision_sample: None,
            last_closed_end_sample: None,
            last_closed_decision_sample: None,
            last_semantic_decision: None,
            last_human_hold_token_anchor: None,
        }
    }

    fn next_turn_id(&self) -> String {
        format!(
            "{}:turn:{}",
            self.hello.stream_session_id, self.next_turn_index
        )
    }

    fn start_turn(
        &mut self,
        start_sample: u64,
        source: &'static str,
        writer: &mut DetectorWriter<'_>,
    ) -> Result<()> {
        let turn_id = self.next_turn_id();
        self.next_turn_index = self.next_turn_index.saturating_add(1);
        self.turns_started = self.turns_started.saturating_add(1);
        self.open_turn = Some(OpenTurn {
            turn_id: turn_id.clone(),
            start_sample,
        });
        writer.write(&TurnStartedEvent {
            event: "turn_started",
            stream_id: self.hello.stream_id.clone(),
            stream_session_id: self.hello.stream_session_id.clone(),
            adapter_id: self.hello.adapter_id.clone(),
            turn_id,
            source,
            start_sample,
            daemon_mono_ns: now_mono_ns(),
        })
    }

    #[allow(clippy::too_many_arguments)]
    fn close_turn(
        &mut self,
        turn_id: String,
        source: &'static str,
        degraded: bool,
        detector: &'static str,
        confidence: Option<f32>,
        end_sample: u64,
        decision_sample: u64,
        reason: &'static str,
        writer: &mut DetectorWriter<'_>,
    ) -> Result<()> {
        self.open_turn.take();
        self.close_specific_turn(
            turn_id,
            source,
            degraded,
            detector,
            confidence,
            end_sample,
            decision_sample,
            reason,
            writer,
        )
    }

    #[allow(clippy::too_many_arguments)]
    fn close_specific_turn(
        &mut self,
        turn_id: String,
        source: &'static str,
        degraded: bool,
        detector: &'static str,
        confidence: Option<f32>,
        end_sample: u64,
        decision_sample: u64,
        reason: &str,
        writer: &mut DetectorWriter<'_>,
    ) -> Result<()> {
        self.turns_closed = self.turns_closed.saturating_add(1);
        self.last_human_hold_token_anchor = None;
        self.last_closed_end_sample = Some(end_sample);
        self.last_closed_decision_sample = Some(decision_sample);
        writer.write(&TurnEouEvent {
            event: "turn_eou",
            stream_id: self.hello.stream_id.clone(),
            stream_session_id: self.hello.stream_session_id.clone(),
            adapter_id: self.hello.adapter_id.clone(),
            turn_id: turn_id.clone(),
            source,
            degraded,
            detector,
            confidence,
            end_sample,
            decision_sample,
            sample_time_ms: samples_to_ms(end_sample),
            reason: reason.to_owned(),
            daemon_mono_ns: now_mono_ns(),
        })?;
        writer.write(&TurnClosedEvent {
            event: "turn_closed",
            stream_id: self.hello.stream_id.clone(),
            stream_session_id: self.hello.stream_session_id.clone(),
            adapter_id: self.hello.adapter_id.clone(),
            turn_id,
            source,
            degraded,
            detector,
            end_sample,
            decision_sample,
            reason: reason.to_owned(),
            daemon_mono_ns: now_mono_ns(),
        })
    }
}

struct OpenTurn {
    turn_id: String,
    start_sample: u64,
}

#[derive(Clone, Copy)]
struct SemanticDecisionState {
    detector: &'static str,
    end_sample: u64,
    decision_sample: u64,
    complete: bool,
    available: bool,
    probability: Option<f32>,
}

#[derive(Debug, Serialize)]
struct TurnSessionStartEvent {
    event: &'static str,
    stream_id: String,
    stream_session_id: String,
    adapter_id: String,
    model_eou_close_enabled: bool,
    vad_close_enabled: bool,
    min_vad_speech_ms: u32,
    min_model_eou_speech_ms: u32,
    model_eou_refractory_ms: u32,
    semantic_gate_enabled: bool,
    semantic_gate_close_enabled: bool,
    human_hold_silence_ms: u32,
    daemon_mono_ns: u64,
}

#[derive(Debug, Serialize)]
struct TurnSignalObservedEvent {
    event: &'static str,
    stream_id: String,
    stream_session_id: String,
    adapter_id: String,
    detector: &'static str,
    signal: &'static str,
    sample: u64,
    decision_sample: u64,
    confidence: Option<f32>,
    daemon_mono_ns: u64,
}

#[derive(Debug, Serialize)]
struct TurnStartedEvent {
    event: &'static str,
    stream_id: String,
    stream_session_id: String,
    adapter_id: String,
    turn_id: String,
    source: &'static str,
    start_sample: u64,
    daemon_mono_ns: u64,
}

#[derive(Debug, Serialize)]
struct TurnEouCandidateEvent {
    event: &'static str,
    stream_id: String,
    stream_session_id: String,
    adapter_id: String,
    turn_id: String,
    source: &'static str,
    degraded: bool,
    detector: &'static str,
    confidence: Option<f32>,
    start_sample: Option<u64>,
    end_sample: u64,
    decision_sample: u64,
    text_delta: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    vad_end_to_model_eou_ms: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    vad_decision_to_model_eou_ms: Option<u64>,
    daemon_mono_ns: u64,
}

#[derive(Debug, Serialize)]
struct TurnEouSuppressedEvent {
    event: &'static str,
    stream_id: String,
    stream_session_id: String,
    adapter_id: String,
    source: &'static str,
    detector: &'static str,
    reason: &'static str,
    end_sample: u64,
    decision_sample: u64,
    observed_speech_samples: u64,
    min_required_samples: u64,
    daemon_mono_ns: u64,
}

#[derive(Debug, Serialize)]
struct TurnSemanticDecisionEvent {
    event: &'static str,
    stream_id: String,
    stream_session_id: String,
    adapter_id: String,
    detector: &'static str,
    end_sample: u64,
    decision_sample: u64,
    complete: bool,
    available: bool,
    timed_out: bool,
    probability: Option<f32>,
    threshold: Option<f32>,
    reason: &'static str,
    duration_ms: Option<f64>,
    daemon_mono_ns: u64,
}

#[derive(Debug, Serialize)]
struct TurnHumanHoldEvent {
    event: &'static str,
    stream_id: String,
    stream_session_id: String,
    adapter_id: String,
    turn_id: String,
    detector: &'static str,
    reason: &'static str,
    start_sample: u64,
    end_sample: u64,
    decision_sample: u64,
    last_token_end_sample: u64,
    samples_without_tokens: u64,
    ms_without_tokens: u64,
    probability: Option<f32>,
    threshold: Option<f32>,
    daemon_mono_ns: u64,
}

#[derive(Debug, Serialize)]
struct TurnEouEvent {
    event: &'static str,
    stream_id: String,
    stream_session_id: String,
    adapter_id: String,
    turn_id: String,
    source: &'static str,
    degraded: bool,
    detector: &'static str,
    confidence: Option<f32>,
    end_sample: u64,
    decision_sample: u64,
    sample_time_ms: u64,
    reason: String,
    daemon_mono_ns: u64,
}

#[derive(Debug, Serialize)]
struct TurnClosedEvent {
    event: &'static str,
    stream_id: String,
    stream_session_id: String,
    adapter_id: String,
    turn_id: String,
    source: &'static str,
    degraded: bool,
    detector: &'static str,
    end_sample: u64,
    decision_sample: u64,
    reason: String,
    daemon_mono_ns: u64,
}

#[derive(Debug, Serialize)]
struct TurnSessionEndEvent {
    event: &'static str,
    stream_id: String,
    stream_session_id: String,
    adapter_id: String,
    reason: String,
    turns_started: u64,
    turns_closed: u64,
    daemon_mono_ns: u64,
}

fn samples_to_ms(sample: u64) -> u64 {
    sample.saturating_mul(1_000) / 16_000
}

fn ms_to_samples(ms: u32) -> u64 {
    u64::from(ms).saturating_mul(16_000) / 1_000
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sample_time_is_16khz() {
        assert_eq!(samples_to_ms(16_000), 1_000);
        assert_eq!(samples_to_ms(480), 30);
    }
}
