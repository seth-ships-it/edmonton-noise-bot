"""Small SSH-side helper. It never opens the mic or changes station settings.

The hub supplies this source over the existing pinned SSH connection. Inventory
hashes are cached by complete filesystem identity. Delete requests must match
that inventory exactly and can only name completed files in recordings/.
"""
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
import time

NAME = re.compile(r'noise_event_(\d{8})_(\d{6})_\d+dba_[a-zA-Z0-9_-]+\.wav\Z')


def emit(value):
    print(json.dumps(value), flush=True)


def identity(path):
    s = path.lstat()
    if not stat.S_ISREG(s.st_mode) or path.is_symlink():
        raise ValueError('not_regular_file')
    return {'bytes': s.st_size, 'mtime_ns': s.st_mtime_ns,
            'ctime_ns': s.st_ctime_ns, 'inode': s.st_ino}


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024*1024), b''):
            h.update(block)
    return h.hexdigest()


def inspect_file(path, cache=None):
    before = identity(path)
    signature = json.dumps(before, sort_keys=True)
    stored = cache.execute('SELECT signature,sha FROM hashes WHERE name=?', (path.name,)).fetchone() if cache else None
    sha = stored[1] if stored and stored[0] == signature else digest(path)
    if identity(path) != before:
        raise ValueError('file_changed_during_hash')
    if cache:
        cache.execute('INSERT OR REPLACE INTO hashes VALUES(?,?,?)', (path.name, signature, sha))
    return {**before, 'sha256': sha}


def inventory(root, minimum_age_seconds=60):
    directory = root/'recordings'
    state = root/'.archive-retention'
    state.mkdir(mode=0o700, exist_ok=True)
    with closing(sqlite3.connect(state/'hashes.sqlite')) as cache, cache:
        cache.execute('CREATE TABLE IF NOT EXISTS hashes(name TEXT PRIMARY KEY, signature TEXT, sha TEXT)')
        skipped = 0
        count = 0
        for path in sorted(directory.iterdir(), key=lambda p: p.name):
            match = NAME.fullmatch(path.name)
            if not match or path.name.endswith('_temp.wav'):
                continue
            try:
                info = identity(path)
                if time.time()-info['mtime_ns']/1e9 < minimum_age_seconds:
                    skipped += 1
                    continue
                captured = datetime.strptime(''.join(match.groups()), '%Y%m%d%H%M%S').replace(tzinfo=timezone.utc).timestamp()
                wav = inspect_file(path, cache)
                sidecar = path.with_suffix('.event.json')
                meta = inspect_file(sidecar, cache) if sidecar.exists() or sidecar.is_symlink() else None
                emit({'type': 'file', 'name': path.name, **wav, 'captured_utc': captured, 'sidecar': meta})
                count += 1
                if count % 100 == 0:
                    cache.commit()
            except (OSError, ValueError):
                skipped += 1
        # Bound stale cache entries after successful local pruning/retagging.
        names = {p.name for p in directory.iterdir() if p.is_file()}
        for (name,) in cache.execute('SELECT name FROM hashes').fetchall():
            if name not in names:
                cache.execute('DELETE FROM hashes WHERE name=?', (name,))
    emit({'type': 'inventory_complete', 'count': count, 'skipped': skipped})


def same_identity(path, record):
    return identity(path) == {k: record[k] for k in ('bytes', 'mtime_ns', 'ctime_ns', 'inode')}


def delete_verified(root, records, minimum_age_seconds=3600):
    if not 3600 <= minimum_age_seconds <= 30*86400 or len(records) > 1000:
        raise ValueError('invalid_delete_limits')
    directory = root/'recordings'
    state = root/'.archive-retention'
    state.mkdir(mode=0o700, exist_ok=True)
    # Only receipts from the current authenticated hub session are accepted.
    with closing(sqlite3.connect(state/'hashes.sqlite')) as cache, cache:
        for row in records:
            name = row['name']
            if not NAME.fullmatch(name) or name.endswith('_temp.wav'):
                raise ValueError('invalid_recording_name')
            path = directory/name
            try:
                match = NAME.fullmatch(name)
                captured = datetime.strptime(''.join(match.groups()), '%Y%m%d%H%M%S').replace(tzinfo=timezone.utc).timestamp()
                if time.time()-max(captured, row['mtime_ns']/1e9) < minimum_age_seconds:
                    raise ValueError('too_recent')
                if not same_identity(path, row):
                    raise ValueError('file_changed')
                cached = cache.execute('SELECT signature,sha FROM hashes WHERE name=?', (name,)).fetchone()
                if not cached or json.loads(cached[0]) != identity(path) or cached[1] != row['sha256']:
                    raise ValueError('not_verified_in_inventory')
                sidecar = path.with_suffix('.event.json')
                meta = row.get('sidecar')
                if meta:
                    if not same_identity(sidecar, meta) or digest(sidecar) != meta['sha256']:
                        raise ValueError('metadata_changed')
                elif sidecar.exists() or sidecar.is_symlink():
                    raise ValueError('new_metadata')
                if not same_identity(path, row):
                    raise ValueError('file_changed')
                path.unlink()
                # Delete only the matching sidecar from this same receipt.
                if meta and same_identity(sidecar, meta):
                    sidecar.unlink()
                cache.execute('DELETE FROM hashes WHERE name IN (?,?)', (name, sidecar.name))
                emit({'type': 'deleted', 'name': name, 'bytes': row['bytes']})
            except (OSError, ValueError, KeyError) as error:
                emit({'type': 'retained', 'name': name, 'reason': type(error).__name__})
    emit({'type': 'delete_complete', 'free_bytes': shutil.disk_usage(root).free})


def remember_archives(root, records, config):
    """Keep hub-backed clips discoverable after their local audio is removed."""
    path = root/'.archive-retention'/'catalog.json'
    fleet = config['fleet_hub']
    catalog = {'schema': 1, 'station_id': fleet['station_id'], 'hub_url': fleet['hub_url'].rstrip('/'), 'events': {}}
    if path.exists():
        previous = json.loads(path.read_text())
        if previous.get('station_id') != catalog['station_id'] or previous.get('hub_url') != catalog['hub_url']:
            raise ValueError('archive_catalog_identity_changed')
        catalog['events'] = previous['events']
    for row in records:
        if not NAME.fullmatch(row['name']) or row['name'].endswith('_temp.wav'):
            raise ValueError('invalid_catalog_filename')
        catalog['events'][row['name']] = row['mtime_ns']/1e9
    catalog['events'] = dict(sorted(catalog['events'].items(), reverse=True)[:20000])
    fd, pending = tempfile.mkstemp(prefix='.catalog-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(json.dumps(catalog)); f.flush(); os.fsync(f.fileno())
        os.replace(pending, path)
    finally:
        Path(pending).unlink(missing_ok=True)


@contextmanager
def process_lock(root):
    import fcntl
    state = root/'.archive-retention'
    state.mkdir(mode=0o700, exist_ok=True)
    with (state/'agent.lock').open('a+b') as f:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def main():
    request = json.load(sys.stdin)
    root = Path(request['root']).resolve(strict=True)
    config = json.loads((root/'config.json').read_text())
    if config.get('fleet_hub', {}).get('station_id') != request['station_id']:
        raise ValueError('station_identity_mismatch')
    output = Path(config.get('output_directory', './recordings'))
    if not output.is_absolute():
        output = root/output
    if output.resolve() != root/'recordings' or (root/'recordings').is_symlink():
        raise ValueError('unsupported_recording_directory')
    try:
        os.nice(15)
    except OSError:
        pass
    with process_lock(root):
        usage = shutil.disk_usage(root)
        emit({'type': 'station', 'station_id': request['station_id'], 'free_bytes': usage.free,
              'total_bytes': usage.total, 'root': str(root)})
        if request['action'] == 'inventory':
            inventory(root)
        elif request['action'] == 'delete':
            remember_archives(root, request['records'], config)
            delete_verified(root, request['records'], request['minimum_age_seconds'])
        else:
            raise ValueError('unsupported_action')


if __name__ == '__main__':
    main()
