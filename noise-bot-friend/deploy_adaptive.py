"""Explicit, reversible native-systemd pilot deployment. Run from staging on Pi.

Dry run is default. This pilot intentionally supports Cloverdale's two native
services only; it does not guess how other stations or containers are managed.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

FILES = ('adaptive_detector.py','adaptive_runtime.py','adaptive_report.py','audio_classifier.py',
         'event_metadata.py','config_store.py','station_network.py','noise_detector.py','dashboard.py')
SERVICES = ('noise-detector.service','noise-dashboard.service')


def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def command(*args):
    return subprocess.run(args,check=True,capture_output=True,text=True,timeout=30).stdout.strip()


def health(root, since, mode=None):
    deadline=time.monotonic()+25
    while time.monotonic()<deadline:
        try:
            live=json.loads((root/'live_audio_state.json').read_text())
            if live.get('timestamp',0)>since and time.time()-live['timestamp']<5:
                if mode is None or live.get('adaptive_detection',{}).get('mode')==mode:
                    for unit in SERVICES:command('systemctl','is-active',unit)
                    return
        except (OSError,ValueError,subprocess.CalledProcessError):pass
        time.sleep(.5)
    raise RuntimeError('Fresh audio/service health check failed')


def restore(root, backup, verify=True):
    from event_metadata import atomic_json
    record=json.loads((backup/'deployment.json').read_text())
    if record['root']!=str(root) or set(record['files'])!=set(FILES):raise ValueError('Backup target mismatch')
    if verify:
        for name, item in record['files'].items():
            if digest(root/name)!=item['deployed']:raise ValueError('Live source changed since deployment: '+name)
    for unit in SERVICES:command('sudo','-n','systemctl','stop',unit)
    for name,item in record['files'].items():
        if item['previous'] is None:
            (root/name).unlink(missing_ok=True)
        else:
            if digest(backup/name)!=item['previous']:raise ValueError('Backup checksum mismatch')
            shutil.copy2(backup/name,root/name)
    current=json.loads((root/'config.json').read_text())
    if record['prior_adaptive'] is None:current.pop('adaptive_detection',None)
    else:current['adaptive_detection']=record['prior_adaptive']
    atomic_json(root/'config.json',current)
    since=time.time()
    for unit in SERVICES:command('sudo','-n','systemctl','start',unit)
    health(root,since)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',required=True)
    parser.add_argument('--station-id',required=True)
    group=parser.add_mutually_exclusive_group()
    group.add_argument('--apply',action='store_true')
    group.add_argument('--rollback',type=Path)
    args=parser.parse_args()
    root=Path(args.root).resolve();stage=Path(__file__).resolve().parent
    config=json.loads((root/'config.json').read_text())
    if args.station_id!='noise-bot-cloverdale' or config.get('fleet_hub',{}).get('station_id')!=args.station_id:
        raise ValueError('This deployment is restricted to the Cloverdale pilot')
    for unit in SERVICES:
        if command('systemctl','show',unit,'--property=WorkingDirectory','--value')!=str(root):
            raise ValueError('Unexpected systemd working directory')
    if args.rollback:
        restore(root,args.rollback.resolve())
        print(json.dumps({'rolled_back':True,'station_id':args.station_id}));return
    power = int(command('vcgencmd','get_throttled').split('=')[1], 16)
    if power & 0x5:
        raise ValueError('Current undervoltage or throttling: keep fixed recording and resolve power before the pilot')
    if root==stage:raise ValueError('Run from a separate staging directory')
    manifest=json.loads((stage/'adaptive-manifest.json').read_text())
    for name in FILES:
        if digest(stage/name)!=manifest['files'][name]:raise ValueError('Staging checksum mismatch: '+name)
        compile((stage/name).read_text(),name,'exec')
    if shutil.disk_usage(root).free<400*1024*1024:raise ValueError('Insufficient pilot disk reserve')
    for name in FILES:
        if name in manifest.get('expected_live',{}) and digest(root/name)!=manifest['expected_live'][name]:
            raise ValueError('Live file changed since inventory: '+name)
    print(json.dumps({'dry_run':not args.apply,'station_id':args.station_id,'file_count':len(FILES),
                      'mode':'shadow','calibration_offset':config.get('calibration_offset') }))
    if not args.apply:return
    from config_store import apply_config_patch
    from event_metadata import atomic_json
    backup=root.parent/('adaptive-backup-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    backup.mkdir(mode=0o700)
    record={'root':str(root),'prior_adaptive':config.get('adaptive_detection'),'files':{}}
    shutil.copy2(root/'config.json',backup/'config.private.json')
    for name in FILES:
        existed=(root/name).exists()
        record['files'][name]={'deployed':digest(stage/name),'previous':digest(root/name) if existed else None}
        if existed:shutil.copy2(root/name,backup/name)
    atomic_json(backup/'deployment.json',record)
    try:
        for unit in SERVICES:command('sudo','-n','systemctl','stop',unit)
        for name in FILES:shutil.copy2(stage/name,root/name)
        apply_config_patch(root/'config.json',{'adaptive_detection':{'mode':'shadow','timezone':'America/Edmonton'}})
        since=time.time()
        for unit in SERVICES:command('sudo','-n','systemctl','start',unit)
        health(root,since,'shadow')
        # Calibration, identity, and all unrelated current settings must survive.
        current=json.loads((root/'config.json').read_text())
        for key,value in config.items():
            if key!='adaptive_detection' and current.get(key)!=value:raise ValueError('Unexpected setting change: '+key)
    except Exception:
        restore(root,backup,verify=False)
        raise
    print(json.dumps({'deployed':True,'mode':'shadow','backup':str(backup)}))


if __name__=='__main__':main()
