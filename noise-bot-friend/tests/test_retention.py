from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import archive_retention as hub
import retention_agent as agent

NAME = 'noise_event_20260701_120000_75dba_vehicle.wav'


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.pi = self.base/'pi'; (self.pi/'recordings').mkdir(parents=True)
        self.state = self.base/'state'; self.state.mkdir()
        self.archive = hub.Archive(self.base/'hub', self.state, 'noise-bot-fixture')
        self.addCleanup(self.archive.close)
        disk = patch.object(hub.shutil, 'disk_usage', return_value=SimpleNamespace(free=10*1024**3))
        disk.start(); self.addCleanup(disk.stop)

    def clip(self, name=NAME, content=b'RIFF-original', age=10*86400, metadata=False):
        p = self.pi/'recordings'/name; p.write_bytes(content)
        os.utime(p, (time.time()-age, time.time()-age))
        if metadata:
            p.with_suffix('.event.json').write_text('{"schema":1}')
        return p

    def inventory(self):
        out = io.StringIO()
        with redirect_stdout(out): agent.inventory(self.pi)
        return [x for line in out.getvalue().splitlines() if (x:=json.loads(line))['type']=='file']

    def remove(self, rows):
        out = io.StringIO()
        with redirect_stdout(out): agent.delete_verified(self.pi, rows)
        return [json.loads(line) for line in out.getvalue().splitlines()]

    def test_missing_archive_never_eligible_even_when_disk_full(self):
        self.clip(); rows = self.inventory()
        selected = hub.select_deletions(rows, {}, 0, hub.POLICY, time.time())
        self.assertEqual(selected, [])

    def test_policy_keeps_recent_and_removes_oldest_for_size_or_free_space(self):
        now = time.time()
        rows = [{'name': str(i), 'bytes': 1024**3, 'captured_utc': now-(5-i)*86400,
                 'mtime_ns': int((now-(5-i)*86400)*1e9)} for i in range(4)]
        verified = {r['name']: {} for r in rows}
        self.assertEqual([r['name'] for r in hub.select_deletions(rows, verified, 3*1024**3, hub.POLICY, now)], ['0','1'])
        options = {**hub.POLICY, 'max_local_bytes': 10*1024**3}
        self.assertEqual([r['name'] for r in hub.select_deletions(rows, verified, 0, options, now)], ['0','1'])
        rows[-1]['mtime_ns'] = int(now*1e9)
        self.assertNotIn(rows[-1], hub.select_deletions(rows, verified, 0, {**options, 'keep_days': 1}, now))

    def test_hub_writes_and_reopens_identical_bytes_before_confirmation(self):
        p = self.clip(); row = self.inventory()[0]
        destination = self.archive.directory/NAME
        self.assertTrue(self.archive.receive(destination, row, lambda f: f.write(p.read_bytes())))
        self.assertEqual(destination.read_bytes(), p.read_bytes())
        self.assertTrue(self.archive.matches(destination, row, fresh=True))
        self.assertFalse(self.archive.receive(destination, row, lambda f: self.fail('must not recopy')))

    def test_truncated_or_wrong_transfer_never_becomes_archive(self):
        self.clip(); row = self.inventory()[0]
        destination = self.archive.directory/NAME
        with self.assertRaises(ValueError): self.archive.receive(destination, row, lambda f: f.write(b'wrong'))
        self.assertFalse(destination.exists())
        self.assertEqual(list(self.archive.directory.glob('.incoming-*')), [])

    def test_existing_conflict_is_not_overwritten(self):
        self.clip(); row = self.inventory()[0]
        destination = self.archive.directory/NAME; destination.write_bytes(b'other-original')
        with self.assertRaises(ValueError): self.archive.receive(destination, row, lambda f: f.write(b'new'))
        self.assertEqual(destination.read_bytes(), b'other-original')

    def test_low_hub_disk_prevents_new_archive_confirmation(self):
        self.clip(); row = self.inventory()[0]
        destination = self.archive.directory/NAME
        with patch.object(hub.shutil, 'disk_usage', return_value=SimpleNamespace(free=10)):
            with self.assertRaises(OSError): self.archive.receive(destination, row, lambda f: f.write(b'data'))
        self.assertFalse(destination.exists())

    def test_same_size_changed_hub_file_invalidates_hash_cache(self):
        p = self.clip(content=b'aaaa'); row = self.inventory()[0]
        destination = self.archive.directory/NAME; destination.write_bytes(p.read_bytes())
        self.assertTrue(self.archive.matches(destination, row))
        destination.write_bytes(b'bbbb')
        self.assertFalse(self.archive.matches(destination, row))

    def test_confirmed_local_wav_and_sidecar_are_removed_together(self):
        p = self.clip(metadata=True); row = self.inventory()[0]
        result = self.remove([row])
        self.assertEqual(result[0]['type'], 'deleted')
        self.assertFalse(p.exists()); self.assertFalse(p.with_suffix('.event.json').exists())

    def test_modified_pi_file_is_retained_after_inventory(self):
        p = self.clip(); row = self.inventory()[0]; p.write_bytes(b'changed-data')
        self.assertEqual(self.remove([row])[0]['type'], 'retained')
        self.assertTrue(p.exists())

    def test_changed_or_new_metadata_retains_the_audio(self):
        for existed in (False, True):
            p = self.clip(metadata=existed); row = self.inventory()[0]
            p.with_suffix('.event.json').write_text('{"schema":1,"review":"changed"}')
            self.assertEqual(self.remove([row])[0]['type'], 'retained')
            self.assertTrue(p.exists())

    def test_active_temp_and_recent_clips_are_protected(self):
        p = self.clip(age=100)
        self.clip(name=NAME.replace('vehicle', 'temp'))
        rows = self.inventory(); self.assertEqual(len(rows), 1)
        self.assertEqual(self.remove(rows)[0]['type'], 'retained'); self.assertTrue(p.exists())

    def test_forged_digest_cannot_delete_a_local_file(self):
        p = self.clip(); row = self.inventory()[0]; row['sha256'] = '0'*64
        self.assertEqual(self.remove([row])[0]['type'], 'retained'); self.assertTrue(p.exists())

    def test_traversal_station_and_filename_are_rejected(self):
        for name in ('../'+NAME, '/tmp/'+NAME, 'noise_event_temp.wav'):
            with self.assertRaises(ValueError): hub.checked_path(self.archive.directory, name)
        with self.assertRaises(ValueError): hub.Archive(self.base/'hub', self.state, '../other')
        with self.assertRaises(ValueError): self.remove([{'name':'../'+NAME}])

    def test_symlink_clip_is_not_hashed_or_deleted(self):
        outside = self.base/'outside.wav'; outside.write_bytes(b'keep')
        link = self.pi/'recordings'/NAME
        try: link.symlink_to(outside)
        except OSError: self.skipTest('symlink creation unavailable for this Windows account')
        self.assertEqual(self.inventory(), [])
        self.assertEqual(outside.read_bytes(), b'keep')

    def test_invalid_policy_is_rejected(self):
        for value in ({'keep_days': 0}, {'minimum_age_seconds': 0}, {'unknown': True}, {'max_local_bytes': True}):
            with self.assertRaises(ValueError): hub.policy(value)

    def catalog_config(self):
        return {'fleet_hub': {'station_id': 'noise-bot-fixture', 'hub_url': 'https://example.invalid/yegnoise'}}

    def events(self):
        import dashboard
        with patch.object(dashboard, 'BASE_DIR', str(self.pi)), patch.object(dashboard, 'RECORDINGS_DIR', str(self.pi/'recordings')), \
             patch.object(dashboard, 'load_config', return_value=self.catalog_config()), patch.object(dashboard, '_EVENTS_CACHE', None):
            return object.__new__(dashboard.DashboardHandler).get_events()

    def test_archived_clip_stays_listed_and_points_to_the_hub(self):
        self.clip(); rows = self.inventory()
        agent.remember_archives(self.pi, rows, self.catalog_config())
        self.remove(rows)
        events = self.events()
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0]['archive_only'])
        self.assertEqual(events[0]['url'], 'https://example.invalid/yegnoise/fixture/recordings/'+NAME)

    def test_local_copy_wins_over_archive_index_without_duplicates(self):
        self.clip(); rows = self.inventory()
        agent.remember_archives(self.pi, rows, self.catalog_config())
        events = self.events()
        self.assertEqual(len(events), 1)
        self.assertFalse(events[0]['archive_only'])
        self.assertEqual(events[0]['url'], '/recordings/'+NAME)

    def test_catalog_cannot_cross_station_or_hub_identity(self):
        import dashboard
        self.clip(); rows = self.inventory()
        agent.remember_archives(self.pi, rows, self.catalog_config())
        with patch.object(dashboard, 'BASE_DIR', str(self.pi)):
            cfg = {'fleet_hub': {'station_id': 'noise-bot-other', 'hub_url': 'https://example.invalid/yegnoise'}}
            self.assertEqual(dashboard.archived_catalog(cfg), ({}, ''))
        with self.assertRaises(ValueError): agent.remember_archives(self.pi, rows, cfg)

    def test_corrupt_archive_index_stops_before_local_delete(self):
        self.clip(); rows = self.inventory()
        (self.pi/'.archive-retention/catalog.json').write_text('{broken')
        with self.assertRaises(ValueError): agent.remember_archives(self.pi, rows, self.catalog_config())
        self.assertTrue((self.pi/'recordings'/NAME).exists())

    def test_heartbeat_count_does_not_drop_when_local_copy_is_pruned(self):
        from test_adaptive_integration import detector
        d = detector(); d.BASE_DIR = str(self.pi); d.OUTPUT_DIR = str(self.pi/'recordings')
        self.clip(); rows = self.inventory()
        agent.remember_archives(self.pi, rows, self.catalog_config())
        self.assertEqual(d.recording_count(self.catalog_config()), 1)
        self.remove(rows)
        self.assertEqual(d.recording_count(self.catalog_config()), 1)
        (self.pi/'.archive-retention/catalog.json').write_text('{broken')
        self.assertEqual(d.recording_count(self.catalog_config()), 0)


if __name__ == '__main__': unittest.main()
