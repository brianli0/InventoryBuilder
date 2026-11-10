"""Durable session, file and directory operations. No filesystem traversal."""

import json
from pathlib import Path, PurePosixPath

from .database import transaction
from ..exclusions import load_policy, save_policy, same_policy, DEFAULTS_VERSION
from ..paths import MATCHER_VERSION, relative_key
from .processing import schedule
from ..hashing import fingerprint


def start_run(connection, root_id, now, version, host, config, policy, *, resume_session=None, new_scan=False, reconcile_session=None, invocation=None):
    snapshot = json.dumps({'photos': sorted(config.photo_extensions), 'videos': sorted(config.video_extensions)}, sort_keys=True)
    requested = policy.semantic
    with transaction(connection):
        session = connection.execute("SELECT * FROM scan_sessions WHERE root_id=? AND state NOT IN ('completed','cancelled','superseded')", (root_id,)).fetchone()
        predecessor = None
        if resume_session is not None or reconcile_session is not None:
            selected = connection.execute('SELECT * FROM scan_sessions WHERE id=? AND root_id=?', (resume_session if resume_session is not None else reconcile_session, root_id)).fetchone()
            if selected is None:
                raise ValueError('Selected session does not belong to the supplied target')
            if resume_session is not None:
                if session is None or session['id'] != selected['id']:
                    raise ValueError('Only the active unfinished session can be resumed')
            else:
                if session is not None and session['id'] != selected['id']:
                    raise ValueError('Another active session exists; reconcile that session or use --new-scan')
                predecessor = selected['id']
        if new_scan or reconcile_session is not None:
            if session:
                predecessor = session['id']
                connection.execute("UPDATE scan_sessions SET state='superseded' WHERE id=?", (predecessor,))
                connection.execute("UPDATE scan_runs SET state='interrupted',ended_at=?,termination_reason='explicit successor scan' WHERE session_id=? AND state='running'", (now, predecessor))
                connection.execute("UPDATE media_metadata SET state='pending',claiming_run_id=NULL WHERE state='processing' AND claiming_run_id IN (SELECT id FROM scan_runs WHERE session_id=?)", (predecessor,))
            session = None
        if session:
            if session['policy_id'] is None:
                raise ValueError('Legacy session requires explicit --new-scan or --reconcile-session; history is preserved')
            saved = load_policy(connection, session['policy_id'])
            if not isinstance(saved.semantic.get('processing'),dict):
                raise ValueError('Filesystem-only session requires an explicit --new-scan or --reconcile-session for comprehensive collection')
            if any(saved.semantic['processing'].get(key)!=policy.semantic['processing'].get(key) for key in ('metadata','date','photo_hash','exiftool_version')):
                raise ValueError('Processing/tool version changed; use an explicit successor scan')
            if resume_session is not None:
                if saved.semantic['matcher_version'] != MATCHER_VERSION or saved.semantic['defaults_version'] != DEFAULTS_VERSION:
                    raise ValueError('Saved matching/default safety version is unsupported; create an explicit successor scan')
                for rule in policy.rules:
                    if rule['active'] and rule['origin'] == 'runtime':
                        match = saved.match(rule['key'])
                        if not match or (rule['kind'] == 'directory' and match['kind'] != 'directory'):
                            raise ValueError('Saved rules lack current mandatory runtime protection; create an explicit successor scan')
            elif not same_policy(saved.semantic, policy.semantic):
                changes = [key for key in policy.semantic if saved.semantic.get(key) != policy.semantic[key]]
                raise ValueError(f"Resume policy changed ({', '.join(changes)}). Use --resume-session {session['id']} for saved rules, --new-scan, or --reconcile-session {session['id']}")
            policy = saved
            session_id = session['id']
            connection.execute("UPDATE scan_runs SET state='interrupted',ended_at=?,termination_reason='abandoned process recovered' WHERE session_id=? AND state='running'", (now, session_id))
            connection.execute("UPDATE scan_directories SET state='pending',claiming_run_id=NULL WHERE session_id=? AND state IN ('processing','failed','retry_pending')", (session_id,))
            connection.execute("UPDATE scan_files SET state='pending',claiming_run_id=NULL WHERE session_id=? AND state IN ('processing','failed','retry_pending')", (session_id,))
            # Future extractor claims must also never remain stranded. Preserve all
            # committed metadata/provenance; only reset transient ownership/state.
            connection.execute("UPDATE media_metadata SET state='pending',claiming_run_id=NULL WHERE state='processing' AND (claiming_run_id IN (SELECT id FROM scan_runs WHERE session_id=?) OR media_id IN (SELECT media_id FROM scan_files WHERE session_id=?))", (session_id, session_id))
            connection.execute("UPDATE scan_sessions SET state='running',discovery_state=CASE WHEN coverage_complete=1 THEN 'completed' ELSE 'running' END WHERE id=?", (session_id,))
        else:
            policy_id = save_policy(connection, policy, now)
            session_id = connection.execute("INSERT INTO scan_sessions(root_id,state,created_at,discovery_version,config_json,policy_id,predecessor_id,successor_reason,discovery_state) VALUES (?,'running',?,'targets-v2',?,?,?,?,'running')", (root_id, now, snapshot, policy_id, predecessor, 'reconciled' if reconcile_session is not None else 'new scan' if new_scan else None)).lastrowid
            connection.execute("INSERT INTO scan_directories(session_id,relative_path,path_key) VALUES (?,'','')", (session_id,))
        invocation_json = json.dumps(dict(inputs=invocation, config_exclusions=config.exclude_directories, requested_policy=requested, mode='saved-resume' if resume_session is not None else 'new' if new_scan else 'reconcile' if reconcile_session is not None else 'automatic'))
        run_id = connection.execute("INSERT INTO scan_runs(session_id,started_at,state,application_version,machine_id,invocation_json) VALUES (?,?,'running',?,?,?)", (session_id, now, version, host, invocation_json)).lastrowid
        connection.execute("UPDATE scan_sessions SET processing_state='running' WHERE id=?",(session_id,))
    return session_id, run_id, policy


def claim(connection, table, session_id, run_id):
    assert table in ('scan_directories', 'scan_files')
    with transaction(connection):
        row = connection.execute(f"SELECT * FROM {table} WHERE session_id=? AND state='pending' ORDER BY id LIMIT 1", (session_id,)).fetchone()
        if row:
            connection.execute(f"UPDATE {table} SET state='processing',claiming_run_id=?,attempts=attempts+1 WHERE id=?", (run_id, row['id']))
    return row


def checkpoint(connection, session_id, now):
    connection.execute('UPDATE scan_sessions SET checkpoint_at=? WHERE id=?', (now, session_id))


def record_file(connection, root_record, job, info, media_type, now, run_id, tool_version, attempt_limit, date_precedence):
    relative = job['relative_path']
    path = str(PurePosixPath(root_record['volume_relative_path']) / relative)
    previous = connection.execute('SELECT * FROM media_files WHERE volume_id=? AND path_key=? COLLATE WINPATH', (root_record['volume_id'], relative_key(path))).fetchone()
    unchanged = previous is not None and previous['size_bytes'] == info.st_size and previous['modified_ns'] == info.st_mtime_ns
    if unchanged and previous['current_revision_id'] is not None:
        revision = connection.execute('SELECT size_bytes,modified_ns,device_id,file_id FROM file_revisions WHERE id=?',(previous['current_revision_id'],)).fetchone()
        unchanged = revision is not None and tuple(revision)==fingerprint(info)
    # Windows 3.11 uses ctime for birth time; newer Python exposes birthtime.
    created = getattr(info, 'st_birthtime_ns', None)
    if created is None:
        import os
        if os.name == 'nt':
            created = info.st_ctime_ns
    full_path = str(Path(root_record['last_absolute_path']).joinpath(*PurePosixPath(relative).parts))
    name = PurePosixPath(relative).name
    with transaction(connection):
        if previous is None:
            media_id = connection.execute('INSERT INTO media_files(volume_id,volume_relative_path,last_absolute_path,parent_path,filename,extension,media_type,size_bytes,created_ns,modified_ns,first_inventoried_at,last_inventoried_at,path_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)', (root_record['volume_id'], path, full_path, str(PurePosixPath(path).parent), name, PurePosixPath(name).suffix, media_type, info.st_size, created, info.st_mtime_ns, now, now, relative_key(path))).lastrowid
        else:
            media_id = previous['id']
            if not unchanged:
                connection.execute('UPDATE media_files SET size_bytes=?,created_ns=?,modified_ns=?,last_absolute_path=?,last_inventoried_at=? WHERE id=?', (info.st_size, created, info.st_mtime_ns, full_path, now, media_id))
                connection.execute("UPDATE media_metadata SET state='pending',claiming_run_id=NULL WHERE media_id=?", (media_id,))
            else:
                connection.execute('UPDATE media_files SET last_inventoried_at=?,last_absolute_path=? WHERE id=?', (now, full_path, media_id))
            # A case-only rename is the same Windows identity, but display paths
            # and filenames must reflect the latest observed spelling.
            connection.execute('UPDATE media_files SET volume_relative_path=?,parent_path=?,filename=?,extension=?,path_key=?,media_type=? WHERE id=?', (path, str(PurePosixPath(path).parent), name, PurePosixPath(name).suffix, relative_key(path), media_type, media_id))
        connection.execute('INSERT INTO media_metadata(media_id) VALUES (?) ON CONFLICT(media_id) DO NOTHING', (media_id,))
        schedule(connection,media_id,job['session_id'],relative,info,now,tool_version,attempt_limit,date_precedence)
        connection.execute('INSERT INTO scan_file_observations(session_id,media_id,root_relative_path,size_bytes,modified_ns,observed_at) VALUES (?,?,?,?,?,?) ON CONFLICT(session_id,media_id) DO UPDATE SET size_bytes=excluded.size_bytes,modified_ns=excluded.modified_ns,observed_at=excluded.observed_at', (job['session_id'], media_id, relative, info.st_size, info.st_mtime_ns, now))
        connection.execute("UPDATE scan_files SET state='completed',media_id=?,claiming_run_id=NULL WHERE id=?", (media_id, job['id']))
        counter = 'files_skipped' if unchanged else 'files_processed'
        connection.execute(f'UPDATE scan_runs SET {counter}={counter}+1,bytes_inventoried=bytes_inventoried+? WHERE id=?', (info.st_size, run_id))
        connection.execute("UPDATE scan_errors SET resolved_at=? WHERE session_id=? AND operation='stat' AND message_path=? AND resolved_at IS NULL", (now, job['session_id'], relative))
        connection.execute("DELETE FROM scan_coverage WHERE session_id=? AND path_key=? COLLATE WINPATH AND status='unavailable'", (job['session_id'], relative_key(path)))
        checkpoint(connection, job['session_id'], now)
