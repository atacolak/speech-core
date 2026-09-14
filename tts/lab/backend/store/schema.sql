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
    original_artifact_id TEXT,
    original_format TEXT,
    notes TEXT,
    generation_json TEXT,
    take_limit INTEGER NOT NULL DEFAULT 5,
    source_words_json TEXT,
    transcript_locked INTEGER NOT NULL DEFAULT 0,
    speaker_analysis_id TEXT,
    default_reference_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (source_artifact_id) REFERENCES artifacts(id)
);

CREATE TABLE IF NOT EXISTS voice_sources (
    id TEXT PRIMARY KEY,
    voice_id TEXT NOT NULL,
    label TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    transcript TEXT NOT NULL DEFAULT '',
    words_json TEXT,
    keep_intervals_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    FOREIGN KEY (voice_id) REFERENCES voices(id),
    FOREIGN KEY (artifact_id) REFERENCES artifacts(id)
);

CREATE TABLE IF NOT EXISTS voice_artifacts (
    id TEXT PRIMARY KEY,
    voice_id TEXT NOT NULL,
    role TEXT NOT NULL,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    audio_artifact_id TEXT NOT NULL,
    parent_id TEXT,
    source_id TEXT,
    keep_intervals_json TEXT,
    processor_config_json TEXT,
    processor_cache_key TEXT,
    auk_task TEXT,
    instruction TEXT,
    model_variant TEXT,
    auk_precision TEXT,
    encoder_precision TEXT,
    seed INTEGER,
    settings_json TEXT,
    approved_at TEXT,
    tags_json TEXT NOT NULL DEFAULT '[]',
    is_default INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    FOREIGN KEY (voice_id) REFERENCES voices(id),
    FOREIGN KEY (audio_artifact_id) REFERENCES artifacts(id),
    FOREIGN KEY (parent_id) REFERENCES voice_artifacts(id),
    FOREIGN KEY (source_id) REFERENCES voice_sources(id)
);

CREATE TABLE IF NOT EXISTS breeze_auditions (
    id TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL,
    text TEXT NOT NULL,
    steer TEXT NOT NULL,
    generation_json TEXT,
    output_artifact_id TEXT,
    created_at TEXT NOT NULL,
    pin INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (artifact_id) REFERENCES voice_artifacts(id),
    FOREIGN KEY (output_artifact_id) REFERENCES artifacts(id)
);

-- Migrate window: voice_artifacts is authoritative for experiments/references,
-- reference_variants is the legacy mirror (dual-written until t3/t4 cut over).
CREATE TABLE IF NOT EXISTS reference_variants (
    id TEXT PRIMARY KEY,
    voice_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    audio_artifact_id TEXT NOT NULL,
    processor_config_json TEXT,
    processor_cache_key TEXT,
    duration_s REAL NOT NULL,
    pinned INTEGER NOT NULL DEFAULT 0,
    parent_variant_id TEXT,
    auk_task TEXT,
    instruction TEXT,
    model_variant TEXT,
    auk_precision TEXT,
    encoder_precision TEXT,
    seed INTEGER,
    settings_json TEXT,
    approved INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    FOREIGN KEY (voice_id) REFERENCES voices(id),
    FOREIGN KEY (audio_artifact_id) REFERENCES artifacts(id)
);

CREATE TABLE IF NOT EXISTS speaker_analyses (
    id TEXT PRIMARY KEY,
    voice_id TEXT NOT NULL,
    source_artifact_id TEXT NOT NULL,
    processor TEXT NOT NULL,
    model_id TEXT,
    model_revision TEXT,
    config_json TEXT,
    cache_key TEXT,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (voice_id) REFERENCES voices(id),
    FOREIGN KEY (source_artifact_id) REFERENCES artifacts(id)
);

-- Source bench: a media source is first-class, not a child of one voice.
-- Voices collect clips extracted from it. Speaker ids are source-local.
CREATE TABLE IF NOT EXISTS media_sources (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('file', 'youtube')),
    origin TEXT NOT NULL,
    title TEXT NOT NULL,
    audio_artifact_id TEXT NOT NULL,
    waveform_artifact_id TEXT,
    duration_s REAL,
    meta_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY (audio_artifact_id) REFERENCES artifacts(id),
    FOREIGN KEY (waveform_artifact_id) REFERENCES artifacts(id)
);

-- One analysis per analyzed range: coverage is the union of these ranges,
-- so a covered range is never decoded twice.
CREATE TABLE IF NOT EXISTS source_analyses (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    start_s REAL NOT NULL,
    end_s REAL NOT NULL,
    processor TEXT NOT NULL,
    model_id TEXT,
    config_json TEXT,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (source_id) REFERENCES media_sources(id)
);

CREATE TABLE IF NOT EXISTS source_speakers (
    source_id TEXT NOT NULL,
    local_id TEXT NOT NULL,
    label TEXT NOT NULL,
    duration_s REAL,
    mapped_voice_id TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (source_id, local_id),
    FOREIGN KEY (source_id) REFERENCES media_sources(id),
    FOREIGN KEY (mapped_voice_id) REFERENCES voices(id)
);

-- A clip is the non-overlapping turns one source-local speaker owns inside the
-- operator's ranges. `voice_id` is the mapping that claimed it at extract.
CREATE TABLE IF NOT EXISTS clips (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    speaker_local_id TEXT NOT NULL,
    voice_id TEXT,
    ranges_json TEXT NOT NULL,
    segments_json TEXT NOT NULL DEFAULT '[]',
    audio_artifact_id TEXT NOT NULL,
    clean_transcript TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY (source_id, speaker_local_id) REFERENCES source_speakers(source_id, local_id),
    FOREIGN KEY (voice_id) REFERENCES voices(id),
    FOREIGN KEY (audio_artifact_id) REFERENCES artifacts(id)
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
