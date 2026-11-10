ALTER TABLE volumes ADD COLUMN identity_kind TEXT NOT NULL DEFAULT 'unknown' CHECK(identity_kind IN ('unknown','folder','physical'));
ALTER TABLE scan_errors ADD COLUMN message_path TEXT;
CREATE TABLE folder_bindings (
    root_id INTEGER PRIMARY KEY REFERENCES scan_roots(id),
    machine_id TEXT NOT NULL,
    selected_path TEXT NOT NULL,
    device_id TEXT NOT NULL,
    directory_id TEXT NOT NULL,
    original_scope_uuid TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(machine_id, selected_path)
);
CREATE TABLE scan_files (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES scan_sessions(id),
    directory_id INTEGER NOT NULL REFERENCES scan_directories(id),
    relative_path TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','processing','completed','failed','retry_pending')),
    claiming_run_id INTEGER REFERENCES scan_runs(id),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
    media_id INTEGER REFERENCES media_files(id),
    UNIQUE(session_id, relative_path)
);
CREATE INDEX scan_files_queue ON scan_files(session_id, state);
CREATE TABLE identity_transitions (
    id INTEGER PRIMARY KEY,
    root_id INTEGER NOT NULL REFERENCES scan_roots(id),
    source_volume_id INTEGER NOT NULL REFERENCES volumes(id),
    target_volume_id INTEGER NOT NULL REFERENCES volumes(id),
    old_root_relative_path TEXT NOT NULL,
    new_root_relative_path TEXT NOT NULL,
    transitioned_at TEXT NOT NULL
);
CREATE INDEX scan_errors_retry ON scan_errors(session_id, resolved_at, retryable);
