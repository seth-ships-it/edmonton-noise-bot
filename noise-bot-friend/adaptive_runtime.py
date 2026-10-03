"""Optional bounded shadow worker: statistics, private clips, SQLite and reports.

The capture thread only copies blocks into a bounded queue. Every failure drops
optional analysis, never audio from the existing recorder. No network I/O here.
"""
from collections import deque
import copy
import json
import logging
import math
import os
from pathlib import Path
import queue
import shutil
import sqlite3
import subprocess
import threading
import time
import wave

import numpy as np

from adaptive_detector import Learner, RELEASE, WINDOW_SECONDS, fingerprint, period_at, settings
from event_metadata import atomic_json, calibration_revision, sidecar_path

RESERVE_BYTES = 256 * 1024 * 1024
PRIVATE_BYTES = 128 * 1024 * 1024
MAX_DATABASE_BYTES = 256 * 1024 * 1024


def quality_flags(raw, clipped=False):
    flags = []
    if clipped or np.max(np.abs(raw), initial=0) >= .999:
        flags.append('clipping')
    energy = float(np.mean(raw * raw))
    if not math.isfinite(energy) or energy < 1e-15:
        flags.append('missing_or_zero_audio')
        return flags
    # Conservative screening, not weather identification. Classifier and these
    # checks are heuristic; suspected examples remain reviewable in the report.
    sample = raw[-4096:]
    spectrum = np.abs(np.fft.rfft(sample * np.hanning(len(sample)))) ** 2
    freqs = np.fft.rfftfreq(len(sample), 1/48000)
    total = float(spectrum.sum()) + 1e-30
    if spectrum[freqs < 40].sum()/total > .8:
        flags.append('suspected_wind_or_handling')
    if spectrum[freqs >= 2500].sum()/total > .8:
        flags.append('suspected_weather')
    return flags


def gain_signature(card):
    if card is None:
        return 'unknown'
    try:
        r = subprocess.run(['amixer', '-c', str(card), 'scontents'], capture_output=True, text=True, timeout=2)
        if r.returncode:
            return 'unknown'
        return fingerprint({'capture_controls': [line.strip() for line in r.stdout.splitlines() if 'Capture' in line or '[on]' in line or '[off]' in line]})
    except (OSError, subprocess.SubprocessError):
        return 'unknown'


class ShadowRuntime:
    def __init__(self, root, config, identity, *, start=True):
        self.root = Path(root)/'adaptive-data'
        self.config = copy.deepcopy(config)
        self.identity = dict(identity)
        self.options = settings(config.get('adaptive_detection'))
        self.enabled = self.options['mode'] == 'shadow'
        self.blocks = queue.Queue(maxsize=48)  # About four seconds, <4 MiB.
        self.annotations = queue.Queue(maxsize=16)
        self.pending_annotations = []
        self.lock = threading.Lock()
        self.status = {'mode': self.options['mode'], 'release': RELEASE, 'status': 'learning', 'fallback_reason': 'starting'}
        self.dropped_blocks = 0
        self.gap_count = 0
        self.worker_errors = 0
        self.stop_event = threading.Event()
        self.thread = None
        self.db = None
        self.learner = None
        self.windows = deque(maxlen=640)  # 80 seconds for public-clip annotations.
        self.ring = deque(maxlen=16)
        self.clip = None
        self.clip_calibration = {}
        self.rows = []
        self.raw = np.empty(0, np.float32)
        self.weighted = np.empty(0, np.float32)
        self.marks = deque()
        self.expected_mono = None
        self.last_window_utc = 0
        self.last_flush = 0
        self.last_state_save = 0
        self.last_gain_check = 0
        self.gain = None
        self.last_options_fingerprint = None
        if self.enabled and start:
            self.thread = threading.Thread(target=self._run, name='adaptive-shadow', daemon=True)
            self.thread.start()

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.status)

    def configure(self, config):
        options = settings(config.get('adaptive_detection'))
        with self.lock:
            self.config = copy.deepcopy(config)
            self.options = options

    def submit(self, raw, weighted, utc, monotonic, fixed_captured, fixed_above, clipped=False):
        if not self.enabled or self.stop_event.is_set():
            return
        try:
            self.blocks.put_nowait((np.asarray(raw, dtype=np.float32).copy(),
                                   np.asarray(weighted, dtype=np.float32).copy(), float(utc),
                                   float(monotonic), bool(fixed_captured), bool(fixed_above), bool(clipped)))
        except queue.Full:
            self.dropped_blocks += 1

    def annotate(self, wav, *, trigger_utc, end_utc, duration, peak_dba, tag, reference, config):
        if not self.enabled:
            return
        job = {'wav': str(wav), 'trigger_utc': trigger_utc, 'end_utc': end_utc,
               'duration': duration, 'peak_dba': peak_dba, 'tag': tag, 'reference': reference,
               'offset': config.get('calibration_offset'), 'calibration_revision': calibration_revision(config),
               'deadline': time.monotonic() + 4}
        try:
            self.annotations.put_nowait(job)
        except queue.Full:
            self.worker_errors += 1

    def close(self, wait=True):
        self.stop_event.set()
        if self.thread and wait:
            self.thread.join(timeout=4)

    def _space(self):
        return shutil.disk_usage(self.root).free >= RESERVE_BYTES

    def _open(self):
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root/'clips').mkdir(exist_ok=True)
        if not self._space():
            raise OSError('insufficient_disk_reserve')
        self.db = sqlite3.connect(self.root/'shadow.sqlite', timeout=.5)
        self.db.execute('PRAGMA journal_mode=DELETE')
        self.db.execute('PRAGMA synchronous=NORMAL')
        self.db.execute('PRAGMA max_page_count=65536')
        self.db.executescript('''CREATE TABLE IF NOT EXISTS seconds(utc REAL PRIMARY KEY, period TEXT, level REAL, valid INTEGER, flags TEXT, revision TEXT);
          CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, utc REAL, period TEXT, metadata TEXT, clip TEXT, comparison TEXT);
          CREATE TABLE IF NOT EXISTS fixed_events(id TEXT PRIMARY KEY, utc REAL, metadata TEXT);
          CREATE TABLE IF NOT EXISTS hours(hour INTEGER PRIMARY KEY, valid_seconds INTEGER NOT NULL DEFAULT 0, excluded_seconds INTEGER NOT NULL DEFAULT 0);
          CREATE INDEX IF NOT EXISTS event_time ON events(utc);''')
        self.db.commit()
        self._reset_if_needed(time.time(), initial=True)

    def _reset_if_needed(self, utc, initial=False):
        with self.lock:
            options = copy.deepcopy(self.options)
        gain = gain_signature(self.identity.get('alsa_card'))
        identity = {**self.identity, 'gain': gain, 'timezone': options['timezone'], 'filter': 'A-bilinear-48000-v1'}
        token = fingerprint({'identity': identity, 'reset': options['reset_token']})
        if initial or token != self.last_options_fingerprint:
            learner = Learner(identity, options)
            state_path = self.root/'state.json'
            if initial and state_path.exists():
                try:
                    if state_path.stat().st_size > 4*1024*1024:
                        raise ValueError('Oversized state')
                    learner.restore(json.loads(state_path.read_text()), utc)
                except (ValueError, KeyError, TypeError, OSError, IndexError):
                    learner = Learner(identity, options)
                    learner.recovery_note = 'invalid_saved_state_relearning'
            elif not initial:
                learner.recovery_note = 'microphone_gain_or_location_reset'
            self.learner = learner
            self.clip = None
            self.ring.clear()
            self.windows.clear()
            self.raw = np.empty(0, np.float32)
            self.weighted = np.empty(0, np.float32)
            self.marks.clear()
            self.rows.clear()
            self.expected_mono = None
            self.last_options_fingerprint = token
        else:
            self.learner.options = options
        self.gain = gain
        self.last_gain_check = time.monotonic()

    def _process(self, item):
        raw, weighted, utc, mono, fixed, fixed_above, clipped = item
        if raw.ndim != 1 or raw.shape != weighted.shape or not len(raw):
            raise ValueError('invalid_audio_block')
        duration = len(raw)/48000
        gap = self.expected_mono is not None and abs(mono-self.expected_mono-duration) > .25
        self.expected_mono = mono
        if gap:
            self.gap_count += 1
            ended = self.learner.invalidate('audio_gap')
            if ended:
                self._event(ended)
            self.raw = np.empty(0, np.float32)
            self.weighted = np.empty(0, np.float32)
            self.marks.clear()
            self.rows.clear()
            self.ring.clear()
        self.raw = np.concatenate((self.raw, raw))
        self.weighted = np.concatenate((self.weighted, weighted))
        self.marks.append([len(raw), fixed, fixed_above, clipped, gap])
        size = 6000
        while len(self.raw) >= size:
            segment, self.raw = self.raw[:size], self.raw[size:]
            aweighted, self.weighted = self.weighted[:size], self.weighted[size:]
            window_utc = utc-len(self.raw)/48000
            remaining, captured, above, clip_flag, gap_flag = size, False, False, False, False
            while remaining:
                mark = self.marks[0]
                used = min(remaining, mark[0]);remaining -= used;mark[0] -= used
                captured |= mark[1];above |= mark[2];clip_flag |= mark[3]
                gap_flag |= mark[4]
                if not mark[0]:self.marks.popleft()
            flags = quality_flags(segment, clip_flag)
            if gap_flag:flags.append('audio_gap')
            energy = float(np.mean(aweighted.astype(np.float64)**2))
            if not math.isfinite(energy):
                flags.append('nonfinite_audio')
                energy = 1e-24
            level = 10*math.log10(max(energy, 1e-24))
            pcm = np.clip(segment*32768, -32768, 32767).astype('<i2').tobytes()
            actions = self.learner.step(level, window_utc, flags=flags, fixed_captured=captured, fixed_above=above)
            self.last_window_utc = window_utc
            self.windows.append((window_utc, level, flags))
            # A timestamp gap can end an old passage before starting a new one.
            appended = False
            for kind, event in actions:
                if kind == 'start':
                    self.clip = list(self.ring)
                    with self.lock:
                        self.clip_calibration = {'calibration_offset': self.config.get('calibration_offset'),
                                                 'calibration_revision': calibration_revision(self.config)}
                elif event['end_utc'] < window_utc:
                    self._event(event)
                    continue
                if self.clip is not None and not appended:
                    self.clip.append(pcm);appended = True
                if kind == 'end':self._event(event)
            if not appended and self.clip is not None:self.clip.append(pcm)
            self.ring.append(pcm)
            self.rows.append((window_utc, level, not flags, flags))
            if len(self.rows) == 8:
                self._second()

    def _second(self):
        rows, self.rows = self.rows, []
        utc = rows[-1][0]
        period, _ = period_at(utc, self.options['timezone'])
        valid = all(x[2] for x in rows)
        level = 10*math.log10(sum(10**(x[1]/10) for x in rows)/8)
        flags = sorted({f for x in rows for f in x[3]})
        with self.lock:
            self.status.update(reference=self.learner.reference(utc), updated_utc=utc)
        if self._space():
            self.db.execute('INSERT OR REPLACE INTO seconds VALUES(?,?,?,?,?,?)',
                            (utc, period, level, int(valid), json.dumps(flags), self.learner.revision))
            self.db.execute('INSERT INTO hours VALUES(?,?,?) ON CONFLICT(hour) DO UPDATE SET valid_seconds=valid_seconds+excluded.valid_seconds, excluded_seconds=excluded_seconds+excluded.excluded_seconds',
                            (int(utc//3600)*3600, int(valid), int(not valid)))

    def _event(self, event):
        from audio_classifier import classify_samples
        frames, self.clip = self.clip or [], None
        data = np.frombuffer(b''.join(frames), dtype='<i2')
        # Bound classifier working memory to an eight-second excerpt near the
        # strongest sample. Event energy/peak/decision still use the full event.
        start = max(0, min(len(data)-48000*8, int(np.argmax(np.abs(data.astype(np.int32))))-48000*4)) if len(data) else 0
        excerpt = data[start:start+48000*8]
        label = classify_samples(excerpt, 48000) if len(excerpt) and not event['quality_flags'] else 'review'
        event['classification_window_seconds'] = len(excerpt)/48000
        event['classification'] = label
        event['classification_is_heuristic'] = True
        event['accepted_for_learning'] = self.learner.learn_candidate(event, label)
        event.update(self.clip_calibration)
        event['comparison'] = ('learning' if event['adaptive_would_capture'] is None else
                               'both' if event['fixed_captured'] and event['adaptive_would_capture'] else
                               'fixed_only' if event['fixed_captured'] else
                               'adaptive_only' if event['adaptive_would_capture'] else 'neither')
        if not self._space():return
        date = period_at(event['start_utc'], self.options['timezone'])[1]
        category = 'disagreement' if event['comparison'] in ('adaptive_only', 'fixed_only') else 'ordinary'
        cap = 20 if category == 'disagreement' else 5
        # SQLite row caps persist over restarts; files never enter recordings/.
        key = date + '-' + event['reference']['period'] + '-' + category
        count = self.db.execute('SELECT count(*) FROM events WHERE clip LIKE ?', (key+'-%',)).fetchone()[0]
        clip_name = None
        if count < cap and len(data) and len(data) <= 48000*33:
            clip_name = key + '-' + event['event_id'] + '.wav'
            path = self.root/'clips'/clip_name
            with wave.open(str(path), 'wb') as f:
                f.setnchannels(1);f.setsampwidth(2);f.setframerate(48000);f.writeframes(data.tobytes())
            atomic_json(sidecar_path(path), event)
        self.db.execute('INSERT OR REPLACE INTO events VALUES(?,?,?,?,?,?)',
                        (event['event_id'], event['end_utc'], event['reference']['period'], json.dumps(event, allow_nan=False), clip_name, event['comparison']))

    def _annotate_pending(self):
        while len(self.pending_annotations) < 16:
            try:self.pending_annotations.append(self.annotations.get_nowait())
            except queue.Empty:break
        retained = []
        for job in self.pending_annotations:
            if self.last_window_utc < job['end_utc']-.15 and time.monotonic() < job['deadline']:
                retained.append(job);continue
            wav = Path(job['wav'])
            if not wav.exists() or not self._space():continue
            start = job['end_utc']-job['duration']
            windows = [w for w in self.windows if start <= w[0] <= job['end_utc']]
            complete = bool(windows) and len(windows)*WINDOW_SECONDS >= job['duration']-.3 and all(
                0 < b[0]-a[0] < .4 for a,b in zip(windows, windows[1:]))
            ref = job['reference'].get('reference', {})
            peak = max((w[1] for w in windows), default=None) if complete else None
            meta = {'schema': 1, 'release': RELEASE, 'event_id': fingerprint({'station': self.identity.get('station_id'), 'file': wav.name})[:32],
                    'mode': 'shadow', 'capture_reason': 'legacy_fixed', 'start_utc': start, 'end_utc': job['end_utc'],
                    'trigger_utc': job['trigger_utc'], 'duration_seconds': job['duration'],
                    'legacy_peak_dba': job['peak_dba'], 'legacy_peak_window_seconds': 4096/48000,
                    'peak_dbfs_a': peak, 'peak_window_seconds': WINDOW_SECONDS,
                    'leq_dbfs_a': 10*math.log10(sum(10**(w[1]/10) for w in windows)/len(windows)) if complete else None,
                    'reference': ref, 'classification': job['tag'], 'classification_is_heuristic': True,
                    'calibration_offset': job['offset'], 'calibration_revision': job['calibration_revision'],
                    'quality_flags': sorted({f for w in windows for f in w[2]}) + ([] if complete else ['incomplete_shadow_coverage'])}
            for name in ('traffic', 'background'):
                value = ref.get(name+'_dbfs_a')
                meta[name+'_excess_db'] = peak-value if peak is not None and value is not None else None
            trigger = ref.get('trigger_dbfs_a')
            meta['above_total_seconds'] = sum(w[1] >= trigger for w in windows)*WINDOW_SECONDS if complete and trigger is not None else None
            atomic_json(sidecar_path(wav), meta)
            self.db.execute('INSERT OR REPLACE INTO fixed_events VALUES(?,?,?)',
                            (meta['event_id'], meta['end_utc'], json.dumps(meta)))
        self.pending_annotations = retained

    def _flush(self, force_state=False):
        utc = time.time()
        self.last_flush = time.monotonic()
        if not self._space():
            self.db.rollback()
            with self.lock:self.status = {**self.status, 'status': 'degraded', 'fallback_reason': 'disk_reserve', 'updated_utc': utc}
            return
        self.db.execute('DELETE FROM seconds WHERE utc < ?', (utc-7*86400,))
        self.db.execute('DELETE FROM events WHERE utc < ?', (utc-7*86400,))
        self.db.execute('DELETE FROM fixed_events WHERE utc < ?', (utc-7*86400,))
        self.db.execute('DELETE FROM events WHERE id IN (SELECT id FROM events ORDER BY utc DESC LIMIT -1 OFFSET 10000)')
        self.db.execute('DELETE FROM fixed_events WHERE id IN (SELECT id FROM fixed_events ORDER BY utc DESC LIMIT -1 OFFSET 10000)')
        self.db.execute('DELETE FROM hours WHERE hour < ?', (utc-90*86400,))
        self.db.commit()
        # Delete only this feature's own private files. Never recordings/.
        clips = sorted((self.root/'clips').glob('*.wav'), key=lambda p:p.stat().st_mtime)
        size = sum(p.stat().st_size + (sidecar_path(p).stat().st_size if sidecar_path(p).exists() else 0) for p in clips)
        for path in clips:
            if size <= PRIVATE_BYTES and path.stat().st_mtime >= utc-7*86400:break
            size -= path.stat().st_size + (sidecar_path(path).stat().st_size if sidecar_path(path).exists() else 0)
            path.unlink()
            sidecar_path(path).unlink(missing_ok=True)
        if force_state or time.monotonic()-self.last_state_save >= 60:
            atomic_json(self.root/'state.json', self.learner.dump())
            self.last_state_save = time.monotonic()
        counts = dict(self.db.execute('SELECT comparison,count(*) FROM events GROUP BY comparison'))
        reference = self.learner.reference(utc)
        report = {'schema': 1, 'release': RELEASE, 'mode': 'shadow', 'updated_utc': utc,
                  'status': reference['status'], 'fallback_reason': reference['fallback_reason'],
                  'reference': reference,
                  'candidate_comparisons': counts, 'comparison_retention_days': 7, 'comparison_event_limit': 10000,
                  'fixed_recordings_annotated': self.db.execute('SELECT count(*) FROM fixed_events').fetchone()[0],
                  'dropped_blocks': self.dropped_blocks,
                  'audio_gaps': self.gap_count, 'worker_errors': self.worker_errors,
                  'gain_observed': self.gain != 'unknown', 'recovery_note': self.learner.recovery_note,
                  'activation': 'manual_review_required', 'private_audio_bytes': size,
                  'coverage_hours': [{'utc_hour':row[0], 'valid_seconds':row[1], 'excluded_seconds':row[2]} for row in self.db.execute('SELECT * FROM hours ORDER BY hour DESC LIMIT 168')]}
        # Summaries are public-safe; raw candidate arrays remain only in state.json.
        report['periods'] = {p: {'screened_candidates':len(s['peaks']), 'observed_candidates':len(s['observed']),
                                'accepted_traffic_dbfs_a':s['accepted'], 'anchor_dbfs_a':s['anchor'],
                                'changed_conditions':s['held'], 'valid_seconds':sum(v['seconds'] for v in s['coverage'].values())}
                             for p,s in self.learner.periods.items()}
        atomic_json(self.root/'report.json', report)
        with self.lock:self.status = {k:v for k,v in report.items() if k not in ('coverage_hours',)}
        self.last_flush = time.monotonic()

    def _run(self):
        try:
            self._open()
            self._flush()
            while not self.stop_event.is_set():
                try:item = self.blocks.get(timeout=.2)
                except queue.Empty:item = None
                if time.monotonic()-self.last_gain_check >= 60:
                    self._reset_if_needed(time.time())
                if item is not None:self._process(item)
                self._annotate_pending()
                if time.monotonic()-self.last_flush >= 10:self._flush()
            self._annotate_pending()
            self._flush(force_state=True)
        except Exception as error:
            self.worker_errors += 1
            logging.exception('Optional adaptive analysis stopped; fixed recorder continues')
            with self.lock:self.status.update(status='degraded', fallback_reason=type(error).__name__, worker_errors=self.worker_errors)
        finally:
            if self.db:self.db.close()
            self.enabled = False


class Observer:
    """Nonblocking lifecycle adapter; the legacy recorder never reads a decision."""
    def __init__(self, root, config, identity):
        self.root, self.identity = root, identity
        self.runtime = None
        self.config = {}
        self.error = None
        self.configure(config)

    def configure(self, config):
        try:
            options = settings(config.get('adaptive_detection'))
            self.error = None
        except (ValueError, TypeError, KeyError) as error:
            options = settings()
            self.error = str(error)
        self.config = {**copy.deepcopy(config), 'adaptive_detection': options}
        if self.runtime:
            self.runtime.configure(self.config)
            if options['mode'] == 'off':self.runtime.close(wait=False)

    def submit(self, *args):
        if self.config['adaptive_detection']['mode'] != 'shadow':return
        if self.runtime is None or (self.runtime.stop_event.is_set() and not self.runtime.thread.is_alive()):
            self.runtime = ShadowRuntime(self.root, self.config, self.identity)
        self.runtime.submit(*args)

    def snapshot(self):
        if self.config['adaptive_detection']['mode'] == 'off':
            return {'mode': 'off', 'status': 'degraded' if self.error else 'off',
                    'fallback_reason': self.error, 'release': RELEASE}
        return self.runtime.snapshot() if self.runtime else {'mode': 'shadow', 'status': 'learning', 'fallback_reason': 'starting', 'release': RELEASE}

    def annotate(self, *args, **kwargs):
        if self.runtime:self.runtime.annotate(*args, **kwargs)

    def close(self):
        if self.runtime:self.runtime.close()
