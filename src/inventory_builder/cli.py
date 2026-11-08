import argparse
import logging
from pathlib import Path
import sqlite3
import sys

from .config import load_config
from .locking import scanner_lock, ScannerBusyError
from .logging_setup import configure_logging
from .storage.database import connect
from .storage.migrations import initialize


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local InventoryBuilder foundation (no scanning yet)")
    parser.add_argument("--config", type=Path, help="TOML configuration path")
    parser.add_argument("--database", type=Path, help="Override database path")
    parser.add_argument("command", choices=("init", "status", "volumes"))
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config, args.database)
        if args.command == "init":
            with scanner_lock(config.database):
                logger = configure_logging(config.log_directory)
                initialize(config.database, config.busy_timeout_ms)
                logger.info("Initialized database: %s", config.database)
            print(f"Database ready: {config.database}")
            return 0
        if not config.database.is_file():
            print("Database not initialized. Run init first.")
            return 1
        connection = connect(config.database, readonly=True, timeout_ms=config.busy_timeout_ms)
        try:
            if args.command == "status":
                version = connection.execute("SELECT max(version) FROM schema_migrations").fetchone()[0]
                print(f"Database: {config.database}\nSchema version: {version}")
                for table in ("volumes", "media_files", "scan_sessions"):
                    print(f"{table}: {connection.execute('SELECT count(*) FROM ' + table).fetchone()[0]}")
            else:
                rows = connection.execute("SELECT application_uuid, label, filesystem FROM volumes ORDER BY id").fetchall()
                if not rows:
                    print("No registered volumes.")
                for row in rows:
                    print(f"{row['application_uuid']} | {row['label'] or '(unlabeled)'} | {row['filesystem'] or 'unknown'}")
        finally:
            connection.close()
        return 0
    except (OSError, ValueError, sqlite3.Error, ScannerBusyError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        # Release rotating file handles, including when main is embedded in tests.
        logger = logging.getLogger("inventory_builder")
        for handler in logger.handlers[:]:
            handler.close()
            logger.removeHandler(handler)
