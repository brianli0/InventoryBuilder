"""Atomic migrations. Caller must hold the database writer lock."""

from hashlib import sha256
from importlib.resources import files
from pathlib import Path
import sqlite3

from .database import connect, transaction
from ..paths import compare


def migrate(connection: sqlite3.Connection) -> None:
    connection.create_collation('WINPATH', compare)
    resources = sorted(files("inventory_builder.storage").joinpath("sql").iterdir(), key=lambda p: p.name)
    scripts = [(int(p.name.split("_")[0]), p.read_text(encoding="utf-8")) for p in resources if p.name.endswith(".sql")]
    if connection.in_transaction:
        raise ValueError('Migrations require an idle connection')
    # SQLite's documented table-rebuild procedure requires foreign keys disabled
    # before BEGIN. Validate all references before committing, then restore enforcement.
    foreign_keys = connection.execute('PRAGMA foreign_keys').fetchone()[0]
    connection.execute('PRAGMA foreign_keys=OFF')
    try:
        with transaction(connection):
            connection.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, checksum TEXT NOT NULL, applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))")
            applied = dict(connection.execute("SELECT version, checksum FROM schema_migrations"))
            if set(applied) - {version for version, _ in scripts}:
                raise ValueError("Database schema is newer than this application")
            for version, script in scripts:
                checksum = sha256(script.encode("utf-8")).hexdigest()
                if version in applied:
                    if applied[version] != checksum:
                        raise ValueError(f"Migration {version} checksum mismatch")
                    continue
                # executescript implicitly commits; execute complete statements
                # individually so DDL and the ledger share the transaction.
                statement = ""
                for line in script.splitlines(keepends=True):
                    statement += line
                    if sqlite3.complete_statement(statement):
                        connection.execute(statement)
                        statement = ""
                if statement.strip():
                    raise ValueError(f"Incomplete SQL in migration {version}")
                connection.execute("INSERT INTO schema_migrations(version, checksum) VALUES (?, ?)", (version, checksum))
            if connection.execute('PRAGMA foreign_key_check').fetchone():
                raise ValueError('Migration foreign-key integrity check failed')
    finally:
        connection.execute(f'PRAGMA foreign_keys={foreign_keys}')


def initialize(path: Path, timeout_ms: int = 5000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # sqlite3 must create the file before the normal mode=rw connection.
    sqlite3.connect(path).close()
    connection = connect(path, timeout_ms=timeout_ms)
    try:
        mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if mode != "wal":
            raise RuntimeError("Could not enable SQLite WAL mode")
        migrate(connection)
    finally:
        connection.close()
