"""Hub-controlled verified Pi retention over pinned SSH.

Run on the machine storing the real hub recordings, never against a playback
proxy or HTTP upload acknowledgement. --archive fills missing archive copies;
--apply also deletes eligible Pi copies. With neither flag this is read-only
apart from bounded hash caches/reports. Credentials belong in private config.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import shlex
import shutil
import sqlite3
import stat
import sys
import tempfile
import time

from retention_agent import NAME, digest, identity

POLICY = {'keep_days': 7, 'max_local_bytes': 2*1024**3,
          'min_free_bytes': 2*1024**3, 'minimum_age_seconds': 3600}


def policy(value):
    if not isinstance(value, dict) or set(value)-set(POLICY):
        raise ValueError('invalid_retention_policy')
    result = {**POLICY, **value}
    limits = {'keep_days': (1, 365), 'max_local_bytes': (256*1024**2, 1024**4),
              'min_free_bytes': (512*1024**2, 1024**4), 'minimum_age_seconds': (3600, 30*86400)}
    for key, (low, high) in limits.items():
        if type(result[key]) is not int or not low <= result[key] <= high:
            raise ValueError('invalid_retention_policy_'+key)
    return result


def atomic_json(path, value):
    path = Path(path)
    fd, pending = tempfile.mkstemp(prefix='.retention-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(json.dumps(value, indent=2)); f.flush(); os.fsync(f.fileno())
        os.replace(pending, path)
    finally:
        Path(pending).unlink(missing_ok=True)


@contextmanager
def run_lock(state):
    with (state/'run.lock').open('a+b') as f:
        if os.name == 'nt':
            import msvcrt
            f.seek(0); f.write(b'0'); f.flush(); f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            if os.name == 'nt':
                f.seek(0); msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(f, fcntl.LOCK_UN)


def connect(station):
    import paramiko
    client = paramiko.SSHClient()
    keys = paramiko.HostKeys(station['known_hosts'])
    host = station['host']
    alias = station.get('host_key_alias', host)
    if alias not in keys:
        raise ValueError('missing_pinned_host_key')
    for kind, key in keys[alias].items():
        client.get_host_keys().add(host, kind, key)
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    client.connect(host, username=station['username'], password=station.get('password'),
                   key_filename=station.get('key_filename'), timeout=10,
                   auth_timeout=10, banner_timeout=10,
                   allow_agent=False, look_for_keys=False)
    return client


def remote(client, station, action, **kwargs):
    source = Path(__file__).with_name('retention_agent.py').read_text(encoding='utf-8')
    stdin, stdout, stderr = client.exec_command('python3 -u -c '+shlex.quote(source), timeout=60)
    stdin.write(json.dumps({'root': station['root'], 'station_id': station['station_id'],
                            'action': action, **kwargs}))
    stdin.flush(); stdin.channel.shutdown_write()
    for line in stdout:
        yield json.loads(line)
    if stdout.channel.recv_exit_status() != 0:
        # Do not leak command/config/credentials in unattended logs.
        raise RuntimeError('remote_'+action+'_failed')


def checked_path(directory, name):
    if not NAME.fullmatch(name) or name.endswith('_temp.wav'):
        raise ValueError('invalid_recording_name')
    p = directory/name
    if p.is_symlink() or p.parent.resolve() != directory.resolve():
        raise ValueError('unsafe_archive_path')
    return p


def validate_info(info):
    if type(info.get('bytes')) is not int or not 0 < info['bytes'] <= 64*1024**2:
        raise ValueError('invalid_recording_size')
    if not re.fullmatch(r'[0-9a-f]{64}', str(info.get('sha256', ''))):
        raise ValueError('invalid_content_hash')


class Archive:
    def __init__(self, root, state, station_id):
        if not re.fullmatch(r'noise-bot-[a-z0-9-]+', station_id):
            raise ValueError('invalid_station_id')
        self.directory = Path(root)/station_id
        if self.directory.is_symlink():
            raise ValueError('unsafe_station_archive')
        self.directory.mkdir(parents=True, exist_ok=True)
        self.state = Path(state)
        self.cache = sqlite3.connect(self.state/'hub-hashes.sqlite')
        self.cache.execute('CREATE TABLE IF NOT EXISTS hashes(path TEXT PRIMARY KEY, signature TEXT, sha TEXT)')

    def close(self):
        self.cache.commit(); self.cache.close()

    def matches(self, path, info, *, fresh=False):
        try:
            before = identity(path)
            if before['bytes'] != info['bytes']:
                return False
            signature = json.dumps(before, sort_keys=True)
            cached = self.cache.execute('SELECT signature,sha FROM hashes WHERE path=?', (str(path),)).fetchone()
            sha = cached[1] if not fresh and cached and cached[0] == signature else digest(path)
            if identity(path) != before:
                return False
            self.cache.execute('INSERT OR REPLACE INTO hashes VALUES(?,?,?)', (str(path), signature, sha))
            return sha == info['sha256']
        except (OSError, ValueError):
            return False

    def receive(self, destination, info, reader):
        if destination.exists() or destination.is_symlink():
            if not self.matches(destination, info):
                raise ValueError('archive_conflict')
            return False
        if shutil.disk_usage(self.directory).free < info['bytes']+1024**3:
            raise OSError('hub_disk_reserve')
        fd, temporary = tempfile.mkstemp(prefix='.incoming-', suffix='.tmp', dir=destination.parent)
        try:
            with os.fdopen(fd, 'wb') as f:
                reader(f)
                f.flush(); os.fsync(f.fileno())
            incoming = Path(temporary)
            if not self.matches(incoming, info, fresh=True):
                raise ValueError('transferred_content_mismatch')
            try:
                # Atomic create, with no overwrite of a concurrent hub upload.
                os.link(incoming, destination)
            except FileExistsError:
                if not self.matches(destination, info, fresh=True):
                    raise ValueError('concurrent_archive_conflict')
            if not self.matches(destination, info, fresh=True):
                raise ValueError('archive_verification_failed')
            return True
        finally:
            Path(temporary).unlink(missing_ok=True)
            self.cache.execute('DELETE FROM hashes WHERE path=?', (temporary,))

    def verify(self, row, sftp, station, archive_missing):
        validate_info(row)
        if row.get('sidecar'):
            validate_info(row['sidecar'])
        wav = checked_path(self.directory, row['name'])
        remote_path = station['root'].rstrip('/')+'/recordings/'+row['name']
        copied = False
        if not self.matches(wav, row):
            if wav.exists() or wav.is_symlink():
                raise ValueError('archive_conflict')
            if not archive_missing:
                return None, False
            copied = self.receive(wav, row, lambda f: sftp.getfo(remote_path, f))
        meta_path = None
        if row.get('sidecar'):
            name = Path(row['name']).with_suffix('.event.json').name
            meta_path = self.directory/name
            if meta_path.is_symlink():
                raise ValueError('unsafe_metadata_path')
            if meta_path.exists() and not self.matches(meta_path, row['sidecar']):
                # Keep all metadata revisions without replacing a hub review.
                versions = self.state/'metadata-revisions'/station['station_id']
                versions.mkdir(parents=True, exist_ok=True)
                meta_path = versions/(row['sidecar']['sha256']+'.json')
            if not self.matches(meta_path, row['sidecar']):
                if not archive_missing:
                    return None, copied
                self.receive(meta_path, row['sidecar'], lambda f: sftp.getfo(station['root'].rstrip('/')+'/recordings/'+name, f))
        return {'wav': str(wav), 'sidecar': str(meta_path) if meta_path else None}, copied


def select_deletions(rows, verified, free_bytes, options, now):
    total = sum(r['bytes']+(r.get('sidecar') or {}).get('bytes', 0) for r in rows)
    selected = []
    for row in sorted(rows, key=lambda r: (r['captured_utc'], r['name'])):
        age = now-max(row['captured_utc'], row['mtime_ns']/1e9)
        if age < options['minimum_age_seconds'] or row['name'] not in verified:
            continue
        expired = age > options['keep_days']*86400
        if not (expired or total > options['max_local_bytes'] or free_bytes < options['min_free_bytes']):
            continue
        size = row['bytes']+(row.get('sidecar') or {}).get('bytes', 0)
        total -= size; free_bytes += size
        selected.append(row)
    return selected


def process_station(config, station, *, archive_missing=False, apply=False):
    options = policy(station.get('policy', {}))
    state = Path(config['state_dir'])
    client = connect(station)
    archive = Archive(config['hub_recordings'], state, station['station_id'])
    rows, verified, failures = [], {}, []
    copied = deleted = freed = 0
    disk = None
    last_progress = time.monotonic()
    try:
        sftp = client.open_sftp()
        for item in remote(client, station, 'inventory'):
            if item['type'] == 'station':
                disk = item
            elif item['type'] == 'file':
                rows.append(item)
                try:
                    proof, added = archive.verify(item, sftp, station, archive_missing or apply)
                    copied += int(added)
                    if proof:
                        verified[item['name']] = proof
                    else:
                        failures.append({'name': item['name'], 'reason': 'missing_archive_copy'})
                except (OSError, ValueError) as error:
                    failures.append({'name': item['name'], 'reason': str(error) if isinstance(error, ValueError) else type(error).__name__})
                if time.monotonic()-last_progress >= 15:
                    logging.info('%s: checked=%d verified=%d copied=%d retained_unverified=%d', station['station_id'], len(rows), len(verified), copied, len(failures))
                    archive.cache.commit(); last_progress = time.monotonic()
        if disk is None:
            raise ValueError('missing_station_inventory')
        selected = select_deletions(rows, verified, disk['free_bytes'], options, time.time())
        # Save the concrete receipt/plan before any destructive remote action.
        plan = {'station_id': station['station_id'], 'created_utc': time.time(), 'policy': options,
                'files': rows, 'verified': verified, 'delete_names': [r['name'] for r in selected], 'failures': failures}
        atomic_json(state/(station['station_id']+'-plan.json'), plan)
        if apply:
            for offset in range(0, len(selected), 500):
                batch = []
                for row in selected[offset:offset+500]:
                    proof = verified[row['name']]
                    if not archive.matches(Path(proof['wav']), row, fresh=True):
                        failures.append({'name': row['name'], 'reason': 'archive_changed_before_delete'}); continue
                    if row.get('sidecar') and not archive.matches(Path(proof['sidecar']), row['sidecar'], fresh=True):
                        failures.append({'name': row['name'], 'reason': 'metadata_archive_changed_before_delete'}); continue
                    batch.append(row)
                if batch:
                    by_name = {row['name']: row for row in batch}
                    for item in remote(client, station, 'delete', records=batch, minimum_age_seconds=options['minimum_age_seconds']):
                        if item['type'] == 'deleted':
                            deleted += 1; freed += item['bytes']
                            with (state/(station['station_id']+'-deletions.jsonl')).open('a', encoding='utf-8') as audit:
                                audit.write(json.dumps({**item, 'utc': time.time(), 'archive': verified[item['name']],
                                                        'receipt': by_name[item['name']]})+'\n')
                        elif item['type'] == 'retained':
                            failures.append(item)
                        elif item['type'] == 'delete_complete':
                            disk['free_bytes'] = item['free_bytes']
                    logging.info('%s: removed %d verified local copies', station['station_id'], deleted)
        result = {'station_id': station['station_id'], 'utc': time.time(), 'apply': apply,
                  'checked': len(rows), 'verified': len(verified), 'archive_copies_added': copied,
                  'planned_deletions': len(selected), 'deleted': deleted, 'freed_bytes': freed,
                  'free_bytes': disk['free_bytes'], 'failures': failures, 'policy': options}
        atomic_json(state/(station['station_id']+'-status.json'), result)
        return result
    except Exception as error:
        atomic_json(state/(station['station_id']+'-status.json'),
                    {'station_id': station['station_id'], 'utc': time.time(), 'error': type(error).__name__,
                     'checked': len(rows), 'verified': len(verified), 'archive_copies_added': copied,
                     'deleted': deleted, 'freed_bytes': freed, 'failures': failures})
        raise
    finally:
        archive.close(); client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--archive', action='store_true')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding='utf-8'))
    state = Path(config['state_dir']); state.mkdir(parents=True, exist_ok=True)
    handlers = [RotatingFileHandler(state/'retention.log', maxBytes=5*1024**2, backupCount=3, encoding='utf-8')]
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s', handlers=handlers)
    logging.getLogger('paramiko').setLevel(logging.WARNING)
    if not Path(config['hub_recordings']).is_dir():
        raise ValueError('missing_hub_recording_directory')
    with run_lock(state):
        for station in config['stations']:
            if not station.get('enabled', False):
                continue
            try:
                result = process_station(config, station, archive_missing=args.archive, apply=args.apply)
                if sys.stdout is not None:
                    print(json.dumps({k: v for k, v in result.items() if k != 'failures'}), flush=True)
            except Exception as error:
                # Failed connections never produce deletion requests.
                atomic_json(state/(station['station_id']+'-last-error.json'),
                            {'station_id': station['station_id'], 'utc': time.time(), 'error': type(error).__name__})
                logging.error('%s: stopped (%s); unverified copies retained', station['station_id'], type(error).__name__)
                raise SystemExit(1)


if __name__ == '__main__':
    main()
