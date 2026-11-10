CREATE TABLE file_revisions (
    id INTEGER PRIMARY KEY,
    media_id INTEGER NOT NULL REFERENCES media_files(id),
    size_bytes INTEGER NOT NULL CHECK(size_bytes>=0),
    modified_ns INTEGER NOT NULL,
    device_id TEXT NOT NULL,
    file_id TEXT NOT NULL,
    created_ns INTEGER,
    observed_at TEXT NOT NULL,
    UNIQUE(media_id,size_bytes,modified_ns,device_id,file_id)
);
ALTER TABLE media_files ADD COLUMN current_revision_id INTEGER REFERENCES file_revisions(id);
CREATE TABLE processing_jobs (
    id INTEGER PRIMARY KEY,
    revision_id INTEGER NOT NULL REFERENCES file_revisions(id),
    stage TEXT NOT NULL CHECK(stage IN ('metadata','photo_sha256')),
    policy_version TEXT NOT NULL,
    tool_version TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','processing','completed','retry_pending','failed','superseded')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts>=0),
    attempt_limit INTEGER NOT NULL DEFAULT 3 CHECK(attempt_limit>0),
    claiming_run_id INTEGER REFERENCES scan_runs(id),
    last_attempt_run_id INTEGER REFERENCES scan_runs(id),
    next_retry_at TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    last_error TEXT,
    UNIQUE(revision_id,stage,policy_version,tool_version)
);
CREATE INDEX processing_queue ON processing_jobs(stage,state,next_retry_at,id);
CREATE TABLE session_processing_jobs (
    session_id INTEGER NOT NULL REFERENCES scan_sessions(id),
    job_id INTEGER NOT NULL REFERENCES processing_jobs(id),
    relative_path TEXT NOT NULL,
    required INTEGER NOT NULL DEFAULT 1 CHECK(required IN (0,1)),
    reused INTEGER NOT NULL DEFAULT 0 CHECK(reused IN (0,1)),
    PRIMARY KEY(session_id,job_id)
);
CREATE INDEX session_jobs_job ON session_processing_jobs(job_id);
CREATE TABLE metadata_results (
    id INTEGER PRIMARY KEY,
    job_id INTEGER UNIQUE REFERENCES processing_jobs(id),
    media_id INTEGER NOT NULL REFERENCES media_files(id),
    revision_id INTEGER REFERENCES file_revisions(id),
    extractor TEXT,
    extractor_version TEXT,
    policy_version TEXT,
    extracted_at TEXT,
    original_dates_json TEXT,
    attributes_json TEXT,
    raw_metadata_json TEXT,
    selected_capture_datetime TEXT,
    selected_capture_date TEXT,
    selected_date_source TEXT,
    date_confidence TEXT,
    warnings_json TEXT,
    legacy INTEGER NOT NULL DEFAULT 0 CHECK(legacy IN (0,1))
);
INSERT INTO metadata_results(media_id,extractor,extractor_version,policy_version,extracted_at,original_dates_json,attributes_json,raw_metadata_json,selected_capture_datetime,selected_capture_date,selected_date_source,date_confidence,legacy) SELECT media_id,extractor,extractor_version,extraction_policy_version,extracted_at,original_dates_json,attributes_json,raw_metadata_json,selected_capture_datetime,selected_capture_date,selected_date_source,date_confidence,1 FROM media_metadata;
ALTER TABLE media_metadata ADD COLUMN current_result_id INTEGER REFERENCES metadata_results(id);
UPDATE media_metadata SET current_result_id=(SELECT id FROM metadata_results WHERE metadata_results.media_id=media_metadata.media_id AND legacy=1 ORDER BY id DESC LIMIT 1);
CREATE TABLE file_hashes (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL UNIQUE REFERENCES processing_jobs(id),
    revision_id INTEGER NOT NULL REFERENCES file_revisions(id),
    algorithm TEXT NOT NULL CHECK(algorithm='sha256'),
    digest TEXT NOT NULL CHECK(length(digest)=64 AND digest NOT GLOB '*[^0-9a-f]*'),
    bytes_read INTEGER NOT NULL CHECK(bytes_read>=0),
    completed_at TEXT NOT NULL
);
CREATE INDEX hashes_digest ON file_hashes(digest);
ALTER TABLE scan_errors ADD COLUMN processing_job_id INTEGER REFERENCES processing_jobs(id);
CREATE INDEX errors_job ON scan_errors(processing_job_id,resolved_at);
CREATE TRIGGER photo_jobs_only BEFORE INSERT ON processing_jobs WHEN NEW.stage='photo_sha256' AND (SELECT m.media_type FROM file_revisions r JOIN media_files m ON m.id=r.media_id WHERE r.id=NEW.revision_id)!='photo' BEGIN SELECT RAISE(ABORT,'Videos must never be hashed'); END;
CREATE TRIGGER photo_jobs_only_update BEFORE UPDATE OF revision_id,stage ON processing_jobs WHEN NEW.stage='photo_sha256' AND (SELECT m.media_type FROM file_revisions r JOIN media_files m ON m.id=r.media_id WHERE r.id=NEW.revision_id)!='photo' BEGIN SELECT RAISE(ABORT,'Videos must never be hashed'); END;
CREATE TRIGGER photo_hashes_only BEFORE INSERT ON file_hashes WHEN NOT EXISTS(SELECT 1 FROM processing_jobs j JOIN file_revisions r ON r.id=j.revision_id JOIN media_files m ON m.id=r.media_id WHERE j.id=NEW.job_id AND j.stage='photo_sha256' AND r.id=NEW.revision_id AND m.media_type='photo' AND NEW.bytes_read=r.size_bytes) BEGIN SELECT RAISE(ABORT,'Invalid or partial photo hash'); END;
CREATE TRIGGER hashes_immutable BEFORE UPDATE ON file_hashes BEGIN SELECT RAISE(ABORT,'Hash results are immutable'); END;
CREATE TRIGGER metadata_results_immutable BEFORE UPDATE ON metadata_results BEGIN SELECT RAISE(ABORT,'Metadata results are immutable'); END;
CREATE TRIGGER revision_immutable BEFORE UPDATE ON file_revisions BEGIN SELECT RAISE(ABORT,'File revisions are immutable'); END;
CREATE TRIGGER job_identity_immutable BEFORE UPDATE OF revision_id,stage,policy_version,tool_version ON processing_jobs BEGIN SELECT RAISE(ABORT,'Job identity is immutable'); END;
CREATE TRIGGER metadata_result_identity BEFORE INSERT ON metadata_results WHEN NEW.legacy=0 AND NOT EXISTS(SELECT 1 FROM processing_jobs j JOIN file_revisions r ON r.id=j.revision_id WHERE j.id=NEW.job_id AND j.stage='metadata' AND r.id=NEW.revision_id AND r.media_id=NEW.media_id) BEGIN SELECT RAISE(ABORT,'Invalid metadata result identity'); END;
CREATE TRIGGER job_completion_requires_result BEFORE UPDATE OF state ON processing_jobs WHEN NEW.state='completed' AND ((NEW.stage='metadata' AND NOT EXISTS(SELECT 1 FROM metadata_results WHERE job_id=NEW.id AND revision_id=NEW.revision_id)) OR (NEW.stage='photo_sha256' AND NOT EXISTS(SELECT 1 FROM file_hashes WHERE job_id=NEW.id AND revision_id=NEW.revision_id))) BEGIN SELECT RAISE(ABORT,'Cannot complete a job without a complete result'); END;
