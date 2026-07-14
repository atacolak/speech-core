use anyhow::{bail, Context, Result};
use serde::Serialize;
use speech_core_protocol::{now_mono_ns, AudioFrame, PcmFormat};
use std::collections::HashMap;
use std::ffi::{CStr, CString};
use std::os::raw::{c_char, c_float, c_int};
use std::path::PathBuf;
use std::ptr;
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;
use tokio::sync::{mpsc, oneshot};
use tracing::{info, warn};

use crate::detectors::{DetectorIngress, TranscriptTokenSignal};
use crate::{AudioGapReset, HelloState, JsonlLogger};

/// A lightweight per-token snapshot stored in shared model state so the turn manager
/// can construct per-turn committed text from token sample boundaries.
#[derive(Debug, Clone)]
pub struct CommittedTokenSnapshot {
    pub index: u32,
    pub text: String,
    pub start_sample: u64,
    pub end_sample: u64,
}

/// Shared model progress tracker: stream_session_id → latest committed audio sample.
/// The TurnManager reads this to ensure the model has caught up before emitting turn_closed.
#[derive(Debug, Clone, Default)]
pub struct ModelProgressState {
    pub generation: u64,
    pub audio_committed_samples: u64,
    pub last_token_end_sample: Option<u64>,
    /// Family-reported internal audio still buffered (lookahead / right-context).
    /// Updated on every model feed/finalize update. Used by turn close as a drain hint.
    pub buffered_ms: i64,
    /// Cumulative committed tokens since session start. Written by the model worker
    /// every time a token commits. Turn manager slices this at close time.
    pub committed_tokens: Vec<CommittedTokenSnapshot>,
    /// Latest committed text from the ASR model (cumulative).
    pub last_committed_text: String,
    /// Latest committed token count (len of committed_tokens).
    pub committed_token_count: u32,
    /// Latest revision number from the ASR.
    pub revision: i32,
    /// The currently open turn_id, set by TurnManager on turn_started and cleared on
    /// turn_closed. Model worker reads this for transcript_update/token event tagging.
    pub current_turn_id: Option<String>,
    /// While no turn is open, drop snapshot tokens with end_sample <= this.
    /// Set on transcript_committed; cleared when the next turn opens.
    pub suppress_tokens_at_or_before_sample: Option<u64>,
    /// The per-turn text dispatched in the last transcript_committed event. Used by
    /// transcript_finalized (diagnostic-only) to detect late session-end revisions.
    pub last_dispatched_text: Option<String>,
    /// The turn_id of the last dispatched transcript_committed.
    pub last_dispatched_turn_id: Option<String>,
    /// Turn boundaries of the last dispatched transcript_committed, used to
    /// recompute the per-turn text at finalize time for diagnostic comparison.
    pub last_dispatched_turn_start_sample: Option<u64>,
    pub last_dispatched_turn_end_sample: Option<u64>,
    pub last_dispatched_baseline_token_count: Option<u32>,
}

#[derive(Debug, Clone, Default)]
pub struct ModelProgressMap {
    inner: Arc<Mutex<HashMap<String, ModelProgressState>>>,
}

#[derive(Debug, Clone)]
pub struct ModelDrainRequest {
    pub stream_id: String,
    pub stream_session_id: String,
    pub adapter_id: String,
    pub target_sample: u64,
    pub reason: &'static str,
    pub timeout_ms: u32,
    /// When true: feed remaining real audio through target_sample, feed
    /// `padding_ms` of synthetic silence, call stream finalize (is_final),
    /// snapshot tokens, then re-begin a fresh stream for subsequent speech.
    /// Replaces token-quiescence as the normal close commit mechanism.
    pub finalize_turn: bool,
    /// Synthetic silence tail after real audio (Nemotron right-context pad).
    pub padding_ms: u32,
}

#[derive(Debug, Clone, Copy, Default)]
pub struct ModelDrainResult {
    pub session_found: bool,
    pub chunk_processed: bool,
    pub drained_until_sample: u64,
    /// True when finalize_turn path ran stream finalize + rebegin.
    pub finalized: bool,
    /// True when finalize completed with is_final (not timeout fallback).
    pub finalize_ok: bool,
    pub finalize_timeout: bool,
    pub close_to_input_finished_ms: u64,
    pub finalize_decode_ms: u64,
    pub finalize_chunks: u32,
    pub tokens_added_during_finalize: u32,
    pub padding_ms: u32,
    pub buffered_ms_at_close: Option<i64>,
    pub tokens_before_finalize: u32,
    pub tokens_after_finalize: u32,
}

#[derive(Clone)]
pub struct ModelDrainHandle {
    inner: ModelDrainHandleInner,
}

impl std::fmt::Debug for ModelDrainHandle {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("ModelDrainHandle").finish_non_exhaustive()
    }
}

#[derive(Clone)]
enum ModelDrainHandleInner {
    Worker(mpsc::UnboundedSender<ModelCommand>),
    #[cfg(test)]
    Pending,
    #[cfg(test)]
    Callback(Arc<dyn Fn(ModelDrainRequest) -> Result<ModelDrainResult> + Send + Sync>),
}

impl ModelDrainHandle {
    fn new(sender: mpsc::UnboundedSender<ModelCommand>) -> Self {
        Self {
            inner: ModelDrainHandleInner::Worker(sender),
        }
    }

    pub fn drain_session(&self, request: ModelDrainRequest) -> Result<ModelDrainResult> {
        match &self.inner {
            ModelDrainHandleInner::Worker(sender) => {
                let timeout = Duration::from_millis(request.timeout_ms as u64);
                let (reply_tx, reply_rx) = std::sync::mpsc::channel();
                sender
                    .send(ModelCommand::DrainSession {
                        request,
                        reply: reply_tx,
                    })
                    .map_err(|err| {
                        anyhow::anyhow!("model worker control path rejected drain request: {err}")
                    })?;
                reply_rx
                    .recv_timeout(timeout)
                    .map_err(|err| anyhow::anyhow!("model drain timed out or failed: {err}"))?
            }
            #[cfg(test)]
            ModelDrainHandleInner::Pending => {
                std::thread::sleep(Duration::from_millis(request.timeout_ms as u64));
                anyhow::bail!("test model drain timed out")
            }
            #[cfg(test)]
            ModelDrainHandleInner::Callback(callback) => callback(request),
        }
    }

    #[cfg(test)]
    pub(crate) fn callback_for_test(
        callback: impl Fn(ModelDrainRequest) -> Result<ModelDrainResult> + Send + Sync + 'static,
    ) -> Self {
        Self {
            inner: ModelDrainHandleInner::Callback(Arc::new(callback)),
        }
    }

    #[cfg(test)]
    pub(crate) fn pending_for_test() -> Self {
        Self {
            inner: ModelDrainHandleInner::Pending,
        }
    }
}

impl ModelProgressMap {
    pub fn new() -> Self {
        Self {
            inner: Arc::new(Mutex::new(HashMap::new())),
        }
    }

    pub fn start_session(&self, session_id: &str, generation: u64) {
        if let Ok(mut map) = self.inner.lock() {
            map.insert(
                session_id.to_owned(),
                ModelProgressState {
                    generation,
                    audio_committed_samples: 0,
                    last_token_end_sample: None,
                    buffered_ms: 0,
                    committed_tokens: Vec::new(),
                    last_committed_text: String::new(),
                    committed_token_count: 0,
                    revision: 0,
                    current_turn_id: None,
                    suppress_tokens_at_or_before_sample: None,
                    last_dispatched_text: None,
                    last_dispatched_turn_id: None,
                    last_dispatched_turn_start_sample: None,
                    last_dispatched_turn_end_sample: None,
                    last_dispatched_baseline_token_count: None,
                },
            );
        }
    }

    pub fn update(&self, session_id: &str, committed_samples: u64) {
        if let Ok(mut map) = self.inner.lock() {
            if let Some(state) = map.get_mut(session_id) {
                state.audio_committed_samples = committed_samples;
            }
        }
    }

    pub fn update_buffered_ms(&self, session_id: &str, buffered_ms: i64) {
        if let Ok(mut map) = self.inner.lock() {
            if let Some(state) = map.get_mut(session_id) {
                state.buffered_ms = buffered_ms.max(0);
            }
        }
    }

    pub fn record_token(&self, session_id: &str, token_end_sample: u64) {
        if let Ok(mut map) = self.inner.lock() {
            if let Some(state) = map.get_mut(session_id) {
                state.last_token_end_sample = Some(token_end_sample);
            }
        }
    }

    pub fn get(&self, session_id: &str) -> Option<u64> {
        self.inner.lock().ok().and_then(|map| {
            map.get(session_id)
                .map(|state| state.audio_committed_samples)
        })
    }

    pub fn last_token_end_sample(&self, session_id: &str) -> Option<u64> {
        self.inner.lock().ok().and_then(|map| {
            map.get(session_id)
                .and_then(|state| state.last_token_end_sample)
        })
    }

    pub fn buffered_ms(&self, session_id: &str) -> Option<i64> {
        self.inner.lock().ok().and_then(|map| {
            map.get(session_id).map(|state| state.buffered_ms)
        })
    }

    pub fn committed_token_count(&self, session_id: &str) -> u32 {
        self.inner
            .lock()
            .ok()
            .and_then(|map| map.get(session_id).map(|state| state.committed_token_count))
            .unwrap_or(0)
    }

    /// Set the current open turn_id for a session. Called by TurnManager on turn_started.
    pub fn set_current_turn_id(&self, session_id: &str, turn_id: Option<String>) {
        if let Ok(mut map) = self.inner.lock() {
            if let Some(state) = map.get_mut(session_id) {
                state.current_turn_id = turn_id.clone();
                // Opening a turn lifts the post-close freeze so real new speech
                // can be snapshotted again.
                if turn_id.is_some() {
                    state.suppress_tokens_at_or_before_sample = None;
                }
            }
        }
    }

    /// After transcript_committed, freeze the snapshot ring so late tokens for
    /// the closed turn (trailing ".", delayed ASR) are not stored for the next
    /// turn to inherit. Cleared when the next turn opens.
    pub fn freeze_tokens_through(&self, session_id: &str, sample: u64) {
        if let Ok(mut map) = self.inner.lock() {
            if let Some(state) = map.get_mut(session_id) {
                let prev = state.suppress_tokens_at_or_before_sample.unwrap_or(0);
                state.suppress_tokens_at_or_before_sample = Some(prev.max(sample));
            }
        }
    }

    /// Get the current open turn_id for a session, if any.
    pub fn current_turn_id(&self, session_id: &str) -> Option<String> {
        self.inner.lock().ok().and_then(|map| {
            map.get(session_id)
                .and_then(|state| state.current_turn_id.clone())
        })
    }

    /// Update the committed text and metadata from an ASR transcript update.
    pub fn update_committed_text(
        &self,
        session_id: &str,
        text: &str,
        committed_token_count: u32,
        revision: i32,
    ) {
        if let Ok(mut map) = self.inner.lock() {
            if let Some(state) = map.get_mut(session_id) {
                state.last_committed_text = text.to_owned();
                state.revision = revision;
                // Do not overwrite committed_token_count from native stream counts:
                // after finalize+rebegin the native counter resets while our
                // cumulative token snapshots (and baselines) must stay monotonic.
                // record_token_snapshot owns the authoritative count.
                let _ = committed_token_count;
            }
        }
    }

    /// Record a committed token snapshot. Called from the model worker for every
    /// committed token (including punctuation).
    pub fn record_token_snapshot(&self, session_id: &str, snapshot: CommittedTokenSnapshot) {
        if let Ok(mut map) = self.inner.lock() {
            if let Some(state) = map.get_mut(session_id) {
                let is_punct_only = !snapshot.text.chars().any(|ch| ch.is_alphanumeric());
                // After commit, current_turn_id is None. Orphan punctuation (often
                // a trailing ".") must not enter the snapshot ring or the next turn
                // inherits it as a leading period.
                if is_punct_only && state.current_turn_id.is_none() {
                    return;
                }
                // Hard freeze watermark: while no turn is open, drop tokens whose
                // audio is entirely at/before the closed turn's freeze sample.
                if state.current_turn_id.is_none() {
                    if let Some(min) = state.suppress_tokens_at_or_before_sample {
                        if snapshot.end_sample <= min {
                            return;
                        }
                    }
                }
                state.committed_tokens.push(snapshot);
                state.committed_token_count = state.committed_tokens.len() as u32;
            }
        }
    }

    /// Snapshot the committed token count at turn start. The per-turn committed text
    /// will include tokens from this index onwards (filtered by sample boundaries).
    pub fn snapshot_token_count_at_turn_start(&self, session_id: &str) -> Option<u32> {
        self.inner
            .lock()
            .ok()
            .and_then(|map| map.get(session_id).map(|state| state.committed_token_count))
    }

    /// Get the per-turn committed text for the closing turn. Selects tokens whose
    /// index is >= baseline_token_count AND whose samples fall within the turn
    /// boundaries. Includes trailing punctuation tokens attached to the last selected
    /// speech-evidence token.
    pub fn per_turn_committed_snapshot(
        &self,
        session_id: &str,
        baseline_token_count: u32,
        turn_start_sample: u64,
        turn_end_sample: u64,
    ) -> Option<(String, u32, i32)> {
        self.inner.lock().ok().and_then(|map| {
            map.get(session_id).map(|state| {
                let revision = state.revision;
                // Select tokens: index >= baseline, end_sample > turn_start (token
                // ends after turn began), and start_sample <= turn_end (token began
                // before/at close).
                let mut selected: Vec<&CommittedTokenSnapshot> = state
                    .committed_tokens
                    .iter()
                    .filter(|t| {
                        t.index >= baseline_token_count
                            && (t.end_sample > turn_start_sample || baseline_token_count == 0)
                            && t.start_sample <= turn_end_sample
                    })
                    .collect();
                // Include immediate trailing punctuation tokens: after the
                // in-boundary selected tokens, walk committed tokens in
                // token-index order starting at precisely last_selected.index + 1.
                // Include only consecutive punctuation-only tokens that satisfy
                // the defined tail/boundary condition. Stop at the first index
                // gap, first speech-evidence token, or token that cannot belong
                // to the closing tail. This avoids jumping over a later turn's
                // speech-evidence tokens to grab dangling punctuation.
                if let Some(last_selected) = selected.last() {
                    let last_end = last_selected.end_sample;
                    let mut expected_idx = last_selected.index + 1;
                    let mut extra: Vec<&CommittedTokenSnapshot> = Vec::new();
                    for t in state.committed_tokens.iter() {
                        if t.index < expected_idx {
                            continue;
                        }
                        if t.index != expected_idx {
                            // Index gap: stop walking.
                            break;
                        }
                        // Speech-evidence token (any alphanumeric): stop walking.
                        if t.text.chars().any(|ch| ch.is_alphanumeric()) {
                            break;
                        }
                        // Punctuation token must start at/after the last
                        // selected token's end to belong to this tail.
                        if t.start_sample < last_end {
                            break;
                        }
                        extra.push(t);
                        expected_idx += 1;
                    }
                    selected.append(&mut extra);
                }
                // Drop leading punctuation-only tokens. A trailing "." from the
                // previous utterance must never become the start of this turn's
                // committed text even if it slipped into the snapshot ring.
                while selected
                    .first()
                    .is_some_and(|t| !t.text.chars().any(|ch| ch.is_alphanumeric()))
                {
                    selected.remove(0);
                }
                let token_count = selected.len() as u32;
                let text: String = selected
                    .iter()
                    .map(|t| t.text.as_str())
                    .collect::<Vec<_>>()
                    .join("");
                (text, token_count, revision)
            })
        })
    }

    /// Record a dispatch (transcript_committed) for a session. Stores the per-turn
    /// dispatched text, turn_id, and turn boundaries so transcript_finalized can
    /// recompute the per-turn text at finalize time and detect late revisions.
    pub fn record_dispatch(
        &self,
        session_id: &str,
        turn_id: &str,
        text: &str,
        turn_start_sample: u64,
        turn_end_sample: u64,
        baseline_token_count: u32,
    ) {
        if let Ok(mut map) = self.inner.lock() {
            if let Some(state) = map.get_mut(session_id) {
                state.last_dispatched_text = Some(text.to_owned());
                state.last_dispatched_turn_id = Some(turn_id.to_owned());
                state.last_dispatched_turn_start_sample = Some(turn_start_sample);
                state.last_dispatched_turn_end_sample = Some(turn_end_sample);
                state.last_dispatched_baseline_token_count = Some(baseline_token_count);
            }
        }
    }

    /// Get the last dispatched (transcript_committed) state for a session,
    /// including turn boundaries for per-turn recomputation at finalize time.
    pub fn dispatched_snapshot(&self, session_id: &str) -> Option<(String, String, u64, u64, u32)> {
        self.inner.lock().ok().and_then(|map| {
            map.get(session_id).and_then(|state| {
                Some((
                    state.last_dispatched_text.clone()?,
                    state.last_dispatched_turn_id.clone()?,
                    state.last_dispatched_turn_start_sample?,
                    state.last_dispatched_turn_end_sample?,
                    state.last_dispatched_baseline_token_count?,
                ))
            })
        })
    }

    pub fn remove_generation(&self, session_id: &str, generation: u64) {
        if let Ok(mut map) = self.inner.lock() {
            if map
                .get(session_id)
                .is_some_and(|state| state.generation == generation)
            {
                map.remove(session_id);
            }
        }
    }

    #[cfg(test)]
    pub fn start_session_for_test(&self, session_id: &str) {
        self.start_session(session_id, 1);
    }
}

const TRANSCRIBE_OK: i32 = 0;
const TRANSCRIBE_TIMESTAMPS_TOKEN: i32 = 4;

#[derive(Clone)]
pub struct ModelConfig {
    pub model_path: PathBuf,
    pub stream_chunk_ms: u32,
    pub att_context_right: i32,
    pub queue_frames: usize,
    /// Shared progress tracker so the TurnManager can wait for model catch-up.
    pub model_progress: Option<ModelProgressMap>,
    /// Optional turn-manager event sink for committed transcript tokens.
    pub transcript_sink: Option<DetectorIngress>,
}

#[derive(Clone)]
pub struct ModelIngress {
    control_sender: mpsc::UnboundedSender<ModelCommand>,
    audio_sender: mpsc::Sender<ModelCommand>,
}

impl ModelIngress {
    pub fn start(config: ModelConfig, logger: JsonlLogger) -> Self {
        let (control_sender, mut control_receiver) = mpsc::unbounded_channel();
        let (audio_sender, mut audio_receiver) = mpsc::channel(config.queue_frames.max(1));
        let worker_sender = control_sender.clone();
        let runtime = tokio::runtime::Handle::current();
        thread::spawn(move || {
            info!(model_path = %config.model_path.display(), stream_chunk_ms = config.stream_chunk_ms, att_context_right = config.att_context_right, "starting nemotron model worker");
            let mut worker = ModelWorker::new(
                config,
                logger,
                runtime,
                ModelDrainHandle::new(worker_sender),
            );
            let mut control_closed = false;
            let mut audio_closed = false;
            let mut graceful_shutdown = false;
            loop {
                let mut handled = false;
                loop {
                    match control_receiver.try_recv() {
                        Ok(command) => {
                            // Flush pending audio before processing EndSession
                            // so finalize sees all enqueued frames for this session.
                            if matches!(command, ModelCommand::EndSession { .. }) {
                                loop {
                                    match audio_receiver.try_recv() {
                                        Ok(audio_cmd) => {
                                            worker.handle(audio_cmd);
                                        }
                                        Err(mpsc::error::TryRecvError::Empty) => break,
                                        Err(mpsc::error::TryRecvError::Disconnected) => {
                                            audio_closed = true;
                                            break;
                                        }
                                    }
                                }
                            }
                            if worker.handle(command) {
                                graceful_shutdown = true;
                            }
                            handled = true;
                        }
                        Err(mpsc::error::TryRecvError::Empty) => break,
                        Err(mpsc::error::TryRecvError::Disconnected) => {
                            control_closed = true;
                            break;
                        }
                    }
                }
                if graceful_shutdown {
                    break;
                }
                match audio_receiver.try_recv() {
                    Ok(command) => {
                        if worker.handle(command) {
                            graceful_shutdown = true;
                        }
                        handled = true;
                    }
                    Err(mpsc::error::TryRecvError::Empty) => {}
                    Err(mpsc::error::TryRecvError::Disconnected) => audio_closed = true,
                }
                if graceful_shutdown {
                    break;
                }
                if control_closed && audio_closed {
                    break;
                }
                if !handled {
                    thread::sleep(Duration::from_millis(1));
                }
            }
            if !graceful_shutdown {
                worker.finalize_all("model worker channel closed");
            }
        });
        Self {
            control_sender,
            audio_sender,
        }
    }

    pub fn drain_handle(&self) -> ModelDrainHandle {
        ModelDrainHandle::new(self.control_sender.clone())
    }

    pub async fn start_session(&self, hello: &HelloState, logger: &JsonlLogger) -> Result<()> {
        let (reply_tx, reply_rx) = oneshot::channel();
        let command = ModelCommand::StartSession {
            hello: hello.clone(),
            reply: reply_tx,
        };
        self.control_sender
            .send(command)
            .map_err(|_| anyhow::anyhow!("model worker control path closed at session start"))?;
        match reply_rx.await {
            Ok(Ok(())) => Ok(()),
            Ok(Err(err)) => {
                log_model_start_error(logger, hello, &err.to_string()).await?;
                Err(err)
            }
            Err(_) => {
                let err = anyhow::anyhow!("model worker dropped session start acknowledgement");
                log_model_start_error(logger, hello, &err.to_string()).await?;
                Err(err)
            }
        }
    }

    pub async fn ingest_frame(
        &self,
        frame: &AudioFrame,
        logger: &JsonlLogger,
        ingress_receive_mono_ns: u64,
    ) -> Result<()> {
        match self.audio_sender.try_send(ModelCommand::AudioFrame {
            frame: frame.clone(),
            ingress_receive_mono_ns,
        }) {
            Ok(()) => Ok(()),
            Err(mpsc::error::TrySendError::Full(command)) => {
                log_model_enqueue_error(
                    logger,
                    &command,
                    "model worker queue full; dropping frame",
                )
                .await?;
                let gap = AudioGapReset::from_dropped_frame(frame, "model_worker_audio_queue_full");
                self.reset_audio(gap, logger).await?;
                Ok(())
            }
            Err(mpsc::error::TrySendError::Closed(_)) => {
                bail!("model worker channel closed")
            }
        }
    }

    pub fn set_transcript_sink(&self, sink: Option<DetectorIngress>) -> Result<()> {
        self.control_sender
            .send(ModelCommand::SetTranscriptSink { sink })
            .map_err(|err| {
                anyhow::anyhow!("model worker queue rejected transcript sink update: {err}")
            })
    }

    pub async fn end_session(
        &self,
        hello: &HelloState,
        _logger: &JsonlLogger,
        reason: impl Into<String>,
    ) -> Result<()> {
        let (reply_tx, reply_rx) = oneshot::channel();
        let command = ModelCommand::EndSession {
            stream_id: hello.stream_id.clone(),
            stream_session_id: hello.stream_session_id.clone(),
            adapter_id: hello.adapter_id.clone(),
            reason: reason.into(),
            reply: reply_tx,
        };
        self.control_sender
            .send(command)
            .map_err(|_| anyhow::anyhow!("model worker control path closed at session end"))?;
        reply_rx
            .await
            .map_err(|_| anyhow::anyhow!("model worker dropped session end acknowledgement"))?
    }

    pub async fn reset_audio(&self, gap: AudioGapReset, _logger: &JsonlLogger) -> Result<()> {
        self.control_sender
            .send(ModelCommand::AudioGapReset { gap })
            .map_err(|_| anyhow::anyhow!("model worker control path closed at audio gap reset"))
    }

    pub async fn shutdown(&self) {
        let (reply, done) = oneshot::channel();
        let _ = self.control_sender.send(ModelCommand::Shutdown { reply });
        let _ = done.await;
    }
}

async fn log_model_start_error(
    logger: &JsonlLogger,
    hello: &HelloState,
    message: &str,
) -> Result<()> {
    logger
        .write(&ModelErrorEvent {
            event: "model_error",
            stream_id: hello.stream_id.clone(),
            stream_session_id: hello.stream_session_id.clone(),
            adapter_id: hello.adapter_id.clone(),
            message: message.to_owned(),
            status: None,
            daemon_mono_ns: now_mono_ns(),
        })
        .await
}

async fn log_model_enqueue_error(
    logger: &JsonlLogger,
    command: &ModelCommand,
    message: &str,
) -> Result<()> {
    let (stream_id, stream_session_id, adapter_id) = command.ids();
    logger
        .write(&ModelErrorEvent {
            event: "model_error",
            stream_id,
            stream_session_id,
            adapter_id,
            message: message.to_owned(),
            status: None,
            daemon_mono_ns: now_mono_ns(),
        })
        .await
}

enum ModelCommand {
    StartSession {
        hello: HelloState,
        reply: oneshot::Sender<Result<()>>,
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
        reply: oneshot::Sender<Result<()>>,
    },
    AudioGapReset {
        gap: AudioGapReset,
    },
    SetTranscriptSink {
        sink: Option<DetectorIngress>,
    },
    DrainSession {
        request: ModelDrainRequest,
        reply: std::sync::mpsc::Sender<Result<ModelDrainResult>>,
    },
    Shutdown {
        reply: oneshot::Sender<()>,
    },
}

impl ModelCommand {
    fn ids(&self) -> (String, String, String) {
        match self {
            ModelCommand::StartSession { hello, .. } => (
                hello.stream_id.clone(),
                hello.stream_session_id.clone(),
                hello.adapter_id.clone(),
            ),
            ModelCommand::AudioFrame { frame, .. } => (
                frame.header.stream_id.clone(),
                frame.header.stream_session_id.clone(),
                frame.header.adapter_id.clone(),
            ),
            ModelCommand::EndSession {
                stream_id,
                stream_session_id,
                adapter_id,
                ..
            } => (
                stream_id.clone(),
                stream_session_id.clone(),
                adapter_id.clone(),
            ),
            ModelCommand::SetTranscriptSink { .. } => (String::new(), String::new(), String::new()),
            ModelCommand::AudioGapReset { gap } => (
                gap.stream_id.clone(),
                gap.stream_session_id.clone(),
                gap.adapter_id.clone(),
            ),
            ModelCommand::DrainSession { request, .. } => (
                request.stream_id.clone(),
                request.stream_session_id.clone(),
                request.adapter_id.clone(),
            ),
            ModelCommand::Shutdown { .. } => (String::new(), String::new(), String::new()),
        }
    }
}

struct ModelWorker {
    config: ModelConfig,
    model_path_c: CString,
    logger: JsonlLogger,
    runtime: tokio::runtime::Handle,
    sessions: HashMap<String, ModelSession>,
    model_progress: Option<ModelProgressMap>,
    drain_handle: ModelDrainHandle,
}

impl ModelWorker {
    fn new(
        config: ModelConfig,
        logger: JsonlLogger,
        runtime: tokio::runtime::Handle,
        drain_handle: ModelDrainHandle,
    ) -> Self {
        let model_path_c = CString::new(config.model_path.to_string_lossy().as_bytes())
            .expect("model path contains interior NUL");
        let model_progress = config.model_progress.clone();
        Self {
            config,
            model_path_c,
            logger,
            runtime,
            sessions: HashMap::new(),
            model_progress,
            drain_handle,
        }
    }

    fn handle(&mut self, command: ModelCommand) -> bool {
        let should_stop = matches!(command, ModelCommand::Shutdown { .. });
        let result = match command {
            ModelCommand::StartSession { hello, reply } => {
                let result = self.start_session(hello);
                let reply_result = match &result {
                    Ok(()) => Ok(()),
                    Err(err) => Err(anyhow::anyhow!(err.to_string())),
                };
                let _ = reply.send(reply_result);
                result
            }
            ModelCommand::AudioFrame {
                frame,
                ingress_receive_mono_ns,
            } => self.audio_frame(frame, ingress_receive_mono_ns),
            ModelCommand::EndSession {
                stream_session_id,
                reason,
                reply,
                ..
            } => {
                let result = self.end_session(&stream_session_id, &reason);
                let reply_result = match &result {
                    Ok(()) => Ok(()),
                    Err(err) => Err(anyhow::anyhow!(err.to_string())),
                };
                let _ = reply.send(reply_result);
                result
            }
            ModelCommand::AudioGapReset { gap } => self.audio_gap_reset(gap),
            ModelCommand::SetTranscriptSink { sink } => {
                self.config.transcript_sink = sink;
                Ok(())
            }
            ModelCommand::DrainSession { request, reply } => {
                let result = self.drain_session(&request);
                let _ = reply.send(result);
                Ok(())
            }
            ModelCommand::Shutdown { reply } => {
                self.finalize_all("model worker shutdown");
                let _ = reply.send(());
                Ok(())
            }
        };
        if let Err(err) = result {
            warn!(error = ?err, "model worker command failed");
        }
        should_stop
    }

    fn start_session(&mut self, hello: HelloState) -> Result<()> {
        if hello.sample_rate_hz != 16_000 || hello.channels != 1 {
            self.write_blocking(&ModelErrorEvent {
                event: "model_error",
                stream_id: hello.stream_id,
                stream_session_id: hello.stream_session_id,
                adapter_id: hello.adapter_id,
                message: "nemotron v1 only supports 16 kHz mono PCM".to_owned(),
                status: None,
                daemon_mono_ns: now_mono_ns(),
            })?;
            bail!("nemotron v1 only supports 16 kHz mono PCM");
        }
        if !matches!(hello.format, PcmFormat::PcmF32Le | PcmFormat::PcmS16Le) {
            self.write_blocking(&ModelErrorEvent {
                event: "model_error",
                stream_id: hello.stream_id,
                stream_session_id: hello.stream_session_id,
                adapter_id: hello.adapter_id,
                message: format!("unsupported PCM format for nemotron v1: {}", hello.format),
                status: None,
                daemon_mono_ns: now_mono_ns(),
            })?;
            bail!("unsupported PCM format for nemotron v1");
        }

        self.sessions.remove(&hello.stream_session_id);
        let open_start_mono_ns = now_mono_ns();
        let mut caps = ScTranscribeCapabilities::default();
        let mut raw = ptr::null_mut();
        let cfg = ScTranscribeConfig {
            model_path: self.model_path_c.as_ptr(),
            stream_chunk_ms: self.config.stream_chunk_ms as c_int,
            att_context_right: self.config.att_context_right as c_int,
        };
        let status = unsafe { sc_transcribe_open_stream(&cfg, &mut raw, &mut caps) };
        let open_end_mono_ns = now_mono_ns();
        if status != TRANSCRIBE_OK {
            self.write_blocking(&ModelErrorEvent {
                event: "model_error",
                stream_id: hello.stream_id,
                stream_session_id: hello.stream_session_id,
                adapter_id: hello.adapter_id,
                message: "failed to open nemotron streaming session".to_owned(),
                status: Some(status_string(status)),
                daemon_mono_ns: open_end_mono_ns,
            })?;
            bail!(
                "failed to open nemotron streaming session: {}",
                status_string(status)
            );
        }
        if raw.is_null() {
            bail!("transcribe shim returned null session with OK status");
        }

        let event = ModelSessionStartEvent {
            event: "model_session_start",
            stream_id: hello.stream_id.clone(),
            stream_session_id: hello.stream_session_id.clone(),
            adapter_id: hello.adapter_id.clone(),
            model_path: self.config.model_path.display().to_string(),
            stream_chunk_ms: self.config.stream_chunk_ms,
            att_context_right: self.config.att_context_right,
            native_sample_rate: caps.native_sample_rate,
            supports_streaming: caps.supports_streaming,
            max_timestamp_kind: timestamp_kind_name(caps.max_timestamp_kind),
            accepts_parakeet_stream: caps.accepts_parakeet_stream,
            open_start_mono_ns,
            open_end_mono_ns,
            open_duration_ms: ns_to_ms(open_end_mono_ns.saturating_sub(open_start_mono_ns)),
        };
        self.write_blocking(&event)?;
        if let Some(ref progress) = self.model_progress {
            progress.start_session(&hello.stream_session_id, hello.generation);
        }

        self.sessions.insert(
            hello.stream_session_id.clone(),
            ModelSession::new(
                raw,
                hello,
                self.config.stream_chunk_ms,
                self.drain_handle.clone(),
            ),
        );
        Ok(())
    }

    fn audio_frame(&mut self, frame: AudioFrame, ingress_receive_mono_ns: u64) -> Result<()> {
        let stream_session_id = frame.header.stream_session_id.clone();
        let mut chunks = Vec::new();
        {
            let session = match self.sessions.get_mut(&stream_session_id) {
                Some(session) => session,
                None => return Ok(()),
            };

            let samples = decode_frame_to_f32(&frame)?;
            session.push_samples(&frame, samples);

            while session.buffer.len() >= session.chunk_samples {
                let take = session.chunk_samples;
                let chunk: Vec<f32> = session.buffer.drain(..take).collect();
                let chunk_source_sample_start = session.next_chunk_sample_start;
                session.next_chunk_sample_start =
                    session.next_chunk_sample_start.saturating_add(take as u64);
                chunks.push((chunk, chunk_source_sample_start));
            }
        }

        for (chunk, chunk_source_sample_start) in chunks {
            self.feed_chunk(
                &stream_session_id,
                chunk,
                chunk_source_sample_start,
                ingress_receive_mono_ns,
                false,
            )?;
        }
        Ok(())
    }

    fn end_session(&mut self, stream_session_id: &str, reason: &str) -> Result<()> {
        let Some(mut session) = self.sessions.remove(stream_session_id) else {
            return Ok(());
        };
        let session_id = session.hello.stream_session_id.clone();
        if !session.buffer.is_empty() {
            let chunk = std::mem::take(&mut session.buffer);
            let chunk_source_sample_start = session.next_chunk_sample_start;
            session.next_chunk_sample_start = session
                .next_chunk_sample_start
                .saturating_add(chunk.len() as u64);
            self.sessions.insert(session_id.clone(), session);
            self.feed_chunk(
                &session_id,
                chunk,
                chunk_source_sample_start,
                now_mono_ns(),
                false,
            )?;
            session = self
                .sessions
                .remove(&session_id)
                .expect("session reinserted");
        }

        let model_feed_start_mono_ns = now_mono_ns();
        let mut update = ScTranscribeUpdate::default();
        let status = unsafe { sc_transcribe_finalize(session.raw, &mut update) };
        let model_feed_end_mono_ns = now_mono_ns();
        self.emit_update_events(
            &mut session,
            status,
            &update,
            model_feed_start_mono_ns,
            model_feed_end_mono_ns,
            now_mono_ns(),
            None,
            true,
            Some(reason),
        )?;

        // Emit transcript_finalized if the per-turn text for the last dispatched
        // turn changed after dispatch (late model revisions: punctuation, capitalization).
        // Recompute the per-turn text at finalize time using the same boundaries
        // and baseline, then compare with the stored dispatch text.
        if let Some(ref progress) = self.model_progress {
            if let Some((dispatched_text, dispatched_turn_id, start_sample, end_sample, baseline)) =
                progress.dispatched_snapshot(&session_id)
            {
                if let Some((final_per_turn, _, final_revision)) = progress
                    .per_turn_committed_snapshot(&session_id, baseline, start_sample, end_sample)
                {
                    if final_per_turn != dispatched_text {
                        self.write_blocking(&TranscriptFinalizedEvent {
                            event: "transcript_finalized",
                            stream_id: session.hello.stream_id.clone(),
                            stream_session_id: session.hello.stream_session_id.clone(),
                            adapter_id: session.hello.adapter_id.clone(),
                            turn_id: dispatched_turn_id,
                            final_text: final_per_turn,
                            committed_text: dispatched_text,
                            revision: final_revision,
                            final_reason: reason.to_owned(),
                            daemon_mono_ns: now_mono_ns(),
                        })?;
                    }
                }
            }
        }

        // Keep the progress entry alive after finalize. TurnManager close paths may
        // still consult last_token_end_sample while draining/closing the same input
        // state; dropping it here makes close-time transcript alignment less stable.
        drop(session);
        Ok(())
    }

    fn audio_gap_reset(&mut self, gap: AudioGapReset) -> Result<()> {
        let Some(session) = self.sessions.get_mut(&gap.stream_session_id) else {
            return Ok(());
        };
        session.buffer.clear();
        session.next_chunk_sample_start = gap.observed_sample_start;
        self.write_blocking(&ModelAudioGapResetEvent {
            event: "model_audio_gap_reset",
            stream_id: gap.stream_id,
            stream_session_id: gap.stream_session_id,
            adapter_id: gap.adapter_id,
            expected_sample_start: gap.expected_sample_start,
            observed_sample_start: gap.observed_sample_start,
            delta_samples: gap.delta_samples,
            reason: gap.reason,
            daemon_mono_ns: now_mono_ns(),
        })
    }

    fn finalize_all(&mut self, reason: &str) {
        let ids: Vec<String> = self.sessions.keys().cloned().collect();
        for id in ids {
            if let Err(err) = self.end_session(&id, reason) {
                warn!(stream_session_id = %id, error = ?err, "failed to finalize model session");
            }
        }
    }

    fn drain_session(&mut self, request: &ModelDrainRequest) -> Result<ModelDrainResult> {
        if request.finalize_turn {
            return self.finalize_turn_stream(request);
        }

        let Some(session) = self.sessions.get_mut(&request.stream_session_id) else {
            return Ok(ModelDrainResult::default());
        };
        if session.buffer.is_empty() {
            let drained_until_sample = self
                .model_progress
                .as_ref()
                .and_then(|progress| progress.get(&request.stream_session_id))
                .unwrap_or(session.next_chunk_sample_start);
            return Ok(ModelDrainResult {
                session_found: true,
                chunk_processed: false,
                drained_until_sample,
                ..Default::default()
            });
        }

        let chunk = std::mem::take(&mut session.buffer);
        let chunk_source_sample_start = session.next_chunk_sample_start;
        let chunk_len = chunk.len() as u64;
        let drained_until_sample = chunk_source_sample_start.saturating_add(chunk_len);
        session.next_chunk_sample_start = drained_until_sample;
        self.feed_chunk(
            &request.stream_session_id,
            chunk,
            chunk_source_sample_start,
            now_mono_ns(),
            false,
        )?;
        if let Some(ref progress) = self.model_progress {
            let committed = progress.get(&request.stream_session_id).unwrap_or(0);
            if committed < drained_until_sample {
                progress.update(&request.stream_session_id, drained_until_sample);
            }
        }
        Ok(ModelDrainResult {
            session_found: true,
            chunk_processed: true,
            drained_until_sample,
            ..Default::default()
        })
    }

    /// Deterministic turn finalization (Nemotron equivalent of Sherpa
    /// input_finished + decode-until-ready):
    /// 1) feed remaining real audio already buffered for this session
    /// 2) feed `padding_ms` synthetic silence (right-context pad)
    /// 3) call stream finalize (is_final=true) — model flushes remaining tokens
    /// 4) re-begin a fresh stream so subsequent mic audio is a new utterance
    ///
    /// Late tokens emitted during this path keep the pre-close turn_id via
    /// model progress (caller freezes ownership before invoking).
    fn finalize_turn_stream(&mut self, request: &ModelDrainRequest) -> Result<ModelDrainResult> {
        let started = std::time::Instant::now();
        let session_id = request.stream_session_id.clone();
        let Some(session) = self.sessions.get(&session_id) else {
            return Ok(ModelDrainResult::default());
        };
        let _ = session; // existence check only

        let tokens_before = self
            .model_progress
            .as_ref()
            .map(|p| p.committed_token_count(&session_id))
            .unwrap_or(0);
        let buffered_ms_at_close = self
            .model_progress
            .as_ref()
            .and_then(|p| p.buffered_ms(&session_id));

        let mut finalize_chunks: u32 = 0;
        let mut drained_until_sample: u64 = 0;
        let mut chunk_processed = false;

        // 1) Flush any partial real-audio buffer (may be < chunk size).
        let pending_real = {
            let Some(session) = self.sessions.get_mut(&session_id) else {
                return Ok(ModelDrainResult::default());
            };
            drained_until_sample = session.next_chunk_sample_start;
            if session.buffer.is_empty() {
                None
            } else {
                let chunk = std::mem::take(&mut session.buffer);
                let start = session.next_chunk_sample_start;
                let len = chunk.len() as u64;
                session.next_chunk_sample_start = start.saturating_add(len);
                drained_until_sample = session.next_chunk_sample_start;
                Some((chunk, start))
            }
        };
        if let Some((chunk, start)) = pending_real {
            self.feed_chunk(&session_id, chunk, start, now_mono_ns(), false)?;
            finalize_chunks = finalize_chunks.saturating_add(1);
            chunk_processed = true;
        }

        // 2) Synthetic silence pad (default 320ms @ 16kHz).
        let padding_ms = if request.padding_ms == 0 {
            320
        } else {
            request.padding_ms
        };
        let pad_samples = (u64::from(padding_ms).saturating_mul(16_000) / 1_000).max(1) as usize;
        // Feed pad in stream_chunk-sized pieces so the family sees normal steps.
        let chunk_samples = self
            .sessions
            .get(&session_id)
            .map(|s| s.chunk_samples)
            .unwrap_or(2560);
        let mut pad_left = pad_samples;
        while pad_left > 0 {
            let take = pad_left.min(chunk_samples);
            let silence = vec![0.0f32; take];
            let start = self
                .sessions
                .get(&session_id)
                .map(|s| s.next_chunk_sample_start)
                .unwrap_or(drained_until_sample);
            if let Some(session) = self.sessions.get_mut(&session_id) {
                session.next_chunk_sample_start = start.saturating_add(take as u64);
                drained_until_sample = session.next_chunk_sample_start;
            }
            self.feed_chunk(&session_id, silence, start, now_mono_ns(), false)?;
            finalize_chunks = finalize_chunks.saturating_add(1);
            chunk_processed = true;
            pad_left = pad_left.saturating_sub(take);
        }

        let close_to_input_finished_ms = started
            .elapsed()
            .as_millis()
            .try_into()
            .unwrap_or(u64::MAX);

        // 3) Stream finalize — flushes right-context / remaining decode.
        let decode_started = std::time::Instant::now();
        let mut finalize_ok = false;
        let mut finalize_timeout = false;
        if let Some(mut session) = self.sessions.remove(&session_id) {
            let model_feed_start_mono_ns = now_mono_ns();
            let mut update = ScTranscribeUpdate::default();
            let status = unsafe { sc_transcribe_finalize(session.raw, &mut update) };
            let model_feed_end_mono_ns = now_mono_ns();
            // Emit as final so tokens/text land with is_final=true.
            if let Err(err) = self.emit_update_events(
                &mut session,
                status,
                &update,
                model_feed_start_mono_ns,
                model_feed_end_mono_ns,
                now_mono_ns(),
                None,
                true,
                Some(request.reason),
            ) {
                warn!(error = ?err, "finalize_turn emit_update_events failed");
            }
            finalize_ok = status == TRANSCRIBE_OK || update.is_final;

            // 4) Re-begin so the live mic session can accept the next utterance.
            let rebegin_status = unsafe { sc_transcribe_rebegin(session.raw) };
            if rebegin_status != TRANSCRIBE_OK {
                warn!(
                    status = %status_string(rebegin_status),
                    "sc_transcribe_rebegin failed after turn finalize; session will be dropped"
                );
                drop(session);
            } else {
                // Native token indices restart at 0 after rebegin; keep global
                // snapshot indices monotonic for per-turn baseline slicing.
                let base = self
                    .model_progress
                    .as_ref()
                    .map(|p| p.committed_token_count(&session_id))
                    .unwrap_or(0);
                session.next_committed_token = 0;
                session.token_index_base = base;
                // Token timestamps restart at 0 on the new stream; map them
                // into the continuous session sample clock.
                session.stream_sample_origin = session.next_chunk_sample_start;
                self.sessions.insert(session_id.clone(), session);
            }
        } else {
            finalize_timeout = true;
        }

        if let Some(ref progress) = self.model_progress {
            if progress.get(&session_id).unwrap_or(0) < drained_until_sample {
                progress.update(&session_id, drained_until_sample);
            }
        }

        let tokens_after = self
            .model_progress
            .as_ref()
            .map(|p| p.committed_token_count(&session_id))
            .unwrap_or(tokens_before);
        let tokens_added = tokens_after.saturating_sub(tokens_before);
        let finalize_decode_ms = decode_started
            .elapsed()
            .as_millis()
            .try_into()
            .unwrap_or(u64::MAX);

        // Bound: if overall elapsed exceeds timeout, mark degraded timeout.
        let elapsed_ms: u64 = started.elapsed().as_millis().try_into().unwrap_or(u64::MAX);
        if elapsed_ms > request.timeout_ms as u64 {
            finalize_timeout = true;
        }

        Ok(ModelDrainResult {
            session_found: true,
            chunk_processed,
            drained_until_sample,
            finalized: true,
            finalize_ok,
            finalize_timeout,
            close_to_input_finished_ms,
            finalize_decode_ms,
            finalize_chunks,
            tokens_added_during_finalize: tokens_added,
            padding_ms,
            buffered_ms_at_close,
            tokens_before_finalize: tokens_before,
            tokens_after_finalize: tokens_after,
        })
    }

    #[allow(clippy::too_many_arguments)]
    fn feed_chunk(
        &mut self,
        stream_session_id: &str,
        chunk: Vec<f32>,
        chunk_source_sample_start: u64,
        ingress_receive_mono_ns: u64,
        is_final: bool,
    ) -> Result<()> {
        let mut session = self
            .sessions
            .remove(stream_session_id)
            .context("missing model session while feeding chunk")?;
        let model_feed_start_mono_ns = now_mono_ns();
        let mut update = ScTranscribeUpdate::default();
        let status = unsafe {
            sc_transcribe_feed(
                session.raw,
                chunk.as_ptr(),
                chunk.len().try_into().unwrap_or(i32::MAX),
                &mut update,
            )
        };
        let model_feed_end_mono_ns = now_mono_ns();
        self.emit_update_events(
            &mut session,
            status,
            &update,
            model_feed_start_mono_ns,
            model_feed_end_mono_ns,
            ingress_receive_mono_ns,
            Some((chunk_source_sample_start, chunk.len() as u64)),
            is_final,
            None,
        )?;
        self.sessions.insert(stream_session_id.to_owned(), session);
        Ok(())
    }

    #[allow(clippy::too_many_arguments)]
    fn emit_update_events(
        &self,
        session: &mut ModelSession,
        status: i32,
        update: &ScTranscribeUpdate,
        model_feed_start_mono_ns: u64,
        model_feed_end_mono_ns: u64,
        ingress_receive_mono_ns: u64,
        chunk_source_coverage: Option<(u64, u64)>,
        is_final: bool,
        final_reason: Option<&str>,
    ) -> Result<()> {
        let status_text = status_string(status);
        if status != TRANSCRIBE_OK {
            self.write_blocking(&ModelErrorEvent {
                event: "model_error",
                stream_id: session.hello.stream_id.clone(),
                stream_session_id: session.hello.stream_session_id.clone(),
                adapter_id: session.hello.adapter_id.clone(),
                message: "nemotron stream update failed".to_owned(),
                status: Some(status_text.clone()),
                daemon_mono_ns: model_feed_end_mono_ns,
            })?;
        }

        let chunk_event = ModelChunkProcessedEvent {
            event: "model_chunk_processed",
            stream_id: session.hello.stream_id.clone(),
            stream_session_id: session.hello.stream_session_id.clone(),
            adapter_id: session.hello.adapter_id.clone(),
            status: status_text,
            revision: update.revision,
            result_changed: update.result_changed,
            committed_changed: update.committed_changed,
            tentative_changed: update.tentative_changed,
            is_final: update.is_final || is_final,
            final_reason: final_reason.map(ToOwned::to_owned),
            input_received_ms: update.input_received_ms,
            audio_committed_ms: update.audio_committed_ms,
            buffered_ms: update.buffered_ms,
            committed_tokens: update.committed_tokens,
            total_tokens: update.total_tokens,
            returned_timestamp_kind: timestamp_kind_name(update.returned_timestamp_kind),
            chunk_source_sample_start: chunk_source_coverage.map(|(start, _)| start),
            chunk_sample_count: chunk_source_coverage.map(|(_, count)| count),
            ingress_receive_mono_ns,
            model_feed_start_mono_ns,
            model_feed_end_mono_ns,
            model_feed_duration_ms: ns_to_ms(
                model_feed_end_mono_ns.saturating_sub(model_feed_start_mono_ns),
            ),
        };
        self.write_blocking(&chunk_event)?;

        if update.result_changed || update.committed_changed || update.tentative_changed || is_final
        {
            let committed_text = cstr_to_string(update.committed_text);
            let turn_id = self
                .model_progress
                .as_ref()
                .and_then(|progress| progress.current_turn_id(&session.hello.stream_session_id));
            // Update shared committed-text state for turn-manager dispatch.
            if let Some(ref progress) = self.model_progress {
                progress.update_committed_text(
                    &session.hello.stream_session_id,
                    &committed_text,
                    update.committed_tokens.max(0) as u32,
                    update.revision,
                );
            }
            self.write_blocking(&TranscriptUpdateEvent {
                event: "transcript_update",
                stream_id: session.hello.stream_id.clone(),
                stream_session_id: session.hello.stream_session_id.clone(),
                adapter_id: session.hello.adapter_id.clone(),
                turn_id,
                revision: update.revision,
                committed_text,
                tentative_text: cstr_to_string(update.tentative_text),
                committed_token_count: update.committed_tokens.max(0) as u32,
                total_token_count: update.total_tokens.max(0) as u32,
                input_received_ms: update.input_received_ms,
                audio_committed_ms: update.audio_committed_ms,
                buffered_ms: update.buffered_ms,
                model_feed_end_mono_ns,
            })?;
        }

        // Shared progress is updated after committed tokens are read/recorded below.
        // TurnManager uses this value as a catch-up barrier before closing VAD turns;
        // publishing it before token forwarding creates a race where a trailing token
        // can arrive just after turn_closed and get split into a transcript-backed turn.
        let committed = update.committed_tokens.max(0) as i32;
        let total = update.total_tokens.max(0) as i32;
        let upper = committed.min(total);
        for token_index in session.next_committed_token..upper {
            let mut token = ScTranscribeToken::default();
            let token_status =
                unsafe { sc_transcribe_get_token(session.raw, token_index, &mut token) };
            if token_status != TRANSCRIBE_OK {
                self.write_blocking(&ModelErrorEvent {
                    event: "model_error",
                    stream_id: session.hello.stream_id.clone(),
                    stream_session_id: session.hello.stream_session_id.clone(),
                    adapter_id: session.hello.adapter_id.clone(),
                    message: format!("failed to read committed token {token_index}"),
                    status: Some(status_string(token_status)),
                    daemon_mono_ns: now_mono_ns(),
                })?;
                continue;
            }
            let token_text = cstr_to_string(token.text);
            let timestamps_valid = update.returned_timestamp_kind == TRANSCRIBE_TIMESTAMPS_TOKEN
                && token.t1_ms >= token.t0_ms
                && token.t0_ms >= 0;
            if let Some(ref progress) = self.model_progress {
                // Nemotron t0/t1 are relative to the current native stream begin.
                // After finalize+rebegin that origin is stream_sample_origin.
                let token_end_sample = if timestamps_valid {
                    session
                        .stream_sample_origin
                        .saturating_add(ms_to_sample(token.t1_ms))
                } else {
                    session.stream_sample_origin.saturating_add(
                        (update.audio_committed_ms.max(0) as u64).saturating_mul(16),
                    )
                };
                let token_start_sample = if timestamps_valid {
                    session
                        .stream_sample_origin
                        .saturating_add(ms_to_sample(token.t0_ms))
                } else {
                    token_end_sample.saturating_sub(160) // ~10ms estimate
                };
                progress.record_token(&session.hello.stream_session_id, token_end_sample);
                // Record the token snapshot for per-turn text construction.
                progress.record_token_snapshot(
                    &session.hello.stream_session_id,
                    CommittedTokenSnapshot {
                        index: session
                            .token_index_base
                            .saturating_add(token_index as u32),
                        text: token_text.clone(),
                        start_sample: token_start_sample,
                        end_sample: token_end_sample,
                    },
                );
            }
            let probability = if token.probability.is_nan() {
                None
            } else {
                Some(token.probability)
            };
            let source_sample_start_estimate = timestamps_valid.then(|| {
                session
                    .stream_sample_origin
                    .saturating_add(ms_to_sample(token.t0_ms))
            });
            let source_sample_end_estimate = timestamps_valid.then(|| {
                session
                    .stream_sample_origin
                    .saturating_add(ms_to_sample(token.t1_ms))
            });
            let token_turn_id = self
                .model_progress
                .as_ref()
                .and_then(|progress| progress.current_turn_id(&session.hello.stream_session_id));
            self.write_blocking(&TranscriptTokenCommittedEvent {
                event: "transcript_token_committed",
                stream_id: session.hello.stream_id.clone(),
                stream_session_id: session.hello.stream_session_id.clone(),
                adapter_id: session.hello.adapter_id.clone(),
                turn_id: token_turn_id,
                token_index: session.token_index_base.saturating_add(token_index as u32),
                token_id: token.id,
                text: token_text.clone(),
                t0_ms: token.t0_ms,
                t1_ms: token.t1_ms,
                probability,
                source_sample_start_estimate,
                source_sample_end_estimate,
                input_received_ms: update.input_received_ms,
                audio_committed_ms: update.audio_committed_ms,
                buffered_ms: update.buffered_ms,
                ingress_receive_mono_ns,
                model_feed_start_mono_ns,
                model_feed_end_mono_ns,
                model_feed_duration_ms: ns_to_ms(
                    model_feed_end_mono_ns.saturating_sub(model_feed_start_mono_ns),
                ),
                alignment_quality: if timestamps_valid { "token" } else { "unknown" },
            })?;
            if let Some(ref sink) = self.config.transcript_sink {
                // Punctuation-only tail commits (".", "?", etc.) often arrive after
                // the user has stopped speaking. They are part of the transcript text,
                // but they are not independent speech evidence and must not reopen a
                // turn or trigger speech-out by themselves.
                if !is_speech_evidence_text(&token_text) {
                    continue;
                }
                let start_sample = source_sample_start_estimate.unwrap_or_else(|| {
                    session.stream_sample_origin.saturating_add(
                        (update.audio_committed_ms.max(0) as u64).saturating_mul(16),
                    )
                });
                let end_sample = source_sample_end_estimate.unwrap_or_else(|| {
                    session.stream_sample_origin.saturating_add(
                        (update.audio_committed_ms.max(0) as u64).saturating_mul(16),
                    )
                });
                if let Err(err) = sink.transcript_token_committed(TranscriptTokenSignal {
                    stream_id: session.hello.stream_id.clone(),
                    stream_session_id: session.hello.stream_session_id.clone(),
                    adapter_id: session.hello.adapter_id.clone(),
                    token_index: session.token_index_base.saturating_add(token_index as u32),
                    text: token_text,
                    start_sample,
                    end_sample,
                    decision_sample: end_sample,
                    probability,
                    drain_handle: session.drain_handle.clone(),
                }) {
                    self.write_blocking(&ModelErrorEvent {
                        event: "model_error",
                        stream_id: session.hello.stream_id.clone(),
                        stream_session_id: session.hello.stream_session_id.clone(),
                        adapter_id: session.hello.adapter_id.clone(),
                        message: format!(
                            "failed to forward transcript token to turn manager: {err}"
                        ),
                        status: None,
                        daemon_mono_ns: now_mono_ns(),
                    })?;
                }
            }
        }
        if let Some(ref progress) = self.model_progress {
            let committed_samples = (update.audio_committed_ms.max(0) as u64).saturating_mul(16);
            progress.update(&session.hello.stream_session_id, committed_samples);
            progress.update_buffered_ms(
                &session.hello.stream_session_id,
                update.buffered_ms,
            );
        }
        session.next_committed_token = session.next_committed_token.max(upper);
        Ok(())
    }

    fn write_blocking<T: Serialize>(&self, event: &T) -> Result<()> {
        let _ = &self.runtime;
        self.logger.blocking_write(event)
    }
}

struct ModelSession {
    raw: *mut ScTranscribeSession,
    hello: HelloState,
    buffer: Vec<f32>,
    chunk_samples: usize,
    next_chunk_sample_start: u64,
    /// Next native stream token index to read (resets on rebegin).
    next_committed_token: i32,
    /// Added to native token indices when recording snapshots so indices stay
    /// monotonic across per-turn finalize+rebegin boundaries.
    token_index_base: u32,
    /// Session sample clock at the start of the current native stream.
    /// Token t0/t1 from Nemotron are stream-relative and reset on rebegin;
    /// absolute sample = stream_sample_origin + ms_to_sample(t*).
    stream_sample_origin: u64,
    drain_handle: ModelDrainHandle,
}

impl ModelSession {
    fn new(
        raw: *mut ScTranscribeSession,
        hello: HelloState,
        stream_chunk_ms: u32,
        drain_handle: ModelDrainHandle,
    ) -> Self {
        let chunk_samples =
            (u64::from(stream_chunk_ms).saturating_mul(16_000) / 1_000).max(1) as usize;
        Self {
            raw,
            hello,
            buffer: Vec::with_capacity(chunk_samples * 2),
            chunk_samples,
            next_chunk_sample_start: 0,
            next_committed_token: 0,
            token_index_base: 0,
            stream_sample_origin: 0,
            drain_handle,
        }
    }

    fn push_samples(&mut self, frame: &AudioFrame, samples: Vec<f32>) {
        if self.buffer.is_empty() {
            self.next_chunk_sample_start = frame.header.source_sample_start;
        }
        self.buffer.extend(samples);
    }
}

impl Drop for ModelSession {
    fn drop(&mut self) {
        unsafe { sc_transcribe_free(self.raw) };
        self.raw = ptr::null_mut();
    }
}

fn decode_frame_to_f32(frame: &AudioFrame) -> Result<Vec<f32>> {
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

fn status_string(status: i32) -> String {
    unsafe { cstr_to_string(sc_transcribe_status_string(status)) }
}

fn is_speech_evidence_text(text: &str) -> bool {
    text.chars().any(|ch| ch.is_alphanumeric())
}

fn cstr_to_string(ptr: *const c_char) -> String {
    if ptr.is_null() {
        String::new()
    } else {
        unsafe { CStr::from_ptr(ptr).to_string_lossy().into_owned() }
    }
}

fn timestamp_kind_name(kind: i32) -> &'static str {
    match kind {
        0 => "none",
        1 => "auto",
        2 => "segment",
        3 => "word",
        4 => "token",
        _ => "unknown",
    }
}

fn ns_to_ms(ns: u64) -> f64 {
    ns as f64 / 1_000_000.0
}

fn ms_to_sample(ms: i64) -> u64 {
    if ms <= 0 {
        0
    } else {
        (ms as u64).saturating_mul(16_000) / 1_000
    }
}

#[derive(Debug, Serialize)]
struct ModelAudioGapResetEvent {
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
struct ModelSessionStartEvent {
    event: &'static str,
    stream_id: String,
    stream_session_id: String,
    adapter_id: String,
    model_path: String,
    stream_chunk_ms: u32,
    att_context_right: i32,
    native_sample_rate: i32,
    supports_streaming: bool,
    max_timestamp_kind: &'static str,
    accepts_parakeet_stream: bool,
    open_start_mono_ns: u64,
    open_end_mono_ns: u64,
    open_duration_ms: f64,
}

#[derive(Debug, Serialize)]
struct ModelChunkProcessedEvent {
    event: &'static str,
    stream_id: String,
    stream_session_id: String,
    adapter_id: String,
    status: String,
    revision: i32,
    result_changed: bool,
    committed_changed: bool,
    tentative_changed: bool,
    is_final: bool,
    final_reason: Option<String>,
    input_received_ms: i64,
    audio_committed_ms: i64,
    buffered_ms: i64,
    committed_tokens: i32,
    total_tokens: i32,
    returned_timestamp_kind: &'static str,
    chunk_source_sample_start: Option<u64>,
    chunk_sample_count: Option<u64>,
    ingress_receive_mono_ns: u64,
    model_feed_start_mono_ns: u64,
    model_feed_end_mono_ns: u64,
    model_feed_duration_ms: f64,
}

#[derive(Debug, Serialize)]
struct TranscriptTokenCommittedEvent {
    event: &'static str,
    stream_id: String,
    stream_session_id: String,
    adapter_id: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    turn_id: Option<String>,
    token_index: u32,
    token_id: i32,
    text: String,
    t0_ms: i64,
    t1_ms: i64,
    probability: Option<f32>,
    source_sample_start_estimate: Option<u64>,
    source_sample_end_estimate: Option<u64>,
    input_received_ms: i64,
    audio_committed_ms: i64,
    buffered_ms: i64,
    ingress_receive_mono_ns: u64,
    model_feed_start_mono_ns: u64,
    model_feed_end_mono_ns: u64,
    model_feed_duration_ms: f64,
    alignment_quality: &'static str,
}

#[derive(Debug, Serialize)]
struct TranscriptUpdateEvent {
    event: &'static str,
    stream_id: String,
    stream_session_id: String,
    adapter_id: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    turn_id: Option<String>,
    revision: i32,
    committed_text: String,
    tentative_text: String,
    committed_token_count: u32,
    total_token_count: u32,
    input_received_ms: i64,
    audio_committed_ms: i64,
    buffered_ms: i64,
    model_feed_end_mono_ns: u64,
}

/// Emitted after sc_transcribe_finalize if the final transcript differs from the
/// text dispatched in a transcript_committed event. Late revisions (punctuation,
/// capitalization) appear here. This event is diagnostic-only and must not be used
/// to revise already-dispatched conversation state.
#[derive(Debug, Serialize)]
struct TranscriptFinalizedEvent {
    event: &'static str,
    stream_id: String,
    stream_session_id: String,
    adapter_id: String,
    turn_id: String,
    final_text: String,
    committed_text: String,
    revision: i32,
    final_reason: String,
    daemon_mono_ns: u64,
}

#[derive(Debug, Serialize)]
struct ModelErrorEvent {
    event: &'static str,
    stream_id: String,
    stream_session_id: String,
    adapter_id: String,
    message: String,
    status: Option<String>,
    daemon_mono_ns: u64,
}

#[repr(C)]
struct ScTranscribeSession {
    _private: [u8; 0],
}

#[repr(C)]
struct ScTranscribeConfig {
    model_path: *const c_char,
    stream_chunk_ms: c_int,
    att_context_right: c_int,
}

#[repr(C)]
#[derive(Default)]
struct ScTranscribeCapabilities {
    native_sample_rate: c_int,
    supports_streaming: bool,
    max_timestamp_kind: c_int,
    accepts_parakeet_stream: bool,
}

#[repr(C)]
#[derive(Default)]
struct ScTranscribeUpdate {
    result_changed: bool,
    is_final: bool,
    revision: c_int,
    input_received_ms: i64,
    audio_committed_ms: i64,
    buffered_ms: i64,
    committed_changed: bool,
    tentative_changed: bool,
    committed_tokens: c_int,
    total_tokens: c_int,
    returned_timestamp_kind: c_int,
    committed_text: *const c_char,
    tentative_text: *const c_char,
}

#[repr(C)]
#[derive(Default)]
struct ScTranscribeToken {
    id: c_int,
    probability: c_float,
    t0_ms: i64,
    t1_ms: i64,
    seg_index: c_int,
    word_index: c_int,
    text: *const c_char,
}

extern "C" {
    fn sc_transcribe_status_string(status: c_int) -> *const c_char;
    fn sc_transcribe_open_stream(
        config: *const ScTranscribeConfig,
        out_session: *mut *mut ScTranscribeSession,
        out_caps: *mut ScTranscribeCapabilities,
    ) -> c_int;
    fn sc_transcribe_feed(
        session: *mut ScTranscribeSession,
        pcm: *const c_float,
        n_samples: c_int,
        out_update: *mut ScTranscribeUpdate,
    ) -> c_int;
    fn sc_transcribe_finalize(
        session: *mut ScTranscribeSession,
        out_update: *mut ScTranscribeUpdate,
    ) -> c_int;
    fn sc_transcribe_rebegin(session: *mut ScTranscribeSession) -> c_int;
    fn sc_transcribe_free(session: *mut ScTranscribeSession);
    fn sc_transcribe_get_token(
        session: *mut ScTranscribeSession,
        token_index: c_int,
        out_token: *mut ScTranscribeToken,
    ) -> c_int;
}

#[cfg(test)]
mod tests {
    use super::*;
    use speech_core_protocol::{
        AudioFrameHeader, ClockComparability, ClockDomain, SourceKind, TimestampProvenance,
        TimestampQuality, TimestampSemantics,
    };

    fn test_frame(format: PcmFormat, payload: Vec<u8>, sample_count: u32) -> AudioFrame {
        AudioFrame::new(
            AudioFrameHeader {
                stream_id: "s".into(),
                stream_session_id: "ss".into(),
                adapter_id: "a".into(),
                source_kind: SourceKind::Synthetic,
                seq: 0,
                format,
                sample_rate_hz: 16_000,
                channels: 1,
                source_sample_start: 0,
                sample_count,
                source_capture_mono_ns: 0,
                adapter_send_mono_ns: 0,
                timestamp_provenance: TimestampProvenance {
                    adapter_clock_id: "test".into(),
                    adapter_clock_domain: ClockDomain::HostMonotonic,
                    timestamp_quality: TimestampQuality::SyntheticScheduled,
                    timestamp_semantics: TimestampSemantics::FirstSample,
                    clock_comparability: ClockComparability::Uncalibrated,
                    estimated_daemon_offset_ns: None,
                    estimated_offset_uncertainty_ns: None,
                },
                preceding_source_gap: None,
            },
            payload,
        )
        .unwrap()
    }

    #[test]
    fn decodes_s16le_to_f32_for_model_feed() {
        let mut payload = Vec::new();
        payload.extend_from_slice(&0_i16.to_le_bytes());
        payload.extend_from_slice(&i16::MAX.to_le_bytes());
        payload.extend_from_slice(&i16::MIN.to_le_bytes());
        let decoded = decode_frame_to_f32(&test_frame(PcmFormat::PcmS16Le, payload, 3)).unwrap();
        assert_eq!(decoded[0], 0.0);
        assert!((decoded[1] - 0.9999695).abs() < 0.00001);
        assert_eq!(decoded[2], -1.0);
    }

    #[test]
    fn decodes_f32le_for_model_feed() {
        let mut payload = Vec::new();
        payload.extend_from_slice(&0.25_f32.to_le_bytes());
        payload.extend_from_slice(&(-0.5_f32).to_le_bytes());
        let decoded = decode_frame_to_f32(&test_frame(PcmFormat::PcmF32Le, payload, 2)).unwrap();
        assert_eq!(decoded, vec![0.25, -0.5]);
    }

    #[test]
    fn progress_map_keeps_token_state_until_explicit_remove() {
        let progress = ModelProgressMap::new();
        progress.start_session_for_test("session");
        progress.update("session", 1_600);
        progress.record_token("session", 1_200);

        assert_eq!(progress.get("session"), Some(1_600));
        assert_eq!(progress.last_token_end_sample("session"), Some(1_200));

        progress.remove_generation("session", 1);
        assert_eq!(progress.get("session"), None);
        assert_eq!(progress.last_token_end_sample("session"), None);
    }
}
