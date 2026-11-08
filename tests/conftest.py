import pytest

from inventory_builder.locking import scanner_lock
from inventory_builder.storage.database import connect
from inventory_builder.storage.migrations import initialize


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "inventory.sqlite3"
    with scanner_lock(path):
        initialize(path)
    return path


@pytest.fixture
def connection(database):
    connection = connect(database)
    yield connection
    connection.close()
