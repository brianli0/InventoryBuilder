from dataclasses import replace
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from inventory_builder.config import Config
from inventory_builder.scanner import scan_folder
from inventory_builder.storage.database import connect
from inventory_builder.storage.identities import promote_folder
from inventory_builder.storage.migrations import migrate


@pytest.fixture
def setup_scan(tmp_path, database, monkeypatch):
    root = tmp_path / 'disposable source'
    root.mkdir()
    from inventory_builder.volumes import Volume
    monkeypatch.setattr('inventory_builder.scanner.selected_volume', lambda path: Volume(root, 'test-guid', '0001', 'TESTFS', 1000000, 'Disposable volume'))
    return root, Config(database, tmp_path / 'logs', batch_size=1)


def run(root, config, **kwargs):
    return scan_folder(root, config, progress=lambda text: None, **kwargs)


def query(config, sql, parameters=()):
    connection = connect(config.database)
    try:
        return [dict(r) for r in connection.execute(sql, parameters)]
    finally:
        connection.close()


def populate(root):
    (root / 'empty').mkdir()
    (root / 'space 日本語').mkdir()
    for relative in ('a.JPG', 'space 日本語/a.JPG', 'space 日本語/movie.MOV'):
        (root / relative).write_bytes(b'disposable sample')
    (root / 'ignored.txt').write_text('unsupported')


def test_discovery_incremental_and_source_unchanged(setup_scan):
    root, config = setup_scan
    populate(root)
    paths = [p for p in root.rglob('*') if p.is_file()]
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_ctime_ns) for p in paths}
    assert run(root, config) == 0
    files = query(config, 'SELECT * FROM media_files')
    assert len(files) == 3
    assert {r['filename'] for r in files} == {'a.JPG', 'movie.MOV'}
    assert all(Path(r['last_absolute_path']).is_relative_to(root) for r in files)
    assert all(r['created_ns'] is not None for r in files) if os.name == 'nt' else True
    assert query(config, 'SELECT count(*) n FROM scan_directories')[0]['n'] == 3
    assert query(config, 'SELECT unsupported_files FROM scan_runs')[0]['unsupported_files'] == 1
    assert run(root, config) == 0
    assert query(config, 'SELECT files_skipped FROM scan_runs ORDER BY id DESC LIMIT 1')[0]['files_skipped'] == 3
    assert query(config, 'SELECT count(*) n FROM media_files')[0]['n'] == 3
    assert query(config, 'SELECT count(*) n FROM scan_file_observations')[0]['n'] == 6
    assert before == {p: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_ctime_ns) for p in paths}
    old_id = next(r['id'] for r in files if r['volume_relative_path'] == 'a.JPG')
    (root / 'a.JPG').write_bytes(b'changed disposable content')
    (root / 'new.mp4').write_bytes(b'new')
    (root / 'space 日本語/movie.MOV').unlink()
    assert run(root, config) == 0
    latest = query(config, 'SELECT * FROM scan_runs ORDER BY id DESC LIMIT 1')[0]
    assert latest['files_processed'] == 2
    assert latest['files_skipped'] == 1
    assert query(config, 'SELECT id FROM media_files WHERE volume_relative_path=?', ('a.JPG',))[0]['id'] == old_id
    assert query(config, 'SELECT count(*) n FROM media_files')[0]['n'] == 4


@pytest.mark.parametrize('boundary', ['run_started', 'directory_claimed', 'discovery_committed', 'file_claimed', 'file_committed', 'directory_committed'])
def test_interrupt_resume_every_boundary(setup_scan, boundary):
    root, config = setup_scan
    populate(root)

    def interrupt(event):
        if event == boundary:
            raise KeyboardInterrupt

    assert run(root, config, _event=interrupt) == 130
    observations = query(config, 'SELECT * FROM scan_file_observations')
    assert run(root, config) == 0
    assert query(config, 'SELECT count(*) n FROM media_files')[0]['n'] == 3
    assert query(config, 'SELECT count(*) n FROM scan_file_observations')[0]['n'] == 3
    for observed in observations:
        saved = query(config, 'SELECT * FROM scan_file_observations WHERE session_id=? AND media_id=?', (observed['session_id'], observed['media_id']))[0]
        assert saved == observed
    assert query(config, 'SELECT count(*) n FROM scan_sessions')[0]['n'] == 1
    assert query(config, "SELECT count(*) n FROM scan_files WHERE state!='completed'")[0]['n'] == 0


def test_real_process_crash_with_unfinished_file_and_observation(setup_scan):
    root, config = setup_scan
    populate(root)
    code = '''
import os, sys
from pathlib import Path
from inventory_builder.config import Config
from inventory_builder.scanner import scan_folder
from inventory_builder.volumes import Volume
import inventory_builder.scanner as scanner
scanner.selected_volume = lambda path: Volume(Path(sys.argv[1]), 'test-guid', '0001', 'TESTFS', 1000000, 'Disposable volume')
class FixtureTool:
    version = 'fixture'
    def __init__(self, config): pass
    def extract_batch(self, paths): return [dict(SourceFile=str(path), **{'File:FileType':'JPEG'}) for path in paths]
scanner.ExifTool = FixtureTool
commits = 0
def fault(event):
    global commits
    if event == 'file_committed': commits += 1
    if event == 'file_claimed' and commits == 1: os._exit(23)
scan_folder(Path(sys.argv[1]), Config(Path(sys.argv[2]), Path(sys.argv[3]), batch_size=1), progress=lambda text: None, _event=fault)
'''
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'src'))
    process = subprocess.run([sys.executable, '-c', code, str(root), str(config.database), str(config.log_directory)], env=env, timeout=20, capture_output=True, text=True)
    assert process.returncode == 23, process.stderr
    saved = query(config, 'SELECT * FROM scan_file_observations')
    assert len(saved) == 1
    assert query(config, "SELECT count(*) n FROM scan_files WHERE state='processing'")[0]['n'] == 1
    assert run(root, config) == 0
    assert query(config, 'SELECT count(*) n FROM scan_file_observations')[0]['n'] == 3
    assert query(config, 'SELECT count(*) n FROM media_files')[0]['n'] == 3
    assert saved[0] in query(config, 'SELECT * FROM scan_file_observations')
    assert query(config, 'SELECT state FROM scan_runs ORDER BY id')[0]['state'] == 'interrupted'


def test_abandoned_metadata_claim_preserves_observation(setup_scan):
    root, config = setup_scan
    populate(root)

    def interrupt(event):
        if event == 'file_committed':
            connection = connect(config.database)
            try:
                connection.execute("UPDATE media_metadata SET state='processing',claiming_run_id=1,raw_metadata_json='preserved' WHERE media_id=1")
            finally:
                connection.close()
            raise KeyboardInterrupt

    assert run(root, config, _event=interrupt) == 130
    saved = query(config, 'SELECT * FROM scan_file_observations')
    assert run(root, config) == 0
    metadata = query(config, 'SELECT * FROM media_metadata WHERE media_id=1')[0]
    assert metadata['state'] == 'completed'
    assert metadata['claiming_run_id'] is None
    assert query(config,"SELECT raw_metadata_json FROM metadata_results WHERE raw_metadata_json='preserved'")
    assert saved[0] in query(config, 'SELECT * FROM scan_file_observations')


def test_inaccessible_file_retry(setup_scan, monkeypatch):
    root, config = setup_scan
    populate(root)
    import inventory_builder.scanner as scanner
    original = scanner.safe_path

    def denied(selected, relative, expected):
        if relative == 'a.JPG':
            raise PermissionError('injected file access denial')
        return original(selected, relative, expected)

    monkeypatch.setattr(scanner, 'safe_path', denied)
    assert run(root, config) == 1
    assert query(config, 'SELECT count(*) n FROM media_files')[0]['n'] == 2
    assert query(config, 'SELECT coverage_complete FROM scan_sessions')[0]['coverage_complete'] == 0
    monkeypatch.setattr(scanner, 'safe_path', original)
    assert run(root, config) == 0
    assert query(config, 'SELECT count(*) n FROM media_files')[0]['n'] == 3
    assert query(config, 'SELECT count(*) n FROM scan_errors WHERE resolved_at IS NULL')[0]['n'] == 0


def test_inaccessible_directory_retry(setup_scan, monkeypatch):
    root, config = setup_scan
    populate(root)
    original = os.scandir

    def denied(path):
        if Path(path).name == 'space 日本語':
            raise PermissionError('injected directory access denial')
        return original(path)

    monkeypatch.setattr(os, 'scandir', denied)
    assert run(root, config) == 1
    assert query(config, 'SELECT count(*) n FROM media_files')[0]['n'] == 1
    monkeypatch.setattr(os, 'scandir', original)
    assert run(root, config) == 0
    assert query(config, 'SELECT count(*) n FROM media_files')[0]['n'] == 3


def test_safety_root_runtime_overlap_and_disconnection(setup_scan):
    root, config = setup_scan
    populate(root)
    # Never invoke a real drive root in tests. Ambiguous drive-relative syntax is
    # rejected before any volume/source access.
    with pytest.raises(ValueError, match='Drive-relative'):
        run(Path('E:'), config)
    assert run(root, replace(config, log_directory=root / 'logs')) == 0
    with pytest.raises(ValueError, match='outside'):
        run(root, replace(config, database=root / 'db.sqlite3'))
    assert (root / 'logs' / 'inventory_builder.log').is_file()
    assert not (root / 'db.sqlite3').exists()
    assert run(root, config) == 0
    assert run(root / 'empty', config) == 0
    root.rename(root.with_name('temporarily absent'))
    with pytest.raises(OSError):
        run(root, config)
    assert query(config, 'SELECT count(*) n FROM media_files')[0]['n'] == 3


def test_skip_reparse_directory_without_enumerating(setup_scan, monkeypatch):
    root, config = setup_scan
    populate(root)
    import inventory_builder.scanner as scanner
    import stat
    # DirEntry.stat() on Windows may report inode 0, unlike Path.stat().
    os.utime(root / 'space 日本語', ns=(1_600_000_000_000_000_000, 1_600_000_000_000_000_000))
    blocked = (root / 'space 日本語').stat().st_mtime_ns
    original = scanner.is_reparse
    monkeypatch.setattr(scanner, 'is_reparse', lambda info: (stat.S_ISDIR(info.st_mode) and info.st_mtime_ns == blocked) or original(info))
    assert run(root, config) == 0
    assert query(config, 'SELECT count(*) n FROM media_files')[0]['n'] == 1


def physical(connection):
    return connection.execute("INSERT INTO volumes(application_uuid,identity_kind,first_seen_at,last_seen_at) VALUES ('physical','physical','now','now')").lastrowid


def test_identity_conversion_preserves_ids_metadata_and_history(setup_scan, monkeypatch):
    root, config = setup_scan
    populate(root)
    assert run(root, config) == 0
    connection = connect(config.database)
    try:
        connection.execute("UPDATE volumes SET identity_kind='folder' WHERE id=1")
        target = physical(connection)
        connection.execute("UPDATE media_metadata SET raw_metadata_json='keep me'")
        connection.execute("UPDATE media_metadata SET state='completed', processed_size_bytes=(SELECT size_bytes FROM media_files WHERE id=media_id), processed_modified_ns=(SELECT modified_ns FROM media_files WHERE id=media_id), extractor='future-test',extractor_version='1',extraction_policy_version='1',extracted_at='now'")
        metadata = [tuple(r) for r in connection.execute('SELECT * FROM media_metadata ORDER BY media_id')]
        observations = list(connection.execute('SELECT * FROM scan_file_observations'))
        promote_folder(connection, 1, target, 'Pictures/Selected', 'now')
        assert all(r['volume_id'] == target and r['volume_relative_path'].startswith('Pictures/Selected/') for r in connection.execute('SELECT * FROM media_files'))
        assert list(connection.execute('SELECT * FROM scan_file_observations')) == observations
        assert all(r[0] == 'keep me' for r in connection.execute('SELECT raw_metadata_json FROM media_metadata'))
        assert [tuple(r) for r in connection.execute('SELECT * FROM media_metadata ORDER BY media_id')] == metadata
        assert connection.execute('SELECT count(*) FROM identity_transitions').fetchone()[0] == 1
        assert connection.execute('SELECT selected_path FROM folder_bindings').fetchone()[0] == str(root)
    finally:
        connection.close()
    from inventory_builder.volumes import Volume
    new_root = root.parent / 'Pictures' / 'Selected'
    new_root.parent.mkdir()
    root.rename(new_root)
    monkeypatch.setattr('inventory_builder.scanner.selected_volume', lambda path: Volume(root.parent, 'confirmed-target', '0002', 'TESTFS', 1000000))
    connection = connect(config.database)
    try:
        connection.execute("INSERT INTO volume_observations(volume_id,machine_id,volume_guid,filesystem_serial,mount_path,capacity_bytes,observed_at) VALUES (2,'test','confirmed-target','0002',?,1000000,'now')", (str(root.parent),))
    finally:
        connection.close()
    assert run(new_root, config) == 0
    assert query(config, 'SELECT count(*) n FROM media_files')[0]['n'] == 3
    assert query(config, 'SELECT files_skipped FROM scan_runs ORDER BY id DESC LIMIT 1')[0]['files_skipped'] == 3
    assert all(r['state'] == 'completed' for r in query(config, 'SELECT state FROM media_metadata'))


@pytest.mark.parametrize('collision', ['root', 'file', 'case_alias'])
def test_identity_collision_rolls_back_everything(setup_scan, collision):
    root, config = setup_scan
    populate(root)
    assert run(root, config) == 0
    connection = connect(config.database)
    try:
        connection.execute("UPDATE volumes SET identity_kind='folder' WHERE id=1")
        target = physical(connection)
        if collision == 'root':
            connection.execute("INSERT INTO scan_roots(volume_id,volume_relative_path,last_absolute_path,created_at) VALUES (?,'Pictures','other','now')", (target,))
        else:
            connection.execute("INSERT INTO media_files(volume_id,volume_relative_path,last_absolute_path,parent_path,filename,extension,media_type,size_bytes,modified_ns,first_inventoried_at,last_inventoried_at) SELECT ?, 'Pictures/' || volume_relative_path,last_absolute_path,parent_path,filename,extension,media_type,size_bytes,modified_ns,first_inventoried_at,last_inventoried_at FROM media_files WHERE id=(SELECT max(id) FROM media_files)", (target,))
            if collision == 'case_alias':
                connection.execute('UPDATE media_files SET volume_relative_path=upper(volume_relative_path) WHERE volume_id=?', (target,))
        before = [tuple(r) for r in connection.execute('SELECT * FROM media_files ORDER BY id')]
        with pytest.raises(ValueError, match='collision'):
            promote_folder(connection, 1, target, 'Pictures', 'now')
        assert [tuple(r) for r in connection.execute('SELECT * FROM media_files ORDER BY id')] == before
        assert connection.execute('SELECT volume_id FROM scan_roots WHERE id=1').fetchone()[0] != target
        assert connection.execute('SELECT count(*) FROM identity_transitions').fetchone()[0] == 0
    finally:
        connection.close()


def test_upgrade_from_real_v1_preserves_records(tmp_path):
    from inventory_builder.storage.migrations import files
    path = tmp_path / 'v1.sqlite3'
    connection = sqlite3.connect(path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        script = files('inventory_builder.storage').joinpath('sql/001_initial.sql').read_text(encoding='utf-8')
        connection.executescript(script)
        from hashlib import sha256
        connection.execute('CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY,checksum TEXT NOT NULL,applied_at TEXT)')
        connection.execute('INSERT INTO schema_migrations VALUES (1,?,?)', (sha256(script.encode()).hexdigest(), 'now'))
        connection.execute("INSERT INTO volumes(application_uuid,first_seen_at,last_seen_at) VALUES ('existing','now','now')")
        migrate(connection)
        assert connection.execute('SELECT application_uuid,identity_kind FROM volumes').fetchone()[:] == ('existing', 'unknown')
        assert connection.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 4
    finally:
        connection.close()


def test_completed_directory_unfinished_files_resume(setup_scan):
    root, config = setup_scan
    (root / 'a.jpg').write_bytes(b'a')
    (root / 'b.jpg').write_bytes(b'b')
    config = replace(config, batch_size=100)

    def interrupt(event):
        if event == 'directory_committed':
            raise KeyboardInterrupt

    assert run(root, config, _event=interrupt) == 130
    assert query(config, "SELECT state FROM scan_directories")[0]['state'] == 'completed'
    assert query(config, "SELECT count(*) n FROM scan_files WHERE state='pending'")[0]['n'] == 2
    assert run(root, config) == 0
    assert query(config, 'SELECT count(*) n FROM media_files')[0]['n'] == 2
    assert query(config, 'SELECT count(*) n FROM scan_file_observations')[0]['n'] == 2


def test_file_and_observation_atomic_rollback(setup_scan):
    root, config = setup_scan
    populate(root)
    connection = connect(config.database)
    try:
        connection.execute("CREATE TRIGGER fail_observation BEFORE INSERT ON scan_file_observations WHEN (SELECT count(*) FROM scan_file_observations) >= 1 BEGIN SELECT RAISE(ABORT, 'injected database failure'); END")
    finally:
        connection.close()
    with pytest.raises(sqlite3.IntegrityError, match='injected'):
        run(root, config)
    assert query(config, 'SELECT count(*) n FROM media_files')[0]['n'] == 1
    saved = query(config, 'SELECT * FROM scan_file_observations')
    assert len(saved) == 1
    connection = connect(config.database)
    try:
        connection.execute('DROP TRIGGER fail_observation')
    finally:
        connection.close()
    assert run(root, config) == 0
    assert query(config, 'SELECT count(*) n FROM media_files')[0]['n'] == 3
    assert saved[0] in query(config, 'SELECT * FROM scan_file_observations')


def test_signal_interrupt_and_cli(setup_scan, monkeypatch, capsys):
    import signal
    from inventory_builder.cli import main
    root, config = setup_scan
    populate(root)

    def interrupt(event):
        if event == 'file_committed':
            signal.raise_signal(signal.SIGINT)

    assert run(root, config, _event=interrupt) == 130
    monkeypatch.setenv('LOCALAPPDATA', str(root.parent / 'localappdata'))
    args = ['--database', str(config.database)]
    assert main([*args, 'scan', str(root)]) == 0
    assert main([*args, 'status']) == 0
    assert main([*args, 'volumes']) == 0
    output = capsys.readouterr().out
    assert 'Scan completed' in output
    assert 'checkpoint:' in output
    assert '| physical |' in output


def test_replaced_folder_refused_without_losing_inventory(setup_scan):
    root, config = setup_scan
    populate(root)
    assert run(root, config) == 0
    root.rename(root.with_name('old disposable source'))
    root.mkdir()
    with pytest.raises(ValueError, match='identity changed'):
        run(root, config)
    assert query(config, 'SELECT count(*) n FROM media_files')[0]['n'] == 3


def test_extension_configuration(tmp_path):
    from inventory_builder.config import load_config
    path = tmp_path / 'config.toml'
    path.write_text('photo_extensions=[".JPEG", ".test"]\nvideo_extensions=[]', encoding='utf-8')
    assert load_config(path).photo_extensions == ('.jpeg', '.test')
    path.write_text('photo_extensions=["jpg"]', encoding='utf-8')
    with pytest.raises(ValueError, match='extensions'):
        load_config(path)
    path.write_text('photo_extensions=[".mp4"]', encoding='utf-8')
    with pytest.raises(ValueError, match='overlap'):
        load_config(path)


def test_never_write_source_contents_or_enumerate_unrelated_folders(setup_scan, monkeypatch):
    root, config = setup_scan
    populate(root)
    unrelated = root.parent / 'unrelated'
    unrelated.mkdir()
    (unrelated / 'private.jpg').write_bytes(b'do not visit')
    import builtins
    original_open = builtins.open
    original_scandir = os.scandir
    enumerated = []

    def guarded_open(file, mode='r', *args, **kwargs):
        if isinstance(file,(str,os.PathLike)) and Path(file).is_relative_to(root):
            assert mode=='rb', 'source files may only be opened for binary reads'
        return original_open(file,mode,*args,**kwargs)

    def guarded_scandir(path):
        if Path(path) == Path(__file__).resolve().parents[1] / 'src/inventory_builder/storage/sql':
            # Reading packaged application migrations is not source discovery.
            return original_scandir(path)
        assert Path(path).is_relative_to(root), 'enumeration escaped selected root'
        enumerated.append(Path(path))
        return original_scandir(path)

    monkeypatch.setattr(builtins,'open',guarded_open)
    monkeypatch.setattr(os, 'scandir', guarded_scandir)
    assert run(root, config) == 0
    assert set(enumerated) == {root, root / 'empty', root / 'space 日本語'}
    assert query(config, 'SELECT count(*) n FROM media_files')[0]['n'] == 3
