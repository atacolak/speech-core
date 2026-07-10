use anyhow::{Context, Result};
use serde::Serialize;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;
use tokio::fs::{create_dir_all, rename, OpenOptions};
use tokio::io::{AsyncWriteExt, BufWriter};
use tokio::sync::{broadcast, mpsc, oneshot};
use tokio::task::JoinHandle;
use tracing::{info, warn};

#[derive(Debug, Clone)]
pub(crate) struct JsonlLoggerConfig {
    pub log_dir: PathBuf,
    pub max_bytes: u64,
    pub max_files: usize,
    pub flush_interval_ms: u64,
    pub flush_batch_lines: usize,
}

impl JsonlLoggerConfig {
    fn active_path(&self) -> PathBuf {
        self.log_dir.join("events.jsonl")
    }
}

#[derive(Clone)]
pub(crate) struct JsonlLogger {
    sender: mpsc::Sender<LoggerCommand>,
    event_tx: broadcast::Sender<String>,
    worker: Arc<tokio::sync::Mutex<Option<JoinHandle<Result<()>>>>>,
}

impl JsonlLogger {
    pub(crate) async fn open(
        log_dir: PathBuf,
        event_tx: broadcast::Sender<String>,
    ) -> Result<Self> {
        Self::open_with_config(
            JsonlLoggerConfig {
                log_dir,
                max_bytes: 256 * 1024 * 1024,
                max_files: 8,
                flush_interval_ms: 1000,
                flush_batch_lines: 256,
            },
            event_tx,
        )
        .await
    }

    pub(crate) async fn open_with_config(
        config: JsonlLoggerConfig,
        event_tx: broadcast::Sender<String>,
    ) -> Result<Self> {
        create_dir_all(&config.log_dir)
            .await
            .with_context(|| format!("creating log directory {}", config.log_dir.display()))?;
        let (sender, receiver) = mpsc::channel(4096);
        let worker = tokio::spawn(LoggerWorker::new(config).run(receiver));
        Ok(Self {
            sender,
            event_tx,
            worker: Arc::new(tokio::sync::Mutex::new(Some(worker))),
        })
    }

    pub(crate) async fn write<T: Serialize>(&self, event: &T) -> Result<()> {
        let line = serde_json::to_string(event).context("serializing jsonl event")?;
        let durable = !is_jsonl_filtered_event(&line);
        self.write_serialized(line, durable).await
    }

    pub(crate) async fn write_serialized(&self, line: String, durable: bool) -> Result<()> {
        // Preserve immediate live semantics: subscribers see events before durable I/O runs.
        let _ = self.event_tx.send(line.clone());
        if durable {
            self.sender
                .send(LoggerCommand::Line(line))
                .await
                .context("jsonl logger worker stopped")?;
        }
        Ok(())
    }

    pub(crate) fn blocking_write<T: Serialize>(&self, event: &T) -> Result<()> {
        let line = serde_json::to_string(event).context("serializing jsonl event")?;
        let durable = !is_jsonl_filtered_event(&line);
        let _ = self.event_tx.send(line.clone());
        if durable {
            self.sender
                .blocking_send(LoggerCommand::Line(line))
                .context("jsonl logger worker stopped")?;
        }
        Ok(())
    }

    pub(crate) async fn flush(&self) -> Result<()> {
        let (tx, rx) = oneshot::channel();
        self.sender
            .send(LoggerCommand::Flush(tx))
            .await
            .context("jsonl logger worker stopped")?;
        rx.await.context("jsonl logger flush response dropped")?
    }

    pub(crate) async fn shutdown(&self) -> Result<()> {
        let (tx, rx) = oneshot::channel();
        let _ = self.sender.send(LoggerCommand::Shutdown(tx)).await;
        let flush_result = rx
            .await
            .unwrap_or_else(|_| Err(anyhow::anyhow!("jsonl logger shutdown response dropped")));
        let mut worker = self.worker.lock().await;
        if let Some(handle) = worker.take() {
            handle.await.context("joining jsonl logger worker")??;
        }
        flush_result
    }

    pub(crate) fn subscribe(&self) -> broadcast::Receiver<String> {
        self.event_tx.subscribe()
    }
}

#[derive(Debug)]
enum LoggerCommand {
    Line(String),
    Flush(oneshot::Sender<Result<()>>),
    Shutdown(oneshot::Sender<Result<()>>),
}

struct LoggerWorker {
    config: JsonlLoggerConfig,
    writer: Option<BufWriter<tokio::fs::File>>,
    active_bytes: u64,
    unflushed_lines: usize,
}

impl LoggerWorker {
    fn new(config: JsonlLoggerConfig) -> Self {
        Self {
            config,
            writer: None,
            active_bytes: 0,
            unflushed_lines: 0,
        }
    }

    async fn run(mut self, mut receiver: mpsc::Receiver<LoggerCommand>) -> Result<()> {
        self.open_active().await?;
        let mut flush_interval =
            tokio::time::interval(Duration::from_millis(self.config.flush_interval_ms.max(1)));
        flush_interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
        loop {
            tokio::select! {
                biased;
                command = receiver.recv() => {
                    match command {
                        Some(LoggerCommand::Line(line)) => self.write_line(&line).await?,
                        Some(LoggerCommand::Flush(reply)) => {
                            let result = self.flush().await;
                            let _ = reply.send(result);
                        }
                        Some(LoggerCommand::Shutdown(reply)) => {
                            let result = self.flush().await;
                            let _ = reply.send(result);
                            break;
                        }
                        None => {
                            self.flush().await?;
                            break;
                        }
                    }
                }
                _ = flush_interval.tick() => {
                    if let Err(err) = self.flush().await {
                        warn!(error = ?err, "periodic jsonl flush failed");
                    }
                }
            }
        }
        Ok(())
    }

    async fn open_active(&mut self) -> Result<()> {
        let path = self.config.active_path();
        let file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&path)
            .await
            .with_context(|| format!("opening jsonl log {}", path.display()))?;
        let metadata = file
            .metadata()
            .await
            .with_context(|| format!("reading jsonl log metadata {}", path.display()))?;
        self.active_bytes = metadata.len();
        self.writer = Some(BufWriter::new(file));
        info!(path = %path.display(), max_bytes = self.config.max_bytes, max_files = self.config.max_files, "writing jsonl events");
        Ok(())
    }

    async fn write_line(&mut self, line: &str) -> Result<()> {
        let line_bytes = line.as_bytes().len().saturating_add(1) as u64;
        if self.config.max_bytes > 0
            && self.active_bytes > 0
            && self.active_bytes.saturating_add(line_bytes) > self.config.max_bytes
        {
            self.rotate().await?;
        }
        let writer = self.writer.as_mut().context("jsonl writer not open")?;
        writer.write_all(line.as_bytes()).await?;
        writer.write_all(b"\n").await?;
        self.active_bytes = self.active_bytes.saturating_add(line_bytes);
        self.unflushed_lines = self.unflushed_lines.saturating_add(1);
        if self.unflushed_lines >= self.config.flush_batch_lines.max(1) {
            self.flush().await?;
        }
        Ok(())
    }

    async fn flush(&mut self) -> Result<()> {
        if let Some(writer) = &mut self.writer {
            writer.flush().await?;
            self.unflushed_lines = 0;
        }
        Ok(())
    }

    async fn rotate(&mut self) -> Result<()> {
        self.flush().await?;
        self.writer = None;
        rotate_files(&self.config).await?;
        self.active_bytes = 0;
        self.open_active().await
    }
}

async fn rotate_files(config: &JsonlLoggerConfig) -> Result<()> {
    let active = config.active_path();
    if config.max_files <= 1 {
        let _ = tokio::fs::remove_file(&active).await;
        return Ok(());
    }

    let oldest = config
        .log_dir
        .join(format!("events.{}.jsonl", config.max_files - 1));
    let _ = tokio::fs::remove_file(oldest).await;
    for idx in (1..config.max_files - 1).rev() {
        let from = config.log_dir.join(format!("events.{idx}.jsonl"));
        let to = config.log_dir.join(format!("events.{}.jsonl", idx + 1));
        if tokio::fs::metadata(&from).await.is_ok() {
            rename(&from, &to)
                .await
                .with_context(|| format!("rotating {} to {}", from.display(), to.display()))?;
        }
    }
    if tokio::fs::metadata(&active).await.is_ok() {
        let first = config.log_dir.join("events.1.jsonl");
        rename(&active, &first)
            .await
            .with_context(|| format!("rotating {} to {}", active.display(), first.display()))?;
    }
    Ok(())
}

fn is_jsonl_filtered_event(text: &str) -> bool {
    matches!(
        event_type_from_text(text).as_deref(),
        Some("vad_meter") | Some("turn_hold") | Some("audio_frame_ingested")
    )
}

fn event_type_from_text(text: &str) -> Option<String> {
    let Ok(value) = serde_json::from_str::<serde_json::Value>(text) else {
        return None;
    };
    event_type_from_value(&value).map(str::to_owned)
}

pub(crate) fn event_type_from_value(value: &serde_json::Value) -> Option<&str> {
    value
        .get("event")
        .or_else(|| value.get("type"))
        .and_then(|v| v.as_str())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use tempfile::tempdir;

    #[tokio::test]
    async fn logger_preserves_order_across_rotation_and_flush() {
        let dir = tempdir().unwrap();
        let (tx, _) = broadcast::channel(16);
        let logger = JsonlLogger::open_with_config(
            JsonlLoggerConfig {
                log_dir: dir.path().to_path_buf(),
                max_bytes: 70,
                max_files: 8,
                flush_interval_ms: 60_000,
                flush_batch_lines: 100,
            },
            tx,
        )
        .await
        .unwrap();

        for idx in 0..6u64 {
            logger
                .write(&json!({"event":"test_event","seq":idx,"payload":"xxxxxxxxxxxx"}))
                .await
                .unwrap();
        }
        logger.flush().await.unwrap();
        logger.shutdown().await.unwrap();

        let mut observed = Vec::new();
        for name in [
            "events.7.jsonl",
            "events.6.jsonl",
            "events.5.jsonl",
            "events.4.jsonl",
            "events.3.jsonl",
            "events.2.jsonl",
            "events.1.jsonl",
            "events.jsonl",
        ] {
            let path = dir.path().join(name);
            if !path.exists() {
                continue;
            }
            let text = std::fs::read_to_string(path).unwrap();
            for line in text.lines() {
                let value: serde_json::Value = serde_json::from_str(line).unwrap();
                observed.push(value["seq"].as_u64().unwrap());
            }
        }
        assert_eq!(observed, vec![0, 1, 2, 3, 4, 5]);
    }

    #[tokio::test]
    async fn logger_broadcasts_immediately_but_filters_noisy_durable_events() {
        let dir = tempdir().unwrap();
        let (tx, mut rx) = broadcast::channel(16);
        let logger = JsonlLogger::open_with_config(
            JsonlLoggerConfig {
                log_dir: dir.path().to_path_buf(),
                max_bytes: 1024,
                max_files: 2,
                flush_interval_ms: 60_000,
                flush_batch_lines: 100,
            },
            tx,
        )
        .await
        .unwrap();

        logger
            .write(&json!({"type":"audio_frame_ingested","seq":1}))
            .await
            .unwrap();
        assert_eq!(
            serde_json::from_str::<serde_json::Value>(&rx.recv().await.unwrap()).unwrap()["seq"],
            1
        );
        logger.shutdown().await.unwrap();
        let durable = std::fs::read_to_string(dir.path().join("events.jsonl")).unwrap();
        assert!(
            durable.is_empty(),
            "filtered event should not be durable: {durable}"
        );
    }
}
