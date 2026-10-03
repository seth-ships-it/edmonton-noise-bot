import importlib.util
from datetime import datetime
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.request
import wave

import numpy as np

APP=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(APP))
from event_metadata import atomic_json, sidecar_path


def detector():
    spec=importlib.util.spec_from_file_location('detector_under_test',APP/'noise_detector.py')
    module=importlib.util.module_from_spec(spec)
    previous=sys.modules.get('pyaudio')
    sys.modules['pyaudio']=SimpleNamespace(paInt16=8)
    try:spec.loader.exec_module(module)
    finally:
        if previous is None:sys.modules.pop('pyaudio',None)
        else:sys.modules['pyaudio']=previous
    return module


class IntegrationTests(unittest.TestCase):
    def test_shadow_comparison_cannot_change_fixed_recordings(self):
        results=[]
        for mode in ('off','shadow'):
            with tempfile.TemporaryDirectory() as directory:
                root=Path(directory);d=detector();index=[0]
                config={'adaptive_detection':{'mode':mode},'audio_device_name':'test mic','calibration_offset':105.98,
                        'threshold_dba':70,'record_event_seconds':3,'cooldown_period_minutes':.01,
                        'output_directory':str(root/'recordings'),'fleet_hub':{'enabled':False}}
                atomic_json(root/'config.json',config)
                class Stream:
                    def read(self,n,**kwargs):
                        t=index[0]*4096/48000
                        if t>=14:raise KeyboardInterrupt
                        amplitude=.12 if 2<t<3.5 or 7<t<8.5 else .001
                        x=np.sin((np.arange(n)+index[0]*4096)*2*np.pi*1000/48000)*amplitude
                        index[0]+=1
                        return (x*32768).astype('<i2').tobytes()
                    def stop_stream(self):pass
                    def close(self):pass
                device=SimpleNamespace(get_device_count=lambda:1,get_device_info_by_index=lambda _: {'name':'test mic','maxInputChannels':1},
                    open=lambda **_:Stream(),terminate=lambda:None,get_sample_size=lambda _:2)
                observer=Mock()
                # An aggressively different hypothetical trigger must still
                # produce byte-identical fixed clips with identical lengths.
                observer.snapshot.return_value={'mode':mode,'reference':{'trigger_dbfs_a':100,'revision':'never-capture'}}
                d.pyaudio=SimpleNamespace(paInt16=8,PyAudio=lambda:device)
                d.time=SimpleNamespace(time=lambda:1800000000+index[0]*4096/48000,monotonic=lambda:index[0]*4096/48000)
                d.datetime=SimpleNamespace(now=lambda tz:datetime.fromtimestamp(d.time.time(),tz))
                d.BASE_DIR=str(root);d.CONFIG_FILE=str(root/'config.json');d.LIVE_STATE_FILE=str(root/'live.json')
                with patch.object(d,'Observer',return_value=observer),patch.object(d,'Notifier'),patch.object(d,'start_fleet_sync_thread'),\
                     patch.object(d,'classify_audio',return_value='vehicle'),patch.object(d.threading,'Thread'),patch.object(sys,'argv',['detector']):
                    d.main()
                clips=[]
                for path in sorted((root/'recordings').glob('*.wav')):
                    with wave.open(str(path)) as wav:clips.append((wav.getparams(),wav.readframes(wav.getnframes())))
                self.assertEqual(len(clips),2)
                self.assertEqual(observer.annotate.call_count,2)
                results.append(clips)
        self.assertEqual(results[0],results[1])

    def test_first_upload_and_retry_reuse_original_sidecar(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);d=detector();d.CONFIG_FILE=str(root/'config.json')
            atomic_json(d.CONFIG_FILE,{'fleet_hub':{'hub_url':'https://example.invalid','station_id':'test'}})
            wav=root/'noise_event_20261003_010000_75dba_vehicle.wav';wav.write_bytes(b'fixture')
            meta={'schema':1,'event_id':'stable-event','mode':'shadow','start_utc':1790989192,'calibration_offset':105.98,
                  'reference':{'traffic_dbfs_a':-40,'revision':'frozen'}}
            atomic_json(sidecar_path(wav),meta);sent=[]
            class Response:
                status=201
                def __enter__(self):return self
                def __exit__(self,*_):pass
            def post(request,timeout):sent.append(json.loads(request.data));return Response()
            with patch.object(d.urllib.request,'urlopen',side_effect=post):
                for _ in range(2):self.assertTrue(d.upload_recording_to_hub(str(wav),{'dba':75}))
            self.assertEqual(sent[0],sent[1]);self.assertEqual(sent[0]['event_metadata'],meta)
            self.assertEqual(sent[0]['timestamp'],meta['start_utc'])

    def test_private_report_and_audio_require_exact_station_code(self):
        import dashboard
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);atomic_json(root/'config.json',{'admin_passcode':'fixture-only','adaptive_detection':{'mode':'off'}})
            clipdir=root/'adaptive-data'/'clips';clipdir.mkdir(parents=True);(clipdir/'sample.wav').write_bytes(b'RIFFfixture')
            with patch.object(dashboard,'BASE_DIR',str(root)),patch.object(dashboard,'CONFIG_FILE',str(root/'config.json')):
                handler=next(v for v in vars(dashboard).values() if isinstance(v,type) and v.__module__=='dashboard' and hasattr(v,'do_GET'))
                server=dashboard.ThreadingHTTPServer(('127.0.0.1',0),handler)
                thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
                try:
                    base='http://127.0.0.1:'+str(server.server_port)
                    for path in ('/api/adaptive','/api/adaptive/clip/sample.wav'):
                        with self.assertRaises(urllib.error.HTTPError) as caught:urllib.request.urlopen(base+path)
                        self.assertEqual(caught.exception.code,403)
                    request=urllib.request.Request(base+'/api/adaptive',headers={'X-Admin-Key':'fixture-only'})
                    with urllib.request.urlopen(request) as response:self.assertEqual(json.load(response)['mode'],'off')
                    request=urllib.request.Request(base+'/api/adaptive/clip/..%2Fconfig.json',headers={'X-Admin-Key':'fixture-only'})
                    with self.assertRaises(urllib.error.HTTPError) as caught:urllib.request.urlopen(request)
                    self.assertEqual(caught.exception.code,404)
                    request=urllib.request.Request(base+'/api/adaptive',data=b'{"mode":"adaptive"}',headers={'X-Admin-Key':'fixture-only','Content-Type':'application/json'})
                    with self.assertRaises(urllib.error.HTTPError) as caught:urllib.request.urlopen(request)
                    self.assertEqual(caught.exception.code,400)
                finally:server.shutdown();server.server_close();thread.join(2)


if __name__=='__main__':unittest.main()
