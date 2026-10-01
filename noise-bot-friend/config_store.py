"""Merge station settings atomically without replacing unrelated calibration/keys."""
from contextlib import contextmanager
import copy
import errno
import json
import math
import os
from pathlib import Path
import tempfile
import threading

_LOCK = threading.RLock()


def merge_values(current, patch):
    result = copy.deepcopy(current)
    for key, value in patch.items():
        result[key] = merge_values(result.get(key, {}), value) if isinstance(value, dict) and isinstance(result.get(key, {}), dict) else copy.deepcopy(value)
    return result


@contextmanager
def config_lock(path):
    with _LOCK, open(str(path) + '.lock', 'a+b') as lock:
        if os.name == 'nt':
            import msvcrt
            lock.seek(0); lock.write(b'0'); lock.flush(); lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == 'nt':
                lock.seek(0); msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock, fcntl.LOCK_UN)


def read_config(path):
    """Coordinate readers with updates to Docker's single-file config mount."""
    with config_lock(path):
        return json.loads(Path(path).read_text(encoding='utf-8'))


def _replace_config(pending, path):
    try:
        os.replace(pending, path)
    except OSError as exc:
        if exc.errno != errno.EBUSY:
            raise
        # A bind-mounted file cannot be renamed. Keep its inode and serialize
        # readers/writers with config_lock; ordinary files use atomic replace.
        replacement = Path(pending).read_bytes()
        with open(path, 'r+b') as mounted:
            original = mounted.read()
            try:
                mounted.seek(0)
                mounted.write(replacement)
                mounted.truncate()
                mounted.flush()
                os.fsync(mounted.fileno())
            except Exception:
                mounted.seek(0)
                mounted.write(original)
                mounted.truncate()
                mounted.flush()
                os.fsync(mounted.fileno())
                raise


def apply_config_patch(path, patch, revision=None):
    if not isinstance(patch, dict):
        raise ValueError('Settings must be a JSON object')
    patch = {k: v for k, v in patch.items() if not k.startswith('_')}
    path = Path(path)
    with config_lock(path):
        current = json.loads(path.read_text(encoding='utf-8'))
        updated = merge_values(current, patch)
        if 'floor_number' in patch or 'horizontal_setback_meters' in patch:
            floor = float(updated.get('floor_number', 1))
            setback = float(updated.get('horizontal_setback_meters', 5))
            if not math.isfinite(floor) or not floor.is_integer() or not 1 <= floor <= 30:
                raise ValueError('Floor must be a whole number from 1 to 30')
            if not math.isfinite(setback) or not 0 <= setback <= 10000:
                raise ValueError('Setback must be a finite, nonnegative distance')
            updated.update(floor_number=int(floor), horizontal_setback_meters=setback,
                           distance_to_road_meters=round(max(0.5, math.hypot(setback, (floor - 1) * 3)), 1))
        if 'station_name' in patch or 'location_name' in patch:
            name = patch.get('station_name') or patch.get('location_name')
            updated.setdefault('fleet_hub', {})['station_name'] = name
        changed = updated != current
        if revision is not None:
            updated['_hub_config_revision'] = revision
        if updated == current:
            return updated, False
        stat = path.stat()
        fd, pending = tempfile.mkstemp(prefix='.config-', suffix='.tmp', dir=path.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(updated, f, indent=2); f.write('\n'); f.flush(); os.fsync(f.fileno())
            os.chmod(pending, stat.st_mode & 0o777)
            if hasattr(os, 'chown'):
                os.chown(pending, stat.st_uid, stat.st_gid)
            _replace_config(pending, path)
        finally:
            if os.path.exists(pending): os.unlink(pending)
        return updated, changed
