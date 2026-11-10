"""Explicit, transactional provisional-to-physical identity conversion."""

from pathlib import PurePosixPath
import sqlite3

from ..paths import MATCHER_VERSION, relative_key
from .database import transaction


def promote_folder_in_transaction(connection, root_id, physical_volume_id, prefix, now, *, allow_unfinished=False):
    prefix = relative_key(prefix)
    root = connection.execute('SELECT r.*,v.identity_kind FROM scan_roots r JOIN volumes v ON v.id=r.volume_id WHERE r.id=?', (root_id,)).fetchone()
    target = connection.execute('SELECT identity_kind FROM volumes WHERE id=?', (physical_volume_id,)).fetchone()
    if root is None or root['identity_kind'] != 'folder' or target is None or target[0] != 'physical':
        raise ValueError('Conversion requires a provisional folder and a confirmed physical volume')
    if not allow_unfinished and connection.execute("SELECT 1 FROM scan_sessions WHERE root_id=? AND state NOT IN ('completed','cancelled','superseded')", (root_id,)).fetchone():
        raise ValueError('Finish or explicitly supersede the active session before conversion')
    if connection.execute('SELECT count(*) FROM scan_roots WHERE volume_id=?', (root['volume_id'],)).fetchone()[0] != 1:
        raise ValueError('Ambiguous provisional scope: expected exactly one root')
    if connection.execute('SELECT 1 FROM scan_roots WHERE volume_id=? AND volume_relative_path=? COLLATE WINPATH', (physical_volume_id, prefix)).fetchone():
        raise ValueError('Identity/path collision: existing physical root; explicit reconciliation required')
    last_id = 0
    while (row := connection.execute('SELECT id,volume_relative_path FROM media_files WHERE volume_id=? AND id>? ORDER BY id LIMIT 1', (root['volume_id'], last_id)).fetchone()) is not None:
        last_id = row['id']
        path = relative_key(str(PurePosixPath(prefix) / row['volume_relative_path']))
        if connection.execute('SELECT 1 FROM media_files WHERE volume_id=? AND volume_relative_path=? COLLATE WINPATH', (physical_volume_id, path)).fetchone():
            raise ValueError('Identity/path collision: existing physical file; explicit reconciliation required')
        connection.execute('UPDATE media_files SET volume_id=?,volume_relative_path=?,parent_path=?,path_key=? WHERE id=?', (physical_volume_id, path, str(PurePosixPath(path).parent), path, row['id']))
    connection.execute('UPDATE scan_roots SET volume_id=?,volume_relative_path=?,path_key=? WHERE id=?', (physical_volume_id, prefix, prefix, root_id))
    for table in ('scan_directories', 'scan_files'):
        rows = connection.execute(f'SELECT id,relative_path FROM {table} WHERE session_id IN (SELECT id FROM scan_sessions WHERE root_id=?)', (root_id,)).fetchall()
        for row in rows:
            connection.execute(f'UPDATE {table} SET path_key=? WHERE id=?', (relative_key(row['relative_path']), row['id']))
    connection.execute('UPDATE volumes SET comparison_version=? WHERE id=?', (MATCHER_VERSION, physical_volume_id))
    connection.execute('INSERT INTO identity_transitions(root_id,source_volume_id,target_volume_id,old_root_relative_path,new_root_relative_path,transitioned_at,confirmation,comparison_version) VALUES (?,?,?,?,?,?,?,?)', (root_id, root['volume_id'], physical_volume_id, root['volume_relative_path'], prefix, now, 'explicit caller confirmation', MATCHER_VERSION))


def promote_folder(connection, root_id, physical_volume_id, volume_relative_root, now):
    try:
        with transaction(connection):
            promote_folder_in_transaction(connection, root_id, physical_volume_id, volume_relative_root, now)
    except sqlite3.IntegrityError as exc:
        raise ValueError('Identity/path collision: conversion rolled back; explicit reconciliation required') from exc
