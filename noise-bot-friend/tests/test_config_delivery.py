import copy
import errno
import io
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import urllib.parse
import urllib.request

APP=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(APP))
from config_store import apply_config_patch, read_config, config_lock
import ast

def load_functions(path,names,namespace):
    tree=ast.parse(path.read_text(encoding='utf-8'))
    selected=[n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name in names]
    assert len(selected)==len(names)
    exec(compile(ast.Module(body=selected,type_ignores=[]),str(path),'exec'),namespace)



class ConfigDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.path=self.root/'config.json'
        self.original={'floor_number':1,'horizontal_setback_meters':5,'distance_to_road_meters':5,
            'calibration_offset':105.74,'microphone_calibration':{'method':'phone'},
            'bluesky':{'enabled':True,'app_password':'test-secret'},
            'fleet_hub':{'enabled':True,'station_id':'noise-bot-03','station_name':'Parkallen','hub_url':'http://example.invalid'}}
        self.path.write_text(json.dumps(self.original))

    def test_setback_edit_preserves_calibration_and_credentials(self):
        result,changed=apply_config_patch(self.path,{'horizontal_setback_meters':35},'revision-a')
        self.assertTrue(changed)
        self.assertEqual(result['distance_to_road_meters'],35)
        for key in ('calibration_offset','microphone_calibration','bluesky','fleet_hub'):
            self.assertEqual(result[key],self.original[key])
        self.assertEqual(json.loads(self.path.read_text()),result)

    def test_nested_edits_merge_without_erasing_password(self):
        result,_=apply_config_patch(self.path,{'bluesky':{'enabled':False}})
        self.assertEqual(result['bluesky'],{'enabled':False,'app_password':'test-secret'})

    def test_bad_setback_leaves_file_unchanged(self):
        raw=self.path.read_bytes()
        for value in (-1,float('nan'),float('inf'),'bad'):
            with self.assertRaises((ValueError,TypeError)):
                apply_config_patch(self.path,{'horizontal_setback_meters':value})
            self.assertEqual(raw,self.path.read_bytes())

    def test_retry_does_not_trigger_another_reload(self):
        patch={'horizontal_setback_meters':35}
        apply_config_patch(self.path,patch,'a')
        before=self.path.stat().st_mtime_ns
        _,changed=apply_config_patch(self.path,patch,'a')
        self.assertFalse(changed)
        self.assertEqual(before,self.path.stat().st_mtime_ns)

    def test_docker_bind_mount_write_preserves_inode_and_calibration(self):
        inode=self.path.stat().st_ino
        with patch('config_store.os.replace',side_effect=OSError(errno.EBUSY,'mount point')):
            result,_=apply_config_patch(self.path,{'horizontal_setback_meters':35},'docker-a')
        self.assertEqual(self.path.stat().st_ino,inode)
        self.assertEqual(read_config(self.path),result)
        self.assertEqual(result['calibration_offset'],105.74)
        self.assertEqual(result['_hub_config_revision'],'docker-a')
        self.assertEqual(result['horizontal_setback_meters'],35)

    def test_other_write_errors_do_not_fall_back_or_acknowledge(self):
        original=self.path.read_bytes()
        with patch('config_store.os.replace',side_effect=PermissionError(errno.EACCES,'denied')):
            with self.assertRaises(PermissionError):
                apply_config_patch(self.path,{'horizontal_setback_meters':35},'unwritten')
        self.assertEqual(self.path.read_bytes(),original)

    def test_config_read_waits_for_writer(self):
        started=threading.Event();done=threading.Event();values=[]
        def reader():
            started.set();values.append(read_config(self.path));done.set()
        with config_lock(self.path):
            thread=threading.Thread(target=reader)
            thread.start()
            self.assertTrue(started.wait(2))
            self.assertFalse(done.wait(0.05))
        thread.join(timeout=2)
        self.assertTrue(done.is_set())
        self.assertEqual(values,[self.original])



    def test_actual_detector_heartbeat_applies_and_acknowledges(self):
        sent=[]
        class Response:
            status=200
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self):return json.dumps({'config_update':{'horizontal_setback_meters':35},'config_update_revision':'a'}).encode()
        def urlopen(req,timeout):
            sent.append(json.loads(req.data));return Response()
        ns=dict(os=os,json=json,time=time,CONFIG_FILE=str(self.path),BASE_DIR=str(self.root),
            LIVE_STATE_FILE=str(self.root/'absent'),OUTPUT_DIR=str(self.root/'recordings'),
            get_hardware_station_id=lambda cfg:'noise-bot-03',wifi_network_id=lambda _:None,
            apply_config_patch=apply_config_patch, read_config=read_config,
            urllib=SimpleNamespace(request=SimpleNamespace(Request=urllib.request.Request,urlopen=urlopen)))
        load_functions(APP/'noise_detector.py',{'send_fleet_heartbeat'},ns)
        ns['send_fleet_heartbeat']();ns['send_fleet_heartbeat']()
        self.assertEqual(sent[-1]['config_update_ack'],'a')
        self.assertEqual(sent[-1]['horizontal_setback_meters'],35)
        self.assertEqual(sent[-1]['config_state']['calibration_offset'],105.74)
        self.assertTrue((self.root/'.reload_trigger').exists())



if __name__=='__main__':unittest.main()
