"""Replay two minutes of synthetic sound without touching the mic or config."""
import json
from pathlib import Path
import resource
import tempfile
import time
import numpy as np
from audio_classifier import classify_samples
from adaptive_runtime import ShadowRuntime
from adaptive_detector import period_at


def main():
    with tempfile.TemporaryDirectory(prefix='noise-shadow-benchmark-') as directory:
        observer = ShadowRuntime(directory, {'adaptive_detection': {'mode': 'shadow'}, 'calibration_offset': 105.98},
                                 {'station_id': 'synthetic-benchmark', 'alsa_card': None}, start=False)
        observer._open()
        utc = time.time()-120
        # A filled seven-day model exercises steady-state costs, not just an
        # empty bootstrap. These synthetic measurements never leave the temp dir.
        for name, state in observer.learner.periods.items():
            state['peaks'] = [(utc-i, -40) for i in range(5000)]
            state['observed'] = [(utc-i, -40) for i in range(5000)]
        observer.learner.background.extend((utc-i, -60) for i in range(900,0,-1))
        observer.learner.background_db = -60
        baseline = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        start = time.process_time()
        count = 48000*120
        last_flush = 0
        for sample in range(0, count, 4096):
            n = min(4096, count-sample)
            passage = 5 < (sample/48000)%20 < 10
            level = -40 if passage else -60
            block = (np.sin((np.arange(n)+sample)*2*np.pi*300/48000)*np.sqrt(2)*10**(level/20)).astype(np.float32)
            end = (sample+n)/48000
            observer._process((block, block, utc+end, end, False, False, False))
            if end-last_flush >= 10:
                observer._flush();last_flush = end
        observer._flush(force_state=True)
        result = {'replay_seconds':120, 'cpu_seconds':time.process_time()-start,
                  'incremental_peak_rss_mib':(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss-baseline)/1024,
                  'gaps':observer.gap_count, 'worker_errors':observer.worker_errors,
                  'stored_seconds':observer.db.execute('SELECT count(*) FROM seconds').fetchone()[0]}
        result['one_core_percent_for_realtime'] = result['cpu_seconds']/120*100
        observer.db.close()
        print(json.dumps(result))


if __name__ == '__main__':main()
