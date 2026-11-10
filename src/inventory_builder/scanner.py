"""Read-only selected-target inventory with independent durable processing stages."""

from datetime import datetime, timezone
from dataclasses import replace
import logging
import os
from pathlib import PurePosixPath
import stat
import time

from . import __version__
from .locking import scanner_lock
from .logging_setup import configure_logging
from .sources import bind_folder, is_reparse, machine_id, safe_path, validate_folder, selected_volume, reject_case_sensitive
from .paths import relative_key
from .exclusions import compile_policy
from .storage.database import connect, transaction
from .storage.discovery import checkpoint, claim, record_file, start_run
from .storage.migrations import initialize
from .processing import Processing,ExifTool
from .metadata import METADATA_POLICY,DATE_POLICY
from .storage.processing import recover,stage_counts,verification,HASH_POLICY


def now():
    return datetime.now(timezone.utc).isoformat()


def record_error(connection, session_id, run_id, table, job, operation, error):
    timestamp = now()
    with transaction(connection):
        connection.execute(f"UPDATE {table} SET state='failed',claiming_run_id=NULL WHERE id=?", (job['id'],))
        directory_id = job['id'] if table == 'scan_directories' else job['directory_id']
        connection.execute('INSERT INTO scan_errors(session_id,run_id,directory_id,operation,category,error_code,message,message_path,occurred_at,retryable) VALUES (?,?,?,?,?,?,?,?,?,1)', (session_id, run_id, directory_id, operation, type(error).__name__, str(getattr(error, 'winerror', None) or getattr(error, 'errno', '')), str(error), job['relative_path'], timestamp))
        connection.execute('UPDATE scan_runs SET files_failed=files_failed+1 WHERE id=?', (run_id,))
        checkpoint(connection, session_id, timestamp)
    logging.getLogger('inventory_builder').warning('%s %s: %s', operation, job['relative_path'], error)


def scan_folder(folder, config, *, exclude=(), resume_session=None, new_scan=False, reconcile_session=None, volume_uuid=None, confirm_legacy=False, retry_failed=False, progress=print, _event=lambda event: None):
    """Scan only the explicit target. _event is a deterministic fault-injection seam."""
    root = validate_folder(folder, config)
    if not config.database.is_file():
        raise ValueError('Database not initialized. Run init first.')
    with scanner_lock(config.database):
        initialize(config.database, config.busy_timeout_ms)
        connection = connect(config.database, timeout_ms=config.busy_timeout_ms)
        try:
            volume = selected_volume(root)
            root_id, expected = bind_folder(connection, root, now(), volume=volume, volume_uuid=volume_uuid, confirm_legacy=confirm_legacy, allow_legacy_active=new_scan or reconcile_session is not None)
            root_record = connection.execute('SELECT * FROM scan_roots WHERE id=?', (root_id,)).fetchone()
            requested_policy = compile_policy(root, volume, root_record['volume_id'], root_record['volume_relative_path'], config, exclude)
            tool = ExifTool(config)
            requested_policy.semantic['processing'] = dict(metadata=METADATA_POLICY,date=DATE_POLICY,photo_hash=HASH_POLICY,exiftool_version=tool.version,date_precedence=list(config.capture_date_precedence))
            session_id, run_id, policy = start_run(connection, root_id, now(), __version__, machine_id(), config, requested_policy, resume_session=resume_session, new_scan=new_scan, reconcile_session=reconcile_session, invocation=dict(target=str(folder), cli_exclusions=list(exclude)))
            if resume_session is not None:
                progress(f'Using saved policy from session {session_id}; current configuration differences do not change its rules.')
            config = replace(config, photo_extensions=tuple(policy.semantic['photos']), video_extensions=tuple(policy.semantic['videos']),capture_date_precedence=tuple(policy.semantic['processing']['date_precedence']))
            logger = configure_logging(config.log_directory)
            with transaction(connection):
                recover(connection,session_id,retry_failed,config.processing_max_attempts)
            started = time.monotonic()
            last_display = 0
            current = ''

            def display(force=False):
                nonlocal last_display
                tick = time.monotonic()
                if not force and tick - last_display < 1:
                    return
                last_display = tick
                counts = connection.execute('SELECT * FROM scan_runs WHERE id=?', (run_id,)).fetchone()
                stamp = connection.execute('SELECT checkpoint_at FROM scan_sessions WHERE id=?', (session_id,)).fetchone()[0]
                elapsed = tick - started
                pruned = connection.execute("SELECT count(*) FROM scan_coverage WHERE session_id=? AND status='excluded'", (session_id,)).fetchone()[0]
                progress(f"Target: {root} | directory: {current or '.'} | discovered: {counts['files_discovered']} | processed: {counts['files_processed']} | unchanged: {counts['files_skipped']} | errors: {counts['files_failed']} | excluded paths: {pruned} | unsupported: {counts['unsupported_files']} | bytes: {counts['bytes_inventoried']} | elapsed: {elapsed:.1f}s | throughput: {(counts['files_processed'] + counts['files_skipped']) / max(elapsed, .001):.1f} files/s | checkpoint: {stamp or 'none'}")
                counts = stage_counts(connection,session_id)
                for stage,label in (('metadata','Metadata'),('photo_sha256','Photo SHA-256')):
                    values = counts[stage]
                    progress(f"{label}: completed {values['completed']} | pending {values['pending']} | failed {values['failed']} | retried {values['retried']} | reused {values['reused']}")
                progress(f'Current photo hash bytes: {worker.active_hash_bytes if worker else 0}')
                hashed = connection.execute('SELECT coalesce(sum(h.bytes_read),0) FROM session_processing_jobs l JOIN file_hashes h ON h.job_id=l.job_id WHERE l.session_id=? AND l.required=1',(session_id,)).fetchone()[0]
                progress(f'Completed photo hash bytes: {hashed}')

            worker = Processing(connection,config,root,root_record,expected,session_id,run_id,policy,tool,_event,display,retry_failed)

            def volume_path(relative):
                return relative_key(str(PurePosixPath(root_record['volume_relative_path']) / relative))

            def coverage(relative, status, rule=None):
                key = volume_path(relative)
                if status == 'enumerated':
                    connection.execute("DELETE FROM scan_coverage WHERE session_id=? AND path_key=? COLLATE WINPATH AND status='unavailable'", (session_id, key))
                connection.execute('INSERT INTO scan_coverage(session_id,volume_relative_path,path_key,status,rule_id,observed_at) VALUES (?,?,?,?,?,?) ON CONFLICT(session_id,path_key,status) DO UPDATE SET rule_id=excluded.rule_id,observed_at=excluded.observed_at', (session_id, key, key, status, rule.get('id') if rule else None, now()))

            def excluded_job(table, job):
                rule = policy.match(volume_path(job['relative_path']))
                if rule:
                    with transaction(connection):
                        connection.execute(f"UPDATE {table} SET state='excluded',claiming_run_id=NULL WHERE id=?", (job['id'],))
                        coverage(job['relative_path'], 'excluded', rule)
                        checkpoint(connection, session_id, now())
                    return True
                return False

            def flush(directory, batch):
                with transaction(connection):
                    for kind, relative in batch:
                        rule = policy.match(volume_path(relative))
                        if rule or kind == 'skipped':
                            coverage(relative, 'excluded', rule)
                            continue
                        if kind == 'directory':
                            connection.execute('INSERT INTO scan_directories(session_id,relative_path,path_key) VALUES (?,?,?) ON CONFLICT DO NOTHING', (session_id, relative, relative_key(relative)))
                        elif kind == 'file':
                            inserted = connection.execute('INSERT INTO scan_files(session_id,directory_id,relative_path,path_key) VALUES (?,?,?,?) ON CONFLICT DO NOTHING', (session_id, directory['id'], relative, relative_key(relative))).rowcount
                            connection.execute('UPDATE scan_runs SET files_discovered=files_discovered+? WHERE id=?', (inserted, run_id))
                        elif kind == 'unsupported':
                            connection.execute('UPDATE scan_runs SET unsupported_files=unsupported_files+1 WHERE id=?', (run_id,))
                    checkpoint(connection, session_id, now())
                _event('discovery_committed')
                display()

            def process_files():
                while (job := claim(connection, 'scan_files', session_id, run_id)) is not None:
                    if excluded_job('scan_files', job):
                        continue
                    _event('file_claimed')
                    try:
                        path = safe_path(root, job['relative_path'], expected)
                        info = path.lstat()
                        if is_reparse(info) or not stat.S_ISREG(info.st_mode):
                            raise OSError('Candidate is no longer a regular file')
                    except OSError as exc:
                        record_error(connection, session_id, run_id, 'scan_files', job, 'stat', exc)
                        with transaction(connection):
                            coverage(job['relative_path'], 'unavailable')
                        continue
                    extension = path.suffix.lower()
                    record_file(connection, root_record, job, info, 'photo' if extension in config.photo_extensions else 'video', now(), run_id,tool.version,config.processing_max_attempts,config.capture_date_precedence)
                    _event('file_committed')
                    display()
                discovered = not connection.execute("SELECT 1 FROM scan_directories WHERE session_id=? AND state NOT IN ('completed','excluded') UNION ALL SELECT 1 FROM scan_files WHERE session_id=? AND state NOT IN ('completed','excluded') LIMIT 1",(session_id,session_id)).fetchone()
                if discovered:
                    with transaction(connection):
                        connection.execute("UPDATE scan_sessions SET discovery_state='completed',coverage_complete=1,discovery_completed_at=coalesce(discovery_completed_at,?) WHERE id=?",(now(),session_id))
                worker.drain()

            def finish(state, reason=None, discovery_complete=False):
                timestamp = now()
                with transaction(connection):
                    connection.execute('UPDATE scan_runs SET state=?,ended_at=?,termination_reason=? WHERE id=?', ('completed' if state == 'completed' else 'interrupted' if state == 'interrupted' else 'failed', timestamp, reason, run_id))
                    existing = connection.execute('SELECT discovery_state,coverage_complete FROM scan_sessions WHERE id=?',(session_id,)).fetchone()
                    discovered = discovery_complete or bool(existing['coverage_complete'])
                    connection.execute('UPDATE scan_sessions SET state=?,discovery_state=?,processing_state=?,completed_at=?,discovery_completed_at=CASE WHEN ? THEN coalesce(discovery_completed_at,?) ELSE discovery_completed_at END,processing_completed_at=?,coverage_complete=?,checkpoint_at=? WHERE id=?', (state,'completed' if discovered else state,'completed' if state=='completed' else 'blocked' if state=='blocked' else 'pending',timestamp if state=='completed' else None,int(discovered),timestamp,timestamp if state=='completed' else None,int(discovered),timestamp,session_id))
                    connection.execute('UPDATE volumes SET last_seen_at=? WHERE id=?', (timestamp, root_record['volume_id']))

            try:
                _event('run_started')
                display(True)
                # Files already discovered before a crash are processed even when
                # their containing directory had completed enumeration.
                worker.refresh_unfinished()
                process_files()
                while (directory := claim(connection, 'scan_directories', session_id, run_id)) is not None:
                    current = directory['relative_path']
                    if excluded_job('scan_directories', directory):
                        continue
                    _event('directory_claimed')
                    batch = []
                    try:
                        path = safe_path(root, current, expected)
                        reject_case_sensitive(path)
                        with os.scandir(path) as entries:
                            for entry in entries:
                                safe_path(root, current, expected)
                                relative = str(PurePosixPath(current) / entry.name)
                                if policy.match(volume_path(relative)):
                                    batch.append(('excluded', relative))
                                    if len(batch) >= config.batch_size:
                                        flush(directory, batch)
                                        batch.clear()
                                    continue
                                try:
                                    info = entry.stat(follow_symlinks=False)
                                    if is_reparse(info):
                                        kind = 'skipped'
                                    elif stat.S_ISDIR(info.st_mode):
                                        kind = 'directory'
                                    elif stat.S_ISREG(info.st_mode):
                                        kind = 'file' if PurePosixPath(entry.name).suffix.lower() in (*config.photo_extensions, *config.video_extensions) else 'unsupported'
                                    else:
                                        kind = 'skipped'
                                except OSError:
                                    # Persist supported candidates so a stat failure
                                    # has its own durable file retry job.
                                    if PurePosixPath(entry.name).suffix.lower() in (*config.photo_extensions, *config.video_extensions):
                                        kind = 'file'
                                    else:
                                        raise
                                batch.append((kind, relative))
                                if len(batch) >= config.batch_size:
                                    flush(directory, batch)
                                    batch.clear()
                                    process_files()
                        if batch:
                            flush(directory, batch)
                            batch.clear()
                        # Recheck that the selected root and queued path have not
                        # become links or changed identity during enumeration.
                        safe_path(root, current, expected)
                    except OSError as exc:
                        if batch:
                            flush(directory, batch)
                        record_error(connection, session_id, run_id, 'scan_directories', directory, 'enumerate', exc)
                        with transaction(connection):
                            coverage(current, 'unavailable')
                    else:
                        with transaction(connection):
                            connection.execute("UPDATE scan_directories SET state='completed',completed_at=?,claiming_run_id=NULL WHERE id=?", (now(), directory['id']))
                            connection.execute("UPDATE scan_errors SET resolved_at=? WHERE session_id=? AND operation='enumerate' AND directory_id=? AND resolved_at IS NULL", (now(), session_id, directory['id']))
                            checkpoint(connection, session_id, now())
                            coverage(current, 'enumerated')
                        _event('directory_committed')
                    process_files()
                incomplete = connection.execute("SELECT 1 FROM scan_directories WHERE session_id=? AND state NOT IN ('completed','excluded') UNION ALL SELECT 1 FROM scan_files WHERE session_id=? AND state NOT IN ('completed','excluded') LIMIT 1", (session_id, session_id)).fetchone()
                while worker.drain():
                    pass
                # File changes detected by processing leave filesystem retry jobs.
                incomplete = connection.execute("SELECT 1 FROM scan_directories WHERE session_id=? AND state NOT IN ('completed','excluded') UNION ALL SELECT 1 FROM scan_files WHERE session_id=? AND state NOT IN ('completed','excluded') LIMIT 1", (session_id,session_id)).fetchone()
                with transaction(connection):
                    if not incomplete:
                        connection.execute("UPDATE scan_sessions SET discovery_state='completed',coverage_complete=1,discovery_completed_at=coalesce(discovery_completed_at,?) WHERE id=?",(now(),session_id))
                complete = verification(connection,session_id)['complete']
                state = 'completed' if complete else 'blocked'
                finish(state,discovery_complete=not incomplete)
                display(True)
                progress(f'Scan {state}; session {session_id}.')
                progress('Fully inventoried: discovery, metadata, and required photo hashes completed.' if complete else 'Incomplete inventory: required discovery/metadata/photo-hash work remains. Resume or retry failed jobs.')
                return 0 if complete else 1
            except KeyboardInterrupt:
                finish('interrupted', 'Ctrl+C')
                display(True)
                progress('Scan interrupted; run the same command to resume.')
                return 130
            except Exception:
                # SQLite may be full or damaged: do not attempt further writes.
                # The committed run remains recoverable as an abandoned run.
                logger.exception('Scan stopped; committed work is preserved')
                raise
        finally:
            connection.close()
            logger = logging.getLogger('inventory_builder')
            for handler in logger.handlers[:]:
                handler.close()
                logger.removeHandler(handler)
