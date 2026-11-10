ALTER TABLE volumes ADD COLUMN comparison_version TEXT;
ALTER TABLE scan_roots ADD COLUMN path_key TEXT COLLATE WINPATH;
ALTER TABLE media_files ADD COLUMN path_key TEXT COLLATE WINPATH;
CREATE UNIQUE INDEX roots_path_key ON scan_roots(volume_id,path_key COLLATE WINPATH);
CREATE UNIQUE INDEX media_path_key ON media_files(volume_id,path_key COLLATE WINPATH);
ALTER TABLE identity_transitions ADD COLUMN confirmation TEXT;
ALTER TABLE identity_transitions ADD COLUMN comparison_version TEXT;
CREATE TABLE scan_policies (
    id INTEGER PRIMARY KEY,
    semantic_digest TEXT NOT NULL,
    canonical_json TEXT NOT NULL,
    config_version INTEGER NOT NULL,
    defaults_version TEXT NOT NULL,
    matcher_version TEXT NOT NULL,
    processing_version TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE scan_exclusions (
    id INTEGER PRIMARY KEY,
    policy_id INTEGER NOT NULL REFERENCES scan_policies(id),
    supplied_path TEXT NOT NULL,
    normalized_path TEXT NOT NULL,
    volume_id INTEGER REFERENCES volumes(id),
    path_key TEXT COLLATE WINPATH,
    match_kind TEXT NOT NULL CHECK(match_kind IN ('directory','file')),
    origin TEXT NOT NULL,
    global_rule_id TEXT,
    active INTEGER NOT NULL CHECK(active IN (0,1))
);
CREATE TRIGGER policies_no_update BEFORE UPDATE ON scan_policies BEGIN SELECT RAISE(ABORT,'Policies are immutable'); END;
CREATE TRIGGER policies_no_delete BEFORE DELETE ON scan_policies BEGIN SELECT RAISE(ABORT,'Policies are immutable'); END;
CREATE TRIGGER exclusions_no_update BEFORE UPDATE ON scan_exclusions BEGIN SELECT RAISE(ABORT,'Policies are immutable'); END;
CREATE TRIGGER exclusions_no_delete BEFORE DELETE ON scan_exclusions BEGIN SELECT RAISE(ABORT,'Policies are immutable'); END;
CREATE TABLE scan_sessions_new (
    id INTEGER PRIMARY KEY,
    root_id INTEGER NOT NULL REFERENCES scan_roots(id),
    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','running','interrupted','blocked','completed','cancelled','superseded')),
    created_at TEXT NOT NULL,
    completed_at TEXT,
    checkpoint_at TEXT,
    discovery_version TEXT NOT NULL,
    config_json TEXT NOT NULL DEFAULT '{}',
    coverage_complete INTEGER NOT NULL DEFAULT 0 CHECK(coverage_complete IN (0,1)),
    policy_id INTEGER REFERENCES scan_policies(id),
    predecessor_id INTEGER REFERENCES scan_sessions(id),
    successor_reason TEXT,
    discovery_state TEXT NOT NULL DEFAULT 'pending' CHECK(discovery_state IN ('pending','running','interrupted','blocked','completed','cancelled','superseded')),
    discovery_completed_at TEXT,
    processing_state TEXT NOT NULL DEFAULT 'not_required' CHECK(processing_state IN ('not_required','pending','running','blocked','completed')),
    processing_completed_at TEXT,
    CHECK(coverage_complete = 0 OR discovery_state = 'completed')
);
INSERT INTO scan_sessions_new(id,root_id,state,created_at,completed_at,checkpoint_at,discovery_version,config_json,coverage_complete,discovery_state,discovery_completed_at) SELECT id,root_id,state,created_at,completed_at,checkpoint_at,discovery_version,config_json,coverage_complete,state,completed_at FROM scan_sessions;
DROP TABLE scan_sessions;
ALTER TABLE scan_sessions_new RENAME TO scan_sessions;
CREATE UNIQUE INDEX one_open_session_per_root ON scan_sessions(root_id) WHERE state NOT IN ('completed','cancelled','superseded');
CREATE TRIGGER exclusions_no_late_insert BEFORE INSERT ON scan_exclusions WHEN EXISTS(SELECT 1 FROM scan_sessions WHERE policy_id=NEW.policy_id) BEGIN SELECT RAISE(ABORT,'Policies are immutable'); END;
ALTER TABLE scan_runs ADD COLUMN invocation_json TEXT;
CREATE TABLE scan_directories_new (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES scan_sessions(id),
    relative_path TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','processing','completed','failed','retry_pending','excluded')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
    claiming_run_id INTEGER REFERENCES scan_runs(id),
    completed_at TEXT,
    path_key TEXT COLLATE WINPATH,
    UNIQUE(session_id,relative_path)
);
INSERT INTO scan_directories_new(id,session_id,relative_path,state,attempts,claiming_run_id,completed_at) SELECT id,session_id,relative_path,state,attempts,claiming_run_id,completed_at FROM scan_directories;
DROP TABLE scan_directories;
ALTER TABLE scan_directories_new RENAME TO scan_directories;
CREATE INDEX scan_directories_queue ON scan_directories(session_id,state);
CREATE UNIQUE INDEX directories_path_key ON scan_directories(session_id,path_key COLLATE WINPATH);
CREATE TABLE scan_files_new (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES scan_sessions(id),
    directory_id INTEGER NOT NULL REFERENCES scan_directories(id),
    relative_path TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','processing','completed','failed','retry_pending','excluded')),
    claiming_run_id INTEGER REFERENCES scan_runs(id),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
    media_id INTEGER REFERENCES media_files(id),
    path_key TEXT COLLATE WINPATH,
    UNIQUE(session_id,relative_path)
);
INSERT INTO scan_files_new(id,session_id,directory_id,relative_path,state,claiming_run_id,attempts,media_id) SELECT id,session_id,directory_id,relative_path,state,claiming_run_id,attempts,media_id FROM scan_files;
DROP TABLE scan_files;
ALTER TABLE scan_files_new RENAME TO scan_files;
CREATE INDEX scan_files_queue ON scan_files(session_id,state);
CREATE UNIQUE INDEX files_path_key ON scan_files(session_id,path_key COLLATE WINPATH);
CREATE TABLE scan_coverage (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES scan_sessions(id),
    volume_relative_path TEXT NOT NULL,
    path_key TEXT NOT NULL COLLATE WINPATH,
    status TEXT NOT NULL CHECK(status IN ('enumerated','excluded','unavailable')),
    rule_id INTEGER REFERENCES scan_exclusions(id),
    error_id INTEGER REFERENCES scan_errors(id),
    observed_at TEXT NOT NULL,
    UNIQUE(session_id,path_key,status)
);
