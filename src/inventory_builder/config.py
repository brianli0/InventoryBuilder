"""Machine-local configuration; importing this module creates nothing."""

from dataclasses import dataclass
import os
from pathlib import Path
import tomllib


@dataclass(frozen=True)
class Config:
    database: Path
    log_directory: Path
    batch_size: int = 100
    busy_timeout_ms: int = 5000


def runtime_directory() -> Path:
    value = os.environ.get("LOCALAPPDATA")
    if not value:
        raise ValueError("LOCALAPPDATA is unavailable; supply an explicit --config file")
    return Path(value) / "InventoryBuilder"


def load_config(path: Path | None = None, database: Path | None = None) -> Config:
    # Explicit files can be used in isolated tests without Windows environment variables.
    if path is None:
        base = runtime_directory()
        candidate = base / "config.toml"
        path = candidate if candidate.is_file() else None
    else:
        path = path.absolute()
        base = path.parent
    data = {}
    if path is not None:
        with path.open("rb") as stream:
            data = tomllib.load(stream)
    unknown = set(data) - {"database", "log_directory", "batch_size", "busy_timeout_ms"}
    if unknown:
        raise ValueError(f"Unknown configuration keys: {', '.join(sorted(unknown))}")

    def local_path(value: str | Path) -> Path:
        if not isinstance(value, (str, Path)) or not str(value).strip():
            raise ValueError("Configuration paths must be nonempty strings")
        result = Path(value).expanduser()
        return (base / result).absolute() if not result.is_absolute() else result

    batch = data.get("batch_size", 100)
    timeout = data.get("busy_timeout_ms", 5000)
    for name, value in (("batch_size", batch), ("busy_timeout_ms", timeout)):
        if type(value) is not int or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
    return Config(
        database.absolute() if database is not None else local_path(data.get("database", "inventory.sqlite3")),
        local_path(data.get("log_directory", "logs")), batch, timeout,
    )
