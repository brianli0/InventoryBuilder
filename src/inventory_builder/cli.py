import argparse
import logging
import os
from pathlib import Path
import sqlite3
import sys

from .config import load_config
from .locking import scanner_lock, ScannerBusyError
from .logging_setup import configure_logging
from .storage.database import connect
from .storage.migrations import initialize
from .scanner import scan_folder
from .storage.processing import verification,stage_counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local InventoryBuilder: explicit folder inventory")
    parser.add_argument("--config", type=Path, help="TOML configuration path")
    parser.add_argument("--database", type=Path, help="Override database path")
    parser.add_argument("command", choices=("init", "status", "volumes", "scan", "verify"))
    parser.add_argument("folder", type=Path, nargs="?")
    parser.add_argument('--exclude', action='append', default=[], metavar='DIRECTORY', help='Exclude a subtree; repeat as needed')
    session_mode = parser.add_mutually_exclusive_group()
    session_mode.add_argument('--resume-session', type=int)
    session_mode.add_argument('--new-scan', action='store_true')
    session_mode.add_argument('--reconcile-session', type=int)
    parser.add_argument('--volume-id', help="Explicitly confirm a physical volume UUID, or 'new' to register a distinct identity")
    parser.add_argument('--confirm-legacy-volume', action='store_true', help='Confirm binding this legacy folder to its selected physical volume')
    parser.add_argument('--retry-failed', action='store_true', help='Give failed jobs another bounded attempt budget')
    args = parser.parse_args(argv)
    if args.command in ('scan','verify') and args.folder is None or args.command not in ('scan','verify','status') and args.folder is not None:
        parser.error('scan/verify require a target; status optionally accepts a target')
    if args.command != 'scan' and (args.exclude or args.resume_session is not None or args.new_scan or args.reconcile_session is not None or args.volume_id or args.confirm_legacy_volume or args.retry_failed):
        parser.error('Scan options require the scan command')
    try:
        config = load_config(args.config, args.database)
        if args.command == 'scan':
            return scan_folder(args.folder, config, exclude=args.exclude, resume_session=args.resume_session, new_scan=args.new_scan, reconcile_session=args.reconcile_session, volume_uuid=args.volume_id, confirm_legacy=args.confirm_legacy_volume,retry_failed=args.retry_failed)
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
            if args.command in ('status','verify'):
                version = connection.execute("SELECT max(version) FROM schema_migrations").fetchone()[0]
                print(f"Database: {config.database}\nSchema version: {version}")
                for table in ("volumes", "media_files", "scan_sessions"):
                    print(f"{table}: {connection.execute('SELECT count(*) FROM ' + table).fetchone()[0]}")
                if version < 3:
                    print('Run init to upgrade the database before scanning.')
                    return 1 if args.command=='verify' else 0
                print('Recorded checkpoint verification; source contents are not re-read. Legacy sessions mean filesystem-only completion.')
                if args.folder is not None:
                    sessions = connection.execute('SELECT s.*,r.last_absolute_path FROM scan_sessions s JOIN scan_roots r ON r.id=s.root_id WHERE r.last_absolute_path=? COLLATE WINPATH ORDER BY s.id DESC LIMIT 1',(os.path.abspath(args.folder),)).fetchall()
                else:
                    sessions = connection.execute('SELECT s.*,r.last_absolute_path FROM scan_sessions s JOIN scan_roots r ON r.id=s.root_id ORDER BY s.id DESC LIMIT 10').fetchall()
                if args.folder is not None and not sessions:
                    print('No recorded session for that target. Scan the explicitly authorized target first.')
                    return 1
                verified = False
                for row in sessions:
                    sid = row['id']
                    print(f"Session {sid}: {row['state']} | {row['last_absolute_path']} | checkpoint: {row['checkpoint_at'] or 'none'}")
                    for label, table in (('Directories', 'scan_directories'), ('Files', 'scan_files')):
                        counts = ', '.join(f"{r['state']}={r['n']}" for r in connection.execute(f'SELECT state,count(*) n FROM {table} WHERE session_id=? GROUP BY state', (sid,)))
                        print(f'  {label}: {counts or "none discovered"}')
                    pruned = connection.execute("SELECT count(*) FROM scan_coverage WHERE session_id=? AND status='excluded'", (sid,)).fetchone()[0]
                    errors = connection.execute('SELECT count(*) FROM scan_errors WHERE session_id=? AND resolved_at IS NULL', (sid,)).fetchone()[0]
                    print(f"  Excluded paths: {pruned} (subtrees not counted recursively). Unresolved errors: {errors}.")
                    if row['policy_id'] is None:
                        print('  Legacy policy: explicit identity confirmation and successor scan may be required.')
                    else:
                        rules = connection.execute('SELECT count(*) FROM scan_exclusions WHERE policy_id=? AND active=1', (row['policy_id'],)).fetchone()[0]
                        print(f"  Policy {row['policy_id']}: {rules} active exclusion rules; discovery {row['discovery_state']}; processing {row['processing_state']}.")
                    if row['state'] in ('interrupted', 'blocked', 'running'):
                        print('  Run scan TARGET again to resume matching rules; blocked errors retry on resume.')
                    if row['predecessor_id'] is not None:
                        print(f"  Successor of session {row['predecessor_id']}: {row['successor_reason']}.")
                    for error in connection.execute('SELECT operation,message_path,message FROM scan_errors WHERE session_id=? AND resolved_at IS NULL ORDER BY id DESC LIMIT 3', (sid,)):
                        print(f"  Error: {error['operation']} {error['message_path'] or ''}: {error['message']}")
                    if version>=4:
                        result = verification(connection,sid)
                        verified = result['complete']
                        print('  Fully inventoried: '+('YES' if verified else 'NO')+f" | discovery complete: {result['discovery_complete']} | missing required results: {result['missing_required_results']}")
                        for stage,counts in result['stages'].items():
                            print(f"  {stage}: completed={counts['completed']} pending={counts['pending']} failed={counts['failed']} retried={counts['retried']} reused={counts['reused']}")
                    else:
                        print('  Comprehensive collection requires Migration 004.')
                if args.command=='verify':
                    return 0 if verified else 1
            else:
                rows = connection.execute("SELECT application_uuid, label, filesystem, identity_kind FROM volumes ORDER BY id").fetchall()
                if not rows:
                    print("No registered volumes.")
                for row in rows:
                    print(f"{row['application_uuid']} | {row['identity_kind']} | {row['label'] or '(unlabeled)'} | {row['filesystem'] or 'unknown'}")
        finally:
            connection.close()
        return 0
    except KeyboardInterrupt:
        print('Interrupted; previously committed work is preserved.', file=sys.stderr)
        return 130
    except (OSError, ValueError, sqlite3.Error, ScannerBusyError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        # Release rotating file handles, including when main is embedded in tests.
        logger = logging.getLogger("inventory_builder")
        for handler in logger.handlers[:]:
            handler.close()
            logger.removeHandler(handler)
