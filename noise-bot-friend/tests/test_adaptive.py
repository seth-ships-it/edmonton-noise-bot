import copy
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import queue
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import numpy as np
from scipy.io import wavfile

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
from adaptive_detector import Learner, settings, period_at, percentile
from adaptive_runtime import ShadowRuntime, Observer, quality_flags
from adaptive_report import load_report
from audio_classifier import classify_audio, classify_samples
from config_store import apply_config_patch
from event_metadata import atomic_json, read_metadata, sidecar_path, retag_metadata, delete_metadata, upsert_hub_event, validated_event_metadata

IDENTITY = {'station_id': 'test', 'microphone': 'usb-test', 'alsa_card': None, 'sample_rate': 48000, 'channels': 1}
OPTIONS = {'mode': 'shadow', 'timezone': 'America/Edmonton'}


def seed(learner, utc, gain=0):
    period, date = period_at(utc, OPTIONS['timezone'])
    previous = period_at(utc-86400, OPTIONS['timezone'])[1]
    state = learner.periods[period]
    state.update(coverage={previous: {'seconds': 3600, 'last_utc': utc-86400}, date: {'seconds': 3600, 'last_utc': utc}},
                 peaks=[(utc-i, -40+gain) for i in range(50)], observed=[(utc-i, -40+gain) for i in range(50)],
                 anchor=-40+gain, accepted=-40+gain, updated_at=utc)
    learner.background.extend((utc-i, -60+gain) for i in range(60, 0, -1))
    learner.background_db = -60+gain


class LearnerTests(unittest.TestCase):
    def setUp(self):
        self.t = datetime(2026, 10, 3, 3, tzinfo=timezone.utc).timestamp()
        self.learner = Learner(IDENTITY, OPTIONS)

    def passage(self, learner=None, peak=-25):
        learner = learner or self.learner
        actions = []
        for i, level in enumerate([peak]*8 + [-60]*8):
            actions.extend(learner.step(level, self.t+(i+1)*.125))
        return next(e for kind,e in actions if kind == 'end')

    def test_off_default_and_cannot_enable_adaptive_capture(self):
        self.assertEqual(settings()['mode'], 'off')
        for value in ({'mode':'adaptive'}, {'traffic_margin_db':float('nan')}, {'unused':1}):
            with self.assertRaises(ValueError):settings(value)

    def test_local_periods_and_dst(self):
        for iso, expected in [('2026-10-03T12:00:00+00:00','day'), ('2026-10-03T00:00:00+00:00','evening'),
                              ('2026-11-01T07:30:00+00:00','night'), ('2026-11-01T08:30:00+00:00','night')]:
            self.assertEqual(period_at(datetime.fromisoformat(iso).timestamp(), OPTIONS['timezone'])[0], expected)

    def test_no_missing_samples_as_silence_and_energy_not_db_average(self):
        for i in range(8):self.learner.step(-60 if i < 4 else -40, self.t+i*.125)
        self.assertAlmostEqual(self.learner.background_db, 10*math.log10((1e-6+1e-4)/2), places=8)
        before = sum(v['seconds'] for s in self.learner.periods.values() for v in s['coverage'].values())
        for i in range(8):self.learner.step(-240, self.t+100+i*.125, flags=['missing_or_zero_audio'])
        after = sum(v['seconds'] for s in self.learner.periods.values() for v in s['coverage'].values())
        self.assertEqual(before, after)
        self.assertEqual(len(self.learner.background), 1)

    def test_readiness_requires_two_dates_and_does_not_activate(self):
        seed(self.learner,self.t)
        self.assertEqual(self.learner.reference(self.t)['status'], 'ready')
        self.assertEqual(self.learner.options['mode'], 'shadow')
        s=self.learner.periods['evening'];s['coverage'].pop(next(iter(s['coverage'])))
        self.assertIsNone(self.learner.reference(self.t)['trigger_dbfs_a'])

    def test_reference_frozen_and_loud_outlier_excluded(self):
        seed(self.learner,self.t)
        event=self.passage()
        self.assertEqual(event['reference']['trigger_dbfs_a'], -34)
        self.assertTrue(event['adaptive_would_capture'])
        self.assertFalse(self.learner.learn_candidate(event,'vehicle'))
        self.assertEqual(self.learner.periods['evening']['accepted'], -40)
        self.assertEqual(event['traffic_excess_db'],15)

    def test_raw_level_shift_does_not_change_excess(self):
        events=[]
        for gain in (0, 12):
            l=Learner(IDENTITY,OPTIONS);seed(l,self.t,gain)
            actions=[]
            for i,level in enumerate([-25+gain]*8+[-60+gain]*8):actions+=l.step(level,self.t+(i+1)*.125)
            events.append(next(e for k,e in actions if k=='end'))
        self.assertEqual(events[0]['traffic_excess_db'],events[1]['traffic_excess_db'])
        self.assertEqual(events[0]['adaptive_would_capture'],events[1]['adaptive_would_capture'])

    def test_short_transient_review_and_gap_exclusion(self):
        seed(self.learner,self.t)
        actions=[]
        for i,level in enumerate([-20]+[-60]*8):actions+=self.learner.step(level,self.t+(i+1)*.125)
        event=next(e for k,e in actions if k=='end')
        self.assertEqual(event['decision_reason'],'transient_review')
        self.learner.step(-25,self.t+2)
        ended=self.learner.invalidate('audio_gap')
        self.assertFalse(ended['adaptive_would_capture'])
        self.assertIn('audio_gap',ended['quality_flags'])

    def test_changed_conditions_hold_anchor_and_bound_drift(self):
        seed(self.learner,self.t)
        state=self.learner.periods['evening']
        state['peaks']=[(self.t,-38)]*50;state['observed']=[(self.t,-38)]*50
        event=self.passage(peak=-38);event['end_utc']=self.t+43200
        self.learner.learn_candidate(event,'vehicle')
        self.assertAlmostEqual(state['accepted'],-39.5)
        state['observed']=[(self.t,-30)]*50
        self.learner.learn_candidate(event,'vehicle')
        self.assertTrue(state['held']);self.assertAlmostEqual(state['accepted'],-39.5)

    def test_restart_identity_and_manual_reset(self):
        seed(self.learner,self.t)
        saved=self.learner.dump()
        same=Learner(IDENTITY,OPTIONS)
        self.assertTrue(same.restore(saved,self.t))
        self.assertEqual(same.reference(self.t)['traffic_dbfs_a'],-40)
        for identity,options in [({**IDENTITY,'microphone':'new'},OPTIONS),(IDENTITY,{**OPTIONS,'reset_token':'moved'})]:
            other=Learner(identity,options);self.assertFalse(other.restore(saved,self.t))

    def test_corrupt_and_future_state_rejected(self):
        seed(self.learner,self.t)
        saved=copy.deepcopy(self.learner.dump());saved['periods']['day']['anchor']=float('nan')
        with self.assertRaises(ValueError):Learner(IDENTITY,OPTIONS).restore(saved,self.t)
        saved=self.learner.dump();saved['last_utc']=self.t+500
        self.assertFalse(Learner(IDENTITY,OPTIONS).restore(saved,self.t))


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.config={'adaptive_detection':OPTIONS,'calibration_offset':105.98,'calibration_status':'approximate_phone_reference'}
        self.runtime=ShadowRuntime(self.root,self.config,IDENTITY,start=False)
        self.runtime._open()
        self.addCleanup(self.runtime.db.close)
        self.t=time.time()

    def block(self,start=0,level=-50,count=4096):
        tone=np.sin(2*np.pi*300*(np.arange(count)+start)/48000)*10**(level/20)*math.sqrt(2)
        return tone.astype(np.float32)

    def feed(self,seconds=2,level=-50):
        count=int(48000*seconds);start=0
        while start<count:
            n=min(4096,count-start);block=self.block(start,level,n);start+=n
            self.runtime._process((block,block,self.t+start/48000,start/48000,False,False,False))

    def test_exact_energy_windows_across_block_boundaries(self):
        self.feed()
        rows=self.runtime.db.execute('SELECT level,valid FROM seconds').fetchall()
        self.assertEqual(len(rows),2)
        for level,valid in rows:self.assertAlmostEqual(level,-50,places=4);self.assertEqual(valid,1)

    def test_queue_is_bounded_nonblocking(self):
        block=self.block()
        for _ in range(100):self.runtime.submit(block,block,self.t,1,False,False)
        self.assertEqual(self.runtime.blocks.qsize(),48)
        self.assertEqual(self.runtime.dropped_blocks,52)

    def test_gaps_clear_partial_second_and_flag_missing_coverage(self):
        self.feed(.5)
        self.t+=10
        self.feed(1)
        self.assertEqual(self.runtime.gap_count,1)
        rows=self.runtime.db.execute('SELECT valid,flags FROM seconds').fetchall()
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0][0],0);self.assertIn('audio_gap',rows[0][1])

    def test_quality_screening(self):
        self.assertIn('missing_or_zero_audio',quality_flags(np.zeros(6000)))
        self.assertIn('clipping',quality_flags(np.ones(6000)))
        for hz,flag in [(10,'suspected_wind_or_handling'),(5000,'suspected_weather')]:
            x=.01*np.sin(np.arange(6000)*2*np.pi*hz/48000)
            self.assertIn(flag,quality_flags(x))

    def test_offset_change_keeps_raw_model_and_gain_change_resets(self):
        revision=self.runtime.learner.revision
        self.runtime.configure({**self.config,'calibration_offset':99})
        self.runtime._reset_if_needed(self.t)
        self.assertEqual(revision,self.runtime.learner.revision)
        with patch('adaptive_runtime.gain_signature',return_value='changed-gain'):
            self.runtime._reset_if_needed(self.t)
        self.assertNotEqual(revision,self.runtime.learner.revision)

    def test_private_samples_capped_and_outside_public_upload_scan(self):
        seed(self.runtime.learner,self.t)
        event={'event_id':'event0','start_utc':self.t,'end_utc':self.t+2,'duration_seconds':2,'peak_dbfs_a':-40,
               'quality_flags':[], 'reference':self.runtime.learner.reference(self.t),
               'adaptive_would_capture':False,'fixed_captured':False}
        for i in range(8):
            self.runtime.clip=[(self.block(0,-40,48000)*32768).astype('<i2').tobytes()]
            self.runtime._event({**copy.deepcopy(event),'event_id':'event'+str(i)})
        self.assertEqual(len(list((self.runtime.root/'clips').glob('*.wav'))),5)
        self.assertFalse((self.root/'recordings').exists())
        self.assertEqual(self.runtime.db.execute('SELECT count(*) FROM events').fetchone()[0],8)

    def test_storage_reserve_stops_writes_without_breaking_learner(self):
        with patch.object(self.runtime,'_space',return_value=False):
            self.feed();self.runtime._flush()
        self.assertEqual(self.runtime.snapshot()['fallback_reason'],'disk_reserve')
        self.assertEqual(self.runtime.db.execute('SELECT count(*) FROM seconds').fetchone()[0],0)
        self.assertEqual(len(self.runtime.learner.background),2)

    def test_report_marks_stale_and_off_truthfully(self):
        self.feed();self.runtime._flush()
        report=load_report(self.root,self.config)
        self.assertEqual(report['mode'],'shadow')
        with patch('adaptive_report.time.time',return_value=self.t+500):
            self.assertEqual(load_report(self.root,self.config)['status'],'degraded')
        self.assertEqual(load_report(self.root,{})['status'],'off')

    def test_legacy_metadata_survives_retag_and_delete(self):
        self.feed(3)
        wav=self.root/'recordings'/'noise_event_20261003_010000_75dba_vehicle.wav'
        wav.parent.mkdir();wav.write_bytes(b'test-wav')
        self.runtime.annotate(wav,trigger_utc=self.t+.5,end_utc=self.t+3,duration=3,peak_dba=75,tag='vehicle',reference={'reference':{}},config=self.config)
        self.runtime._annotate_pending()
        meta=read_metadata(wav)
        self.assertAlmostEqual(meta['leq_dbfs_a'],-50,places=4)
        self.assertEqual(meta['calibration_offset'],105.98)
        renamed=wav.with_name(wav.name.replace('vehicle','siren'));wav.rename(renamed)
        retag_metadata(wav,renamed,'siren')
        self.assertEqual(read_metadata(renamed)['event_id'],meta['event_id'])
        self.assertEqual(read_metadata(renamed)['reviewed_classification'],'siren')
        delete_metadata(renamed);self.assertIsNone(read_metadata(renamed))

    def test_classifier_memory_matches_wav(self):
        for hz in (120,900,5000):
            data=(np.sin(np.arange(48000)*2*np.pi*hz/48000)*5000).astype(np.int16)
            path=self.root/'test.wav';wavfile.write(path,48000,data)
            self.assertEqual(classify_samples(data,48000),classify_audio(path))

    def test_partial_config_preserves_calibration_and_rejects_activation(self):
        path=self.root/'config.json';atomic_json(path,{**self.config,'private_key':'fixture-secret'})
        updated,_=apply_config_patch(path,{'adaptive_detection':{'mode':'off'}})
        self.assertEqual(updated['calibration_offset'],105.98);self.assertEqual(updated['private_key'],'fixture-secret')
        before=path.read_bytes()
        with self.assertRaises(ValueError):apply_config_patch(path,{'adaptive_detection':{'mode':'adaptive'}})
        self.assertEqual(path.read_bytes(),before)

    def test_off_does_not_create_worker_or_files(self):
        offroot=self.root/'off';observer=Observer(offroot,{},IDENTITY)
        observer.submit(self.block(),self.block(),self.t,0,False,False)
        self.assertIsNone(observer.runtime);self.assertFalse(offroot.exists())

    def test_hub_deduplicates_in_both_orders_scoped_to_station(self):
        meta={'schema':1,'event_id':'fixture','start_utc':123}
        heartbeat={'station_id':'a','filename':'same.wav','timestamp':130}
        upload={**heartbeat,'timestamp':123,'event_metadata':meta}
        for sequence in ((heartbeat,upload),(upload,heartbeat),(heartbeat,upload,upload)):
            events=[]
            for event in sequence:upsert_hub_event(events,dict(event))
            self.assertEqual(len(events),1);self.assertEqual(events[0]['timestamp'],123)
            self.assertEqual(events[0]['event_metadata'],meta)
            upsert_hub_event(events,{**upload,'station_id':'b'})
            self.assertEqual(len(events),2)

    def test_hub_rejects_malformed_optional_metadata(self):
        for value in (None,{}, {'schema':1,'event_id':'x','start_utc':float('nan')},
                      {'schema':1,'event_id':'x','start_utc':'y'},
                      {'schema':1,'event_id':'x','start_utc':123,'extra':'x'*70000}):
            self.assertIsNone(validated_event_metadata(value))

    def test_worker_recovers_corrupt_state_and_never_owns_an_audio_device(self):
        self.runtime.db.close()
        (self.runtime.root/'state.json').write_text('{broken')
        recovered=ShadowRuntime(self.root,self.config,IDENTITY,start=False)
        recovered._open();self.addCleanup(recovered.db.close)
        self.assertEqual(recovered.learner.recovery_note,'invalid_saved_state_relearning')
        self.assertNotIn('pyaudio',sys.modules)


if __name__=='__main__':unittest.main()
