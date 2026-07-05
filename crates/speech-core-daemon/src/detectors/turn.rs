use anyhow::Result;
use serde::Serialize;
use speech_core_protocol::now_mono_ns;
use std::collections::HashMap;

use super::{DetectorSignal, DetectorWriter};
use crate::HelloState;

#[derive(Debug, Clone)]
pub struct TurnManagerConfig {
    /// Promote Parakeet EOU tokens into authoritative non-degraded turn closure.
    pub model_eou_close_enabled: bool,
    /// Promote VAD speech-end into degraded turn closure. Useful as fallback or comparison mode.
    pub vad_close_enabled: bool,
    /// Ignore model EOU tokens before at least this much speech has been observed.
    pub min_model_eou_speech_ms: u32,
    /// Ignore repeated EOU tokens this close to the previous close.
    pub model_eou_refractory_ms: u32,
}

impl Default for TurnManagerConfig {
    fn default() -> Self {
        Self {
            model_eou_close_enabled: true,
            vad_close_enabled: false,
            min_model_eou_speech_ms: 300,
            model_eou_refractory_ms: 700,
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
            min_model_eou_speech_ms: self.config.min_model_eou_speech_ms,
            model_eou_refractory_ms: self.config.model_eou_refractory_ms,
            daemon_mono_ns: now_mono_ns(),
        })
    }

    pub fn handle_signal(
        &mut self,
        signal: DetectorSignal,
        writer: &mut DetectorWriter<'_>,
    ) -> Result<()> {
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
                if session.open_turn.is_none() {
                    session.start_turn(start_sample, "vad", writer)?;
                }
                session.saw_vad_signal = true;
                session.last_vad_start_sample = Some(start_sample);
                session.in_speech = true;
                writer.write(&TurnSignalObservedEvent {
                    event: "turn_signal_observed",
                    stream_id,
                    stream_session_id,
                    adapter_id,
                    detector,
                    signal: "vad_speech_start",
                    sample: start_sample,
                    decision_sample,
                    confidence,
                    daemon_mono_ns: now_mono_ns(),
                })
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
                let session = self.session_mut(&stream_id, &stream_session_id, &adapter_id);
                if session.open_turn.is_none() {
                    session.start_turn(start_sample, "vad", writer)?;
                }
                session.saw_vad_signal = true;
                session.in_speech = false;
                session.last_vad_end_sample = Some(end_sample);
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
                    daemon_mono_ns: now_mono_ns(),
                })?;
                if vad_close_enabled {
                    session.close_turn(
                        turn_id,
                        "vad",
                        true,
                        detector,
                        confidence,
                        end_sample,
                        decision_sample,
                        "vad_speech_end",
                        writer,
                    )?;
                }
                Ok(())
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
                    return Ok(());
                }
                if session.open_turn.is_none() {
                    session.start_turn(provisional_start, "model", writer)?;
                }
                let effective_end_sample = session
                    .last_vad_end_sample
                    .filter(|sample| *sample <= decision_sample)
                    .unwrap_or(end_sample);
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
                    daemon_mono_ns: now_mono_ns(),
                })?;
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
                }
                Ok(())
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
    last_closed_end_sample: Option<u64>,
    last_closed_decision_sample: Option<u64>,
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
            last_closed_end_sample: None,
            last_closed_decision_sample: None,
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

#[derive(Debug, Serialize)]
struct TurnSessionStartEvent {
    event: &'static str,
    stream_id: String,
    stream_session_id: String,
    adapter_id: String,
    model_eou_close_enabled: bool,
    vad_close_enabled: bool,
    min_model_eou_speech_ms: u32,
    model_eou_refractory_ms: u32,
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
