use anyhow::Result;
use serde::Serialize;
use speech_core_protocol::now_mono_ns;
use std::collections::HashMap;
use std::time::{Duration, Instant};

use super::{DetectorAction, DetectorSignal, DetectorWriter, EouResetMode};
use crate::model::{ModelDrainHandle, ModelDrainRequest, ModelProgressMap};
use crate::{AudioGapReset, HelloState};

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
    /// Optional model worker handle used to flush buffered trailing audio before VAD closure.
    pub model_drain: Option<ModelDrainHandle>,
    /// Max wait for the model to catch up before emitting a degraded VAD turn closure.
    pub model_alignment_timeout_ms: u32,
    /// Emit a non-closing human-presence event after this much speech-like audio without committed tokens.
    pub human_hold_silence_ms: u32,
    /// Close transcript-backed turns after this much low-VAD silence when VAD never opened acoustically.
    pub transcript_silence_close_ms: u32,
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
            model_drain: None,
            model_alignment_timeout_ms: 3000,
            human_hold_silence_ms: 12000,
            transcript_silence_close_ms: 700,
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
            transcript_silence_close_ms: self.config.transcript_silence_close_ms,
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
                let model_drain = self.config.model_drain.clone();
                let model_alignment_timeout_ms = self.config.model_alignment_timeout_ms;
                let session = self.session_mut(&stream_id, &stream_session_id, &adapter_id);
                // If semantic gate already closed a turn at or past this VAD
                // boundary, don't resurrect a dead turn from the old segment.
                if session.open_turn.is_none()
                    && session
                        .last_closed_decision_sample
                        .is_some_and(|closed| closed >= decision_sample)
                {
                    return Ok(Vec::new());
                }
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
                            // Smart Turn completion is an endpoint decision. Do not gate it on
                            // committed transcript tokens: ASR tokens can legitimately lag the
                            // endpoint decision, especially with Nemotron right-context.
                            close_source = "smart_turn";
                            close_detector = decision.detector;
                            close_confidence = decision.probability;
                            close_degraded = false;
                            close_reason = "smart_turn_complete_after_vad_speech_end";
                        }
                        Some(decision)
                            if decision.end_sample == end_sample
                                && decision.decision_sample == decision_sample
                                && decision.available
                                && !decision.timed_out =>
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
                                    .unwrap_or(start_sample);
                                let samples_without_tokens =
                                    decision_sample.saturating_sub(token_anchor);
                                if samples_without_tokens >= human_hold_silence_samples
                                    && session.last_human_hold_token_anchor != Some(token_anchor)
                                {
                                    session.last_human_hold_token_anchor = Some(token_anchor);
                                    writer.write(&TurnHumanHoldEvent {
                                        event: "turn_human_hold",
                                        stream_id: stream_id.clone(),
                                        stream_session_id: stream_session_id.clone(),
                                        adapter_id: adapter_id.clone(),
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
                                    close_source = "human_hold";
                                    close_detector = decision.detector;
                                    close_confidence = decision.probability;
                                    close_degraded = true;
                                    close_reason = "human_hold_speech_like_audio_without_tokens";
                                } else {
                                    return Ok(Vec::new());
                                }
                            } else {
                                return Ok(Vec::new());
                            }
                        }
                        Some(decision)
                            if decision.end_sample == end_sample
                                && decision.decision_sample == decision_sample
                                && decision.available
                                && decision.timed_out =>
                        {
                            // Fail open: Smart Turn timed out before reaching a complete decision,
                            // so preserve the current VAD behavior rather than suppressing closure.
                            close_reason = "smart_turn_timeout_vad_fallback";
                        }
                        _ => {
                            // Fail open: if Smart Turn is unavailable or did not produce a decision
                            // for this VAD boundary, preserve the current VAD behavior.
                            close_reason = "smart_turn_unavailable_vad_fallback";
                        }
                    }
                }
                if vad_close_enabled {
                    // Flush any model-worker partial chunk before waiting for ASR catch-up.
                    // VAD hangover can produce speech_end while Nemotron still has <160ms of
                    // buffered trailing audio; draining that buffer first lets final tokens commit
                    // before turn_closed rather than reopening as a transcript-backed turn later.
                    let alignment = align_model_for_close(
                        model_progress.as_ref(),
                        model_drain.as_ref(),
                        &stream_id,
                        &stream_session_id,
                        &adapter_id,
                        end_sample,
                        decision_sample,
                        close_source,
                        close_reason,
                        model_alignment_timeout_ms,
                    );
                    writer.write(&alignment.event(&stream_id, &stream_session_id, &adapter_id))?;
                    let effective_end_sample = alignment.effective_end_sample;
                    let effective_decision_sample = alignment.effective_decision_sample;
                    session.close_turn(
                        turn_id,
                        close_source,
                        close_degraded,
                        close_detector,
                        close_confidence,
                        effective_end_sample,
                        effective_decision_sample,
                        close_reason,
                        writer,
                    )?;
                    actions.push(DetectorAction::ResetEouState {
                        stream_id,
                        stream_session_id,
                        adapter_id,
                        mode: EouResetMode::Decoder,
                        anchor_sample: effective_decision_sample,
                        source: close_source,
                        reason: close_reason,
                        decision_sample: effective_decision_sample,
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
                let hold_target_ms = u64::from(self.config.human_hold_silence_ms);
                let model_progress = self.config.model_progress.clone();
                let session = self.session_mut(&stream_id, &stream_session_id, &adapter_id);
                if human_hold_silence_samples == 0 || session.open_turn.is_none() {
                    return Ok(Vec::new());
                }
                let token_anchor = model_progress
                    .as_ref()
                    .and_then(|progress| progress.last_token_end_sample(&stream_session_id))
                    .unwrap_or(start_sample);
                let samples_without_tokens = decision_sample.saturating_sub(token_anchor);
                let hold_progress_ms = samples_to_ms(samples_without_tokens);
                let open_turn_id = session.open_turn.as_ref().map(|t| t.turn_id.clone());
                let turn_id = open_turn_id.unwrap_or_else(|| session.next_turn_id());
                if samples_without_tokens >= human_hold_silence_samples
                    && session.last_human_hold_token_anchor != Some(token_anchor)
                {
                    session.last_human_hold_token_anchor = Some(token_anchor);
                    writer.write(&TurnHumanHoldEvent {
                        event: "turn_human_hold",
                        stream_id: stream_id.clone(),
                        stream_session_id: stream_session_id.clone(),
                        adapter_id: adapter_id.clone(),
                        turn_id: turn_id.clone(),
                        detector,
                        reason: "speech_like_audio_without_tokens",
                        start_sample,
                        end_sample: decision_sample,
                        decision_sample,
                        last_token_end_sample: token_anchor,
                        samples_without_tokens,
                        ms_without_tokens: hold_progress_ms,
                        probability: confidence,
                        threshold: None,
                        daemon_mono_ns: now_mono_ns(),
                    })?;
                }
                // Emit hold-timer progress every frame for the TUI bar.
                writer.write(&TurnHoldProgressEvent {
                    event: "turn_hold",
                    stream_id,
                    stream_session_id,
                    adapter_id,
                    turn_id,
                    hold_progress_ms,
                    hold_target_ms,
                    daemon_mono_ns: now_mono_ns(),
                })?;
                Ok(Vec::new())
            }
            DetectorSignal::TranscriptTokenCommitted {
                detector,
                stream_id,
                stream_session_id,
                adapter_id,
                token_index,
                text,
                start_sample,
                end_sample,
                decision_sample,
                confidence,
                drain_handle,
            } => {
                if self.config.model_drain.is_none() {
                    self.config.model_drain = Some(drain_handle);
                }
                let session = self.session_mut(&stream_id, &stream_session_id, &adapter_id);
                if !is_speech_evidence_text(&text) {
                    return Ok(Vec::new());
                }
                let late_for_closed_turn = session
                    .last_closed_decision_sample
                    .is_some_and(|closed| end_sample <= closed);
                writer.write(&TurnSignalObservedEvent {
                    event: "turn_signal_observed",
                    stream_id: stream_id.clone(),
                    stream_session_id: stream_session_id.clone(),
                    adapter_id: adapter_id.clone(),
                    detector,
                    signal: if late_for_closed_turn {
                        "transcript_token_late"
                    } else {
                        "transcript_token_committed"
                    },
                    sample: start_sample,
                    decision_sample,
                    confidence,
                    daemon_mono_ns: now_mono_ns(),
                })?;
                if late_for_closed_turn {
                    return Ok(Vec::new());
                }
                if session.open_turn.is_none() {
                    session.start_turn(start_sample, "transcript", writer)?;
                }
                session.in_speech = true;
                // A new token resets the hold timer naturally: ModelProgressMap now
                // reports this end_sample. Keep last_human_hold_token_anchor solely
                // as a duplicate-fire guard for a hold that already emitted.
                let _ = token_index;
                Ok(Vec::new())
            }
            DetectorSignal::VadLowSilence {
                detector,
                stream_id,
                stream_session_id,
                adapter_id,
                start_sample: low_silence_start_sample,
                decision_sample,
                silence_samples,
                confidence,
            } => {
                let transcript_silence_samples =
                    ms_to_samples(self.config.transcript_silence_close_ms);
                let model_progress = self.config.model_progress.clone();
                let model_alignment_timeout_ms = self.config.model_alignment_timeout_ms;
                let session = self.session_mut(&stream_id, &stream_session_id, &adapter_id);
                let Some(open_turn) = session.open_turn.as_ref() else {
                    return Ok(Vec::new());
                };
                if transcript_silence_samples == 0 || session.saw_vad_signal {
                    return Ok(Vec::new());
                }
                let token_anchor = model_progress
                    .as_ref()
                    .and_then(|progress| progress.last_token_end_sample(&stream_session_id))
                    .unwrap_or(open_turn.start_sample);
                let transcript_quiet_samples = decision_sample.saturating_sub(token_anchor);
                if silence_samples < transcript_silence_samples
                    || transcript_quiet_samples < transcript_silence_samples
                {
                    return Ok(Vec::new());
                }
                let turn_id = open_turn.turn_id.clone();
                let end_sample = token_anchor.max(open_turn.start_sample);
                writer.write(&TurnEouCandidateEvent {
                    event: "turn_eou_candidate",
                    stream_id: stream_id.clone(),
                    stream_session_id: stream_session_id.clone(),
                    adapter_id: adapter_id.clone(),
                    turn_id: turn_id.clone(),
                    source: "transcript_silence",
                    degraded: true,
                    detector,
                    confidence,
                    start_sample: Some(open_turn.start_sample.max(low_silence_start_sample)),
                    end_sample,
                    decision_sample,
                    text_delta: None,
                    vad_end_to_model_eou_ms: None,
                    vad_decision_to_model_eou_ms: None,
                    daemon_mono_ns: now_mono_ns(),
                })?;
                if let Some(ref model_progress) = model_progress {
                    wait_for_model_progress(
                        model_progress,
                        &stream_session_id,
                        end_sample,
                        model_alignment_timeout_ms,
                    );
                }
                session.close_turn(
                    turn_id,
                    "transcript_silence",
                    true,
                    detector,
                    confidence,
                    end_sample,
                    decision_sample,
                    "transcript_backed_turn_low_vad_silence",
                    writer,
                )?;
                Ok(vec![DetectorAction::ResetEouState {
                    stream_id,
                    stream_session_id,
                    adapter_id,
                    mode: EouResetMode::Decoder,
                    anchor_sample: decision_sample,
                    source: "transcript_silence",
                    reason: "transcript_backed_turn_low_vad_silence",
                    decision_sample,
                }])
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
                        source: "model_eou",
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
                    source: "model_eou",
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
                        "model_eou",
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
                        source: "model_eou",
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
                // Only record the decision if we're not going to close.
                let record_only = !complete || !self.config.semantic_gate_close_enabled;
                {
                    let session = self.session_mut(&stream_id, &stream_session_id, &adapter_id);
                    session.last_semantic_decision = Some(SemanticDecisionState {
                        detector,
                        end_sample,
                        decision_sample,
                        complete,
                        available,
                        timed_out,
                        probability,
                    });
                }
                writer.write(&TurnSemanticDecisionEvent {
                    event: "turn_semantic_decision",
                    stream_id: stream_id.clone(),
                    stream_session_id: stream_session_id.clone(),
                    adapter_id: adapter_id.clone(),
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
                if record_only {
                    return Ok(Vec::new());
                }
                // Smart turn says complete and semantic gate close is enabled.
                // Close the turn directly without waiting for VAD to emit
                // VadSegmentEnd. This handles noisy environments where VAD stays
                // in speech and never fires speech_end.
                self.close_turn_from_semantic(
                    &stream_id,
                    &stream_session_id,
                    &adapter_id,
                    detector,
                    end_sample,
                    decision_sample,
                    probability,
                    writer,
                )
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
                        wait_for_model_progress(
                            model_progress,
                            &stream_session_id,
                            end_sample,
                            model_alignment_timeout_ms,
                        );
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

    pub fn audio_gap_reset(
        &mut self,
        gap: &AudioGapReset,
        writer: &mut DetectorWriter<'_>,
    ) -> Result<()> {
        if let Some(mut session) = self.sessions.remove(&gap.stream_session_id) {
            if let Some(turn) = session.open_turn.take() {
                session.close_specific_turn(
                    turn.turn_id,
                    "audio_gap",
                    true,
                    "audio_gap",
                    None,
                    gap.expected_sample_start,
                    gap.observed_sample_start,
                    &gap.reason,
                    writer,
                )?;
            }
            session.in_speech = false;
            session.saw_vad_signal = false;
            session.last_vad_start_sample = None;
            session.last_vad_end_sample = None;
            session.last_vad_end_decision_sample = None;
            session.last_semantic_decision = None;
            session.last_human_hold_token_anchor = None;
            writer.write(&TurnAudioGapResetEvent {
                event: "turn_audio_gap_reset",
                stream_id: gap.stream_id.clone(),
                stream_session_id: gap.stream_session_id.clone(),
                adapter_id: gap.adapter_id.clone(),
                expected_sample_start: gap.expected_sample_start,
                observed_sample_start: gap.observed_sample_start,
                delta_samples: gap.delta_samples,
                reason: gap.reason.clone(),
                daemon_mono_ns: now_mono_ns(),
            })?;
            self.sessions.insert(gap.stream_session_id.clone(), session);
        }
        Ok(())
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

    /// Direct turn close triggered by a complete semantic (smart-turn) decision.
    /// This bypasses the VAD-segment-end path so that turns close even when VAD
    /// stays in speech (noisy environments). Mirrors the drain + wait + close
    /// logic in VadSegmentEnd.
    fn close_turn_from_semantic(
        &mut self,
        stream_id: &str,
        stream_session_id: &str,
        adapter_id: &str,
        detector: &'static str,
        end_sample: u64,
        decision_sample: u64,
        confidence: Option<f32>,
        writer: &mut DetectorWriter<'_>,
    ) -> Result<Vec<DetectorAction>> {
        let model_progress = self.config.model_progress.clone();
        let model_drain = self.config.model_drain.clone();
        let model_alignment_timeout_ms = self.config.model_alignment_timeout_ms;
        let session = self.session_mut(stream_id, stream_session_id, adapter_id);
        if session.open_turn.is_none() {
            return Ok(Vec::new());
        }
        let turn_id = session
            .open_turn
            .as_ref()
            .map(|turn| turn.turn_id.clone())
            .unwrap_or_else(|| session.next_turn_id());
        // Smart Turn completion is an endpoint decision. Do not require a token
        // whose timestamp reaches decision_sample: that sample includes VAD hangover
        // silence and may be impossible for transcript tokens to reach.
        let alignment = align_model_for_close(
            model_progress.as_ref(),
            model_drain.as_ref(),
            stream_id,
            stream_session_id,
            adapter_id,
            end_sample,
            decision_sample,
            "smart_turn",
            "smart_turn_complete_direct",
            model_alignment_timeout_ms,
        );
        writer.write(&alignment.event(stream_id, stream_session_id, adapter_id))?;
        let effective_end_sample = alignment.effective_end_sample;
        let effective_decision_sample = alignment.effective_decision_sample;
        session.close_turn(
            turn_id,
            "smart_turn",
            false,
            detector,
            confidence,
            effective_end_sample,
            effective_decision_sample,
            "smart_turn_complete_direct",
            writer,
        )?;
        Ok(vec![DetectorAction::ResetEouState {
            stream_id: stream_id.to_owned(),
            stream_session_id: stream_session_id.to_owned(),
            adapter_id: adapter_id.to_owned(),
            mode: EouResetMode::Decoder,
            anchor_sample: effective_decision_sample,
            source: "smart_turn",
            reason: "smart_turn_complete_direct",
            decision_sample: effective_decision_sample,
        }])
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
                    generation: 0,
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
    timed_out: bool,
    probability: Option<f32>,
}

#[derive(Clone, Copy)]
struct CloseModelAlignment {
    effective_end_sample: u64,
    effective_decision_sample: u64,
    audio_target_sample: u64,
    audio_committed_sample: Option<u64>,
    last_token_end_sample: Option<u64>,
    audio_caught_up: bool,
    token_quiescent: bool,
    token_quiescence_elapsed_ms: u64,
    drain_attempted: bool,
    drain_succeeded: bool,
    drain_session_found: Option<bool>,
    drain_chunk_processed: Option<bool>,
    drain_until_sample: Option<u64>,
    elapsed_ms: u64,
    budget_ms: u32,
}

impl CloseModelAlignment {
    fn event(
        &self,
        stream_id: &str,
        stream_session_id: &str,
        adapter_id: &str,
    ) -> TurnCloseAlignmentEvent {
        TurnCloseAlignmentEvent {
            event: "turn_close_alignment",
            stream_id: stream_id.to_owned(),
            stream_session_id: stream_session_id.to_owned(),
            adapter_id: adapter_id.to_owned(),
            audio_target_sample: self.audio_target_sample,
            audio_committed_sample: self.audio_committed_sample,
            last_token_end_sample: self.last_token_end_sample,
            effective_end_sample: self.effective_end_sample,
            effective_decision_sample: self.effective_decision_sample,
            audio_caught_up: self.audio_caught_up,
            token_quiescent: self.token_quiescent,
            token_quiescence_elapsed_ms: self.token_quiescence_elapsed_ms,
            drain_attempted: self.drain_attempted,
            drain_succeeded: self.drain_succeeded,
            drain_session_found: self.drain_session_found,
            drain_chunk_processed: self.drain_chunk_processed,
            drain_until_sample: self.drain_until_sample,
            elapsed_ms: self.elapsed_ms,
            budget_ms: self.budget_ms,
            daemon_mono_ns: now_mono_ns(),
        }
    }
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
    transcript_silence_close_ms: u32,
    daemon_mono_ns: u64,
}

#[derive(Debug, Serialize)]
struct TurnAudioGapResetEvent {
    event: &'static str,
    stream_id: String,
    stream_session_id: String,
    adapter_id: String,
    expected_sample_start: u64,
    observed_sample_start: u64,
    delta_samples: i64,
    reason: String,
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
struct TurnCloseAlignmentEvent {
    event: &'static str,
    stream_id: String,
    stream_session_id: String,
    adapter_id: String,
    audio_target_sample: u64,
    audio_committed_sample: Option<u64>,
    last_token_end_sample: Option<u64>,
    effective_end_sample: u64,
    effective_decision_sample: u64,
    audio_caught_up: bool,
    token_quiescent: bool,
    token_quiescence_elapsed_ms: u64,
    drain_attempted: bool,
    drain_succeeded: bool,
    drain_session_found: Option<bool>,
    drain_chunk_processed: Option<bool>,
    drain_until_sample: Option<u64>,
    elapsed_ms: u64,
    budget_ms: u32,
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

/// Fires every frame during VAD speech with hold-timer progress for the TUI.
#[derive(Debug, Serialize)]
struct TurnHoldProgressEvent {
    event: &'static str,
    stream_id: String,
    stream_session_id: String,
    adapter_id: String,
    turn_id: String,
    /// Wall-clock ms since the last committed transcript token.
    hold_progress_ms: u64,
    /// Threshold at which human-hold fires (typically 7000ms).
    hold_target_ms: u64,
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

fn is_speech_evidence_text(text: &str) -> bool {
    text.chars().any(|ch| ch.is_alphanumeric())
}

fn ms_to_samples(ms: u32) -> u64 {
    u64::from(ms).saturating_mul(16_000) / 1_000
}

const CLOSE_TOKEN_QUIESCENCE_MS: u64 = 25;

fn model_alignment_deadline(timeout_ms: u32) -> Instant {
    Instant::now() + Duration::from_millis(timeout_ms as u64)
}

fn remaining_timeout_ms(deadline: Instant) -> u32 {
    deadline
        .saturating_duration_since(Instant::now())
        .as_millis()
        .try_into()
        .unwrap_or(u32::MAX)
}

fn wait_for_model_progress(
    model_progress: &ModelProgressMap,
    stream_session_id: &str,
    target_sample: u64,
    timeout_ms: u32,
) {
    let deadline = model_alignment_deadline(timeout_ms);
    let _ =
        wait_for_model_progress_until(model_progress, stream_session_id, target_sample, deadline);
}

fn wait_for_model_progress_until(
    model_progress: &ModelProgressMap,
    stream_session_id: &str,
    target_sample: u64,
    deadline: Instant,
) -> bool {
    loop {
        let committed = model_progress.get(stream_session_id).unwrap_or(0);
        if committed >= target_sample {
            return true;
        }
        if Instant::now() >= deadline {
            return false;
        }
        std::thread::sleep(Duration::from_millis(1));
    }
}

struct TokenQuiescence {
    last_token_end_sample: Option<u64>,
    quiescent: bool,
    elapsed_ms: u64,
}

fn wait_for_token_quiescence_until(
    model_progress: &ModelProgressMap,
    stream_session_id: &str,
    deadline: Instant,
    stable_for: Duration,
) -> TokenQuiescence {
    let started = Instant::now();
    let mut last_seen = model_progress.last_token_end_sample(stream_session_id);
    let mut stable_since = started;
    loop {
        let now = Instant::now();
        if now >= deadline {
            return TokenQuiescence {
                last_token_end_sample: last_seen,
                quiescent: false,
                elapsed_ms: started.elapsed().as_millis().try_into().unwrap_or(u64::MAX),
            };
        }
        let current = model_progress.last_token_end_sample(stream_session_id);
        if current != last_seen {
            last_seen = current;
            stable_since = now;
        }
        if now.duration_since(stable_since) >= stable_for {
            return TokenQuiescence {
                last_token_end_sample: last_seen,
                quiescent: true,
                elapsed_ms: started.elapsed().as_millis().try_into().unwrap_or(u64::MAX),
            };
        }
        let remaining_deadline = deadline.saturating_duration_since(now);
        let remaining_stable = stable_for.saturating_sub(now.duration_since(stable_since));
        std::thread::sleep(
            remaining_deadline
                .min(remaining_stable)
                .min(Duration::from_millis(5)),
        );
    }
}

#[allow(clippy::too_many_arguments)]
fn align_model_for_close(
    model_progress: Option<&ModelProgressMap>,
    model_drain: Option<&ModelDrainHandle>,
    stream_id: &str,
    stream_session_id: &str,
    adapter_id: &str,
    end_sample: u64,
    decision_sample: u64,
    close_source: &'static str,
    close_reason: &'static str,
    timeout_ms: u32,
) -> CloseModelAlignment {
    let started = Instant::now();
    let deadline = model_alignment_deadline(timeout_ms);
    let audio_target_sample = decision_sample;
    let mut drain_attempted = false;
    let mut drain_succeeded = false;
    let mut drain_session_found = None;
    let mut drain_chunk_processed = None;
    let mut drain_until_sample = None;

    if let Some(model_drain) = model_drain {
        drain_attempted = true;
        if let Ok(result) = model_drain.drain_session(ModelDrainRequest {
            stream_id: stream_id.to_owned(),
            stream_session_id: stream_session_id.to_owned(),
            adapter_id: adapter_id.to_owned(),
            target_sample: audio_target_sample,
            reason: close_reason,
            timeout_ms: remaining_timeout_ms(deadline),
        }) {
            drain_succeeded = true;
            drain_session_found = Some(result.session_found);
            drain_chunk_processed = Some(result.chunk_processed);
            drain_until_sample = Some(result.drained_until_sample);
        }
    }

    let mut audio_caught_up = true;
    let mut token_quiescent = true;
    let mut token_quiescence_elapsed_ms = 0;
    let mut audio_committed_sample = None;
    let mut last_token_end_sample = None;
    let mut effective_end_sample = end_sample;
    let mut effective_decision_sample = decision_sample;

    if let Some(model_progress) = model_progress {
        audio_caught_up = wait_for_model_progress_until(
            model_progress,
            stream_session_id,
            audio_target_sample,
            deadline,
        );
        audio_committed_sample = model_progress.get(stream_session_id);
        let token_quiescence = wait_for_token_quiescence_until(
            model_progress,
            stream_session_id,
            deadline,
            Duration::from_millis(CLOSE_TOKEN_QUIESCENCE_MS),
        );
        last_token_end_sample = token_quiescence.last_token_end_sample;
        token_quiescent = token_quiescence.quiescent;
        token_quiescence_elapsed_ms = token_quiescence.elapsed_ms;
        apply_trailing_token_extension(
            model_progress,
            stream_session_id,
            decision_sample,
            &mut effective_end_sample,
            &mut effective_decision_sample,
        );
    }

    let _ = close_source;
    CloseModelAlignment {
        effective_end_sample,
        effective_decision_sample,
        audio_target_sample,
        audio_committed_sample,
        last_token_end_sample,
        audio_caught_up,
        token_quiescent,
        token_quiescence_elapsed_ms,
        drain_attempted,
        drain_succeeded,
        drain_session_found,
        drain_chunk_processed,
        drain_until_sample,
        elapsed_ms: started.elapsed().as_millis().try_into().unwrap_or(u64::MAX),
        budget_ms: timeout_ms,
    }
}

fn apply_trailing_token_extension(
    model_progress: &ModelProgressMap,
    stream_session_id: &str,
    decision_sample: u64,
    effective_end_sample: &mut u64,
    effective_decision_sample: &mut u64,
) {
    if let Some(token_end) = model_progress.last_token_end_sample(stream_session_id) {
        let max_tail_sample = decision_sample.saturating_add(ms_to_samples(320));
        if token_end > *effective_end_sample && token_end <= max_tail_sample {
            *effective_end_sample = token_end;
            *effective_decision_sample = (*effective_decision_sample).max(token_end);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::Value;
    use tempfile::{tempdir, TempDir};
    use tokio::runtime::Runtime;
    use tokio::sync::broadcast;

    const STREAM_ID: &str = "test.stream";
    const SESSION_ID: &str = "test.session";
    const ADAPTER_ID: &str = "test.adapter";
    const VAD: &str = "silero_vad";
    const SMART_TURN: &str = "pipecat_smart_turn_v3";
    const MODEL_EOU: &str = "parakeet_realtime_eou_120m_v1";

    struct TurnHarness {
        manager: TurnManager,
        logger: crate::JsonlLogger,
        runtime: Runtime,
        events: broadcast::Receiver<String>,
        _dir: TempDir,
    }

    impl TurnHarness {
        fn new(config: TurnManagerConfig) -> Self {
            let runtime = Runtime::new().expect("test runtime should start");
            let dir = tempdir().expect("temp log dir should be created");
            let (event_tx, events) = broadcast::channel(256);
            let logger = runtime
                .block_on(crate::JsonlLogger::open(dir.path().to_path_buf(), event_tx))
                .expect("test logger should open");
            let mut harness = Self {
                manager: TurnManager::new(config),
                logger,
                runtime,
                events,
                _dir: dir,
            };
            let hello = test_hello();
            {
                let mut writer = DetectorWriter::new(&harness.logger, harness.runtime.handle());
                harness
                    .manager
                    .start_session(&hello, &mut writer)
                    .expect("turn session should start");
            }
            harness.drain_events();
            harness
        }

        fn send(&mut self, signal: DetectorSignal) -> Vec<DetectorAction> {
            let mut writer = DetectorWriter::new(&self.logger, self.runtime.handle());
            self.manager
                .handle_signal(signal, &mut writer)
                .expect("turn manager should handle test signal")
        }

        fn drain_events(&mut self) -> Vec<Value> {
            let mut events = Vec::new();
            loop {
                match self.events.try_recv() {
                    Ok(line) => {
                        events.push(serde_json::from_str(&line).unwrap_or_else(|err| {
                            panic!("event should be valid json: {err}; {line}")
                        }))
                    }
                    Err(broadcast::error::TryRecvError::Empty) => break,
                    Err(broadcast::error::TryRecvError::Lagged(skipped)) => {
                        panic!("test event receiver lagged by {skipped} events")
                    }
                    Err(broadcast::error::TryRecvError::Closed) => break,
                }
            }
            events
        }
    }

    fn test_hello() -> HelloState {
        HelloState {
            adapter_id: ADAPTER_ID.to_owned(),
            stream_id: STREAM_ID.to_owned(),
            stream_session_id: SESSION_ID.to_owned(),
            source_kind: speech_core_protocol::SourceKind::Synthetic,
            sample_rate_hz: 16_000,
            channels: 1,
            format: speech_core_protocol::PcmFormat::PcmF32Le,
            timestamp_provenance: speech_core_protocol::TimestampProvenance::uncalibrated(
                "test-clock",
                speech_core_protocol::ClockDomain::HostMonotonic,
                speech_core_protocol::TimestampQuality::SyntheticScheduled,
            ),
            generation: 0,
        }
    }

    fn vad_start(start_sample: u64, decision_sample: u64) -> DetectorSignal {
        DetectorSignal::VadSegmentStart {
            detector: VAD,
            stream_id: STREAM_ID.to_owned(),
            stream_session_id: SESSION_ID.to_owned(),
            adapter_id: ADAPTER_ID.to_owned(),
            start_sample,
            decision_sample,
            confidence: Some(0.9),
        }
    }

    fn vad_end(start_sample: u64, end_sample: u64, decision_sample: u64) -> DetectorSignal {
        DetectorSignal::VadSegmentEnd {
            detector: VAD,
            stream_id: STREAM_ID.to_owned(),
            stream_session_id: SESSION_ID.to_owned(),
            adapter_id: ADAPTER_ID.to_owned(),
            start_sample,
            end_sample,
            decision_sample,
            confidence: Some(0.1),
        }
    }

    fn semantic_decision(
        end_sample: u64,
        decision_sample: u64,
        complete: bool,
        available: bool,
        timed_out: bool,
    ) -> DetectorSignal {
        DetectorSignal::SemanticTurnDecision {
            detector: SMART_TURN,
            stream_id: STREAM_ID.to_owned(),
            stream_session_id: SESSION_ID.to_owned(),
            adapter_id: ADAPTER_ID.to_owned(),
            end_sample,
            decision_sample,
            complete,
            probability: Some(if complete { 0.88 } else { 0.2 }),
            threshold: Some(0.5),
            timed_out,
            available,
            reason: if !available {
                "smart_turn_unavailable"
            } else if timed_out {
                "smart_turn_timeout"
            } else if complete {
                "smart_turn_complete"
            } else {
                "smart_turn_incomplete"
            },
            duration_ms: Some(if timed_out { 100.0 } else { 10.0 }),
        }
    }

    fn model_eou(end_sample: u64, decision_sample: u64) -> DetectorSignal {
        DetectorSignal::ModelEou {
            detector: MODEL_EOU,
            stream_id: STREAM_ID.to_owned(),
            stream_session_id: SESSION_ID.to_owned(),
            adapter_id: ADAPTER_ID.to_owned(),
            end_sample,
            decision_sample,
            text_delta: String::new(),
            confidence: Some(0.7),
        }
    }

    fn transcript_token(
        start_sample: u64,
        end_sample: u64,
        decision_sample: u64,
    ) -> DetectorSignal {
        DetectorSignal::TranscriptTokenCommitted {
            detector: "nemotron_ctc",
            stream_id: STREAM_ID.to_owned(),
            stream_session_id: SESSION_ID.to_owned(),
            adapter_id: ADAPTER_ID.to_owned(),
            token_index: 0,
            text: "hello".to_owned(),
            start_sample,
            end_sample,
            decision_sample,
            confidence: Some(0.95),
            drain_handle: ModelDrainHandle::pending_for_test(),
        }
    }

    fn low_silence(
        start_sample: u64,
        decision_sample: u64,
        silence_samples: u64,
    ) -> DetectorSignal {
        DetectorSignal::VadLowSilence {
            detector: VAD,
            stream_id: STREAM_ID.to_owned(),
            stream_session_id: SESSION_ID.to_owned(),
            adapter_id: ADAPTER_ID.to_owned(),
            start_sample,
            decision_sample,
            silence_samples,
            confidence: Some(0.01),
        }
    }

    fn assert_reset_action(
        actions: &[DetectorAction],
        expected_source: &'static str,
        expected_reason: &'static str,
        expected_anchor: u64,
    ) {
        assert!(
            actions.iter().any(|action| matches!(
                action,
                DetectorAction::ResetEouState {
                    mode: EouResetMode::Decoder,
                    source,
                    reason,
                    anchor_sample,
                    ..
                } if *source == expected_source
                    && *reason == expected_reason
                    && *anchor_sample == expected_anchor
            )),
            "expected a decoder reset action with source={expected_source:?}, reason={expected_reason:?}, \
             anchor_sample={expected_anchor}; got actions: {actions:#?}"
        );
    }

    fn event_field<'a>(event: &'a Value, field: &str) -> &'a str {
        event
            .get(field)
            .and_then(Value::as_str)
            .unwrap_or_else(|| panic!("event should contain string field {field}: {event}"))
    }

    fn find_event<'a>(events: &'a [Value], event_name: &str) -> Option<&'a Value> {
        events
            .iter()
            .find(|event| event.get("event").and_then(Value::as_str) == Some(event_name))
    }

    fn assert_no_event(events: &[Value], event_name: &str) {
        assert!(
            find_event(events, event_name).is_none(),
            "did not expect {event_name} event; got events: {events:#?}"
        );
    }

    fn event_count(events: &[Value], event_name: &str) -> usize {
        events
            .iter()
            .filter(|event| event.get("event").and_then(Value::as_str) == Some(event_name))
            .count()
    }

    fn assert_turn_closed(
        events: &[Value],
        expected_source: &'static str,
        expected_degraded: bool,
        expected_reason: &'static str,
    ) {
        let closed = find_event(events, "turn_closed")
            .unwrap_or_else(|| panic!("expected turn_closed event; got events: {events:#?}"));
        assert_eq!(
            event_field(closed, "source"),
            expected_source,
            "turn_closed should use the expected close source; event: {closed}"
        );
        assert_eq!(
            closed.get("degraded").and_then(Value::as_bool),
            Some(expected_degraded),
            "turn_closed should mark degraded={expected_degraded}; event: {closed}"
        );
        assert_eq!(
            event_field(closed, "reason"),
            expected_reason,
            "turn_closed should use the expected reason; event: {closed}"
        );
    }

    fn assert_suppressed(
        events: &[Value],
        expected_source: &'static str,
        expected_reason: &'static str,
    ) {
        assert!(
            events.iter().any(|event| {
                event.get("event").and_then(Value::as_str) == Some("turn_eou_suppressed")
                    && event.get("source").and_then(Value::as_str) == Some(expected_source)
                    && event.get("reason").and_then(Value::as_str) == Some(expected_reason)
            }),
            "expected turn_eou_suppressed source={expected_source:?} reason={expected_reason:?}; \
             got events: {events:#?}"
        );
    }

    #[test]
    fn sample_time_is_16khz() {
        assert_eq!(samples_to_ms(16_000), 1_000);
        assert_eq!(samples_to_ms(480), 30);
    }

    #[test]
    fn smart_turn_timeout_fails_open_but_non_timeout_incomplete_suppresses() {
        let mut timed_out = TurnHarness::new(TurnManagerConfig {
            vad_close_enabled: true,
            semantic_gate_enabled: true,
            semantic_gate_close_enabled: true,
            ..Default::default()
        });
        timed_out.send(vad_start(0, 3_200));
        timed_out.send(semantic_decision(16_000, 17_920, false, true, true));
        timed_out.drain_events();

        let actions = timed_out.send(vad_end(0, 16_000, 17_920));
        let events = timed_out.drain_events();

        assert_reset_action(&actions, "vad", "smart_turn_timeout_vad_fallback", 17_920);
        assert_turn_closed(&events, "vad", true, "smart_turn_timeout_vad_fallback");
        assert!(
            !events.iter().any(|event| {
                event.get("event").and_then(Value::as_str) == Some("turn_eou_suppressed")
                    && event.get("reason").and_then(Value::as_str)
                        == Some("semantic_incomplete")
            }),
            "a timed-out Smart Turn incomplete decision must fail open to VAD, not suppress closure; \
             got events: {events:#?}"
        );

        let mut incomplete = TurnHarness::new(TurnManagerConfig {
            vad_close_enabled: true,
            semantic_gate_enabled: true,
            semantic_gate_close_enabled: true,
            ..Default::default()
        });
        incomplete.send(vad_start(0, 3_200));
        incomplete.send(semantic_decision(16_000, 17_920, false, true, false));
        incomplete.drain_events();

        let actions = incomplete.send(vad_end(0, 16_000, 17_920));
        let events = incomplete.drain_events();

        assert!(
            actions.is_empty(),
            "non-timed-out Smart Turn incomplete decisions should suppress VAD close and emit no reset; got actions: {actions:#?}"
        );
        assert_suppressed(&events, "semantic", "semantic_incomplete");
        assert_no_event(&events, "turn_closed");
    }

    #[test]
    fn smart_turn_complete_closes_non_degraded_turn() {
        let progress = ModelProgressMap::new();
        progress.start_session_for_test("test.session");
        progress.record_token(SESSION_ID, 3_200);
        let mut harness = TurnHarness::new(TurnManagerConfig {
            vad_close_enabled: true,
            semantic_gate_enabled: true,
            semantic_gate_close_enabled: true,
            model_progress: Some(progress),
            ..Default::default()
        });
        harness.send(vad_start(0, 3_200));
        harness.drain_events();

        // SemanticTurnDecision with complete=true now closes directly.
        let actions = harness.send(semantic_decision(16_000, 17_920, true, true, false));
        let events = harness.drain_events();

        assert_reset_action(&actions, "smart_turn", "smart_turn_complete_direct", 17_920);
        assert_turn_closed(&events, "smart_turn", false, "smart_turn_complete_direct");
    }

    #[test]
    fn smart_turn_unavailable_fails_open_to_vad_fallback() {
        let mut harness = TurnHarness::new(TurnManagerConfig {
            vad_close_enabled: true,
            semantic_gate_enabled: true,
            semantic_gate_close_enabled: true,
            ..Default::default()
        });
        harness.send(vad_start(0, 3_200));
        harness.send(semantic_decision(16_000, 17_920, false, false, false));
        harness.drain_events();

        let actions = harness.send(vad_end(0, 16_000, 17_920));
        let events = harness.drain_events();

        assert_reset_action(
            &actions,
            "vad",
            "smart_turn_unavailable_vad_fallback",
            17_920,
        );
        assert_turn_closed(&events, "vad", true, "smart_turn_unavailable_vad_fallback");
        assert_no_event(&events, "turn_eou_suppressed");
    }

    #[test]
    fn close_wait_does_not_require_token_to_reach_silence_decision_sample() {
        let progress = ModelProgressMap::new();
        progress.update(SESSION_ID, 17_920);
        progress.record_token(SESSION_ID, 15_000);
        let mut harness = TurnHarness::new(TurnManagerConfig {
            vad_close_enabled: true,
            model_progress: Some(progress),
            model_alignment_timeout_ms: 3_000,
            ..Default::default()
        });
        harness.send(vad_start(0, 3_200));
        harness.drain_events();

        let started = Instant::now();
        let actions = harness.send(vad_end(0, 16_000, 17_920));
        let elapsed = started.elapsed();
        let events = harness.drain_events();

        assert!(
            elapsed < Duration::from_millis(100),
            "close should not wait for last_token_end_sample to reach the VAD silence decision sample; elapsed={elapsed:?}"
        );
        assert_reset_action(&actions, "vad", "vad_speech_end", 17_920);
        assert_turn_closed(&events, "vad", true, "vad_speech_end");
        let alignment = find_event(&events, "turn_close_alignment")
            .expect("close should emit model alignment instrumentation");
        assert_eq!(
            alignment
                .get("last_token_end_sample")
                .and_then(Value::as_u64),
            Some(15_000)
        );
        assert_eq!(
            alignment.get("audio_caught_up").and_then(Value::as_bool),
            Some(true)
        );
    }

    #[test]
    fn vad_close_drains_trailing_model_audio_before_turn_closed() {
        let progress = ModelProgressMap::new();
        progress.start_session_for_test("test.session");
        progress.update(SESSION_ID, 16_000);
        let drain_progress = progress.clone();
        let drain_called = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
        let drain_called_for_callback = std::sync::Arc::clone(&drain_called);
        let drain_handle = ModelDrainHandle::callback_for_test(move |request| {
            assert_eq!(request.stream_id, STREAM_ID);
            assert_eq!(request.stream_session_id, SESSION_ID);
            assert_eq!(request.adapter_id, ADAPTER_ID);
            assert_eq!(request.target_sample, 17_920);
            assert_eq!(request.reason, "vad_speech_end");
            drain_called_for_callback.store(true, std::sync::atomic::Ordering::SeqCst);
            drain_progress.record_token(SESSION_ID, 18_240);
            drain_progress.update(SESSION_ID, 17_920);
            Ok(crate::model::ModelDrainResult {
                session_found: true,
                chunk_processed: true,
                drained_until_sample: 17_920,
            })
        });
        let mut harness = TurnHarness::new(TurnManagerConfig {
            vad_close_enabled: true,
            model_progress: Some(progress),
            model_drain: Some(drain_handle),
            model_alignment_timeout_ms: 100,
            ..Default::default()
        });
        harness.send(vad_start(0, 3_200));
        harness.drain_events();

        let actions = harness.send(vad_end(0, 16_000, 17_920));
        let events = harness.drain_events();

        assert!(
            drain_called.load(std::sync::atomic::Ordering::SeqCst),
            "VAD close should drain buffered model audio before emitting turn_closed"
        );
        assert_reset_action(&actions, "vad", "vad_speech_end", 18_240);
        assert_turn_closed(&events, "vad", true, "vad_speech_end");
        let closed = find_event(&events, "turn_closed").unwrap();
        assert_eq!(
            closed.get("end_sample").and_then(Value::as_u64),
            Some(18_240)
        );
        assert_eq!(
            closed.get("decision_sample").and_then(Value::as_u64),
            Some(18_240)
        );
    }

    #[test]
    fn vad_close_with_model_caught_up_closes_immediately() {
        let progress = ModelProgressMap::new();
        progress.start_session_for_test("test.session");
        progress.update(SESSION_ID, 17_920);
        let mut harness = TurnHarness::new(TurnManagerConfig {
            vad_close_enabled: true,
            model_progress: Some(progress),
            model_alignment_timeout_ms: 100,
            ..Default::default()
        });
        harness.send(vad_start(0, 3_200));
        harness.drain_events();

        let started = Instant::now();
        let actions = harness.send(vad_end(0, 16_000, 17_920));
        let elapsed = started.elapsed();
        let events = harness.drain_events();

        assert!(
            elapsed < Duration::from_millis(50),
            "when model_progress has already reached decision_sample, VAD close should not wait; elapsed={elapsed:?}"
        );
        assert_reset_action(&actions, "vad", "vad_speech_end", 17_920);
        assert_turn_closed(&events, "vad", true, "vad_speech_end");
    }

    #[test]
    fn min_vad_speech_ms_filters_short_utterances() {
        let mut harness = TurnHarness::new(TurnManagerConfig {
            vad_close_enabled: true,
            min_vad_speech_ms: 300,
            ..Default::default()
        });
        harness.send(vad_start(0, 320));
        harness.drain_events();

        let actions = harness.send(vad_end(0, 1_600, 1_920));
        let events = harness.drain_events();

        assert!(
            actions.is_empty(),
            "a 100ms VAD segment is shorter than min_vad_speech_ms=300 and should not close; got actions: {actions:#?}"
        );
        assert_suppressed(&events, "vad", "vad_too_short");
        assert_no_event(&events, "turn_closed");
    }

    #[test]
    fn human_hold_closes_turn_when_semantic_gate_suppresses_and_hold_exceeded() {
        let mut harness = TurnHarness::new(TurnManagerConfig {
            vad_close_enabled: true,
            semantic_gate_enabled: true,
            semantic_gate_close_enabled: true,
            human_hold_silence_ms: 1_000,
            ..Default::default()
        });
        harness.send(vad_start(0, 3_200));
        harness.send(semantic_decision(16_000, 16_000, false, true, false));
        harness.drain_events();

        let actions = harness.send(vad_end(0, 16_000, 16_000));
        let events = harness.drain_events();

        assert_reset_action(
            &actions,
            "human_hold",
            "human_hold_speech_like_audio_without_tokens",
            16_000,
        );
        assert_suppressed(&events, "semantic", "semantic_incomplete");
        assert!(
            events.iter().any(|event| {
                event.get("event").and_then(Value::as_str) == Some("turn_human_hold")
                    && event.get("reason").and_then(Value::as_str)
                        == Some("speech_like_audio_without_tokens")
                    && event
                        .get("samples_without_tokens")
                        .and_then(Value::as_u64)
                        .is_some_and(|samples| samples >= 16_000)
            }),
            "after >= human_hold_silence_ms of speech-like audio without tokens, TurnHumanHoldEvent should be emitted; got events: {events:#?}"
        );
        assert_turn_closed(
            &events,
            "human_hold",
            true,
            "human_hold_speech_like_audio_without_tokens",
        );
    }

    #[test]
    fn old_segment_end_after_direct_semantic_close_does_not_duplicate_close() {
        let progress = ModelProgressMap::new();
        progress.update(SESSION_ID, 17_920);
        let mut harness = TurnHarness::new(TurnManagerConfig {
            vad_close_enabled: true,
            semantic_gate_enabled: true,
            semantic_gate_close_enabled: true,
            model_progress: Some(progress),
            ..Default::default()
        });
        harness.send(vad_start(0, 3_200));
        let actions = harness.send(semantic_decision(16_000, 17_920, true, true, false));
        let events = harness.drain_events();
        assert_reset_action(&actions, "smart_turn", "smart_turn_complete_direct", 17_920);
        assert_eq!(event_count(&events, "turn_closed"), 1);

        let stale_actions = harness.send(vad_end(0, 16_000, 17_920));
        let stale_events = harness.drain_events();
        assert!(
            stale_actions.is_empty(),
            "old VAD segment end should not emit another reset: {stale_actions:#?}"
        );
        assert_no_event(&stale_events, "turn_closed");
    }

    #[test]
    fn model_eou_closes_turn_with_model_eou_source() {
        let mut harness = TurnHarness::new(TurnManagerConfig {
            model_eou_close_enabled: true,
            min_model_eou_speech_ms: 300,
            ..Default::default()
        });

        let actions = harness.send(model_eou(16_000, 16_000));
        let events = harness.drain_events();

        assert_reset_action(&actions, "model_eou", "eou_token_detected", 16_000);
        assert_turn_closed(&events, "model_eou", false, "eou_token_detected");
    }

    #[test]
    fn model_eou_refractory_suppresses_second_eou() {
        let mut harness = TurnHarness::new(TurnManagerConfig {
            model_eou_close_enabled: true,
            min_model_eou_speech_ms: 300,
            model_eou_refractory_ms: 700,
            ..Default::default()
        });
        let first_actions = harness.send(model_eou(10_000, 16_000));
        let first_events = harness.drain_events();
        assert_reset_action(&first_actions, "model_eou", "eou_token_detected", 16_000);
        assert_turn_closed(&first_events, "model_eou", false, "eou_token_detected");

        let second_actions = harness.send(model_eou(18_000, 20_000));
        let second_events = harness.drain_events();

        assert!(
            second_actions.is_empty(),
            "a second ModelEou inside model_eou_refractory_ms should be suppressed and emit no reset; got actions: {second_actions:#?}"
        );
        assert_suppressed(&second_events, "model_eou", "refractory");
        assert_no_event(&second_events, "turn_closed");
    }

    #[test]
    fn transcript_silence_closes_transcript_backed_turn() {
        let progress = ModelProgressMap::new();
        progress.start_session_for_test("test.session");
        progress.update(SESSION_ID, 3_200);
        progress.record_token(SESSION_ID, 3_200);
        let mut harness = TurnHarness::new(TurnManagerConfig {
            model_progress: Some(progress),
            transcript_silence_close_ms: 700,
            ..Default::default()
        });
        harness.send(transcript_token(0, 3_200, 3_200));
        harness.drain_events();

        let actions = harness.send(low_silence(3_200, 15_200, 12_000));
        let events = harness.drain_events();

        assert_reset_action(
            &actions,
            "transcript_silence",
            "transcript_backed_turn_low_vad_silence",
            15_200,
        );
        assert_turn_closed(
            &events,
            "transcript_silence",
            true,
            "transcript_backed_turn_low_vad_silence",
        );
    }
}
