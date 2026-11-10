import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from inventory_builder.cli import main
from inventory_builder.config import load_config
from inventory_builder.locking import scanner_lock
from inventory_builder.storage.database import connect, transaction
from inventory_builder.storage.migrations import initialize, migrate


def seed(connection):
    connection.execute("INSERT INTO volumes(application_uuid,first_seen_at,last_seen_at) VALUES ('volume-1','now','now')")
    connection.execute("INSERT INTO scan_roots(volume_id,volume_relative_path,last_absolute_path,created_at) VALUES (1,'photos','X:/photos','now')")
    connection.execute("INSERT INTO scan_sessions(root_id,created_at,discovery_version) VALUES (1,'now','1')")


def test_initialization_repeat_and_persistence(database):
    with scanner_lock(database):
        initialize(database)
    connection = connect(database)
    try:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 4
        seed(connection)
    finally:
        connection.close()
    connection = connect(database, readonly=True)
    try:
        assert connection.execute("SELECT application_uuid FROM volumes").fetchone()[0] == 'volume-1'
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("DELETE FROM volumes")
    finally:
        connection.close()


def test_schema_constraints(connection):
    seed(connection)
    connection.execute("INSERT INTO scan_directories(session_id,relative_path) VALUES (1,'')")
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("INSERT INTO scan_directories(session_id,relative_path) VALUES (1,'')")
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("INSERT INTO scan_directories(session_id,relative_path) VALUES (999,'other')")
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("UPDATE scan_directories SET state='imaginary'")
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("DELETE FROM volumes")
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("INSERT INTO scan_sessions(root_id,created_at,discovery_version) VALUES (1,'now','1')")


def test_checkpoint_and_inventory_rollback(connection):
    seed(connection)
    with pytest.raises(KeyboardInterrupt):
        with transaction(connection):
            connection.execute("INSERT INTO media_files(volume_id,volume_relative_path,last_absolute_path,parent_path,filename,extension,media_type,size_bytes,modified_ns,first_inventoried_at,last_inventoried_at) VALUES (1,'photos/a.jpg','X:/photos/a.jpg','photos','a.jpg','.jpg','photo',42,100,'now','now')")
            connection.execute("INSERT INTO scan_directories(session_id,relative_path,state) VALUES (1,'','completed')")
            raise KeyboardInterrupt
    assert connection.execute("SELECT count(*) FROM media_files").fetchone()[0] == 0
    assert connection.execute("SELECT count(*) FROM scan_directories").fetchone()[0] == 0
    with transaction(connection):
        connection.execute("INSERT INTO scan_directories(session_id,relative_path) VALUES (1,'')")
    assert connection.execute("SELECT count(*) FROM scan_directories").fetchone()[0] == 1


@pytest.mark.parametrize("alteration", ["UPDATE schema_migrations SET checksum='changed'", "INSERT INTO schema_migrations(version,checksum) VALUES (999,'future')"])
def test_incompatible_migrations_refused(connection, alteration):
    connection.execute(alteration)
    with pytest.raises(ValueError):
        migrate(connection)
    assert not connection.in_transaction


def test_migration_failure_rolls_back(connection):
    connection.execute("DROP TABLE schema_migrations")
    # Existing volumes table makes migration fail after creating the ledger.
    with pytest.raises(sqlite3.OperationalError):
        migrate(connection)
    assert connection.execute("SELECT count(*) FROM sqlite_master WHERE name='schema_migrations'").fetchone()[0] == 0


def test_configuration_precedence_and_no_side_effects(tmp_path, monkeypatch):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    default = load_config()
    assert default.database == tmp_path / 'InventoryBuilder' / 'inventory.sqlite3'
    assert not default.database.parent.exists()
    config = tmp_path / 'settings.toml'
    config.write_text('database="local.sqlite3"\nlog_directory="logs"\nbatch_size=7\n', encoding='utf-8')
    override = tmp_path / 'override.sqlite3'
    loaded = load_config(config, override)
    assert loaded.database == override
    assert loaded.log_directory == tmp_path / 'logs'
    assert loaded.batch_size == 7


@pytest.mark.parametrize('text', ['batch_size=0', 'busy_timeout_ms=true', 'unknown=2', 'batch_size="x"', 'database=42', 'log_directory=""'])
def test_invalid_configuration(tmp_path, text):
    config = tmp_path / 'bad.toml'
    config.write_text(text, encoding='utf-8')
    with pytest.raises(ValueError):
        load_config(config)


def test_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    assert main(['status']) == 1
    assert main(['init']) == 0
    assert main(['init']) == 0
    assert main(['status']) == 0
    assert main(['volumes']) == 0
    output = capsys.readouterr().out
    assert 'Schema version: 4' in output
    assert 'No registered volumes.' in output
    assert (tmp_path / 'InventoryBuilder/logs/inventory_builder.log').is_file()
    with pytest.raises(SystemExit):
        main(['scan'])


def test_process_lock_and_release(tmp_path):
    database = tmp_path / 'test.sqlite3'
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'src'))
    code = "from pathlib import Path; from inventory_builder.locking import scanner_lock; import sys\nwith scanner_lock(Path(sys.argv[1])): print('locked', flush=True)"
    with scanner_lock(database):
        result = subprocess.run([sys.executable, '-c', code, str(database)], env=env, capture_output=True, text=True, timeout=15)
        assert result.returncode != 0
        assert 'Another writer' in result.stderr
    result = subprocess.run([sys.executable, '-c', code, str(database)], env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    # Abrupt process exit releases the OS lock even though the file remains.
    crash = code.replace("print('locked', flush=True)", "__import__('os')._exit(7)")
    assert subprocess.run([sys.executable, '-c', crash, str(database)], env=env, timeout=15).returncode == 7
    with scanner_lock(database):
        pass
