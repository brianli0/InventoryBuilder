"""Compile immutable inclusion boundaries without enumerating excluded trees."""

from dataclasses import dataclass
from functools import cmp_to_key
from hashlib import sha256
import json
import os
from pathlib import Path

from .paths import MATCHER_VERSION, compare, contains, relative_key, within, relative_to

DEFAULTS_VERSION = 'windows-runtime-v1'
SYSTEM_DIRECTORIES = ('$RECYCLE.BIN', 'System Volume Information', 'Windows', 'Program Files', 'Program Files (x86)', 'ProgramData', 'Recovery', '$WinREAgent')


@dataclass
class Policy:
    semantic: dict
    rules: list[dict]

    def match(self, volume_relative):
        key = relative_key(volume_relative)
        for rule in self.rules:
            if rule['active'] and (contains(rule['key'], key) if rule['kind'] == 'directory' else compare(rule['key'], key) == 0):
                return rule
        return None


def validate_syntax(text):
    if not text or '\0' in text or text.startswith(('\\\\', '//')):
        raise ValueError('Unsupported network/device namespace or empty path')
    supplied = Path(text)
    if supplied.drive and not supplied.is_absolute():
        raise ValueError('Drive-relative paths are ambiguous')
    remainder = str(supplied)[len(supplied.drive):]
    if ':' in remainder or any(part.endswith((' ', '.')) and part not in ('.', '..') for part in supplied.parts):
        raise ValueError('Alternate streams and ambiguous trailing dots/spaces are not supported')


def compile_policy(root, volume, volume_id, root_relative, config, cli_exclusions=()):
    rules = []

    def add(path, kind, origin, supplied, global_id=None):
        absolute = Path(os.path.abspath(path))
        active = within(absolute, volume.mount) and (within(absolute, root) or (kind == 'directory' and within(root, absolute)))
        key = relative_key(relative_to(absolute, volume.mount).as_posix()) if within(absolute, volume.mount) else str(absolute)
        if active and kind == 'directory' and contains(key, root_relative):
            raise ValueError(f'Selected target is entirely excluded by {origin}: {supplied}')
        rules.append(dict(supplied=str(supplied), path=str(absolute), key=key, kind=kind, origin=origin, global_id=global_id, active=bool(active), volume_id=volume_id if within(absolute, volume.mount) else None))

    for origin, values in (('config', config.exclude_directories), ('cli', cli_exclusions)):
        for text in values:
            validate_syntax(text)
            target = Path(text)
            if not target.is_absolute():
                target = root / target
            target = Path(os.path.abspath(target))
            if not within(target, root):
                raise ValueError(f'Exclusion outside selected root: {text}')
            # Inspect path components only. No scandir, resolve or entry into subtree.
            from .sources import is_reparse
            parts = relative_to(target, root).parts
            current = root
            for component in parts:
                current = current / component
                try:
                    info = current.lstat()
                except FileNotFoundError:
                    break
                if is_reparse(info):
                    raise ValueError(f'Exclusion passes through a reparse point: {text}')
            if target.exists() and not target.is_dir():
                raise ValueError('Directory exclusion names an existing file')
            add(target, 'directory', origin, text)
    for name in SYSTEM_DIRECTORIES:
        add(volume.mount / name, 'directory', 'system', name, f'volume-root:{name}')
    database = config.database.absolute()
    dirs = [config.log_directory, config.temp_directory or database.parent / 'tmp', config.backup_directory or database.parent / 'backups', config.report_directory or database.parent / 'reports']
    # A dedicated default application data directory may be pruned as a whole.
    if database.parent.name.lower() in ('inventorybuilder', 'runtime'):
        dirs.append(database.parent)
    for path in dirs:
        add(path, 'directory', 'runtime', str(path), 'runtime-directory')
    for suffix in ('', '-wal', '-shm', '.lock'):
        add(Path(str(database) + suffix), 'file', 'runtime', str(database) + suffix, 'database-companion')
    effective = []
    for rule in sorted((r for r in rules if r['active']), key=cmp_to_key(lambda a, b: compare(a['key'], b['key']) or ((a['kind'] > b['kind']) - (a['kind'] < b['kind'])))):
        if any(r['kind'] == 'directory' and contains(r['key'], rule['key']) or r['kind'] == rule['kind'] and compare(r['key'], rule['key']) == 0 for r in effective):
            continue
        effective.append(rule)
    semantic = dict(volume_id=volume_id, root=relative_key(root_relative), config_version=config.config_schema_version, defaults_version=DEFAULTS_VERSION, matcher_version=MATCHER_VERSION, processing='filesystem-only-v2', photos=sorted(config.photo_extensions), videos=sorted(config.video_extensions), exclusions=[dict(key=r['key'], kind=r['kind']) for r in effective])
    return Policy(semantic, rules)


def same_policy(left, right):
    a, b = dict(left), dict(right)
    ar, br = a.pop('root'), b.pop('root')
    ae, be = a.pop('exclusions'), b.pop('exclusions')
    return a == b and compare(ar, br) == 0 and len(ae) == len(be) and all(x['kind'] == y['kind'] and compare(x['key'], y['key']) == 0 for x, y in zip(ae, be))


def save_policy(connection, policy, timestamp):
    canonical = json.dumps(policy.semantic, sort_keys=True, ensure_ascii=True)
    identity = sha256(canonical.encode()).hexdigest()  # Policy text only; no media hashing.
    processing_version = json.dumps(policy.semantic['processing'],sort_keys=True) if isinstance(policy.semantic['processing'],dict) else policy.semantic['processing']
    policy_id = connection.execute('INSERT INTO scan_policies(semantic_digest,canonical_json,config_version,defaults_version,matcher_version,processing_version,created_at) VALUES (?,?,?,?,?,?,?)', (identity, canonical, policy.semantic['config_version'], DEFAULTS_VERSION, MATCHER_VERSION, processing_version, timestamp)).lastrowid
    for rule in policy.rules:
        rule['id'] = connection.execute('INSERT INTO scan_exclusions(policy_id,supplied_path,normalized_path,volume_id,path_key,match_kind,origin,global_rule_id,active) VALUES (?,?,?,?,?,?,?,?,?)', (policy_id, rule['supplied'], rule['path'], rule['volume_id'], rule['key'], rule['kind'], rule['origin'], rule['global_id'], int(rule['active']))).lastrowid
    return policy_id


def load_policy(connection, policy_id):
    row = connection.execute('SELECT canonical_json FROM scan_policies WHERE id=?', (policy_id,)).fetchone()
    rules = [dict(id=r['id'], supplied=r['supplied_path'], path=r['normalized_path'], volume_id=r['volume_id'], key=r['path_key'], kind=r['match_kind'], origin=r['origin'], global_id=r['global_rule_id'], active=bool(r['active'])) for r in connection.execute('SELECT * FROM scan_exclusions WHERE policy_id=? ORDER BY id', (policy_id,))]
    return Policy(json.loads(row[0]), rules)
