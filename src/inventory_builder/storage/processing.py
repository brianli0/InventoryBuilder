"""Transactional job scheduling/results; all functions called under the writer lock."""

from datetime import datetime,timezone
import json

from ..hashing import fingerprint
from ..metadata import METADATA_POLICY, DATE_POLICY

HASH_POLICY = 'photo-sha256-v1'


def schedule(connection, media_id, session_id, relative, info, timestamp, tool_version, attempt_limit, date_precedence):
    size,modified,device,file_id = fingerprint(info)
    connection.execute('INSERT INTO file_revisions(media_id,size_bytes,modified_ns,device_id,file_id,created_ns,observed_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT DO NOTHING', (media_id,size,modified,device,file_id,getattr(info,'st_birthtime_ns',None),timestamp))
    revision = connection.execute('SELECT id FROM file_revisions WHERE media_id=? AND size_bytes=? AND modified_ns=? AND device_id=? AND file_id=?', (media_id,size,modified,device,file_id)).fetchone()[0]
    connection.execute('UPDATE media_files SET current_revision_id=? WHERE id=?',(revision,media_id))
    media_type = connection.execute('SELECT media_type FROM media_files WHERE id=?',(media_id,)).fetchone()[0]
    # Prior session requirements retain history, but this session requires the
    # currently observed revision only.
    connection.execute('UPDATE session_processing_jobs SET required=0 WHERE session_id=? AND job_id IN (SELECT j.id FROM processing_jobs j JOIN file_revisions r ON r.id=j.revision_id WHERE r.media_id=? AND r.id!=?)',(session_id,media_id,revision))
    connection.execute('UPDATE scan_errors SET resolved_at=? WHERE session_id=? AND resolved_at IS NULL AND processing_job_id IN (SELECT j.id FROM processing_jobs j JOIN file_revisions r ON r.id=j.revision_id WHERE r.media_id=? AND r.id!=?)',(timestamp,session_id,media_id,revision))
    metadata_policy = METADATA_POLICY+';'+DATE_POLICY+';'+','.join(date_precedence)
    for stage,policy,tool in [('metadata',metadata_policy,tool_version)] + ([('photo_sha256',HASH_POLICY,'stdlib-sha256')] if media_type=='photo' else []):
        connection.execute('INSERT INTO processing_jobs(revision_id,stage,policy_version,tool_version,attempt_limit,created_at) VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING',(revision,stage,policy,tool,attempt_limit,timestamp))
        job = connection.execute('SELECT id,state FROM processing_jobs WHERE revision_id=? AND stage=? AND policy_version=? AND tool_version=?',(revision,stage,policy,tool)).fetchone()
        connection.execute('INSERT INTO session_processing_jobs(session_id,job_id,relative_path,reused) VALUES (?,?,?,?) ON CONFLICT(session_id,job_id) DO UPDATE SET required=1,relative_path=excluded.relative_path',(session_id,job['id'],relative,int(job['state']=='completed')))
        if stage=='metadata' and job['state']=='completed':
            result = connection.execute('SELECT id FROM metadata_results WHERE job_id=?',(job['id'],)).fetchone()
            if result:
                apply_metadata_result(connection,result['id'])
    return revision


def recover(connection, session_id, retry_failed, attempt_limit):
    # No live competing worker can own these claims under the process-held lock.
    connection.execute("UPDATE processing_jobs SET state='pending',claiming_run_id=NULL WHERE state='processing'")
    if retry_failed:
        connection.execute("UPDATE processing_jobs SET state='pending',next_retry_at=NULL,attempt_limit=attempts+? WHERE state='failed' AND id IN (SELECT job_id FROM session_processing_jobs WHERE session_id=? AND required=1)",(attempt_limit,session_id))


def commit_metadata(connection, job, raw, normalized, timestamp):
    # Preserve imported/legacy projections that are not already represented by
    # an immutable result, including payloads collected before this job system.
    connection.execute("INSERT INTO metadata_results(media_id,extractor,extractor_version,policy_version,extracted_at,original_dates_json,attributes_json,raw_metadata_json,selected_capture_datetime,selected_capture_date,selected_date_source,date_confidence,legacy) SELECT media_id,extractor,extractor_version,extraction_policy_version,extracted_at,original_dates_json,attributes_json,raw_metadata_json,selected_capture_datetime,selected_capture_date,selected_date_source,date_confidence,1 FROM media_metadata WHERE media_id=? AND raw_metadata_json IS NOT NULL AND (current_result_id IS NULL OR raw_metadata_json IS NOT (SELECT raw_metadata_json FROM metadata_results WHERE id=current_result_id))",(job['media_id'],))
    selected = normalized['selected'] or {}
    encode = lambda value: json.dumps(value,ensure_ascii=True,allow_nan=False)
    attributes,dates,original,warnings = map(encode,(normalized['attributes'],normalized['dates'],raw,normalized['warnings']))
    result = connection.execute('INSERT INTO metadata_results(job_id,media_id,revision_id,extractor,extractor_version,policy_version,extracted_at,original_dates_json,attributes_json,raw_metadata_json,selected_capture_datetime,selected_capture_date,selected_date_source,date_confidence,warnings_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(job['id'],job['media_id'],job['revision_id'],'ExifTool',job['tool_version'],job['policy_version'],timestamp,dates,attributes,original,selected.get('datetime'),selected.get('date'),selected.get('source'),selected.get('confidence','unknown'),warnings)).lastrowid
    apply_metadata_result(connection,result)


def apply_metadata_result(connection,result_id):
    result = connection.execute('SELECT result.*,r.size_bytes,r.modified_ns FROM metadata_results result JOIN file_revisions r ON r.id=result.revision_id WHERE result.id=?',(result_id,)).fetchone()
    connection.execute("UPDATE media_metadata SET state='completed',processed_size_bytes=?,processed_modified_ns=?,extractor=?,extractor_version=?,extraction_policy_version=?,extracted_at=?,claiming_run_id=NULL,selected_capture_datetime=?,selected_capture_date=?,selected_date_source=?,date_confidence=?,date_policy_version=?,original_dates_json=?,attributes_json=?,raw_metadata_json=?,current_result_id=? WHERE media_id=?",(result['size_bytes'],result['modified_ns'],result['extractor'],result['extractor_version'],result['policy_version'],result['extracted_at'],result['selected_capture_datetime'],result['selected_capture_date'],result['selected_date_source'],result['date_confidence'],DATE_POLICY,result['original_dates_json'],result['attributes_json'],result['raw_metadata_json'],result_id,result['media_id']))


def stage_counts(connection, session_id):
    output = {}
    for stage in ('metadata','photo_sha256'):
        values = dict(completed=0,pending=0,failed=0,retried=0,reused=0,processing=0)
        for row in connection.execute('SELECT j.state,count(*) n,sum(max(0,j.attempts-1)) retries,sum(l.reused) reused FROM session_processing_jobs l JOIN processing_jobs j ON j.id=l.job_id WHERE l.session_id=? AND l.required=1 AND j.stage=? GROUP BY j.state',(session_id,stage)):
            category = 'completed' if row['state']=='completed' else 'failed' if row['state'] in ('failed','superseded') else 'pending'
            values[category] += row['n']
            values['retried'] += row['retries']
            values['reused'] += row['reused']
            values['processing'] += row['n'] if row['state']=='processing' else 0
        output[stage] = values
    return output


def verification(connection, session_id):
    session = connection.execute('SELECT * FROM scan_sessions WHERE id=?',(session_id,)).fetchone()
    if session is None:
        raise ValueError('Session not found')
    policy = connection.execute('SELECT canonical_json FROM scan_policies WHERE id=?',(session['policy_id'],)).fetchone()
    comprehensive = bool(policy and isinstance(json.loads(policy[0]).get('processing'),dict))
    counts = stage_counts(connection,session_id)
    missing = 0
    # Every observation must be backed by both required stages (photo) or metadata
    # only (video), even if a scheduling/result bug left no visible pending job.
    for stage,table,condition in (('metadata','metadata_results','1=1'),('photo_sha256','file_hashes',"m.media_type='photo'")):
        missing += connection.execute(f"SELECT count(*) FROM scan_file_observations o JOIN media_files m ON m.id=o.media_id WHERE o.session_id=? AND {condition} AND NOT EXISTS(SELECT 1 FROM session_processing_jobs l JOIN processing_jobs j ON j.id=l.job_id JOIN {table} result ON result.job_id=j.id WHERE l.session_id=o.session_id AND l.required=1 AND j.revision_id=m.current_revision_id AND j.stage=? AND j.state='completed')",(session_id,stage)).fetchone()[0]
    required_unfinished = any(values['pending'] or values['failed'] for values in counts.values())
    discovery = session['discovery_state']=='completed' and session['coverage_complete']==1
    complete = comprehensive and discovery and not missing and not required_unfinished
    return dict(complete=complete,comprehensive_policy=comprehensive,discovery_complete=discovery,missing_required_results=missing,stages=counts)
