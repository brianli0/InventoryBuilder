from dataclasses import replace
from hashlib import sha256
from importlib.resources import files
import json
import os
from pathlib import Path
import sqlite3

import pytest

from inventory_builder.config import Config, load_config
from inventory_builder.exclusions import compile_policy
from inventory_builder.paths import MATCHER_VERSION, compare
from inventory_builder.scanner import scan_folder
from inventory_builder.sources import machine_id
from inventory_builder.storage.database import connect
from inventory_builder.storage.migrations import migrate
from inventory_builder.volumes import Volume


@pytest.fixture
def target(tmp_path, database, monkeypatch):
    mount = tmp_path / 'disposable volume'
    mount.mkdir()
    volume = Volume(mount, 'guid-selected', 'serial-1', 'TESTFS', 10000000)
    monkeypatch.setattr('inventory_builder.scanner.selected_volume', lambda path: volume)
    return mount, Config(database, tmp_path / 'logs', batch_size=1), volume


def scan(root, config, **options):
    return scan_folder(root, config, progress=lambda text: None, **options)


def rows(config, sql, args=()):
    connection = connect(config.database)
    try:
        return [dict(row) for row in connection.execute(sql, args)]
    finally:
        connection.close()


def interrupt_after_file(event):
    if event == 'file_committed':
        raise KeyboardInterrupt


def test_mocked_volume_root_and_overlapping_roots_share_ids(target):
    root, config, _ = target
    photos = root / 'Photos'
    photos.mkdir()
    (photos / 'a.jpg').write_bytes(b'a')
    (root / 'video.mp4').write_bytes(b'video')
    assert scan(photos, config) == 0
    before = rows(config, 'SELECT id FROM media_files')[0]['id']
    assert scan(root, config) == 0
    assert len(rows(config, 'SELECT * FROM media_files')) == 2
    assert rows(config, "SELECT id FROM media_files WHERE volume_relative_path='Photos/a.jpg'")[0]['id'] == before
    assert len(rows(config, 'SELECT * FROM volumes')) == 1
    assert len(rows(config, 'SELECT * FROM scan_roots')) == 2
    assert rows(config, 'SELECT files_skipped FROM scan_runs ORDER BY id DESC LIMIT 1')[0]['files_skipped'] == 1


def test_changed_mount_path_resumes_same_volume_and_session(target, monkeypatch):
    root, config, volume = target
    (root / 'a.jpg').write_bytes(b'a')
    (root / 'b.jpg').write_bytes(b'b')
    assert scan(root, config, _event=interrupt_after_file) == 130
    before = rows(config, 'SELECT * FROM scan_file_observations')
    moved = root.with_name('different drive letter simulation')
    root.rename(moved)
    monkeypatch.setattr('inventory_builder.scanner.selected_volume', lambda path: replace(volume, mount=moved))
    assert scan(moved, config) == 0
    assert len(rows(config, 'SELECT * FROM volumes')) == 1
    assert len(rows(config, 'SELECT * FROM scan_sessions')) == 1
    assert len(rows(config, 'SELECT * FROM media_files')) == 2
    assert before[0] in rows(config, 'SELECT * FROM scan_file_observations')
    assert rows(config, 'SELECT last_absolute_path FROM scan_roots')[0]['last_absolute_path'] == str(moved)


def test_excluded_subtrees_are_never_enumerated_and_nested_names_are_not_global(target, monkeypatch):
    root, config, _ = target
    for name in ('Old', 'Older', 'Cache', 'Windows', 'Photos', 'Photos/Windows', '$RECYCLE.BIN'):
        path = root / name
        path.mkdir(parents=True, exist_ok=True)
        (path / 'sample.jpg').write_bytes(b'sample')
    original = os.scandir
    visited = []
    forbidden = {root / 'Old', root / 'Cache', root / 'Windows', root / '$RECYCLE.BIN'}

    def guarded(path):
        assert Path(path) not in forbidden
        visited.append(Path(path))
        return original(path)

    monkeypatch.setattr(os, 'scandir', guarded)
    assert scan(root, replace(config, exclude_directories=('Cache',)), exclude=['old', 'Old/child']) == 0
    paths = {r['volume_relative_path'] for r in rows(config, 'SELECT * FROM media_files')}
    assert paths == {'Older/sample.jpg', 'Photos/sample.jpg', 'Photos/Windows/sample.jpg'}
    assert len(rows(config, "SELECT * FROM scan_coverage WHERE status='excluded'")) >= 4
    assert not forbidden.intersection(visited)


@pytest.mark.parametrize('exclusion', ['../outside', 'E:relative', 'safe:stream', '.', 'folder/../../escape', '\\\\?\\C:\\escape'])
def test_invalid_exclusion_paths_refused_before_traversal(target, exclusion, monkeypatch):
    root, config, _ = target
    original = os.scandir

    def guarded(path):
        assert not Path(path).is_relative_to(root), 'traversal occurred despite invalid exclusion'
        return original(path)

    monkeypatch.setattr(os, 'scandir', guarded)
    with pytest.raises(ValueError):
        scan(root, config, exclude=[exclusion])


def test_file_exclusion_refused_nonexistent_directory_accepted(target):
    root, config, _ = target
    (root / 'a.jpg').write_bytes(b'a')
    with pytest.raises(ValueError, match='existing file'):
        scan(root, config, exclude=['a.jpg'])
    assert scan(root, config, exclude=['future']) == 0


def test_policy_change_requires_explicit_successor_and_preserves_history(target):
    root, config, _ = target
    (root / 'a.jpg').write_bytes(b'a')
    (root / 'b.jpg').write_bytes(b'b')
    assert scan(root, config, _event=interrupt_after_file) == 130
    saved = rows(config, 'SELECT * FROM scan_file_observations')
    connection = connect(config.database)
    try:
        connection.execute("UPDATE media_metadata SET state='processing',claiming_run_id=1,raw_metadata_json='preserved prior payload'")
    finally:
        connection.close()
    old_policy = rows(config, 'SELECT * FROM scan_policies')
    with pytest.raises(ValueError, match='policy changed'):
        scan(root, config, exclude=['new exclusion'])
    assert scan(root, config, exclude=['new exclusion'], new_scan=True) == 0
    sessions = rows(config, 'SELECT * FROM scan_sessions ORDER BY id')
    assert sessions[0]['state'] == 'superseded'
    assert sessions[1]['predecessor_id'] == sessions[0]['id']
    assert saved[0] in rows(config, 'SELECT * FROM scan_file_observations')
    assert old_policy[0] in rows(config, 'SELECT * FROM scan_policies')
    assert len(rows(config, 'SELECT * FROM media_files')) == 2
    assert rows(config, 'SELECT state FROM media_metadata WHERE media_id=1')[0]['state']=='completed'
    assert rows(config,"SELECT raw_metadata_json FROM metadata_results WHERE raw_metadata_json='preserved prior payload'")


def test_explicit_saved_resume_and_same_policy_auto_resume(target):
    root, config, _ = target
    (root / 'a.jpg').write_bytes(b'a')
    (root / 'b.jpg').write_bytes(b'b')
    assert scan(root, config, exclude=['Old', 'Cache'], _event=interrupt_after_file) == 130
    # Ordering, spelling, duplicates and narrower redundant rules have no effect.
    assert scan(root, config, exclude=['cache', 'old', 'Old', 'Old/nested']) == 0
    assert len(rows(config, 'SELECT * FROM scan_sessions')) == 1
    assert scan(root, config, _event=interrupt_after_file) == 130
    session = rows(config, 'SELECT max(id) id FROM scan_sessions')[0]['id']
    assert scan(root, config, exclude=['different'], resume_session=session) == 0


def test_reconcile_preserves_previously_excluded_or_unavailable_records(target):
    root, config, _ = target
    private = root / 'Private'
    private.mkdir()
    (private / 'a.jpg').write_bytes(b'a')
    assert scan(root, config) == 0
    assert scan(root, config, exclude=['Private'], reconcile_session=1) == 0
    assert len(rows(config, 'SELECT * FROM media_files')) == 1
    assert not rows(config, 'SELECT * FROM scan_file_observations WHERE session_id=2')
    assert rows(config, 'SELECT predecessor_id FROM scan_sessions WHERE id=2')[0]['predecessor_id'] == 1


def test_newly_mandatory_runtime_rules_block_saved_resume(target):
    root, config, _ = target
    (root / 'a.jpg').write_bytes(b'a')
    (root / 'b.jpg').write_bytes(b'b')
    assert scan(root, config, _event=interrupt_after_file) == 130
    # Keep the same DB path for session access; a newly in-scope temp path changes policy.
    newer = replace(config, temp_directory=root / 'generated-temp')
    with pytest.raises(ValueError, match='mandatory runtime'):
        scan(root, newer, resume_session=1)


def test_policies_are_immutable_and_status_is_understandable(target, capsys, monkeypatch):
    from inventory_builder.cli import main
    root, config, _ = target
    (root / 'a.jpg').write_bytes(b'a')
    assert scan(root, config, exclude=['Skip']) == 0
    connection = connect(config.database)
    try:
        for sql in ('UPDATE scan_policies SET canonical_json=\'{}\'', 'DELETE FROM scan_policies', "INSERT INTO scan_exclusions(policy_id,supplied_path,normalized_path,match_kind,origin,active) VALUES (1,'x','x','directory','cli',1)"):
            with pytest.raises(sqlite3.IntegrityError, match='immutable'):
                connection.execute(sql)
    finally:
        connection.close()
    monkeypatch.setenv('LOCALAPPDATA', str(root.parent / 'config'))
    assert main(['--database', str(config.database), 'status']) == 0
    text = capsys.readouterr().out
    assert 'completed' in text and 'Excluded paths:' in text and 'Unresolved errors:' in text
    assert 'Fully inventoried: YES' in text


def legacy_v2(path, root):
    connection = sqlite3.connect(path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys=ON')
    connection.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY,checksum TEXT NOT NULL,applied_at TEXT NOT NULL DEFAULT 'now')")
    for name in ('001_initial.sql', '002_folder_sources.sql'):
        script = files('inventory_builder.storage').joinpath('sql', name).read_text(encoding='utf-8')
        connection.executescript(script)
        connection.execute('INSERT INTO schema_migrations(version,checksum) VALUES (?,?)', (int(name[:3]), sha256(script.encode()).hexdigest()))
    connection.execute("INSERT INTO volumes(application_uuid,identity_kind,first_seen_at,last_seen_at) VALUES ('legacy','folder','now','now')")
    connection.execute("INSERT INTO scan_roots(volume_id,volume_relative_path,last_absolute_path,created_at) VALUES (1,'',?,'now')", (str(root),))
    info = root.stat()
    connection.execute('INSERT INTO folder_bindings VALUES (1,?,?,?,?,?,?)', (machine_id(), str(root), str(info.st_dev), str(info.st_ino), 'legacy', 'now'))
    connection.execute("INSERT INTO scan_sessions(root_id,state,created_at,discovery_version,config_json) VALUES (1,'interrupted','now','folder-v1','{}')")
    connection.execute("INSERT INTO scan_runs(session_id,started_at,state,application_version,machine_id) VALUES (1,'now','interrupted','0.1','test')")
    connection.execute("INSERT INTO scan_directories(session_id,relative_path,state) VALUES (1,'','processing')")
    connection.execute("INSERT INTO media_files(volume_id,volume_relative_path,last_absolute_path,parent_path,filename,extension,media_type,size_bytes,modified_ns,first_inventoried_at,last_inventoried_at) VALUES (1,'a.jpg',?,'','a.jpg','.jpg','photo',1,1,'now','now')", (str(root / 'a.jpg'),))
    connection.execute("INSERT INTO scan_files(session_id,directory_id,relative_path,state,media_id) VALUES (1,1,'a.jpg','processing',1)")
    connection.execute("INSERT INTO media_metadata(media_id,raw_metadata_json) VALUES (1,'preserve')")
    connection.execute("INSERT INTO scan_file_observations VALUES (1,1,'a.jpg',1,1,'now')")
    connection.execute("INSERT INTO scan_errors(session_id,run_id,directory_id,operation,category,message,occurred_at) VALUES (1,1,1,'enumerate','test','preserve error','now')")
    return connection


@pytest.mark.parametrize('fail', [False, True])
def test_populated_v2_migration_preserves_all_history_or_rolls_back(tmp_path, fail):
    root = tmp_path / 'disposable'
    root.mkdir()
    connection = legacy_v2(tmp_path / 'legacy.sqlite3', root)
    tables = ('volumes', 'scan_roots', 'scan_sessions', 'scan_runs', 'scan_directories', 'scan_files', 'media_files', 'media_metadata', 'scan_file_observations', 'scan_errors', 'folder_bindings')
    before = {table: ([r[1] for r in connection.execute(f'PRAGMA table_info({table})')], [tuple(r) for r in connection.execute(f'SELECT * FROM {table}')]) for table in tables}
    try:
        if fail:
            connection.execute('CREATE TABLE scan_coverage(deliberate_late_collision TEXT)')
            with pytest.raises(sqlite3.OperationalError):
                migrate(connection)
            assert connection.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 2
            assert not connection.execute("SELECT 1 FROM sqlite_master WHERE name='scan_policies'").fetchone()
        else:
            migrate(connection)
            assert connection.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 4
            assert not connection.execute('PRAGMA foreign_key_check').fetchone()
        for table, (columns, values) in before.items():
            selected = ','.join(columns)
            assert [tuple(r) for r in connection.execute(f'SELECT {selected} FROM {table}')] == values
        assert connection.execute('PRAGMA foreign_keys').fetchone()[0] == 1
    finally:
        connection.close()


def test_legacy_conversion_requires_confirmation_and_successor(target):
    root, config, _ = target
    legacy_path = config.database.with_name('legacy.sqlite3')
    connection = legacy_v2(legacy_path, root)
    connection.close()
    (root / 'a.jpg').write_bytes(b'a')
    legacy_config = replace(config, database=legacy_path)
    with pytest.raises(ValueError, match='confirm-legacy'):
        scan(root, legacy_config)
    with pytest.raises(ValueError, match='explicit'):
        scan(root, legacy_config, confirm_legacy=True)
    assert scan(root, legacy_config, confirm_legacy=True, new_scan=True) == 0
    assert len(rows(legacy_config, 'SELECT * FROM media_files')) == 1
    assert rows(legacy_config, 'SELECT id FROM media_files')[0]['id'] == 1
    assert rows(legacy_config, "SELECT raw_metadata_json FROM metadata_results WHERE raw_metadata_json='preserve'")
    assert rows(legacy_config, 'SELECT state FROM scan_sessions WHERE id=1')[0]['state'] == 'superseded'
    assert rows(legacy_config, 'SELECT message FROM scan_errors WHERE id=1')[0]['message'] == 'preserve error'


def test_config_persistent_exclusions_and_windows_comparison(tmp_path):
    config = tmp_path / 'config.toml'
    config.write_text('config_schema_version=2\nexclude_directories=["Cache", "Private"]', encoding='utf-8')
    assert load_config(config).exclude_directories == ('Cache', 'Private')
    assert compare('Photos/Old', 'photos/old') == 0 if os.name == 'nt' else compare('a', 'b') < 0


def test_overlapping_unresolved_legacy_scope_blocks_duplicate_inventory(target):
    root, config, _ = target
    child = root / 'Photos'
    child.mkdir()
    legacy_path = config.database.with_name('legacy.sqlite3')
    connection = legacy_v2(legacy_path, child)
    connection.close()
    (child / 'a.jpg').write_bytes(b'a')
    legacy_config = replace(config, database=legacy_path)
    with pytest.raises(ValueError, match='unresolved legacy'):
        scan(root, legacy_config)
    assert len(rows(legacy_config, 'SELECT * FROM media_files')) == 1


def test_queued_excluded_jobs_are_pruned_on_recovery(target, monkeypatch):
    root, config, _ = target
    (root / 'a.jpg').write_bytes(b'a')
    private = root / 'Private'
    private.mkdir()
    (private / 'secret.jpg').write_bytes(b'never visit')
    assert scan(root, config, exclude=['Private'], _event=interrupt_after_file) == 130
    connection = connect(config.database)
    try:
        connection.execute("INSERT INTO scan_directories(session_id,relative_path,path_key,state) VALUES (1,'Private','Private','processing')")
        connection.execute("INSERT INTO scan_files(session_id,directory_id,relative_path,path_key,state) VALUES (1,1,'Private/secret.jpg','Private/secret.jpg','processing')")
    finally:
        connection.close()
    original = os.scandir

    def guarded(path):
        assert Path(path) != private
        return original(path)

    monkeypatch.setattr(os, 'scandir', guarded)
    assert scan(root, config, exclude=['Private']) == 0
    assert rows(config, "SELECT state FROM scan_files WHERE relative_path='Private/secret.jpg'")[0]['state'] == 'excluded'
    assert len(rows(config, 'SELECT * FROM media_files')) == 1
    assert len(rows(config, 'SELECT * FROM scan_file_observations')) == 1


def test_ambiguous_volume_evidence_requires_explicit_decision(target, monkeypatch):
    root, config, volume = target
    (root / 'a.jpg').write_bytes(b'a')
    assert scan(root, config) == 0
    uuid = rows(config, 'SELECT application_uuid FROM volumes')[0]['application_uuid']
    monkeypatch.setattr('inventory_builder.scanner.selected_volume', lambda path: replace(volume, guid='different-guid'))
    with pytest.raises(ValueError, match='Ambiguous'):
        scan(root, config)
    assert scan(root, config, volume_uuid=uuid) == 0
    assert len(rows(config, 'SELECT * FROM volumes')) == 1


def test_entire_runtime_target_refused_before_creating_files(target):
    root, config, _ = target
    runtime = root / 'InventoryBuilder'
    runtime.mkdir()
    configured = replace(config, database=runtime / 'inventory.sqlite3', log_directory=runtime / 'logs')
    with pytest.raises(ValueError, match='runtime'):
        scan(runtime, configured)
    assert list(runtime.iterdir()) == []


def test_dedicated_runtime_pruned_inside_whole_mocked_volume(target, monkeypatch):
    from inventory_builder.storage.migrations import initialize
    from inventory_builder.locking import scanner_lock
    root, config, _ = target
    runtime = root / 'InventoryBuilder'
    configured = replace(config, database=runtime / 'inventory.sqlite3', log_directory=runtime / 'logs')
    with scanner_lock(configured.database):
        initialize(configured.database)
    (root / 'a.jpg').write_bytes(b'a')
    original = os.scandir

    def guarded(path):
        assert Path(path) != runtime
        return original(path)

    monkeypatch.setattr(os, 'scandir', guarded)
    assert scan(root, configured) == 0
    assert len(rows(configured, 'SELECT * FROM media_files')) == 1
    assert rows(configured, "SELECT * FROM scan_coverage WHERE volume_relative_path='InventoryBuilder' AND status='excluded'")


def test_failed_integrity_check_rolls_back_migration(tmp_path):
    root = tmp_path / 'source'
    root.mkdir()
    connection = legacy_v2(tmp_path / 'legacy.sqlite3', root)
    try:
        connection.execute('PRAGMA foreign_keys=OFF')
        connection.execute('UPDATE scan_runs SET session_id=999')
        connection.execute('PRAGMA foreign_keys=ON')
        with pytest.raises(ValueError, match='foreign-key'):
            migrate(connection)
        assert connection.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 2
        assert connection.execute('PRAGMA foreign_keys').fetchone()[0] == 1
        assert not connection.execute("SELECT 1 FROM sqlite_master WHERE name='scan_policies'").fetchone()
    finally:
        connection.close()


def test_cli_repeated_and_persistent_exclusions(target, capsys):
    from inventory_builder.cli import main
    root, config, _ = target
    for name in ('Private', 'Cache', 'Old', 'Keep'):
        child = root / name
        child.mkdir()
        (child / 'a.jpg').write_bytes(b'a')
    settings = root.parent / 'settings.toml'
    settings.write_text(f'database="{config.database.as_posix()}"\nlog_directory="{config.log_directory.as_posix()}"\nexclude_directories=["Private"]\n', encoding='utf-8')
    assert main(['--config', str(settings), 'scan', str(root), '--exclude', 'Cache', '--exclude', 'Old']) == 0
    assert [r['volume_relative_path'] for r in rows(config, 'SELECT * FROM media_files')] == ['Keep/a.jpg']
    assert 'Scan completed' in capsys.readouterr().out


def test_windows_keys_prevent_case_alias_duplicates_without_unicode_expansion(target):
    root, config, _ = target
    (root / 'a.JPG').write_bytes(b'a')
    (root / 'ß.jpg').write_bytes(b'one')
    (root / 'ss.jpg').write_bytes(b'two')
    assert scan(root, config) == 0
    connection = connect(config.database)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO media_files(volume_id,volume_relative_path,last_absolute_path,parent_path,filename,extension,media_type,size_bytes,modified_ns,first_inventoried_at,last_inventoried_at,path_key) SELECT volume_id,'A.jpg',last_absolute_path,parent_path,'A.jpg',extension,media_type,size_bytes,modified_ns,first_inventoried_at,last_inventoried_at,'A.jpg' FROM media_files WHERE filename='a.JPG'")
        assert connection.execute('SELECT count(*) FROM media_files').fetchone()[0] == 3
    finally:
        connection.close()


def test_selected_session_cannot_redirect_target(target):
    root, config, _ = target
    (root / 'a.jpg').write_bytes(b'a')
    assert scan(root, config, _event=interrupt_after_file) == 130
    child = root / 'other'
    child.mkdir()
    with pytest.raises(ValueError, match='supplied target'):
        scan(child, config, resume_session=1)


def test_changed_defaults_block_automatic_and_unsafe_saved_resume(target, monkeypatch):
    root, config, _ = target
    (root / 'a.jpg').write_bytes(b'a')
    assert scan(root, config, _event=interrupt_after_file) == 130
    monkeypatch.setattr('inventory_builder.exclusions.DEFAULTS_VERSION', 'new-defaults')
    monkeypatch.setattr('inventory_builder.storage.discovery.DEFAULTS_VERSION', 'new-defaults')
    with pytest.raises(ValueError, match='policy changed'):
        scan(root, config)
    with pytest.raises(ValueError, match='safety version'):
        scan(root, config, resume_session=1)
    assert scan(root, config, new_scan=True) == 0


def test_case_only_rename_preserves_id_and_refreshes_display_path(target):
    root, config, _ = target
    original = root / 'original.JPG'
    original.write_bytes(b'a')
    assert scan(root, config) == 0
    media_id = rows(config, 'SELECT id FROM media_files')[0]['id']
    # A two-step rename makes this deterministic on Windows too; only disposable data.
    temporary = root / 'intermediate'
    original.rename(temporary)
    temporary.rename(root / 'ORIGINAL.jpg')
    assert scan(root, config) == 0
    observed = rows(config, 'SELECT * FROM media_files')
    assert len(observed) == 1
    assert observed[0]['id'] == media_id
    assert observed[0]['filename'] == 'ORIGINAL.jpg'
