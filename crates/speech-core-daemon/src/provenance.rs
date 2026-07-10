use crate::Args;
use serde::Serialize;
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::fs::File;
use std::io::{Read, Seek, SeekFrom};
use std::path::{Path, PathBuf};
use std::time::UNIX_EPOCH;

pub(crate) const EVENT_SCHEMA_VERSION: &str = "speech-core.events.v1";
const SMALL_FILE_FULL_HASH_LIMIT: u64 = 64 * 1024 * 1024;
const LARGE_FILE_EDGE_BYTES: usize = 1024 * 1024;
const MAX_DIR_FILES_HASHED: usize = 512;

#[derive(Debug, Clone, Serialize)]
pub(crate) struct RuntimeProvenanceEvent {
    pub event: &'static str,
    pub event_schema_version: &'static str,
    pub runtime_id: String,
    pub build_id: String,
    pub config_id: String,
    pub binary: BinaryInfo,
    pub source: SourceInfo,
    pub build: BuildInfo,
    pub config: EffectiveConfig,
    pub models: Vec<ModelArtifactProvenance>,
}

impl RuntimeProvenanceEvent {
    pub(crate) fn collect(args: &Args) -> Self {
        let redacted = redacted_config(args);
        let config_fingerprint = fingerprint_json_value(&redacted);
        let build = BuildInfo::current();
        let build_id = build.concise_id();
        let config_id = short_fingerprint(&config_fingerprint);
        let runtime_id = format!("{build_id}/cfg:{config_id}");
        Self {
            event: "runtime_provenance",
            event_schema_version: EVENT_SCHEMA_VERSION,
            runtime_id,
            build_id,
            config_id,
            binary: BinaryInfo::current(),
            source: SourceInfo::current(),
            build,
            config: EffectiveConfig {
                fingerprint: config_fingerprint,
                redacted,
            },
            models: model_artifacts(args),
        }
    }
}

#[derive(Debug, Clone, Serialize)]
pub(crate) struct BinaryInfo {
    pub name: &'static str,
    pub version: &'static str,
}

impl BinaryInfo {
    fn current() -> Self {
        Self {
            name: env!("CARGO_PKG_NAME"),
            version: env!("CARGO_PKG_VERSION"),
        }
    }
}

#[derive(Debug, Clone, Serialize)]
pub(crate) struct SourceInfo {
    pub repository: &'static str,
}

impl SourceInfo {
    fn current() -> Self {
        Self {
            repository: env!("CARGO_PKG_REPOSITORY"),
        }
    }
}

#[derive(Debug, Clone, Serialize)]
pub(crate) struct BuildInfo {
    pub git_commit: &'static str,
    pub git_dirty: &'static str,
    pub target: &'static str,
    pub profile: &'static str,
}

impl BuildInfo {
    fn current() -> Self {
        Self {
            git_commit: option_env!("SPEECH_CORE_BUILD_GIT_COMMIT").unwrap_or("unknown"),
            git_dirty: option_env!("SPEECH_CORE_BUILD_GIT_DIRTY").unwrap_or("unknown"),
            target: option_env!("SPEECH_CORE_BUILD_TARGET").unwrap_or("unknown"),
            profile: option_env!("SPEECH_CORE_BUILD_PROFILE").unwrap_or("unknown"),
        }
    }

    fn concise_id(&self) -> String {
        let short = self.git_commit.get(..12).unwrap_or(self.git_commit);
        match self.git_dirty {
            "true" => format!("git:{short}+dirty"),
            "false" => format!("git:{short}"),
            _ => format!("git:{short}+unknown"),
        }
    }
}

#[derive(Debug, Clone, Serialize)]
pub(crate) struct EffectiveConfig {
    pub fingerprint: String,
    pub redacted: Value,
}

#[derive(Debug, Clone, Serialize)]
pub(crate) struct ModelArtifactProvenance {
    pub role: &'static str,
    pub path: String,
    pub kind: String,
    pub fingerprint: Option<String>,
    pub fingerprint_method: String,
    pub size_bytes: Option<u64>,
    pub modified_unix_ms: Option<u128>,
    pub status: String,
}

fn model_artifacts(args: &Args) -> Vec<ModelArtifactProvenance> {
    let mut artifacts = Vec::new();
    push_artifact(&mut artifacts, "asr", args.model_path.as_ref());
    push_artifact(&mut artifacts, "vad", args.vad_model_path.as_ref());
    push_artifact(&mut artifacts, "eou", args.eou_model_dir.as_ref());
    push_artifact(
        &mut artifacts,
        "smart_turn",
        args.smart_turn_model_path.as_ref(),
    );
    artifacts
}

fn push_artifact(
    artifacts: &mut Vec<ModelArtifactProvenance>,
    role: &'static str,
    path: Option<&PathBuf>,
) {
    if let Some(path) = path.filter(|p| !p.as_os_str().is_empty()) {
        artifacts.push(fingerprint_artifact(role, path));
    }
}

fn fingerprint_artifact(role: &'static str, path: &Path) -> ModelArtifactProvenance {
    match std::fs::metadata(path) {
        Ok(metadata) if metadata.is_file() => fingerprint_file(role, path, &metadata),
        Ok(metadata) if metadata.is_dir() => fingerprint_dir(role, path, &metadata),
        Ok(metadata) => ModelArtifactProvenance {
            role,
            path: path.display().to_string(),
            kind: "other".to_owned(),
            fingerprint: None,
            fingerprint_method: "unsupported".to_owned(),
            size_bytes: Some(metadata.len()),
            modified_unix_ms: modified_unix_ms(&metadata),
            status: "unsupported_artifact_kind".to_owned(),
        },
        Err(err) => ModelArtifactProvenance {
            role,
            path: path.display().to_string(),
            kind: "missing".to_owned(),
            fingerprint: None,
            fingerprint_method: "unavailable".to_owned(),
            size_bytes: None,
            modified_unix_ms: None,
            status: format!("metadata_error:{err}"),
        },
    }
}

fn fingerprint_file(
    role: &'static str,
    path: &Path,
    metadata: &std::fs::Metadata,
) -> ModelArtifactProvenance {
    let modified = modified_unix_ms(metadata);
    let mut hasher = Fnv1a64::default();
    hasher.update(
        path.file_name()
            .and_then(|s| s.to_str())
            .unwrap_or("")
            .as_bytes(),
    );
    hasher.update(&metadata.len().to_le_bytes());
    if let Some(ms) = modified {
        hasher.update(&ms.to_le_bytes());
    }
    let method = if metadata.len() <= SMALL_FILE_FULL_HASH_LIMIT {
        match File::open(path).and_then(|mut file| hash_reader(&mut file, &mut hasher)) {
            Ok(()) => "fnv1a64:file_full+metadata".to_owned(),
            Err(err) => {
                return ModelArtifactProvenance {
                    role,
                    path: path.display().to_string(),
                    kind: "file".to_owned(),
                    fingerprint: None,
                    fingerprint_method: "fnv1a64:file_full+metadata".to_owned(),
                    size_bytes: Some(metadata.len()),
                    modified_unix_ms: modified,
                    status: format!("read_error:{err}"),
                }
            }
        }
    } else {
        match hash_large_file_edges(path, metadata.len(), &mut hasher) {
            Ok(()) => "fnv1a64:file_edges_1m+metadata".to_owned(),
            Err(err) => {
                return ModelArtifactProvenance {
                    role,
                    path: path.display().to_string(),
                    kind: "file".to_owned(),
                    fingerprint: None,
                    fingerprint_method: "fnv1a64:file_edges_1m+metadata".to_owned(),
                    size_bytes: Some(metadata.len()),
                    modified_unix_ms: modified,
                    status: format!("read_error:{err}"),
                }
            }
        }
    };

    ModelArtifactProvenance {
        role,
        path: path.display().to_string(),
        kind: "file".to_owned(),
        fingerprint: Some(format!("fnv1a64:{:016x}", hasher.finish())),
        fingerprint_method: method,
        size_bytes: Some(metadata.len()),
        modified_unix_ms: modified,
        status: "ok".to_owned(),
    }
}

fn fingerprint_dir(
    role: &'static str,
    path: &Path,
    metadata: &std::fs::Metadata,
) -> ModelArtifactProvenance {
    let mut entries = match collect_dir_files(path) {
        Ok(entries) => entries,
        Err(err) => {
            return ModelArtifactProvenance {
                role,
                path: path.display().to_string(),
                kind: "directory".to_owned(),
                fingerprint: None,
                fingerprint_method: "fnv1a64:dir_file_metadata+small_contents".to_owned(),
                size_bytes: None,
                modified_unix_ms: modified_unix_ms(metadata),
                status: format!("read_dir_error:{err}"),
            }
        }
    };
    entries.sort();
    let truncated = entries.len() > MAX_DIR_FILES_HASHED;
    entries.truncate(MAX_DIR_FILES_HASHED);

    let mut hasher = Fnv1a64::default();
    let mut total_size = 0u64;
    for file in &entries {
        let rel = file.strip_prefix(path).unwrap_or(file);
        hasher.update(rel.to_string_lossy().as_bytes());
        if let Ok(file_meta) = std::fs::metadata(file) {
            total_size = total_size.saturating_add(file_meta.len());
            hasher.update(&file_meta.len().to_le_bytes());
            if file_meta.len() <= 1024 * 1024 {
                if let Ok(mut fh) = File::open(file) {
                    let _ = hash_reader(&mut fh, &mut hasher);
                }
            }
        }
    }
    hasher.update(&(truncated as u8).to_le_bytes());

    ModelArtifactProvenance {
        role,
        path: path.display().to_string(),
        kind: "directory".to_owned(),
        fingerprint: Some(format!("fnv1a64:{:016x}", hasher.finish())),
        fingerprint_method: if truncated {
            "fnv1a64:dir_first_512_file_metadata+small_contents".to_owned()
        } else {
            "fnv1a64:dir_file_metadata+small_contents".to_owned()
        },
        size_bytes: Some(total_size),
        modified_unix_ms: modified_unix_ms(metadata),
        status: if truncated { "truncated" } else { "ok" }.to_owned(),
    }
}

fn collect_dir_files(path: &Path) -> std::io::Result<Vec<PathBuf>> {
    let mut out = Vec::new();
    let mut stack = vec![path.to_path_buf()];
    while let Some(dir) = stack.pop() {
        for entry in std::fs::read_dir(dir)? {
            let entry = entry?;
            let entry_path = entry.path();
            let metadata = entry.metadata()?;
            if metadata.is_dir() {
                stack.push(entry_path);
            } else if metadata.is_file() {
                out.push(entry_path);
            }
        }
    }
    Ok(out)
}

fn hash_reader(reader: &mut impl Read, hasher: &mut Fnv1a64) -> std::io::Result<()> {
    let mut buf = [0u8; 8192];
    loop {
        let n = reader.read(&mut buf)?;
        if n == 0 {
            return Ok(());
        }
        hasher.update(&buf[..n]);
    }
}

fn hash_large_file_edges(path: &Path, len: u64, hasher: &mut Fnv1a64) -> std::io::Result<()> {
    let mut file = File::open(path)?;
    let mut first = vec![0u8; LARGE_FILE_EDGE_BYTES.min(len as usize)];
    file.read_exact(&mut first)?;
    hasher.update(&first);
    if len > LARGE_FILE_EDGE_BYTES as u64 {
        let tail_len = LARGE_FILE_EDGE_BYTES.min(len as usize);
        file.seek(SeekFrom::End(-(tail_len as i64)))?;
        let mut tail = vec![0u8; tail_len];
        file.read_exact(&mut tail)?;
        hasher.update(&tail);
    }
    Ok(())
}

fn modified_unix_ms(metadata: &std::fs::Metadata) -> Option<u128> {
    metadata
        .modified()
        .ok()?
        .duration_since(UNIX_EPOCH)
        .ok()
        .map(|duration| duration.as_millis())
}

fn redacted_config(args: &Args) -> Value {
    json!({
        "bind": args.bind.to_string(),
        "log_dir": redact_path(&args.log_dir),
        "log_max_bytes": args.log_max_bytes,
        "log_max_files": args.log_max_files,
        "log_flush_interval_ms": args.log_flush_interval_ms,
        "log_flush_batch_lines": args.log_flush_batch_lines,
        "model_path": option_redacted_path(args.model_path.as_ref()),
        "stream_chunk_ms": args.stream_chunk_ms,
        "att_context_right": args.att_context_right,
        "model_queue_frames": args.model_queue_frames,
        "vad_model_path": option_redacted_path(args.vad_model_path.as_ref()),
        "vad_threshold": args.vad_threshold,
        "vad_onset_frames": args.vad_onset_frames,
        "vad_hangover_frames": args.vad_hangover_frames,
        "vad_pre_speech_frames": args.vad_pre_speech_frames,
        "vad_emit_frames": args.vad_emit_frames,
        "vad_smoothing_alpha": args.vad_smoothing_alpha,
        "vad_stop_threshold": args.vad_stop_threshold,
        "vad_fallback_threshold": args.vad_fallback_threshold,
        "vad_acoustic_fallback_silence_ms": args.vad_acoustic_fallback_silence_ms,
        "vad_energy_enabled": args.vad_energy_enabled,
        "vad_energy_threshold": args.vad_energy_threshold,
        "eou_model_dir": option_redacted_path(args.eou_model_dir.as_ref()),
        "smart_turn_model_path": option_redacted_path(args.smart_turn_model_path.as_ref()),
        "smart_turn_threshold": args.smart_turn_threshold,
        "smart_turn_timeout_ms": args.smart_turn_timeout_ms,
        "smart_turn_cpu_count": args.smart_turn_cpu_count,
        "smart_turn_max_audio_secs": args.smart_turn_max_audio_secs,
        "smart_turn_pre_speech_ms": args.smart_turn_pre_speech_ms,
        "smart_turn_recheck_interval_ms": args.smart_turn_recheck_interval_ms,
        "smart_turn_recheck_max_attempts": args.smart_turn_recheck_max_attempts,
        "smart_turn_recheck_offsets_ms": args.smart_turn_recheck_offsets_ms,
        "eou_chunk_ms": args.eou_chunk_ms,
        "eou_reset_on_token": args.eou_reset_on_token,
        "eou_emit_transcript": args.eou_emit_transcript,
        "detector_queue_frames": args.detector_queue_frames,
        "turn_vad_close_enabled": args.turn_vad_close_enabled,
        "turn_semantic_gate_enabled": args.turn_semantic_gate_enabled,
        "turn_semantic_gate_close_enabled": args.turn_semantic_gate_close_enabled,
        "turn_model_eou_close_enabled": args.turn_model_eou_close_enabled,
        "turn_min_vad_speech_ms": args.turn_min_vad_speech_ms,
        "turn_human_hold_silence_ms": args.turn_human_hold_silence_ms,
        "turn_transcript_silence_close_ms": args.turn_transcript_silence_close_ms,
        "turn_min_model_eou_speech_ms": args.turn_min_model_eou_speech_ms,
        "turn_model_alignment_timeout_ms": args.turn_model_alignment_timeout_ms,
        "turn_model_eou_refractory_ms": args.turn_model_eou_refractory_ms,
    })
}

fn option_redacted_path(path: Option<&PathBuf>) -> Option<String> {
    path.map(|p| redact_path(p.as_path()))
}

fn redact_path(path: &Path) -> String {
    let home = std::env::var_os("HOME").map(PathBuf::from);
    redact_path_for_home(path, home.as_deref())
}

fn redact_path_for_home(path: &Path, home: Option<&Path>) -> String {
    if let Some(home) = home.filter(|home| !home.as_os_str().is_empty()) {
        if let Ok(rest) = path.strip_prefix(home) {
            if rest.as_os_str().is_empty() {
                return "~".to_owned();
            }
            return format!("~/{}", rest.display());
        }
    }
    path.display().to_string()
}

fn fingerprint_json_value(value: &Value) -> String {
    let mut canonical = String::new();
    canonical_json(value, &mut canonical);
    format!("fnv1a64:{:016x}", fnv1a64(canonical.as_bytes()))
}

fn short_fingerprint(fingerprint: &str) -> String {
    fingerprint
        .rsplit_once(':')
        .map(|(_, hex)| hex)
        .unwrap_or(fingerprint)
        .chars()
        .take(12)
        .collect()
}

fn canonical_json(value: &Value, out: &mut String) {
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(v) => out.push_str(if *v { "true" } else { "false" }),
        Value::Number(v) => out.push_str(&v.to_string()),
        Value::String(v) => out.push_str(&serde_json::to_string(v).expect("string serialization")),
        Value::Array(values) => {
            out.push('[');
            for (idx, item) in values.iter().enumerate() {
                if idx > 0 {
                    out.push(',');
                }
                canonical_json(item, out);
            }
            out.push(']');
        }
        Value::Object(map) => {
            out.push('{');
            let sorted: BTreeMap<_, _> = map.iter().collect();
            for (idx, (key, item)) in sorted.iter().enumerate() {
                if idx > 0 {
                    out.push(',');
                }
                out.push_str(&serde_json::to_string(key).expect("key serialization"));
                out.push(':');
                canonical_json(item, out);
            }
            out.push('}');
        }
    }
}

#[derive(Default)]
struct Fnv1a64(u64);

impl Fnv1a64 {
    fn update(&mut self, bytes: &[u8]) {
        if self.0 == 0 {
            self.0 = FNV_OFFSET;
        }
        for byte in bytes {
            self.0 ^= u64::from(*byte);
            self.0 = self.0.wrapping_mul(FNV_PRIME);
        }
    }

    fn finish(mut self) -> u64 {
        if self.0 == 0 {
            self.0 = FNV_OFFSET;
        }
        self.0
    }
}

const FNV_OFFSET: u64 = 0xcbf29ce484222325;
const FNV_PRIME: u64 = 0x00000100000001b3;

fn fnv1a64(bytes: &[u8]) -> u64 {
    let mut hasher = Fnv1a64::default();
    hasher.update(bytes);
    hasher.finish()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use tempfile::tempdir;

    #[test]
    fn config_fingerprint_is_stable_for_object_key_order() {
        let a = json!({"b": [2, 3], "a": {"x": true, "y": null}});
        let b = json!({"a": {"y": null, "x": true}, "b": [2, 3]});
        assert_eq!(fingerprint_json_value(&a), fingerprint_json_value(&b));
    }

    #[test]
    fn path_redaction_replaces_home_prefix_only() {
        let home = Path::new("/home/alice");
        assert_eq!(
            redact_path_for_home(Path::new("/home/alice/models/a.gguf"), Some(home)),
            "~/models/a.gguf"
        );
        assert_eq!(
            redact_path_for_home(Path::new("/srv/models/a.gguf"), Some(home)),
            "/srv/models/a.gguf"
        );
    }

    #[test]
    fn artifact_fingerprint_changes_with_contents() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("model.onnx");
        std::fs::write(&path, b"first").unwrap();
        let first = fingerprint_artifact("vad", &path).fingerprint.unwrap();
        std::fs::write(&path, b"second").unwrap();
        let second = fingerprint_artifact("vad", &path).fingerprint.unwrap();
        assert_ne!(first, second);
    }

    #[test]
    fn runtime_provenance_serializes_as_backward_compatible_event() {
        let config = json!({"model_path": "~/model.gguf"});
        let event = RuntimeProvenanceEvent {
            event: "runtime_provenance",
            event_schema_version: EVENT_SCHEMA_VERSION,
            runtime_id: "git:abc/cfg:def".to_owned(),
            build_id: "git:abc".to_owned(),
            config_id: "def".to_owned(),
            binary: BinaryInfo::current(),
            source: SourceInfo::current(),
            build: BuildInfo::current(),
            config: EffectiveConfig {
                fingerprint: fingerprint_json_value(&config),
                redacted: config,
            },
            models: Vec::new(),
        };
        let value = serde_json::to_value(event).unwrap();
        assert_eq!(
            value.get("event").and_then(Value::as_str),
            Some("runtime_provenance")
        );
        assert_eq!(
            value.get("event_schema_version").and_then(Value::as_str),
            Some(EVENT_SCHEMA_VERSION)
        );
        assert!(value
            .get("runtime_id")
            .and_then(Value::as_str)
            .unwrap()
            .contains("cfg"));
    }
}
