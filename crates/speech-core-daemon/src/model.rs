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
use tokio::sync::mpsc;
use tracing::{info, warn};

use crate::detectors::{DetectorIngress, TranscriptTokenSignal};
use crate::{HelloState, JsonlLogger};

/// Shared model progress tracker: stream_session_id → latest committed audio sample.
/// The TurnManager reads this to ensure the model has caught up before emitting turn_closed.
#[derive(Debug, Clone, Copy, Default)]
pub struct ModelProgressState {
    pub audio_committed_samples: u64,
    pub last_token_end_sample: Option<u64>,
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
}

#[derive(Debug, Clone, Copy, Default)]
pub struct ModelDrainResult {
    pub session_found: bool,
    pub chunk_processed: bool,
    pub drained_until_sample: u64,
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
    Worker(mpsc::Sender<ModelCommand>),
    #[cfg(test)]
    Pending,
    #[cfg(test)]
    Callback(Arc<dyn Fn(ModelDrainRequest) -> Result<ModelDrainResult> + Send + Sync>),
}

impl ModelDrainHandle {
    fn new(sender: mpsc::Sender<ModelCommand>) -> Self {
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
                    .try_send(ModelCommand::DrainSession {
                        request,
                        reply: reply_tx,
                    })
                    .map_err(|err| {
                        anyhow::anyhow!("model worker queue rejected drain request: {err}")
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

    pub fn update(&self, session_id: &str, committed_samples: u64) {
        if let Ok(mut map) = self.inner.lock() {
            let state = map.entry(session_id.to_owned()).or_default();
            state.audio_committed_samples = committed_samples;
        }
    }

    pub fn record_token(&self, session_id: &str, token_end_sample: u64) {
        if let Ok(mut map) = self.inner.lock() {
            let state = map.entry(session_id.to_owned()).or_default();
            state.last_token_end_sample = Some(token_end_sample);
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

    pub fn remove(&self, session_id: &str) {
        if let Ok(mut map) = self.inner.lock() {
            map.remove(session_id);
        }
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
    sender: mpsc::Sender<ModelCommand>,
}

impl ModelIngress {
    pub fn start(config: ModelConfig, logger: JsonlLogger) -> Self {
        let (sender, mut receiver) = mpsc::channel(config.queue_frames.max(1));
        let worker_sender = sender.clone();
        let runtime = tokio::runtime::Handle::current();
        thread::spawn(move || {
            info!(model_path = %config.model_path.display(), stream_chunk_ms = config.stream_chunk_ms, att_context_right = config.att_context_right, "starting nemotron model worker");
            let mut worker = ModelWorker::new(
                config,
                logger,
                runtime,
                ModelDrainHandle::new(worker_sender),
            );
            while let Some(command) = receiver.blocking_recv() {
                worker.handle(command);
            }
            worker.finalize_all("model worker channel closed");
        });
        Self { sender }
    }

    pub fn drain_handle(&self) -> ModelDrainHandle {
        ModelDrainHandle::new(self.sender.clone())
    }

    pub async fn start_session(&self, hello: &HelloState, logger: &JsonlLogger) -> Result<()> {
        match self.sender.try_send(ModelCommand::StartSession {
            hello: hello.clone(),
        }) {
            Ok(()) => Ok(()),
            Err(mpsc::error::TrySendError::Full(command)) => {
                log_model_enqueue_error(
                    logger,
                    &command,
                    "model worker queue full at session start",
                )
                .await?;
                bail!("model worker queue full at session start")
            }
            Err(mpsc::error::TrySendError::Closed(_)) => {
                bail!("model worker channel closed")
            }
        }
    }

    pub async fn ingest_frame(
        &self,
        frame: &AudioFrame,
        logger: &JsonlLogger,
        ingress_receive_mono_ns: u64,
    ) -> Result<()> {
        match self.sender.try_send(ModelCommand::AudioFrame {
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
                Ok(())
            }
            Err(mpsc::error::TrySendError::Closed(_)) => {
                bail!("model worker channel closed")
            }
        }
    }

    pub fn set_transcript_sink(&self, sink: Option<DetectorIngress>) -> Result<()> {
        self.sender
            .try_send(ModelCommand::SetTranscriptSink { sink })
            .map_err(|err| {
                anyhow::anyhow!("model worker queue rejected transcript sink update: {err}")
            })
    }

    pub async fn end_session(
        &self,
        hello: &HelloState,
        logger: &JsonlLogger,
        reason: impl Into<String>,
    ) -> Result<()> {
        let command = ModelCommand::EndSession {
            stream_id: hello.stream_id.clone(),
            stream_session_id: hello.stream_session_id.clone(),
            adapter_id: hello.adapter_id.clone(),
            reason: reason.into(),
        };
        match self.sender.try_send(command) {
            Ok(()) => Ok(()),
            Err(mpsc::error::TrySendError::Full(command)) => {
                log_model_enqueue_error(logger, &command, "model worker queue full at session end")
                    .await?;
                Ok(())
            }
            Err(mpsc::error::TrySendError::Closed(_)) => Ok(()),
        }
    }
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
    SetTranscriptSink {
        sink: Option<DetectorIngress>,
    },
    DrainSession {
        request: ModelDrainRequest,
        reply: std::sync::mpsc::Sender<Result<ModelDrainResult>>,
    },
}

impl ModelCommand {
    fn ids(&self) -> (String, String, String) {
        match self {
            ModelCommand::StartSession { hello } => (
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
            ModelCommand::DrainSession { request, .. } => (
                request.stream_id.clone(),
                request.stream_session_id.clone(),
                request.adapter_id.clone(),
            ),
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

    fn handle(&mut self, command: ModelCommand) {
        let result = match command {
            ModelCommand::StartSession { hello } => self.start_session(hello),
            ModelCommand::AudioFrame {
                frame,
                ingress_receive_mono_ns,
            } => self.audio_frame(frame, ingress_receive_mono_ns),
            ModelCommand::EndSession {
                stream_session_id,
                reason,
                ..
            } => self.end_session(&stream_session_id, &reason),
            ModelCommand::SetTranscriptSink { sink } => {
                self.config.transcript_sink = sink;
                Ok(())
            }
            ModelCommand::DrainSession { request, reply } => {
                let result = self.drain_session(&request);
                let _ = reply.send(result);
                Ok(())
            }
        };
        if let Err(err) = result {
            warn!(error = ?err, "model worker command failed");
        }
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
            return Ok(());
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
            return Ok(());
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
            return Ok(());
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
        if !session.buffer.is_empty() {
            let chunk = std::mem::take(&mut session.buffer);
            let chunk_source_sample_start = session.next_chunk_sample_start;
            session.next_chunk_sample_start = session
                .next_chunk_sample_start
                .saturating_add(chunk.len() as u64);
            self.sessions.insert(stream_session_id.to_owned(), session);
            self.feed_chunk(
                stream_session_id,
                chunk,
                chunk_source_sample_start,
                now_mono_ns(),
                false,
            )?;
            session = self
                .sessions
                .remove(stream_session_id)
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
        if let Some(ref progress) = self.model_progress {
            progress.remove(stream_session_id);
        }
        drop(session);
        Ok(())
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
            self.write_blocking(&TranscriptUpdateEvent {
                event: "transcript_update",
                stream_id: session.hello.stream_id.clone(),
                stream_session_id: session.hello.stream_session_id.clone(),
                adapter_id: session.hello.adapter_id.clone(),
                revision: update.revision,
                committed_text: cstr_to_string(update.committed_text),
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
            let timestamps_valid = update.returned_timestamp_kind == TRANSCRIBE_TIMESTAMPS_TOKEN
                && token.t1_ms >= token.t0_ms
                && token.t0_ms >= 0;
            if let Some(ref progress) = self.model_progress {
                let token_end_sample = if timestamps_valid {
                    ms_to_sample(token.t1_ms)
                } else {
                    (update.audio_committed_ms.max(0) as u64).saturating_mul(16)
                };
                progress.record_token(&session.hello.stream_session_id, token_end_sample);
            }
            let token_text = cstr_to_string(token.text);
            let probability = if token.probability.is_nan() {
                None
            } else {
                Some(token.probability)
            };
            let source_sample_start_estimate = timestamps_valid.then(|| ms_to_sample(token.t0_ms));
            let source_sample_end_estimate = timestamps_valid.then(|| ms_to_sample(token.t1_ms));
            self.write_blocking(&TranscriptTokenCommittedEvent {
                event: "transcript_token_committed",
                stream_id: session.hello.stream_id.clone(),
                stream_session_id: session.hello.stream_session_id.clone(),
                adapter_id: session.hello.adapter_id.clone(),
                token_index: token_index as u32,
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
                    (update.audio_committed_ms.max(0) as u64).saturating_mul(16)
                });
                let end_sample = source_sample_end_estimate.unwrap_or_else(|| {
                    (update.audio_committed_ms.max(0) as u64).saturating_mul(16)
                });
                if let Err(err) = sink.transcript_token_committed(TranscriptTokenSignal {
                    stream_id: session.hello.stream_id.clone(),
                    stream_session_id: session.hello.stream_session_id.clone(),
                    adapter_id: session.hello.adapter_id.clone(),
                    token_index: token_index as u32,
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
        }
        session.next_committed_token = session.next_committed_token.max(upper);
        Ok(())
    }

    fn write_blocking<T: Serialize>(&self, event: &T) -> Result<()> {
        self.runtime.block_on(self.logger.write(event))
    }
}

struct ModelSession {
    raw: *mut ScTranscribeSession,
    hello: HelloState,
    buffer: Vec<f32>,
    chunk_samples: usize,
    next_chunk_sample_start: u64,
    next_committed_token: i32,
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
}
