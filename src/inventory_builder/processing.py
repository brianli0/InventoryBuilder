"""Bounded independent metadata/photo-hash workers for one selected scan scope."""

from datetime import datetime,timedelta,timezone
import json
from pathlib import PurePosixPath
import stat

from .exiftool import ExifTool
from .hashing import FileChangedError,fingerprint,hash_photo
from .metadata import normalize
from .sources import is_reparse,safe_path
from .paths import relative_key
from .storage.database import transaction
from .storage.processing import commit_metadata,schedule


def timestamp():
    return datetime.now(timezone.utc).isoformat()


class Processing:
    def __init__(self, connection, config, root, root_record, expected, session_id, run_id, policy, tool, event, progress, retry_failed=False):
        self.connection,self.config,self.root,self.root_record = connection,config,root,root_record
        self.expected,self.session_id,self.run_id,self.policy = expected,session_id,run_id,policy
        self.tool,self.event,self.progress = tool,event,progress
        self.tool.heartbeat = progress
        self.active_hash_bytes = 0
        self.retry_failed = retry_failed

    def refresh_unfinished(self):
        # A blocked file may have been repaired while the process was stopped.
        # Re-stat only unfinished recorded work, not unrelated/completed trees.
        last_id = 0
        while True:
            jobs = self.connection.execute("SELECT j.*,r.media_id,r.size_bytes,r.modified_ns,r.device_id,r.file_id,m.media_type,l.relative_path FROM processing_jobs j JOIN file_revisions r ON r.id=j.revision_id JOIN media_files m ON m.id=r.media_id JOIN session_processing_jobs l ON l.job_id=j.id WHERE l.session_id=? AND l.required=1 AND j.state!='completed' AND j.id>? ORDER BY j.id LIMIT 32",(self.session_id,last_id)).fetchall()
            if not jobs:
                break
            for row in jobs:
                job = dict(row)
                last_id = job['id']
                try:
                    self.validated(job)
                except FileChangedError:
                    with transaction(self.connection):
                        self.connection.execute("UPDATE scan_files SET state='pending' WHERE session_id=? AND media_id=?",(self.session_id,job['media_id']))
                        self.connection.execute("UPDATE scan_sessions SET coverage_complete=0,discovery_state='running',discovery_completed_at=NULL WHERE id=?",(self.session_id,))
                except OSError:
                    pass  # Normal worker/error handling retains retry provenance.

    def claim(self, stage, limit):
        with transaction(self.connection):
            if self.retry_failed:
                self.connection.execute("UPDATE processing_jobs SET state='pending',next_retry_at=NULL,attempt_limit=attempts+? WHERE state='failed' AND coalesce(last_attempt_run_id,0)!=? AND id IN (SELECT job_id FROM session_processing_jobs WHERE session_id=? AND required=1)",(self.config.processing_max_attempts,self.run_id,self.session_id))
            jobs = self.connection.execute("SELECT j.*,r.media_id,r.size_bytes,r.modified_ns,r.device_id,r.file_id,m.media_type,l.relative_path FROM processing_jobs j JOIN file_revisions r ON r.id=j.revision_id JOIN media_files m ON m.id=r.media_id JOIN session_processing_jobs l ON l.job_id=j.id WHERE l.session_id=? AND l.required=1 AND j.stage=? AND j.state IN ('pending','retry_pending') AND (j.next_retry_at IS NULL OR j.next_retry_at<=?) ORDER BY j.id LIMIT ?",(self.session_id,stage,timestamp(),limit)).fetchall()
            for job in jobs:
                self.connection.execute("UPDATE processing_jobs SET state='processing',claiming_run_id=?,last_attempt_run_id=?,attempts=attempts+1 WHERE id=?",(self.run_id,self.run_id,job['id']))
        return [dict(job,attempts=job['attempts']+1) for job in jobs]

    def validated(self, job):
        pathkey = relative_key(str(PurePosixPath(self.root_record['volume_relative_path']) / job['relative_path']))
        if self.policy.match(pathkey):
            with transaction(self.connection):
                self.connection.execute('UPDATE session_processing_jobs SET required=0 WHERE session_id=? AND job_id=?',(self.session_id,job['id']))
                self.connection.execute("UPDATE processing_jobs SET state='pending',claiming_run_id=NULL WHERE id=?",(job['id'],))
            return None
        path = safe_path(self.root,job['relative_path'],self.expected)
        info = path.lstat()
        if is_reparse(info) or not stat.S_ISREG(info.st_mode):
            raise OSError('Processing target is not a regular source file')
        expected = (job['size_bytes'],job['modified_ns'],job['device_id'],job['file_id'])
        if fingerprint(info)!=expected:
            raise FileChangedError('Source revision changed')
        return path

    def failed(self, job, error, *, retryable=False):
        when = timestamp()
        with transaction(self.connection):
            retry = retryable and job['attempts'] < job['attempt_limit']
            due = (datetime.now(timezone.utc)+timedelta(seconds=self.config.retry_delay_seconds * 2**min(3,job['attempts']-1))).isoformat() if retry else None
            self.connection.execute("UPDATE processing_jobs SET state=?,claiming_run_id=NULL,next_retry_at=?,last_error=? WHERE id=?",('retry_pending' if retry else 'failed',due,str(error)[:4000],job['id']))
            self.connection.execute('INSERT INTO scan_errors(session_id,run_id,media_id,operation,category,message,message_path,occurred_at,retryable,attempt,next_retry_at,processing_job_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',(self.session_id,self.run_id,job['media_id'],job['stage'],type(error).__name__,str(error)[:4000],job['relative_path'],when,int(retryable),job['attempts'],due,job['id']))
            if job['stage']=='metadata':
                self.connection.execute("UPDATE media_metadata SET state=?,attempts=?,retry_at=?,claiming_run_id=NULL WHERE media_id=?",('retry_pending' if retry else 'failed',job['attempts'],due,job['media_id']))
            self.connection.execute('UPDATE scan_sessions SET checkpoint_at=? WHERE id=?',(when,self.session_id))

    def changed(self, job, error):
        # Refresh the observed revision through a durable filesystem job on the
        # next resume. Do not spin forever while an external program rewrites it.
        with transaction(self.connection):
            self.connection.execute("UPDATE scan_files SET state='retry_pending' WHERE session_id=? AND media_id=?",(self.session_id,job['media_id']))
            self.connection.execute("UPDATE scan_sessions SET coverage_complete=0,discovery_state='blocked',discovery_completed_at=NULL WHERE id=?",(self.session_id,))
        self.failed(job,error,retryable=False)

    def complete(self, job, *, raw=None, digest=None, byte_count=None):
        when = timestamp()
        normalized = normalize(raw,self.config.capture_date_precedence) if raw is not None else None
        if normalized is not None:
            mime = normalized['attributes'].get('mime_type') or ''
            if str(mime).startswith(('image/','video/')) and (str(mime).startswith('image/') != (job['media_type']=='photo')):
                normalized['warnings'].append('Extension classification differs from detected image/video content')
        with transaction(self.connection):
            if raw is not None:
                commit_metadata(self.connection,job,raw,normalized,when)
            else:
                self.connection.execute("INSERT INTO file_hashes(job_id,revision_id,algorithm,digest,bytes_read,completed_at) VALUES (?,?,'sha256',?,?,?)",(job['id'],job['revision_id'],digest,byte_count,when))
            self.connection.execute("UPDATE processing_jobs SET state='completed',completed_at=?,claiming_run_id=NULL,next_retry_at=NULL,last_error=NULL WHERE id=?",(when,job['id']))
            self.connection.execute('UPDATE scan_errors SET resolved_at=? WHERE processing_job_id=? AND resolved_at IS NULL',(when,job['id']))
            self.connection.execute('UPDATE scan_sessions SET checkpoint_at=? WHERE id=?',(when,self.session_id))
        self.event(job['stage']+'_committed')

    def drain(self):
        batch_size = 1 if self.config.metadata_mode=='single' else self.config.metadata_batch_size
        jobs = self.claim('metadata',batch_size)
        valid = []
        for job in jobs:
            self.event('metadata_claimed')
            try:
                path = self.validated(job)
                if path:
                    valid.append((job,path))
            except FileChangedError as exc:
                self.changed(job,exc)
            except OSError as exc:
                self.failed(job,exc,retryable=True)
        # Retried files use single-file isolation; fresh work uses bounded batches.
        groups = [[pair] for pair in valid if pair[0]['attempts']>1]
        fresh = [pair for pair in valid if pair[0]['attempts']==1]
        if fresh:
            groups.append(fresh)
        for group in groups:
            try:
                results = self.tool.extract_batch([path for _,path in group])
                if len(results)!=len(group):
                    raise ValueError('Metadata batch response count mismatch')
            except (OSError,ValueError,TimeoutError) as exc:
                for job,_ in group:
                    self.failed(job,exc,retryable=True)
                continue
            for (job,path),result in zip(group,results):
                try:
                    if isinstance(result,Exception):
                        raise result
                    if not isinstance(result,dict):
                        raise ValueError('Metadata response is not an object')
                    self.event('metadata_extracted')
                    if self.validated(job) is None:
                        continue
                except FileChangedError as exc:
                    self.changed(job,exc)
                except (OSError,ValueError) as exc:
                    self.failed(job,exc,retryable=isinstance(exc,OSError))
                else:
                    try:
                        self.complete(job,raw=result)
                    except (ValueError,TypeError) as exc:
                        self.failed(job,exc,retryable=False)
                self.progress()
        hashes = self.claim('photo_sha256',self.config.metadata_batch_size)
        for job in hashes:
            self.event('hash_claimed')
            try:
                metadata = self.connection.execute('SELECT attributes_json FROM metadata_results WHERE revision_id=? AND legacy=0 ORDER BY id DESC LIMIT 1',(job['revision_id'],)).fetchone()
                if metadata:
                    mime = json.loads(metadata[0]).get('mime_type') or ''
                    if str(mime).lower().startswith('video/'):
                        raise ValueError('Video content under a photo extension: hashing refused')
                path = self.validated(job)
                if path is None:
                    continue
                expected = (job['size_bytes'],job['modified_ns'],job['device_id'],job['file_id'])
                self.active_hash_bytes = 0

                def chunk(event):
                    self.active_hash_bytes += min(self.config.hash_chunk_bytes,max(0,job['size_bytes']-self.active_hash_bytes))
                    self.event(event)
                    self.progress()

                digest,byte_count = hash_photo(path,expected,media_type=job['media_type'],chunk_size=self.config.hash_chunk_bytes,event=chunk)
                if self.validated(job) is None:
                    continue
            except FileChangedError as exc:
                self.changed(job,exc)
            except (OSError,ValueError) as exc:
                self.failed(job,exc,retryable=isinstance(exc,OSError))
            else:
                self.complete(job,digest=digest,byte_count=byte_count)
            finally:
                self.active_hash_bytes = 0
            self.progress()
        return len(jobs)+len(hashes)
