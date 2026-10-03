import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from adaptive_guard import GuardPolicy
from adaptive_runtime import Observer, ShadowRuntime
from adaptive_report import load_report
from event_metadata import atomic_json


def sample(t, **changes):
    return dict({'monotonic': t, 'process_cpu_seconds': t*.1, 'rss_mib': 140.,
                 'power_flags': 0x50000, 'free_mib': 500., 'last_capture_monotonic': t,
                 'submitted_blocks': t*12, 'dropped_blocks': 0,
                 'technical_seconds': t, 'worker_errors': 0}, **changes)


class PolicyTests(unittest.TestCase):
    def test_historical_flags_and_healthy_rates_do_not_trip(self):
        policy = GuardPolicy()
        for t in range(0, 1001, 10):
            self.assertIsNone(policy.evaluate(sample(t)))
        self.assertEqual(policy.metrics['technical_coverage'], 1.)
        self.assertLessEqual(len(policy.samples), 32)

    def test_power_requires_continuous_current_fault(self):
        policy = GuardPolicy()
        for t in range(0, 120, 10):
            self.assertIsNone(policy.evaluate(sample(t, power_flags=0x50005)))
        self.assertIsNone(policy.evaluate(sample(120)))  # Clears the streak.
        for t in range(130, 250, 10):
            self.assertIsNone(policy.evaluate(sample(t, power_flags=0x50005)))
        self.assertEqual(policy.evaluate(sample(250, power_flags=0x50005)), 'sustained_power_fault')

    def test_unavailable_power_does_not_claim_continuity(self):
        policy = GuardPolicy()
        policy.evaluate(sample(0, power_flags=5))
        policy.evaluate(sample(110, power_flags=None))
        self.assertIsNone(policy.evaluate(sample(130, power_flags=5)))

    def test_disk_capture_and_worker_stop_without_rate_warmup(self):
        for changes, reason in (({'free_mib': 255}, 'disk_reserve'),
                                ({'last_capture_monotonic': 0}, 'capture_stale'),
                                ({'worker_errors': 1}, 'worker_error')):
            self.assertEqual(GuardPolicy().evaluate(sample(15, **changes)), reason)

    def test_rate_limits_require_full_window_and_recover_old_bad_periods(self):
        for changes, reason in (({'technical_seconds': 269}, 'poor_audio_coverage'),
                                ({'dropped_blocks': 37}, 'analysis_queue_loss'),
                                ({'process_cpu_seconds': 181}, 'process_cpu_load')):
            policy = GuardPolicy(); policy.evaluate(sample(0))
            self.assertIsNone(policy.evaluate(sample(299, **changes)))
            self.assertEqual(policy.evaluate(sample(300, **changes)), reason)
        policy = GuardPolicy(); policy.evaluate(sample(0))
        policy.evaluate(sample(300, technical_seconds=200))
        self.assertIsNone(policy.evaluate(sample(600, technical_seconds=500)))

    def test_rate_boundaries_are_inclusive(self):
        policy = GuardPolicy(); policy.evaluate(sample(0))
        self.assertIsNone(policy.evaluate(sample(300, technical_seconds=270,
                                                 dropped_blocks=36, process_cpu_seconds=180)))

    def test_sustained_memory_growth_not_short_peak(self):
        policy = GuardPolicy(); policy.evaluate(sample(0))
        policy.evaluate(sample(10, rss_mib=205))
        self.assertIsNone(policy.evaluate(sample(120, rss_mib=205)))
        policy.evaluate(sample(130))
        policy.evaluate(sample(140, rss_mib=205))
        self.assertEqual(policy.evaluate(sample(260, rss_mib=205)), 'process_memory_growth')


class RuntimeGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = {'adaptive_detection': {'mode': 'shadow'}, 'calibration_offset': 105.98,
                       'threshold_dba': 70, 'private_key': 'test-fixture',
                       'horizontal_setback_meters': 5, 'floor_number': 3}
        atomic_json(self.root/'config.json', self.config)
        self.runtime = ShadowRuntime(self.root, self.config, {}, start=False)

    def test_stop_persists_only_mode_and_report_survives_restart(self):
        self.runtime._trip_guard('disk_reserve')
        current = json.loads((self.root/'config.json').read_text())
        for key, value in self.config.items():
            if key != 'adaptive_detection': self.assertEqual(current[key], value)
        self.assertEqual(current['adaptive_detection']['mode'], 'off')
        self.assertTrue(self.runtime.stop_event.is_set())
        self.assertEqual(self.runtime.snapshot()['status'], 'guard_stopped')
        self.assertEqual(load_report(self.root, current)['fallback_reason'], 'disk_reserve')
        observer = Observer(self.root, current, {})
        observer.submit(None, None, 0, 0, False, False)
        self.assertIsNone(observer.runtime)

    def test_trip_latches_until_manual_off_then_shadow(self):
        observer = Observer(self.root, self.config, {})
        observer.runtime = self.runtime; observer.resume_requested = False
        self.runtime.thread = Mock(); self.runtime.thread.is_alive.return_value = False
        self.runtime._trip_guard('worker_error')
        with patch('adaptive_runtime.ShadowRuntime') as factory:
            observer.submit(None, None, 0, 0, False, False)
            observer.configure(self.config)  # Stale shadow config cannot restart it.
            observer.submit(None, None, 0, 0, False, False)
            factory.assert_not_called()
            observer.configure({**self.config, 'adaptive_detection': {'mode': 'off'}})
            observer.configure(self.config)
            observer.submit(None, None, 0, 0, False, False)
            factory.assert_called_once()

    def test_failed_persistence_still_stops_in_memory_and_reports_failure(self):
        with patch('adaptive_runtime.apply_config_patch', side_effect=OSError('fixture disk error')):
            self.runtime._trip_guard('disk_reserve')
        self.assertTrue(self.runtime.stop_event.is_set())
        self.assertFalse(self.runtime.guard_trip['persisted_mode_off'])
        self.assertEqual(self.runtime.guard_trip['persistence_error'], 'OSError')
        self.runtime.submit(None, None, 0, 0, False, False)
        self.assertEqual(self.runtime.blocks.qsize(), 0)

    def test_worker_exception_persists_stop(self):
        with patch.object(self.runtime, '_open', side_effect=ValueError('fixture')):
            self.runtime._run()
        self.assertEqual(self.runtime.guard_trip['reason'], 'worker_error')
        self.assertEqual(json.loads((self.root/'config.json').read_text())['adaptive_detection']['mode'], 'off')

    def test_weather_screens_learning_but_is_not_technical_failure(self):
        self.runtime._open(); self.addCleanup(self.runtime.db.close)
        tone = (np.sin(np.arange(6000)*2*np.pi*5000/48000)*.01).astype(np.float32)
        self.runtime._process((tone, tone, 1800000000., 1., False, False, False))
        self.assertEqual(self.runtime.technical_seconds, .125)
        self.assertIn('suspected_weather', self.runtime.rows[0][3])


if __name__ == '__main__': unittest.main()
