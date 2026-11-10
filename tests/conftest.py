import pytest

from inventory_builder.locking import scanner_lock
from inventory_builder.storage.database import connect
from inventory_builder.storage.migrations import initialize


@pytest.fixture(autouse=True)
def legacy_discovery_extractor(request,monkeypatch):
    # Existing discovery tests deliberately use non-media byte strings. Keep
    # those tests focused on filesystem behavior; separate integration tests use
    # the real ExifTool and valid generated media.
    if request.path.name in ('test_scanner.py','test_flexible_targets.py'):
        class FixtureTool:
            version = 'fixture'
            def __init__(self,config):
                pass
            def extract_batch(self,paths):
                return [dict(SourceFile=str(path),**{'File:FileType':'JPEG'}) for path in paths]
        monkeypatch.setattr('inventory_builder.scanner.ExifTool',FixtureTool)


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
