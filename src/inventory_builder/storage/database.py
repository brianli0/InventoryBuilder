from contextlib import contextmanager
from pathlib import Path
import sqlite3
from ..paths import compare


def connect(path: Path, *, readonly: bool = False, timeout_ms: int = 5000) -> sqlite3.Connection:
    uri = path.resolve().as_uri() + ("?mode=ro" if readonly else "?mode=rw")
    connection = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=timeout_ms / 1000)
    connection.row_factory = sqlite3.Row
    connection.create_collation('WINPATH', compare)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA synchronous = FULL")
    return connection


@contextmanager
def transaction(connection: sqlite3.Connection):
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
