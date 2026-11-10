from dataclasses import replace
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import struct
import subprocess
import sys
import time
import zlib

import pytest

from inventory_builder.config import Config
from inventory_builder.exiftool import ExifTool
from inventory_builder.hashing import hash_photo,fingerprint,FileChangedError
from inventory_builder.metadata import normalize
from inventory_builder.scanner import scan_folder
from inventory_builder.storage.database import connect
from inventory_builder.storage.processing import verification
from inventory_builder.volumes import Volume


def tiff_metadata():
    # Synthetic EXIF with original capture time, offset, camera and GPS; all
    # offsets refer to this self-contained little-endian TIFF byte array.
    entries0 = 7
    exif_offset = 8+2+12*entries0+4
    gps_offset = exif_offset+2+12*3+4
    data_offset = gps_offset+2+12*4+4
    extra = bytearray()

    def text(tag,value):
        data = value.encode('ascii')+b'\0'
        if len(data)<=4:
            pointer = data.ljust(4,b'\0')
        else:
            pointer = struct.pack('<I',data_offset+len(extra))
            extra.extend(data)
        return struct.pack('<HHI',tag,2,len(data))+pointer

    def integer(tag,value,kind=4):
        return struct.pack('<HHI',tag,kind,1)+(struct.pack('<H',value)+b'\0\0' if kind==3 else struct.pack('<I',value))

    def rational(tag,values):
        position = data_offset+len(extra)
        for value in values:
            extra.extend(struct.pack('<II',value,1))
        return struct.pack('<HHII',tag,5,len(values),position)

    def ifd(entries):
        return struct.pack('<H',len(entries))+b''.join(sorted(entries,key=lambda item:struct.unpack('<H',item[:2])[0]))+b'\0'*4

    zero = [integer(0x100,2),integer(0x101,3),text(0x10f,'Disposable Camera'),text(0x110,'Test Model'),integer(0x112,6,3),integer(0x8769,exif_offset),integer(0x8825,gps_offset)]
    exif = [text(0x9003,'2024:02:03 04:05:06'),text(0x9004,'2024:02:03 04:05:07'),text(0x9011,'-08:00')]
    gps = [text(1,'N'),rational(2,[47,30,0]),text(3,'W'),rational(4,[122,15,0])]
    return b'II'+struct.pack('<HI',42,8)+ifd(zero)+ifd(exif)+ifd(gps)+bytes(extra)


def png(path, *, metadata=True):
    def chunk(tag,data):
        return struct.pack('>I',len(data))+tag+data+struct.pack('>I',zlib.crc32(tag+data)&0xffffffff)
    body = chunk(b'IHDR',struct.pack('>IIBBBBB',2,3,8,2,0,0,0))
    if metadata:
        body += chunk(b'eXIf',tiff_metadata())
    body += chunk(b'IDAT',zlib.compress((b'\0'+b'\x80\x40\x20'*2)*3))+chunk(b'IEND',b'')
    path.write_bytes(b'\x89PNG\r\n\x1a\n'+body)
    return path


def mov(path, *, brand=b'qt  '):
    def atom(tag,payload):
        return struct.pack('>I',len(payload)+8)+tag+payload
    created = int((datetime(2024,2,3,4,5,6,tzinfo=timezone.utc)-datetime(1904,1,1,tzinfo=timezone.utc)).total_seconds())
    matrix = struct.pack('>9I',65536,0,0,0,65536,0,0,0,1<<30)
    mvhd = b'\0'*4+struct.pack('>4I',created,created,1000,2500)+struct.pack('>IH',65536,256)+b'\0'*10+matrix+b'\0'*24+struct.pack('>I',2)
    tkhd = b'\0\0\0\7'+struct.pack('>5I',created,created,1,0,2500)+b'\0'*8+struct.pack('>4H',0,0,0,0)+matrix+struct.pack('>2I',640<<16,480<<16)
    mdhd = b'\0'*4+struct.pack('>4I',created,created,1000,2500)+b'\0'*4
    hdlr = b'\0'*4+b'mhlr'+b'vide'+b'\0'*12+b'Disposable video\0'
    # One valid raw RGB frame held for 2.5 seconds, with complete sample tables.
    sample = b'\0'*6+struct.pack('>H',1)+b'\0'*16+struct.pack('>HHII',640,480,72<<16,72<<16)+b'\0'*4+struct.pack('>H',1)+b'\0'*32+struct.pack('>Hh',24,-1)
    stsd = b'\0'*4+struct.pack('>I',1)+atom(b'raw ',sample)
    stts = b'\0'*4+struct.pack('>III',1,1,2500)
    stsc = b'\0'*4+struct.pack('>IIII',1,1,1,1)
    frame = b'\x80\x40\x20'*(640*480)
    stsz = b'\0'*4+struct.pack('>II',len(frame),1)
    dref = b'\0'*4+struct.pack('>I',1)+atom(b'url ',b'\0\0\0\1')
    def movie(offset):
        stco = b'\0'*4+struct.pack('>II',1,offset)
        stbl = atom(b'stsd',stsd)+atom(b'stts',stts)+atom(b'stsc',stsc)+atom(b'stsz',stsz)+atom(b'stco',stco)
        minf = atom(b'vmhd',b'\0\0\0\1'+b'\0'*8)+atom(b'dinf',atom(b'dref',dref))+atom(b'stbl',stbl)
        mdia = atom(b'mdhd',mdhd)+atom(b'hdlr',hdlr)+atom(b'minf',minf)
        return atom(b'moov',atom(b'mvhd',mvhd)+atom(b'trak',atom(b'tkhd',tkhd)+atom(b'mdia',mdia))+udta)
    location = b'+47.5000-122.2500+015.0/'
    udta = atom(b'udta',atom(b'\xa9xyz',struct.pack('>HH',len(location),0)+location))
    ftyp = atom(b'ftyp',brand+b'\0'*4+brand)
    body = ftyp+movie(len(ftyp)+len(movie(0))+8)+atom(b'mdat',frame)
    path.write_bytes(body)
    return path


def jpeg(path):
    # Valid tiny grayscale baseline JPEG with one MCU and EXIF. Huffman DC=0,
    # AC=EOB each use the one-bit code 0; remaining entropy bits are padded 1.
    def segment(marker,payload):
        return b'\xff'+bytes([marker])+struct.pack('>H',len(payload)+2)+payload
    huffman = b'\x01'+b'\0'*15+b'\0'
    data = b'\xff\xd8'+segment(0xe1,b'Exif\0\0'+tiff_metadata())
    data += segment(0xdb,b'\0'+b'\x08'*64)
    data += segment(0xc0,struct.pack('>BHHB',8,3,2,1)+b'\x01\x11\0')
    data += segment(0xc4,b'\0'+huffman+b'\x10'+huffman)
    data += segment(0xda,b'\x01\x01\0\0\x3f\0')+b'\x3f\xff\xd9'
    path.write_bytes(data)
    return path


@pytest.fixture
def collection(tmp_path,database,monkeypatch):
    root = tmp_path/'disposable media'
    root.mkdir()
    volume = Volume(root,'test-guid','serial-test','TESTFS',1000000)
    monkeypatch.setattr('inventory_builder.scanner.selected_volume',lambda selected:volume)
    return root,Config(database,tmp_path/'logs',batch_size=100,metadata_batch_size=16,retry_delay_seconds=0)


def scan(root,config,**options):
    return scan_folder(root,config,progress=lambda text:None,**options)


def rows(config,sql,args=()):
    connection = connect(config.database)
    try:
        return [dict(row) for row in connection.execute(sql,args)]
    finally:
        connection.close()


def test_real_exiftool_grouped_dates_photo_video_and_source_safety(collection):
    root,config = collection
    photo = png(root/'photo 日本語.png')
    video = mov(root/'video sample.mov')
    before = {p:(p.read_bytes(),p.stat().st_mtime_ns,p.stat().st_ctime_ns) for p in (photo,video)}
    tool = ExifTool(config)
    values = tool.extract_batch([photo,video])
    assert all(isinstance(value,dict) for value in values),values
    metadata = normalize(values[0])
    assert metadata['selected']['datetime']=='2024-02-03T04:05:06-08:00'
    assert metadata['attributes']['camera_make']=='Disposable Camera'
    assert metadata['attributes']['gps_latitude']==47.5
    assert metadata['attributes']['gps_longitude']==-122.25
    video_meta = normalize(values[1])
    assert video_meta['attributes']['duration_seconds']==2.5
    assert video_meta['attributes']['width']==640
    assert video_meta['attributes']['height']==480
    assert video_meta['attributes']['video_codec'] is not None
    assert video_meta['attributes']['frame_rate']==.4
    assert video_meta['attributes']['gps_latitude']==47.5
    assert video_meta['attributes']['gps_longitude']==-122.25
    assert scan(root,config)==0
    assert len(rows(config,'SELECT * FROM file_hashes'))==1
    assert rows(config,'SELECT digest FROM file_hashes')[0]['digest']==hashlib.sha256(before[photo][0]).hexdigest()
    assert before=={p:(p.read_bytes(),p.stat().st_mtime_ns,p.stat().st_ctime_ns) for p in (photo,video)}
    assert scan(root,config)==0
    assert len(rows(config,'SELECT * FROM file_hashes'))==1
    assert len(rows(config,'SELECT * FROM metadata_results WHERE legacy=0'))==2
    assert all(r['reused']==1 for r in rows(config,'SELECT reused FROM session_processing_jobs WHERE session_id=2'))


def test_missing_capture_dates_never_use_filename_or_filesystem(collection):
    root,config = collection
    photo = png(root/'IMG_20240203_040506.png',metadata=False)
    assert scan(root,config)==0
    result = rows(config,'SELECT * FROM media_metadata')[0]
    assert result['selected_capture_datetime'] is None
    assert result['date_confidence']=='unknown'
    assert rows(config,'SELECT filename FROM media_files')[0]['filename']==photo.name


@pytest.mark.parametrize('boundary',['metadata_claimed','metadata_extracted','metadata_committed','hash_claimed','hash_chunk','photo_sha256_committed'])
def test_independent_stage_interrupt_and_resume(collection,boundary):
    root,config = collection
    png(root/'a.png')
    png(root/'b.png')
    def interrupt(event):
        if event==boundary:
            raise KeyboardInterrupt
    assert scan(root,config,_event=interrupt)==130
    committed = rows(config,'SELECT * FROM metadata_results WHERE legacy=0')
    hashes = rows(config,'SELECT * FROM file_hashes')
    assert scan(root,config)==0
    assert len(rows(config,'SELECT * FROM metadata_results WHERE legacy=0'))==2
    assert len(rows(config,'SELECT * FROM file_hashes'))==2
    assert all(r in rows(config,'SELECT * FROM metadata_results') for r in committed)
    assert all(r in rows(config,'SELECT * FROM file_hashes') for r in hashes)


def test_real_batch_isolates_corrupt_file_and_retries_do_not_hash_videos(collection):
    root,config = collection
    png(root/'good.png')
    (root/'bad.png').write_bytes(b'corrupt disposable data')
    mov(root/'video.mov')
    assert scan(root,config)==1
    counts = rows(config,"SELECT stage,state,count(*) n FROM processing_jobs GROUP BY stage,state")
    assert any(r['stage']=='metadata' and r['state']=='failed' for r in counts)
    assert len(rows(config,'SELECT * FROM file_hashes'))==2
    connection = connect(config.database)
    try:
        assert not verification(connection,1)['complete']
    finally:
        connection.close()
    png(root/'bad.png')
    assert scan(root,config)==0
    assert len(rows(config,'SELECT * FROM file_hashes'))==3  # Retains old revision result.
    assert not rows(config,"SELECT h.id FROM file_hashes h JOIN file_revisions r ON r.id=h.revision_id JOIN media_files m ON m.id=r.media_id WHERE m.media_type='video'")


def test_bounded_batch_performance_and_single_mode(collection,monkeypatch):
    root,config = collection
    paths = [jpeg(root/f'photo {i} 日本語.jpg') for i in range(32)]
    config = replace(config,metadata_batch_size=32)
    tool = ExifTool(config)
    import inventory_builder.exiftool as adapter
    original = adapter.run_bounded
    invocations = []
    def measured(command,**kwargs):
        if '-@' in command:
            invocations.append(command)
        return original(command,**kwargs)
    monkeypatch.setattr(adapter,'run_bounded',measured)
    start = time.perf_counter()
    batched = tool.extract_batch(paths)
    elapsed_batch = time.perf_counter()-start
    assert len(invocations)==1
    assert all(isinstance(value,dict) for value in batched)
    start = time.perf_counter()
    single = [tool.extract_batch([path])[0] for path in paths]
    elapsed_single = time.perf_counter()-start
    assert len(invocations)==33
    assert [normalize(value) for value in single]==[normalize(value) for value in batched]
    print(f'32 disposable JPEGs: batch={elapsed_batch:.3f}s ({32/elapsed_batch:.1f} files/s); single={elapsed_single:.3f}s. Bounded batch uses one metadata process instead of 32.')


def test_verify_cli_tracks_all_required_stages(collection,capsys,monkeypatch):
    from inventory_builder.cli import main
    root,config = collection
    png(root/'photo.png')
    mov(root/'movie.mov')
    monkeypatch.setenv('LOCALAPPDATA',str(root.parent/'local'))
    def interrupt(event):
        if event=='hash_claimed':
            raise KeyboardInterrupt
    assert scan(root,config,_event=interrupt)==130
    args = ['--database',str(config.database)]
    assert main([*args,'verify',str(root)])==1
    output = capsys.readouterr().out
    assert 'Fully inventoried: NO' in output and 'photo_sha256: completed=0' in output
    assert scan(root,config)==0
    assert main([*args,'verify',str(root)])==0
    output = capsys.readouterr().out
    assert 'Fully inventoried: YES' in output and 'pending=0' in output
    assert main([*args,'status',str(root)])==0


def test_photo_hash_known_digest_boundaries_and_video_guard(tmp_path):
    path = tmp_path/'disposable.jpg'
    for data in (b'',b'a',b'abc',b'12345678'*100):
        path.write_bytes(data)
        digest,count = hash_photo(path,fingerprint(path.stat()),media_type='photo',chunk_size=7)
        assert digest==hashlib.sha256(data).hexdigest() and count==len(data)
    with pytest.raises(ValueError,match='Videos'):
        hash_photo(path,fingerprint(path.stat()),media_type='video')
    original = fingerprint(path.stat())
    path.write_bytes(b'changed')
    with pytest.raises(FileChangedError):
        hash_photo(path,original,media_type='photo')


def test_metadata_timestamp_policy_and_provenance():
    raw = {'ExifIFD:DateTimeOriginal':'2024:01:02 03:04:05','ExifIFD:OffsetTimeOriginal':'+05:30','ExifIFD:SubSecTimeOriginal':'125','System:FileCreateDate':'2001:01:01 00:00:00Z','QuickTime:CreateDate':'2020:01:01 01:02:03','IFD0:Orientation':6,'File:ImageWidth':100,'File:ImageHeight':200}
    parsed = normalize(raw)
    assert parsed['selected']['datetime']=='2024-01-02T03:04:05.125000+05:30'
    assert parsed['dates']['ExifIFD:DateTimeOriginal']==raw['ExifIFD:DateTimeOriginal']
    assert 'System:FileCreateDate' not in parsed['dates']
    assert parsed['attributes']['display_width']==200
    assert normalize({'System:FileCreateDate':'2024:01:02 03:04:05'})['selected'] is None
    assert normalize({'ExifIFD:DateTimeOriginal':'0000:00:00 00:00:00'})['selected'] is None
    assert normalize({'QuickTime:CreateDate':'2024:01:02 03:04:05'})['selected']['confidence']=='embedded_timezone_unknown'
    assert normalize({'Matroska:VideoCodecID':'V_MPEG4/ISO/AVC','Matroska:AudioCodecID':'A_AAC'})['attributes']['video_codec']=='V_MPEG4/ISO/AVC'
    assert normalize({'ASF:VideoCodecName':'WMV','ASF:AudioCodecName':'WMA'})['attributes']['audio_codec']=='WMA'


def test_independent_retry_and_explicit_failed_retry(collection,monkeypatch):
    root,config = collection
    png(root/'a.png')
    real = ExifTool(config)
    class BrokenTool:
        version = real.version
        def __init__(self,config):
            pass
        def extract_batch(self,paths):
            raise TimeoutError('injected transient timeout')
    monkeypatch.setattr('inventory_builder.scanner.ExifTool',BrokenTool)
    assert scan(root,config)==1
    metadata = rows(config,"SELECT * FROM processing_jobs WHERE stage='metadata'")[0]
    assert metadata['attempts']==3 and metadata['state']=='failed'
    assert len(rows(config,'SELECT * FROM file_hashes'))==1
    assert len(rows(config,'SELECT * FROM scan_errors WHERE processing_job_id IS NOT NULL'))==3
    monkeypatch.setattr('inventory_builder.scanner.ExifTool',ExifTool)
    assert scan(root,config,retry_failed=True)==0
    assert rows(config,"SELECT attempts FROM processing_jobs WHERE stage='metadata'")[0]['attempts']==4
    assert len(rows(config,'SELECT * FROM file_hashes'))==1


def test_file_change_during_hash_discards_partial_digest_and_recovers(collection):
    root,config = collection
    path = png(root/'a.png')
    config = replace(config,hash_chunk_bytes=8)
    changed = False
    def change(event):
        nonlocal changed
        if event=='hash_chunk' and not changed:
            changed = True
            path.write_bytes(path.read_bytes()+b'disposable trailing data')
    assert scan(root,config,_event=change)==1
    assert not rows(config,'SELECT * FROM file_hashes')
    assert scan(root,config)==0
    assert len(rows(config,'SELECT * FROM file_hashes'))==1
    assert rows(config,'SELECT digest FROM file_hashes')[0]['digest']==hashlib.sha256(path.read_bytes()).hexdigest()


def test_misnamed_video_is_never_hashed(collection):
    root,config = collection
    mov(root/'video disguised as photo.jpg')
    assert scan(root,config)==1
    assert not rows(config,'SELECT * FROM file_hashes')
    assert rows(config,"SELECT last_error FROM processing_jobs WHERE stage='photo_sha256'")[0]['last_error'].startswith('Video content')
    # Explicit classification-policy correction changes only SQLite/configuration,
    # never the source name or bytes, and preserves the old failed job history.
    corrected = replace(config,photo_extensions=tuple(e for e in config.photo_extensions if e!='.jpg'),video_extensions=(*config.video_extensions,'.jpg'))
    assert scan(root,corrected,new_scan=True)==0
    assert rows(config,'SELECT media_type FROM media_files')[0]['media_type']=='video'
    assert not rows(config,'SELECT * FROM file_hashes')


def test_video_signature_guard_even_when_metadata_is_unavailable(collection,monkeypatch):
    root,config = collection
    mov(root/'movie with wrong extension.jpg')
    real = ExifTool(config)
    class NoMetadata:
        version = real.version
        def __init__(self,config): pass
        def extract_batch(self,paths): return [ValueError('injected unreadable metadata') for path in paths]
    monkeypatch.setattr('inventory_builder.scanner.ExifTool',NoMetadata)
    assert scan(root,config)==1
    assert not rows(config,'SELECT * FROM file_hashes')
    assert rows(config,"SELECT last_error FROM processing_jobs WHERE stage='photo_sha256'")[0]['last_error'].startswith('Video signature')


def test_real_metadata_output_bound_and_subprocess_timeout(collection):
    root,config = collection
    photo = png(root/'a.png')
    tool = ExifTool(replace(config,metadata_output_limit=10))
    with pytest.raises(ValueError,match='output limit'):
        tool.extract_batch([photo])
    from inventory_builder.bounded_process import run_bounded
    with pytest.raises(TimeoutError):
        run_bounded([sys.executable,'-c','import time; time.sleep(30)'],timeout=.1)


def schema_three(path,root):
    from importlib.resources import files
    from inventory_builder.paths import compare,MATCHER_VERSION
    from inventory_builder.sources import machine_id
    connection = sqlite3.connect(path,isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.create_collation('WINPATH',compare)
    connection.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY,checksum TEXT NOT NULL,applied_at TEXT NOT NULL DEFAULT 'now')")
    for name in ('001_initial.sql','002_folder_sources.sql','003_scan_policies.sql'):
        script = files('inventory_builder.storage').joinpath('sql',name).read_text(encoding='utf-8')
        connection.executescript(script)
        connection.execute('INSERT INTO schema_migrations(version,checksum) VALUES (?,?)',(int(name[:3]),hashlib.sha256(script.encode()).hexdigest()))
    connection.execute("INSERT INTO volumes(application_uuid,identity_kind,comparison_version,first_seen_at,last_seen_at) VALUES ('schema3-volume','physical',?,'now','now')",(MATCHER_VERSION,))
    connection.execute("INSERT INTO volume_observations(volume_id,machine_id,volume_guid,filesystem_serial,mount_path,capacity_bytes,observed_at) VALUES (1,?,'test-guid','serial-test',?,1000000,'now')",(machine_id(),str(root)))
    connection.execute("INSERT INTO scan_roots(volume_id,volume_relative_path,path_key,last_absolute_path,created_at) VALUES (1,'','',?,'now')",(str(root),))
    connection.execute("INSERT INTO scan_sessions(root_id,state,discovery_state,coverage_complete,created_at,completed_at,discovery_version) VALUES (1,'completed','completed',1,'now','now','targets-v2')")
    connection.execute("INSERT INTO scan_runs(session_id,started_at,ended_at,state,application_version,machine_id) VALUES (1,'now','now','completed','0.2.0','test')")
    connection.execute("INSERT INTO scan_directories(session_id,relative_path,state,path_key) VALUES (1,'','completed','')")
    info = (root/'a.png').stat()
    connection.execute("INSERT INTO media_files(volume_id,volume_relative_path,last_absolute_path,parent_path,filename,extension,media_type,size_bytes,modified_ns,first_inventoried_at,last_inventoried_at,path_key) VALUES (1,'a.png',?,'','a.png','.png','photo',?,?,'now','now','a.png')",(str(root/'a.png'),info.st_size,info.st_mtime_ns))
    connection.execute("INSERT INTO media_metadata(media_id,raw_metadata_json) VALUES (1,'legacy payload')")
    connection.execute("INSERT INTO scan_files(session_id,directory_id,relative_path,state,media_id,path_key) VALUES (1,1,'a.png','completed',1,'a.png')")
    connection.execute("INSERT INTO scan_file_observations VALUES (1,1,'a.png',?,?,'now')",(info.st_size,info.st_mtime_ns))
    connection.execute("INSERT INTO scan_errors(session_id,run_id,media_id,operation,category,message,occurred_at,resolved_at) VALUES (1,1,1,'stat','fixture','preserve history','now','now')")
    connection.execute('PRAGMA foreign_keys=ON')
    return connection


@pytest.mark.parametrize('fail',[False,True])
def test_migration_four_preserves_schema_three_and_rolls_back_late_failure(collection,fail):
    from inventory_builder.storage.migrations import migrate
    root,config = collection
    png(root/'a.png')
    path = config.database.with_name('schema3.sqlite3')
    connection = schema_three(path,root)
    tables = ('volumes','volume_observations','scan_roots','scan_sessions','scan_runs','scan_directories','scan_files','media_files','media_metadata','scan_file_observations','scan_errors','schema_migrations')
    saved = {table:([r[1] for r in connection.execute(f'PRAGMA table_info({table})')],[tuple(r) for r in connection.execute(f'SELECT * FROM {table}')]) for table in tables}
    try:
        if fail:
            connection.execute('CREATE TABLE file_hashes(deliberate_late_failure TEXT)')
            with pytest.raises(sqlite3.OperationalError):
                migrate(connection)
            assert connection.execute('SELECT max(version) FROM schema_migrations').fetchone()[0]==3
            assert not connection.execute("SELECT 1 FROM sqlite_master WHERE name='processing_jobs'").fetchone()
        else:
            migrate(connection)
            assert connection.execute('SELECT max(version) FROM schema_migrations').fetchone()[0]==4
            assert not connection.execute('PRAGMA foreign_key_check').fetchone()
            assert connection.execute('SELECT raw_metadata_json FROM metadata_results').fetchone()[0]=='legacy payload'
        for table,(columns,values) in saved.items():
            if table=='schema_migrations' and not fail:
                assert [tuple(r) for r in connection.execute('SELECT * FROM schema_migrations WHERE version<=3')]==values
            else:
                assert [tuple(r) for r in connection.execute(f"SELECT {','.join(columns)} FROM {table}")]==values
        assert connection.execute('PRAGMA foreign_keys').fetchone()[0]==1
    finally:
        connection.close()
    if not fail:
        legacy_config = replace(config,database=path)
        connection = connect(path)
        try:
            assert not verification(connection,1)['complete']
        finally:
            connection.close()
        assert scan(root,legacy_config)==0
        assert rows(legacy_config,'SELECT id FROM media_files')[0]['id']==1
        assert rows(legacy_config,"SELECT raw_metadata_json FROM metadata_results WHERE legacy=1")[0]['raw_metadata_json']=='legacy payload'
        assert len(rows(legacy_config,'SELECT * FROM file_hashes'))==1


@pytest.mark.parametrize('stage',['metadata_claimed','hash_chunk'])
def test_real_process_crash_preserves_jobs_and_committed_results(collection,stage):
    root,config = collection
    png(root/'a.png')
    png(root/'b.png')
    code = '''
import os,sys
from pathlib import Path
from inventory_builder.config import Config
from inventory_builder.volumes import Volume
import inventory_builder.scanner as scanner
root=Path(sys.argv[1])
scanner.selected_volume=lambda selected:Volume(root,'test-guid','serial-test','TESTFS',1000000)
def fault(event):
    if event==sys.argv[4]: os._exit(41)
scanner.scan_folder(root,Config(Path(sys.argv[2]),Path(sys.argv[3]),metadata_batch_size=16),progress=lambda text:None,_event=fault)
'''
    env = dict(os.environ,PYTHONPATH=str(Path(__file__).resolve().parents[1]/'src'))
    result = subprocess.run([sys.executable,'-c',code,str(root),str(config.database),str(config.log_directory),stage],env=env,capture_output=True,text=True,timeout=20)
    assert result.returncode==41,result.stderr
    before = rows(config,'SELECT * FROM metadata_results WHERE legacy=0')
    assert rows(config,"SELECT id FROM processing_jobs WHERE state='processing'")
    if stage=='hash_chunk':
        assert len(before)==2
        assert not rows(config,'SELECT * FROM file_hashes')
    assert scan(root,config)==0
    assert len(rows(config,'SELECT * FROM metadata_results WHERE legacy=0'))==2
    assert len(rows(config,'SELECT * FROM file_hashes'))==2
    assert all(row in rows(config,'SELECT * FROM metadata_results') for row in before)


def test_result_commit_rollback_and_video_database_guards(collection):
    root,config = collection
    png(root/'a.png')
    mov(root/'b.mov')
    connection = connect(config.database)
    try:
        connection.execute("CREATE TRIGGER fail_hash BEFORE INSERT ON file_hashes BEGIN SELECT RAISE(ABORT,'injected commit failure'); END")
    finally:
        connection.close()
    with pytest.raises(sqlite3.IntegrityError,match='injected'):
        scan(root,config)
    assert not rows(config,'SELECT * FROM file_hashes')
    assert rows(config,"SELECT id FROM processing_jobs WHERE stage='photo_sha256' AND state='processing'")
    connection = connect(config.database)
    try:
        connection.execute('DROP TRIGGER fail_hash')
    finally:
        connection.close()
    assert scan(root,config)==0
    connection = connect(config.database)
    try:
        video = connection.execute("SELECT current_revision_id FROM media_files WHERE media_type='video'").fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError,match='Videos'):
            connection.execute("INSERT INTO processing_jobs(revision_id,stage,policy_version,tool_version,created_at) VALUES (?,'photo_sha256','x','x','now')",(video,))
        photo = connection.execute("SELECT current_revision_id FROM media_files WHERE media_type='photo'").fetchone()[0]
        job = connection.execute("INSERT INTO processing_jobs(revision_id,stage,policy_version,tool_version,created_at) VALUES (?,'photo_sha256','test-other','x','now')",(photo,)).lastrowid
        with pytest.raises(sqlite3.IntegrityError,match='complete result'):
            connection.execute("UPDATE processing_jobs SET state='completed' WHERE id=?",(job,))
        with pytest.raises(sqlite3.IntegrityError,match='partial'):
            connection.execute("INSERT INTO file_hashes(job_id,revision_id,algorithm,digest,bytes_read,completed_at) VALUES (?,?,'sha256',?,0,'now')",(job,photo,'0'*64))
    finally:
        connection.close()


def test_processing_rechecks_exclusions_and_disconnect_retry(collection,monkeypatch):
    root,config = collection
    png(root/'a.png')
    private = root/'Private'
    private.mkdir()
    png(private/'not authorized.png')
    original = __import__('inventory_builder.processing',fromlist=['hash_photo']).hash_photo
    attempts = 0
    def disconnect(path,*args,**kwargs):
        nonlocal attempts
        assert not path.is_relative_to(private)
        attempts+=1
        if attempts==1:
            raise OSError('injected removable-drive disconnect')
        return original(path,*args,**kwargs)
    monkeypatch.setattr('inventory_builder.processing.hash_photo',disconnect)
    assert scan(root,config,exclude=['Private'])==0
    assert attempts==2
    assert len(rows(config,'SELECT * FROM media_files'))==1
    assert len(rows(config,'SELECT * FROM file_hashes'))==1
    assert rows(config,"SELECT attempts FROM processing_jobs WHERE stage='photo_sha256'")[0]['attempts']==2


def test_metadata_change_and_date_policy_reprocess_only_needed_stage(collection):
    root,config = collection
    path = png(root/'a.png')
    changed = False
    def mutate(event):
        nonlocal changed
        if event=='metadata_extracted' and not changed:
            changed=True
            path.write_bytes(path.read_bytes()+b'disposable change')
    assert scan(root,config,_event=mutate)==1
    assert not rows(config,'SELECT * FROM metadata_results WHERE legacy=0')
    assert scan(root,config)==0
    assert len(rows(config,'SELECT * FROM file_hashes'))==1
    new_policy = replace(config,capture_date_precedence=('CreateDate','DateTimeOriginal'))
    assert scan(root,new_policy)==0
    assert len(rows(config,'SELECT * FROM file_hashes'))==1
    assert len(rows(config,'SELECT * FROM metadata_results WHERE legacy=0'))==2
    assert rows(config,'SELECT selected_capture_datetime FROM media_metadata')[0]['selected_capture_datetime'].startswith('2024-02-03T04:05:07')
    assert scan(root,config)==0
    assert len(rows(config,'SELECT * FROM metadata_results WHERE legacy=0'))==2
    assert rows(config,'SELECT selected_capture_datetime FROM media_metadata')[0]['selected_capture_datetime'].startswith('2024-02-03T04:05:06')


def test_incomplete_scheduling_never_verifies_as_complete(collection):
    root,config = collection
    png(root/'a.png')
    assert scan(root,config)==0
    connection = connect(config.database)
    try:
        assert verification(connection,1)['complete']
        connection.execute("UPDATE session_processing_jobs SET required=0 WHERE job_id IN (SELECT id FROM processing_jobs WHERE stage='photo_sha256')")
        assert not verification(connection,1)['complete']
    finally:
        connection.close()


def test_single_file_debug_mode_is_used_by_integrated_scanner(collection,monkeypatch):
    root,config = collection
    for number in range(3):
        png(root/f'{number}.png')
    sizes = []
    original = ExifTool.extract_batch
    def measured(tool,paths):
        sizes.append(len(paths))
        return original(tool,paths)
    monkeypatch.setattr(ExifTool,'extract_batch',measured)
    assert scan(root,replace(config,metadata_mode='single'))==0
    assert sizes==[1,1,1]


def test_exiftool_child_temporary_paths_are_excluded_runtime(collection,monkeypatch):
    root,config = collection
    photo = png(root/'a.png')
    import inventory_builder.exiftool as adapter
    original = adapter.run_bounded
    calls = []
    def checked(command,**kwargs):
        expected = str(config.database.parent/'tmp')
        assert all(kwargs['env'][name]==expected for name in ('TEMP','TMP','TMPDIR'))
        calls.append(command)
        return original(command,**kwargs)
    monkeypatch.setattr(adapter,'run_bounded',checked)
    tool = ExifTool(config)
    assert isinstance(tool.extract_batch([photo])[0],dict)
    assert len(calls)==2


def test_windows_job_closes_descendant_processes():
    from inventory_builder.bounded_process import run_bounded
    script = "import subprocess,sys,time; time.sleep(.1); subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); time.sleep(30)"
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        run_bounded([sys.executable,'-c',script],timeout=.5)
    assert time.monotonic()-start<8
