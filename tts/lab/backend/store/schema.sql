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
