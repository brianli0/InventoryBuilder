"""Explicit folder validation and provisional bindings; never enumerates drives."""

import os
from pathlib import Path, PurePosixPath
import socket
import stat
from uuid import uuid4

from .storage.database import transaction
from .paths import MATCHER_VERSION, relative_key, within, relative_to
from .volumes import selected_volume, reject_case_sensitive


def machine_id():
    # Local binding namespace, not a claimed physical-volume identity.
    return socket.gethostname()


def is_reparse(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)


def validate_folder(folder, config):
    from .exclusions import validate_syntax
    validate_syntax(str(folder))
    supplied = Path(folder)
    if supplied.drive and not supplied.is_absolute():
        raise ValueError('Drive-relative targets are ambiguous; supply an explicit folder path')
    raw = Path(os.path.abspath(folder))
    # Inspect only the explicitly supplied path components, never siblings.
    for part in reversed((raw, *raw.parents)):
        if is_reparse(part.lstat()):
            raise ValueError('Scan roots cannot pass through reparse points or symbolic links')
        reject_case_sensitive(part)
    root = raw.resolve(strict=True)
    if not root.is_dir():
        raise ValueError('Scan target must be an existing folder')
    database_parent = config.database.parent.resolve()
    dedicated_database = database_parent.name.lower() in ('inventorybuilder', 'runtime')
    if within(database_parent, root) and not dedicated_database:
        raise ValueError('Database and lock must be outside included source folders or in a dedicated InventoryBuilder/runtime directory')
    runtime_directories = [config.log_directory.resolve(), (config.temp_directory or database_parent / 'tmp').resolve(), (config.backup_directory or database_parent / 'backups').resolve(), (config.report_directory or database_parent / 'reports').resolve()]
    if dedicated_database:
        runtime_directories.append(database_parent)
    for runtime in runtime_directories:
        if within(root, runtime):
            raise ValueError('Selected target is inside an excluded application runtime directory')
    return root


def safe_path(root, relative, expected):
    parts = PurePosixPath(relative).parts
    if PurePosixPath(relative).is_absolute() or any(p in ('.', '..') or ':' in p or '\\' in p for p in parts):
        raise ValueError('Invalid queued relative path')
    root_info = root.lstat()
    if is_reparse(root_info) or (str(root_info.st_dev), str(root_info.st_ino)) != expected:
        raise OSError('Selected folder identity changed or became unavailable')
    path = root
    for part in parts:
        path = path / part
        if is_reparse(path.lstat()):
            raise OSError('Reparse point encountered; refusing traversal')
    return path


def bind_folder(connection, root, now, *, volume=None, volume_uuid=None, confirm_legacy=False, allow_legacy_active=False):
    info = root.stat()
    expected = (str(info.st_dev), str(info.st_ino))
    host = machine_id()
    volume = volume or selected_volume(root)
    volume_relative = relative_key(relative_to(root, volume.mount).as_posix())
    # New physical identities require consistent evidence; serial-only matches
    # and contradictory observations are ambiguous, never silently merged.
    physical = list(connection.execute("SELECT DISTINCT v.* FROM volumes v JOIN volume_observations o ON o.volume_id=v.id WHERE v.identity_kind='physical' AND (o.volume_guid=? OR o.filesystem_serial=?)", (volume.guid, volume.serial)))
    if volume_uuid == 'new':
        chosen = None  # Explicit identity decision for a distinct/cloned volume.
    elif volume_uuid:
        chosen = connection.execute("SELECT * FROM volumes WHERE application_uuid=? AND identity_kind='physical'", (volume_uuid,)).fetchone()
        if chosen is None:
            raise ValueError('Explicit --volume-id must name an existing physical identity')
    else:
        if len(physical) > 1:
            raise ValueError('Ambiguous volume identity; use --volume-id after explicit confirmation')
        chosen = physical[0] if physical else None
        if chosen:
            observations = connection.execute('SELECT * FROM volume_observations WHERE volume_id=?', (chosen['id'],)).fetchall()
            if not any(r['volume_guid'] == volume.guid and r['filesystem_serial'] == volume.serial and (r['capacity_bytes'] is None or r['capacity_bytes'] == volume.capacity) for r in observations):
                raise ValueError('Ambiguous or changed volume evidence; use --volume-id after explicit confirmation')
    if chosen and chosen['comparison_version'] not in (None, MATCHER_VERSION):
        raise ValueError('Volume comparison version changed; explicit path-key migration is required')
    if chosen and chosen['comparison_version'] is None and connection.execute('SELECT 1 FROM scan_roots WHERE volume_id=?', (chosen['id'],)).fetchone():
        raise ValueError('Legacy physical paths require explicit comparison-key reconciliation')
    legacy = None
    for row in connection.execute('SELECT b.*,v.identity_kind FROM folder_bindings b JOIN scan_roots r ON r.id=b.root_id JOIN volumes v ON v.id=r.volume_id WHERE b.machine_id=?', (host,)):
        previous = Path(row['selected_path'])
        if row['identity_kind'] != 'folder':
            continue
        same_directory = (row['device_id'], row['directory_id']) == expected
        if previous == root or same_directory:
            if (row['device_id'], row['directory_id']) != expected:
                raise ValueError('Folder identity changed; explicit identity resolution is required')
            legacy = row
        elif within(root, previous) or within(previous, root) or (not volume_relative and row['device_id'] == expected[0]):
            raise ValueError(f'Overlapping unresolved legacy scope: confirm its original root first ({previous})')
    if legacy and not confirm_legacy:
        raise ValueError('Legacy folder identity requires --confirm-legacy-volume and an explicit new/reconciled scan if unfinished')
    if legacy and not allow_legacy_active and connection.execute("SELECT 1 FROM scan_sessions WHERE root_id=? AND state NOT IN ('completed','cancelled','superseded')", (legacy['root_id'],)).fetchone():
        raise ValueError('Legacy unfinished session requires an explicit --new-scan or --reconcile-session')
    with transaction(connection):
        volume_id = chosen['id'] if chosen else connection.execute("INSERT INTO volumes(application_uuid,identity_kind,label,filesystem,capacity_bytes,first_seen_at,last_seen_at,comparison_version) VALUES (?,'physical',?,?,?,?,?,?)", (str(uuid4()), volume.label, volume.filesystem, volume.capacity, now, now, MATCHER_VERSION)).lastrowid
        connection.execute('UPDATE volumes SET comparison_version=?,last_seen_at=? WHERE id=?', (MATCHER_VERSION, now, volume_id))
        if legacy:
            from .storage.identities import promote_folder_in_transaction
            promote_folder_in_transaction(connection, legacy['root_id'], volume_id, volume_relative, now, allow_unfinished=True)
        record = connection.execute('SELECT * FROM scan_roots WHERE volume_id=? AND path_key=? COLLATE WINPATH', (volume_id, volume_relative)).fetchone()
        if record is None:
            root_id = connection.execute('INSERT INTO scan_roots(volume_id,volume_relative_path,last_absolute_path,created_at,path_key) VALUES (?,?,?,?,?)', (volume_id, volume_relative, str(root), now, volume_relative)).lastrowid
        else:
            root_id = record['id']
            connection.execute('UPDATE scan_roots SET last_absolute_path=? WHERE id=?', (str(root), root_id))
        connection.execute('INSERT INTO volume_observations(volume_id,machine_id,volume_guid,filesystem_serial,mount_path,drive_letter,capacity_bytes,observed_at) VALUES (?,?,?,?,?,?,?,?)', (volume_id, host, volume.guid, volume.serial, str(volume.mount), volume.mount.drive, volume.capacity, now))
        old_binding = connection.execute('SELECT * FROM folder_bindings WHERE root_id=?', (root_id,)).fetchone()
        if old_binding and (old_binding['device_id'], old_binding['directory_id']) != expected:
            raise ValueError('Folder identity changed; explicit identity resolution is required')
        if old_binding is None:
            uuid = connection.execute('SELECT application_uuid FROM volumes WHERE id=?', (volume_id,)).fetchone()[0]
            connection.execute('INSERT INTO folder_bindings VALUES (?,?,?,?,?,?,?)', (root_id, host, str(root), *expected, uuid, now))
    return root_id, expected
