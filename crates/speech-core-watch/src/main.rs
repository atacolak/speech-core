use anyhow::{Context, Result};
use clap::{Parser, ValueEnum};
use futures_util::{SinkExt, StreamExt};
use speech_core_protocol::ControlMessage;
use tokio_tungstenite::connect_async;
use tokio_tungstenite::tungstenite::Message;

#[derive(Debug, Parser)]
#[command(author, version, about = "watch live speech-core daemon events")]
struct Args {
    /// Websocket URL for daemon audio ingress/events endpoint.
    #[arg(
        long,
        default_value = "ws://127.0.0.1:8765/ws/audio-ingress",
        env = "SPEECH_CORE_WS_URL"
    )]
    url: String,

    /// Only show events for this stream id.
    #[arg(long, env = "SPEECH_CORE_STREAM_ID")]
    stream_id: Option<String>,

    /// Only show events for this stream session id.
    #[arg(long, env = "SPEECH_CORE_STREAM_SESSION_ID")]
    stream_session_id: Option<String>,

    /// Print mode.
    #[arg(long, value_enum, default_value_t = Mode::Transcript)]
    mode: Mode,

    /// Also print model timing/debug events in transcript mode.
    #[arg(long)]
    verbose: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, ValueEnum)]
enum Mode {
    /// Show the live committed transcript as it changes, plus final text.
    Transcript,
    /// Print matching JSONL events exactly as received.
    Jsonl,
}

#[tokio::main]
async fn main() -> Result<()> {
    let args = Args::parse();
    let (mut ws, _) = connect_async(&args.url)
        .await
        .with_context(|| format!("connecting to {}", args.url))?;

    let subscribe = ControlMessage::SubscribeEvents {
        stream_id: args.stream_id.clone(),
        stream_session_id: args.stream_session_id.clone(),
        event: None,
    };
    ws.send(Message::Text(serde_json::to_string(&subscribe)?))
        .await?;

    let mut last_text = String::new();
    let mut final_text = String::new();

    while let Some(msg) = ws.next().await {
        match msg? {
            Message::Text(text) => match args.mode {
                Mode::Jsonl => println!("{text}"),
                Mode::Transcript => {
                    let value: serde_json::Value = match serde_json::from_str(&text) {
                        Ok(value) => value,
                        Err(_) => continue,
                    };
                    let event = value
                        .get("event")
                        .or_else(|| value.get("type"))
                        .and_then(|v| v.as_str())
                        .unwrap_or("");
                    match event {
                        "transcript_update" => {
                            let committed = value
                                .get("committed_text")
                                .and_then(|v| v.as_str())
                                .unwrap_or("");
                            let tentative = value
                                .get("tentative_text")
                                .and_then(|v| v.as_str())
                                .unwrap_or("");
                            let display = if tentative.is_empty() {
                                committed.to_owned()
                            } else {
                                format!("{committed}{tentative}")
                            };
                            if display != last_text {
                                last_text = display.clone();
                                if !display.is_empty() {
                                    println!("{display}");
                                }
                            }
                            final_text = committed.to_owned();
                        }
                        "model_chunk_processed" if args.verbose => {
                            let input_ms = value
                                .get("input_received_ms")
                                .and_then(|v| v.as_i64())
                                .unwrap_or_default();
                            let committed_ms = value
                                .get("audio_committed_ms")
                                .and_then(|v| v.as_i64())
                                .unwrap_or_default();
                            let buffered_ms = value
                                .get("buffered_ms")
                                .and_then(|v| v.as_i64())
                                .unwrap_or_default();
                            let is_final = value
                                .get("is_final")
                                .and_then(|v| v.as_bool())
                                .unwrap_or(false);
                            eprintln!(
                                "model chunk: input={input_ms}ms committed={committed_ms}ms buffered={buffered_ms}ms final={is_final}"
                            );
                        }
                        "model_error" => eprintln!("model error: {value}"),
                        "audio_gap" | "audio_sample_gap" => eprintln!("gap: {value}"),
                        _ => {}
                    }
                }
            },
            Message::Close(_) => break,
            _ => {}
        }
    }

    if args.mode == Mode::Transcript && !final_text.is_empty() {
        eprintln!("\nfinal transcript:\n{final_text}");
    }

    Ok(())
}
