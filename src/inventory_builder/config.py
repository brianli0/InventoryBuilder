"""Machine-local configuration; importing this module creates nothing."""

from dataclasses import dataclass
import os
from pathlib import Path
import tomllib

PHOTO_EXTENSIONS = ('.jpg', '.jpeg', '.heic', '.heif', '.png', '.tif', '.tiff', '.bmp', '.gif', '.webp', '.dng', '.cr2', '.cr3', '.nef', '.arw', '.rw2', '.orf')
VIDEO_EXTENSIONS = ('.mp4', '.mov', '.m4v', '.avi', '.mkv', '.wmv', '.mpg', '.mpeg', '.mts', '.m2ts', '.3gp')

@dataclass(frozen=True)
class Config:
    database: Path
    log_directory: Path
    batch_size: int = 100
    busy_timeout_ms: int = 5000
    photo_extensions: tuple[str, ...] = PHOTO_EXTENSIONS
    video_extensions: tuple[str, ...] = VIDEO_EXTENSIONS
    config_schema_version: int = 2
    exclude_directories: tuple[str, ...] = ()
    temp_directory: Path | None = None
    backup_directory: Path | None = None
    report_directory: Path | None = None
    exiftool_path: str = 'exiftool'
    metadata_mode: str = 'batch'
    metadata_batch_size: int = 32
    metadata_timeout_seconds: int = 60
    metadata_output_limit: int = 16777216
    processing_max_attempts: int = 3
    hash_chunk_bytes: int = 8388608
    retry_delay_seconds: int = 2
    capture_date_precedence: tuple[str,...] = ('DateTimeOriginal','CreationDate','CreateDate','MediaCreateDate','TrackCreateDate')


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
    processing_keys = {'exiftool_path','metadata_mode','metadata_batch_size','metadata_timeout_seconds','metadata_output_limit','processing_max_attempts','hash_chunk_bytes','retry_delay_seconds'}
    unknown = set(data) - ({"database", "log_directory", "batch_size", "busy_timeout_ms", "photo_extensions", "video_extensions", "config_schema_version", "exclude_directories", "temp_directory", "backup_directory", "report_directory", "capture_date_precedence"} | processing_keys)
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
    def extensions(key, default):
        values = data.get(key, default)
        if not isinstance(values, (list, tuple)) or any(not isinstance(v, str) or not v.startswith('.') or len(v) < 2 or any(c in v for c in '/\\:') for v in values):
            raise ValueError(f'{key} must be an array of extensions beginning with a dot')
        return tuple(sorted(set(v.lower() for v in values)))

    photos = extensions('photo_extensions', PHOTO_EXTENSIONS)
    videos = extensions('video_extensions', VIDEO_EXTENSIONS)
    if set(photos) & set(videos):
        raise ValueError('Photo and video extension lists must not overlap')
    config_version = data.get('config_schema_version', 2)
    if type(config_version) is not int or config_version != 2:
        raise ValueError('Supported config_schema_version is 2')
    exclusions = data.get('exclude_directories', [])
    if not isinstance(exclusions, list) or any(not isinstance(v, str) or not v.strip() for v in exclusions):
        raise ValueError('exclude_directories must be a list of nonempty paths')
    processing = {}
    for key in processing_keys:
        value = data.get(key, Config.__dataclass_fields__[key].default)
        if key == 'exiftool_path':
            if not isinstance(value,str) or not value.strip():
                raise ValueError('exiftool_path must name an executable')
            if '/' in value or '\\' in value or Path(value).is_absolute():
                value = str(local_path(value))
        elif key == 'metadata_mode':
            if value not in ('batch','single'):
                raise ValueError('metadata_mode must be batch or single')
        elif type(value) is not int or value < (0 if key=='retry_delay_seconds' else 1):
            raise ValueError(f'{key} must be a positive integer')
        processing[key] = value
    for key, maximum in (('metadata_batch_size',256),('metadata_output_limit',67108864),('hash_chunk_bytes',67108864),('processing_max_attempts',20)):
        if processing[key] > maximum:
            raise ValueError(f'{key} exceeds bounded maximum {maximum}')
    precedence = data.get('capture_date_precedence',list(Config.__dataclass_fields__['capture_date_precedence'].default))
    if not isinstance(precedence,list) or not precedence or any(not isinstance(tag,str) for tag in precedence) or len(set(precedence))!=len(precedence) or any(tag not in Config.__dataclass_fields__['capture_date_precedence'].default for tag in precedence):
        raise ValueError('capture_date_precedence must be distinct supported embedded capture-date tags')
    return Config(
        database.absolute() if database is not None else local_path(data.get("database", "inventory.sqlite3")),
        local_path(data.get("log_directory", "logs")), batch, timeout, photos, videos,
        config_version, tuple(exclusions),
        local_path(data['temp_directory']) if 'temp_directory' in data else None,
        local_path(data['backup_directory']) if 'backup_directory' in data else None,
        local_path(data['report_directory']) if 'report_directory' in data else None,
        **processing,
        capture_date_precedence=tuple(precedence),
    )
