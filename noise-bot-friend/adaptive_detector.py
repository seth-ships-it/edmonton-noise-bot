"""Bounded, replayable relative-noise learner. It never owns an audio device.

This release supports off/shadow only. Adaptive capture needs a reviewed pilot;
readiness alone never changes the recorder or the microphone calibration.
"""
from collections import deque
from datetime import datetime
import hashlib
import json
import math
import statistics
import uuid
from zoneinfo import ZoneInfo

RELEASE = '2026.10.03-shadow.2'
SCHEMA = 1
WINDOW_SECONDS = .125
PERIODS = ('day', 'evening', 'night')
DEFAULTS = {'mode': 'off', 'timezone': 'America/Edmonton', 'reset_token': '',
            'background_margin_db': 10., 'traffic_margin_db': 6.}


def settings(value=None):
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError('Adaptive settings must be an object')
    unknown = set(value) - set(DEFAULTS)
    if unknown:
        raise ValueError('Unknown adaptive settings: ' + ', '.join(sorted(unknown)))
    result = {**DEFAULTS, **value}
    if result['mode'] not in ('off', 'shadow'):
        raise ValueError('This pilot supports off or shadow; adaptive capture is not enabled')
    try:
        ZoneInfo(result['timezone'])
    except (KeyError, TypeError) as error:
        raise ValueError('Unknown learning timezone') from error
    if not isinstance(result['reset_token'], str) or len(result['reset_token']) > 100:
        raise ValueError('Invalid learning reset token')
    for key in ('background_margin_db', 'traffic_margin_db'):
        v = float(result[key])
        if not math.isfinite(v) or not 3 <= v <= 30:
            raise ValueError('Relative margins must be between 3 and 30 dB')
        result[key] = v
    return result


def fingerprint(identity):
    return hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def period_at(utc, timezone):
    local = datetime.fromtimestamp(utc, ZoneInfo(timezone))
    return ('day' if 6 <= local.hour < 18 else 'evening' if 18 <= local.hour < 23 else 'night',
            local.date().isoformat())


def db_energy(energy):
    return 10 * math.log10(max(float(energy), 1e-24))


def percentile(values, fraction):
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    return ordered[lower] + (ordered[min(lower + 1, len(ordered) - 1)] - ordered[lower]) * (position - lower)


class Learner:
    def __init__(self, identity, options=None):
        self.options = settings(options)
        self.identity = fingerprint({**identity, 'reset_token': self.options['reset_token']})
        self.revision = uuid.uuid4().hex
        self.background = deque(maxlen=900)
        self.periods = {p: {'coverage': {}, 'peaks': [], 'observed': [], 'anchor': None,
                            'accepted': None, 'updated_at': None, 'held': False,
                            'excluded_candidates': 0} for p in PERIODS}
        self.active = None
        self.second = []
        self.background_db = None
        self.last_utc = None
        self.excluded_windows = 0
        self.recovery_note = None
        self.last_prune = 0

    def _prune(self, utc):
        cutoff = utc - 7 * 86400
        while self.background and self.background[0][0] < utc - 900:
            self.background.popleft()
        for state in self.periods.values():
            for key in ('peaks', 'observed'):
                state[key] = [x for x in state[key] if x[0] >= cutoff][-5000:]
            state['coverage'] = {d: v for d, v in state['coverage'].items()
                                 if v['last_utc'] >= cutoff}
        self.last_prune = utc

    def reference(self, utc):
        period, _ = period_at(utc, self.options['timezone'])
        state = self.periods[period]
        coverage = sum(x['seconds'] for x in state['coverage'].values())
        dates = sum(x['seconds'] >= 60 for x in state['coverage'].values())
        ready = coverage >= 7200 and len(state['peaks']) >= 50 and dates >= 2
        fresh = bool(self.background and utc - self.background[-1][0] <= 5)
        background = self.background_db if fresh and len(self.background) >= 60 else None
        traffic = state['accepted'] if ready else None
        trigger = max(background + self.options['background_margin_db'], traffic + self.options['traffic_margin_db']) if background is not None and traffic is not None else None
        reason = ('changed_conditions' if state['held'] else 'recent_background_unavailable' if background is None
                  else 'insufficient_period_learning' if not ready else None)
        return {'period': period, 'status': 'degraded' if state['held'] or not fresh else 'ready' if trigger is not None else 'learning',
                'background_dbfs_a': background, 'traffic_dbfs_a': traffic,
                'observed_traffic_dbfs_a': statistics.median(x[1] for x in state['observed']) if state['observed'] else None,
                'trigger_dbfs_a': trigger, 'valid_seconds': round(coverage, 3),
                'screened_candidates': len(state['peaks']), 'dates': dates,
                'excluded_candidates': state['excluded_candidates'], 'fallback_reason': reason,
                'revision': self.revision, 'fingerprint': self.identity,
                'peak_window_seconds': WINDOW_SECONDS, 'background_percentile': 20,
                'policy': {k: self.options[k] for k in ('background_margin_db', 'traffic_margin_db', 'timezone')},
                'background_window_seconds': 900, 'background_age_seconds': round(utc-self.background[-1][0], 3) if self.background else None}

    def invalidate(self, reason):
        self.second.clear()
        if self.active:
            self.active['quality_flags'].add(reason)
            return self._finish(self.last_utc, reason)
        return None

    def step(self, level, utc, *, valid=True, flags=(), fixed_captured=False, fixed_above=False):
        """One exact 125-ms A-weighted energy window; return start/end actions."""
        actions = []
        if self.last_utc is not None and (utc <= self.last_utc or utc - self.last_utc > .4):
            ended = self.invalidate('audio_gap')
            if ended:
                actions.append(('end', ended))
        self.last_utc = utc
        if not math.isfinite(level):
            valid = False
            flags = tuple(flags) + ('nonfinite_audio',)
        valid = valid and not flags
        reference = self.reference(utc)
        bg = reference['background_dbfs_a']
        if self.active is None and valid and bg is not None and level >= bg + 3:
            self.active = {'event_id': uuid.uuid4().hex, 'start_utc': utc-WINDOW_SECONDS,
                           'reference': reference, 'peak_dbfs_a': level, 'energy': 0.,
                           'window_count': 0, 'above_run_seconds': 0., 'above_max_seconds': 0.,
                           'above_total_seconds': 0., 'release_seconds': 0., 'quality_flags': set(),
                           'fixed_captured': False, 'fixed_above_threshold': False}
            actions.append(('start', self.active['event_id']))
        if self.active is not None:
            event = self.active
            event['quality_flags'].update(flags)
            event['fixed_captured'] |= fixed_captured
            event['fixed_above_threshold'] |= fixed_above
            event['window_count'] += 1
            event['energy'] += 10 ** (max(-240., min(40., level))/10) if math.isfinite(level) else 0
            event['peak_dbfs_a'] = max(event['peak_dbfs_a'], level) if valid else event['peak_dbfs_a']
            trigger = event['reference']['trigger_dbfs_a']
            if valid and trigger is not None and level >= trigger:
                event['above_run_seconds'] += WINDOW_SECONDS
                event['above_total_seconds'] += WINDOW_SECONDS
                event['above_max_seconds'] = max(event['above_max_seconds'], event['above_run_seconds'])
            else:
                event['above_run_seconds'] = 0.
            event['release_seconds'] = event['release_seconds'] + WINDOW_SECONDS if level < event['reference']['background_dbfs_a'] + 1.5 else 0.
            capped = event['window_count'] * WINDOW_SECONDS >= 30
            if not valid or capped or event['release_seconds'] >= 1:
                actions.append(('end', self._finish(utc, 'continuous_sound' if capped else None)))
        self.second.append((utc, level, valid))
        if len(self.second) == 8:
            if all(x[2] for x in self.second):
                value = db_energy(sum(10**(x[1]/10) for x in self.second)/8)
                self.background.append((utc, value))
                for t, _, _ in self.second:
                    period, date = period_at(t, self.options['timezone'])
                    coverage = self.periods[period]['coverage'].setdefault(date, {'seconds': 0., 'last_utc': t})
                    coverage['seconds'] += WINDOW_SECONDS
                    coverage['last_utc'] = t
            else:
                self.excluded_windows += sum(not x[2] for x in self.second)
            self.second.clear()
            if utc - self.last_prune >= 60:
                self._prune(utc)
            self.background_db = percentile([x[1] for x in self.background], .2) if self.background else None
        return actions

    def _finish(self, utc, reason):
        event, self.active = self.active, None
        ref = event['reference']
        if reason:
            event['quality_flags'].add(reason)
        event.update(schema=SCHEMA, release=RELEASE, mode='shadow', end_utc=utc,
                     duration_seconds=event['window_count'] * WINDOW_SECONDS,
                     leq_dbfs_a=db_energy(event.pop('energy')/event['window_count']))
        event['quality_flags'] = sorted(event['quality_flags'])
        event['traffic_excess_db'] = event['peak_dbfs_a'] - ref['traffic_dbfs_a'] if ref['traffic_dbfs_a'] is not None else None
        event['background_excess_db'] = event['peak_dbfs_a'] - ref['background_dbfs_a']
        event['adaptive_would_capture'] = None if ref['trigger_dbfs_a'] is None else bool(
            not event['quality_flags'] and (event['above_max_seconds'] >= .5 or event['peak_dbfs_a'] >= ref['trigger_dbfs_a'] + 8))
        event['decision_reason'] = ('learning' if event['adaptive_would_capture'] is None else
                                    'quality_excluded' if event['quality_flags'] else
                                    'sustained_excess' if event['above_max_seconds'] >= .5 else
                                    'transient_review' if event['adaptive_would_capture'] else 'ordinary_passage')
        event.pop('release_seconds')
        return event

    def learn_candidate(self, event, label):
        """A heuristic vehicle candidate is evidence for learning, not a confirmed car."""
        state = self.periods[event['reference']['period']]
        valid = (not event['quality_flags'] and label in ('vehicle', 'ets')
                 and .5 <= event['duration_seconds'] < 30)
        if not valid:
            state['excluded_candidates'] += 1
            return False
        utc, peak = event['end_utc'], event['peak_dbfs_a']
        state['observed'].append((utc, peak))
        # Preserve the observed trend while loud outliers cannot raise their own
        # future trigger. During bootstrap the median supplies robustness.
        accepted = state['accepted'] is None or peak <= state['accepted'] + self.options['traffic_margin_db']
        if accepted:
            state['peaks'].append((utc, peak))
        else:
            state['excluded_candidates'] += 1
        self._prune(utc)
        if state['peaks']:
            target = statistics.median(x[1] for x in state['peaks'])
            coverage = sum(x['seconds'] for x in state['coverage'].values())
            dates = sum(x['seconds'] >= 60 for x in state['coverage'].values())
            if coverage >= 7200 and len(state['peaks']) >= 50 and dates >= 2:
                if state['anchor'] is None:
                    state.update(anchor=target, accepted=target, updated_at=utc)
                else:
                    observed = statistics.median(x[1] for x in state['observed'])
                    state['held'] = abs(observed - state['anchor']) > 3
                    if not state['held']:
                        limit = min(1., max(0., (utc-state['updated_at'])/86400))
                        updated = state['accepted'] + max(-limit, min(limit, target-state['accepted']))
                        state['accepted'] = max(state['anchor']-3, min(state['anchor']+3, updated))
                        state['updated_at'] = utc
                self.revision = uuid.uuid4().hex
        return accepted

    def dump(self):
        return {'schema': SCHEMA, 'identity': self.identity, 'revision': self.revision,
                'periods': self.periods, 'background': list(self.background), 'last_utc': self.last_utc,
                'excluded_windows': self.excluded_windows}

    def restore(self, saved, utc):
        if saved.get('schema') != SCHEMA or saved.get('identity') != self.identity:
            self.recovery_note = 'input_changed_or_new_schema'
            return False
        if set(saved['periods']) != set(PERIODS):
            raise ValueError('Invalid period state')
        # Reject malformed/poisoned files before replacing any live state.
        if len(json.dumps(saved, allow_nan=False)) > 4 * 1024 * 1024:
            raise ValueError('Oversized learning state')
        for state in saved['periods'].values():
            if any(not isinstance(state[k], list) or len(state[k]) > 5000 for k in ('peaks', 'observed')):
                raise ValueError('Invalid candidate state')
            for k in ('anchor', 'accepted', 'updated_at'):
                if state[k] is not None and not math.isfinite(float(state[k])):
                    raise ValueError('Invalid learned reference')
            for key in ('peaks', 'observed'):
                for pair in state[key]:
                    if len(pair) != 2 or not all(isinstance(x, (float, int)) and math.isfinite(x) for x in pair) or not -240 <= pair[1] <= 40:
                        raise ValueError('Invalid candidate measurement')
            if not isinstance(state['coverage'], dict) or len(state['coverage']) > 10:
                raise ValueError('Invalid coverage')
            for date, coverage in state['coverage'].items():
                datetime.fromisoformat(date)
                if not 0 <= coverage['seconds'] <= 86400 or not math.isfinite(coverage['last_utc']):
                    raise ValueError('Invalid coverage measurement')
        for pair in saved['background']:
            if len(pair) != 2 or not all(math.isfinite(x) for x in pair) or not -240 <= pair[1] <= 40:
                raise ValueError('Invalid background measurement')
        if saved.get('last_utc') and saved['last_utc'] > utc + 1:
            self.recovery_note = 'clock_moved_backwards'
            return False
        self.periods = saved['periods']
        self.revision = saved['revision']
        self.background = deque(saved['background'][-900:], maxlen=900)
        self.excluded_windows = saved.get('excluded_windows', 0)
        self._prune(utc)
        self.background_db = percentile([x[1] for x in self.background], .2) if self.background else None
        return True
