"""Pilot stop limits, independent of acoustic learning decisions.

Rates use a full five-minute window. CPU/RSS describe the whole detector
process, not an estimate of the learner's isolated cost. Historical Pi power
flags never count as a current fault. These are conservative pilot limits.
"""
from collections import deque

LIMITS = {
    'sample_seconds': 10, 'rate_window_seconds': 300,
    'active_power_fault_seconds': 120, 'minimum_free_mib': 256,
    'capture_stale_seconds': 15, 'minimum_technical_coverage': .90,
    'maximum_queue_drop_fraction': .01, 'maximum_process_cpu_fraction': .60,
    'maximum_rss_growth_mib': 64, 'rss_growth_seconds': 120,
}
HARD_AUDIO_FLAGS = frozenset(('clipping', 'missing_or_zero_audio', 'nonfinite_audio', 'audio_gap'))


class GuardPolicy:
    def __init__(self):
        self.samples = deque()
        self.power_since = None
        self.memory_since = None
        self.start_rss = None
        self.metrics = {}

    def evaluate(self, sample):
        now = sample['monotonic']
        if self.start_rss is None:
            self.start_rss = sample['rss_mib']
        if sample['power_flags'] is not None and sample['power_flags'] & 0x5:
            if self.power_since is None:
                self.power_since = now
        else:
            self.power_since = None
        growth = max(0, sample['rss_mib'] - self.start_rss)
        if growth > LIMITS['maximum_rss_growth_mib']:
            if self.memory_since is None:
                self.memory_since = now
        else:
            self.memory_since = None
        self.samples.append(dict(sample))
        window = LIMITS['rate_window_seconds']
        while len(self.samples) > 1 and self.samples[1]['monotonic'] <= now-window:
            self.samples.popleft()
        first = self.samples[0]
        span = now-first['monotonic']
        metrics = {key: sample[key] for key in ('free_mib', 'power_flags', 'rss_mib', 'worker_errors')}
        metrics.update(rss_growth_mib=growth, capture_age_seconds=now-sample['last_capture_monotonic'],
                       active_power_fault_seconds=0 if self.power_since is None else now-self.power_since,
                       rate_window_seconds=span, technical_coverage=None,
                       queue_drop_fraction=None, process_cpu_fraction=None)
        if span >= window:
            submitted = sample['submitted_blocks']-first['submitted_blocks']
            metrics.update(
                technical_coverage=max(0, min(1, (sample['technical_seconds']-first['technical_seconds'])/span)),
                queue_drop_fraction=(sample['dropped_blocks']-first['dropped_blocks'])/max(1, submitted),
                process_cpu_fraction=max(0, (sample['process_cpu_seconds']-first['process_cpu_seconds'])/span))
        self.metrics = metrics
        if sample['worker_errors']:
            return 'worker_error'
        if sample['free_mib'] < LIMITS['minimum_free_mib']:
            return 'disk_reserve'
        if metrics['capture_age_seconds'] >= LIMITS['capture_stale_seconds']:
            return 'capture_stale'
        if self.power_since is not None and now-self.power_since >= LIMITS['active_power_fault_seconds']:
            return 'sustained_power_fault'
        if self.memory_since is not None and now-self.memory_since >= LIMITS['rss_growth_seconds']:
            return 'process_memory_growth'
        if span >= window:
            if metrics['queue_drop_fraction'] > LIMITS['maximum_queue_drop_fraction']:
                return 'analysis_queue_loss'
            if metrics['technical_coverage'] < LIMITS['minimum_technical_coverage']:
                return 'poor_audio_coverage'
            if metrics['process_cpu_fraction'] > LIMITS['maximum_process_cpu_fraction']:
                return 'process_cpu_load'
        return None
