# InventoryBuilder

Local Python 3.11+ / SQLite inventory foundation for Windows 11.
Milestone 1 implements configuration, logging, database migrations and writer
locking. It does not scan directories, discover volumes, read media contents,
extract metadata, hash files, or export reports.

## Setup

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[test]"
.\.venv\Scripts\python -m inventory_builder init
.\.venv\Scripts\python -m inventory_builder status
.\.venv\Scripts\python -m inventory_builder volumes
.\.venv\Scripts\python -m pytest
```

Runtime defaults to `%LOCALAPPDATA%\InventoryBuilder`, with `inventory.sqlite3`
and `logs\inventory_builder.log`. Keep this on the internal SSD, outside source
media, Git, and cloud-synchronized directories. Explicit initialization creates
the runtime directory. Status and volumes do not initialize a missing database.

Copy `config/example.toml` to that directory as `config.toml` to customize.
Relative configuration paths resolve against the TOML file's directory.
Defaults are overridden by TOML, then `--database`. Global options precede the
command, for example `inventory-builder --config C:\LocalData\config.toml init`.
An explicit config file can be used if LOCALAPPDATA is unavailable.

The application does not yet verify SSD type or detect cloud-sync products;
the operator must choose a suitable runtime location. No external drives need
to be connected. The database will eventually contain sensitive file paths and
GPS information; keep database backups private and local.

Run `init` after upgrading to apply migrations. Newer schemas and modified
applied migrations are refused. Never copy only a live WAL-mode database file
for backup: use SQLite's backup API or close all connections before copying.

See [architecture](docs/ARCHITECTURE.md) and [requirements](docs/REQUIREMENTS.md).
