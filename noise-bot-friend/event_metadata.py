"""Durable metadata owned by an audio event, including legacy-safe reads."""
import json
import math
import os
from pathlib import Path
import tempfile
import time


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name, suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            # One buffered write avoids thousands of tiny Python writes for a
            # full learner checkpoint on a Pi 3 / microSD card.
            f.write(json.dumps(value, allow_nan=False, separators=(',', ':')))
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def sidecar_path(wav):
    return Path(wav).with_suffix('.event.json')


def read_metadata(wav, wait_seconds=0):
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            p = sidecar_path(wav)
            if p.stat().st_size > 65536:
                return None
            result = json.loads(p.read_text(encoding='utf-8'))
            return result if isinstance(result, dict) and result.get('schema') == 1 else None
        except (OSError, ValueError):
            if time.monotonic() >= deadline:
                return None
            time.sleep(.05)


def calibration_revision(config):
    from adaptive_detector import fingerprint
    return fingerprint({'offset': config.get('calibration_offset'),
                        'status': config.get('calibration_status'),
                        'record': config.get('microphone_calibration')})[:16]


def retag_metadata(old_wav, new_wav, tag):
    meta = read_metadata(old_wav)
    if meta:
        meta['reviewed_classification'] = tag
        atomic_json(sidecar_path(new_wav), meta)
        if sidecar_path(old_wav) != sidecar_path(new_wav):
            sidecar_path(old_wav).unlink(missing_ok=True)


def delete_metadata(wav):
    sidecar_path(wav).unlink(missing_ok=True)


def validated_event_metadata(value):
    """Bound an optional sidecar received by an older/newer fleet hub."""
    if not isinstance(value, dict) or value.get('schema') != 1:
        return None
    try:
        if len(json.dumps(value, allow_nan=False)) > 65536:return None
        if not isinstance(value['event_id'], str) or not 1 <= len(value['event_id']) <= 64:return None
        if not isinstance(value['start_utc'], (int, float)) or not math.isfinite(value['start_utc']):return None
    except (ValueError, TypeError, KeyError):return None
    allowed = {'schema','release','event_id','mode','capture_reason','start_utc','end_utc','trigger_utc',
               'duration_seconds','legacy_peak_dba','legacy_peak_window_seconds','peak_dbfs_a','peak_window_seconds',
               'leq_dbfs_a','reference','classification','classification_is_heuristic','reviewed_classification',
               'calibration_offset','calibration_revision','quality_flags','traffic_excess_db','background_excess_db',
               'above_total_seconds'}
    return {key:value[key] for key in allowed if key in value}


def upsert_hub_event(events, event):
    """Heartbeat and upload may arrive in either order; station scopes identity."""
    event_id = (event.get('event_metadata') or {}).get('event_id')
    for existing in events:
        if existing.get('station_id') != event.get('station_id'):continue
        same_id = event_id and event_id == (existing.get('event_metadata') or {}).get('event_id')
        if same_id or existing.get('filename') == event.get('filename'):
            update = dict(event)
            if existing.get('event_metadata') and not event.get('event_metadata'):
                for key in ('timestamp','time_str','event_metadata'):update.pop(key,None)
            existing.update(update)
            return
    events.append(event)
    del events[:-100]
