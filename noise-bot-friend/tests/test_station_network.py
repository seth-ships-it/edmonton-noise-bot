import hashlib
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import station_network as network


class NetworkReportingTests(unittest.TestCase):
    def setUp(self):
        network._CACHE.update(checked_at=None, ssid=None)

    def result(self, output, code=0):
        return SimpleNamespace(returncode=code, stdout=output)

    def test_active_network_is_hashed_with_device_identity(self):
        with patch.object(network.subprocess, "run", return_value=self.result(':Other\n*:  Cafe\\: East  \n')) as run:
            actual = network.wifi_network_id('noise-bot-05test')
        expected = 'sha256:' + hashlib.sha256(b'noise-bot-05test\0  Cafe: East  ').hexdigest()
        self.assertEqual(actual, expected)
        self.assertEqual(run.call_args.args[0][-2:], ['--rescan', 'no'])
        self.assertEqual(run.call_args.kwargs['timeout'], 2)

    def test_cache_avoids_spawning_process_on_every_heartbeat(self):
        with patch.object(network.subprocess, 'run', return_value=self.result('*:Roots\n')) as run:
            first = network.wifi_network_id('05')
            self.assertEqual(first, network.wifi_network_id('05'))
            self.assertNotEqual(first, network.wifi_network_id('06'))
        self.assertEqual(run.call_count, 1)

    def test_network_change_refreshes_after_ten_seconds(self):
        with patch.object(network.time, 'monotonic', side_effect=[0, 11]), patch.object(network.subprocess, 'run', side_effect=[self.result('*:Home\n'), self.result('*:Roots\n')]):
            self.assertNotEqual(network.wifi_network_id('05'), network.wifi_network_id('05'))

    def test_disconnection_and_setup_hotspot_are_not_reported_as_moves(self):
        for output in ['', ':Home\n', '*:Edmonton-Noise-Bot-Setup\n']:
            network._CACHE.update(checked_at=None, ssid=None)
            with patch.object(network.subprocess, 'run', return_value=self.result(output)):
                self.assertIsNone(network.wifi_network_id('05'))

    def test_missing_nmcli_or_timeout_does_not_break_heartbeat(self):
        for error in [FileNotFoundError(), subprocess.TimeoutExpired('nmcli', 2)]:
            network._CACHE.update(checked_at=None, ssid=None)
            with patch.object(network.subprocess, 'run', side_effect=error):
                self.assertIsNone(network.wifi_network_id('05'))


if __name__ == '__main__':
    unittest.main()
