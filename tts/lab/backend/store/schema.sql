PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL UNIQUE,
    path TEXT NOT NULL,
    suffix TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    sample_rate INTEGER,
    duration_s REAL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pins (
    artifact_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (artifact_id, reason),
    FOREIGN KEY (artifact_id) REFERENCES artifacts(id)
);

CREATE TABLE IF NOT EXISTS cache_entries (
    cache_key TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL,
    processor TEXT NOT NULL,
    config_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (artifact_id) REFERENCES artifacts(id)
);

CREATE TABLE IF NOT EXISTS voices (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    tags_json TEXT NOT NULL,
    source_artifact_id TEXT NOT NULL,
    source_transcript TEXT NOT NULL,
    keep_intervals_json TEXT NOT NULL,
    effective_transcript TEXT NOT NULL,
    active_reference_variant_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (source_artifact_id) REFERENCES artifacts(id)
);

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    voice_id TEXT,
    request_json TEXT NOT NULL,
    output_artifact_id TEXT NOT NULL,
    effective_reference_json TEXT NOT NULL,
    latency_ms REAL NOT NULL,
    first_audio_ms REAL,
    duration_s REAL NOT NULL,
    rating TEXT,
    tags_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (output_artifact_id) REFERENCES artifacts(id)
);
