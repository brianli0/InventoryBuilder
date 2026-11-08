CREATE TABLE volumes (
    id INTEGER PRIMARY KEY,
    application_uuid TEXT NOT NULL UNIQUE,
    label TEXT,
    filesystem TEXT,
    capacity_bytes INTEGER CHECK(capacity_bytes >= 0),
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);
CREATE TABLE volume_observations (
    id INTEGER PRIMARY KEY,
    volume_id INTEGER NOT NULL REFERENCES volumes(id),
    machine_id TEXT NOT NULL,
    volume_guid TEXT,
    filesystem_serial TEXT,
    mount_path TEXT NOT NULL,
    drive_letter TEXT,
    capacity_bytes INTEGER CHECK(capacity_bytes >= 0),
    observed_at TEXT NOT NULL
);
CREATE INDEX volume_observations_identity ON volume_observations(volume_guid, filesystem_serial);
CREATE INDEX volume_observations_volume ON volume_observations(volume_id);
CREATE TABLE scan_roots (
    id INTEGER PRIMARY KEY,
    volume_id INTEGER NOT NULL REFERENCES volumes(id),
    volume_relative_path TEXT NOT NULL,
    last_absolute_path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(volume_id, volume_relative_path)
);
CREATE TABLE scan_sessions (
    id INTEGER PRIMARY KEY,
    root_id INTEGER NOT NULL REFERENCES scan_roots(id),
    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','running','interrupted','blocked','completed','cancelled')),
    created_at TEXT NOT NULL,
    completed_at TEXT,
    checkpoint_at TEXT,
    discovery_version TEXT NOT NULL,
    config_json TEXT NOT NULL DEFAULT '{}',
    coverage_complete INTEGER NOT NULL DEFAULT 0 CHECK(coverage_complete IN (0,1)),
    CHECK(coverage_complete = 0 OR state = 'completed')
);
CREATE UNIQUE INDEX one_open_session_per_root ON scan_sessions(root_id) WHERE state NOT IN ('completed','cancelled');
CREATE TABLE scan_runs (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES scan_sessions(id),
    started_at TEXT NOT NULL,
    ended_at TEXT,
    state TEXT NOT NULL CHECK(state IN ('running','interrupted','failed','completed')),
    application_version TEXT NOT NULL,
    machine_id TEXT NOT NULL,
    termination_reason TEXT,
    files_discovered INTEGER NOT NULL DEFAULT 0 CHECK(files_discovered >= 0),
    files_processed INTEGER NOT NULL DEFAULT 0 CHECK(files_processed >= 0),
    files_skipped INTEGER NOT NULL DEFAULT 0 CHECK(files_skipped >= 0),
    files_failed INTEGER NOT NULL DEFAULT 0 CHECK(files_failed >= 0),
    unsupported_files INTEGER NOT NULL DEFAULT 0 CHECK(unsupported_files >= 0),
    bytes_inventoried INTEGER NOT NULL DEFAULT 0 CHECK(bytes_inventoried >= 0)
);
CREATE INDEX scan_runs_session ON scan_runs(session_id);
CREATE TABLE scan_directories (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES scan_sessions(id),
    relative_path TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','processing','completed','failed','retry_pending')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
    claiming_run_id INTEGER REFERENCES scan_runs(id),
    completed_at TEXT,
    UNIQUE(session_id, relative_path)
);
CREATE INDEX scan_directories_queue ON scan_directories(session_id, state);
CREATE TABLE media_files (
    id INTEGER PRIMARY KEY,
    volume_id INTEGER NOT NULL REFERENCES volumes(id),
    volume_relative_path TEXT NOT NULL,
    last_absolute_path TEXT NOT NULL,
    parent_path TEXT NOT NULL,
    filename TEXT NOT NULL,
    extension TEXT NOT NULL,
    media_type TEXT NOT NULL CHECK(media_type IN ('photo','video')),
    size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
    created_ns INTEGER,
    modified_ns INTEGER NOT NULL,
    first_inventoried_at TEXT NOT NULL,
    last_inventoried_at TEXT NOT NULL,
    UNIQUE(volume_id, volume_relative_path)
);
CREATE INDEX media_files_size ON media_files(size_bytes);
CREATE TABLE scan_file_observations (
    session_id INTEGER NOT NULL REFERENCES scan_sessions(id),
    media_id INTEGER NOT NULL REFERENCES media_files(id),
    root_relative_path TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
    modified_ns INTEGER NOT NULL,
    observed_at TEXT NOT NULL,
    PRIMARY KEY(session_id, media_id)
);
CREATE INDEX scan_file_observations_media ON scan_file_observations(media_id);
CREATE TABLE media_metadata (
    media_id INTEGER PRIMARY KEY REFERENCES media_files(id),
    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','processing','completed','failed','retry_pending','unsupported')),
    processed_size_bytes INTEGER CHECK(processed_size_bytes >= 0),
    processed_modified_ns INTEGER,
    extractor TEXT,
    extractor_version TEXT,
    extraction_policy_version TEXT,
    extracted_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
    retry_at TEXT,
    claiming_run_id INTEGER REFERENCES scan_runs(id),
    selected_capture_datetime TEXT,
    selected_capture_date TEXT,
    selected_date_source TEXT,
    date_confidence TEXT,
    date_policy_version TEXT,
    original_dates_json TEXT,
    attributes_json TEXT,
    raw_metadata_json TEXT,
    CHECK(state != 'completed' OR (processed_size_bytes IS NOT NULL AND processed_modified_ns IS NOT NULL AND extractor IS NOT NULL AND extractor_version IS NOT NULL AND extraction_policy_version IS NOT NULL AND extracted_at IS NOT NULL))
);
CREATE INDEX media_metadata_state ON media_metadata(state, retry_at);
CREATE INDEX media_metadata_date ON media_metadata(selected_capture_date);
CREATE TABLE scan_errors (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES scan_sessions(id),
    run_id INTEGER REFERENCES scan_runs(id),
    media_id INTEGER REFERENCES media_files(id),
    directory_id INTEGER REFERENCES scan_directories(id),
    operation TEXT NOT NULL,
    category TEXT NOT NULL,
    error_code TEXT,
    message TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    retryable INTEGER NOT NULL DEFAULT 0 CHECK(retryable IN (0,1)),
    attempt INTEGER NOT NULL DEFAULT 1 CHECK(attempt > 0),
    next_retry_at TEXT,
    resolved_at TEXT
);
CREATE INDEX scan_errors_session ON scan_errors(session_id, resolved_at);
