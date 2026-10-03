"""Read-only bounded report; private audio is served only by the admin handler."""
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import time


def load_report(root, config):
    directory = Path(root)/'adaptive-data'
    report = {'mode': config.get('adaptive_detection', {}).get('mode', 'off'),
              'status': 'off', 'events': [], 'activation': 'manual_review_required'}
    try:
        report.update(json.loads((directory/'report.json').read_text()))
    except (OSError, ValueError):
        report.update(status='learning', fallback_reason='waiting_for_worker')
    mode = config.get('adaptive_detection', {}).get('mode', 'off')
    report['mode'] = mode
    if mode == 'off':
        report.update(status='off', fallback_reason=None)
        try:
            guard = json.loads((directory/'guard.json').read_text())
            if guard.get('state') == 'tripped':
                report.update(status='guard_stopped', fallback_reason=guard['trip']['reason'], guard=guard)
        except (OSError, ValueError, KeyError):
            pass
    elif time.time()-report.get('updated_utc', 0) > 30:
        report.update(status='degraded', fallback_reason='stale_worker_report')
    try:
        # Live status also exposes low-disk and stopped-worker failures which
        # cannot be written to report.json without breaking the reserve rule.
        live = json.loads((Path(root)/'live_audio_state.json').read_text())
        if time.time()-live.get('timestamp', 0) < 5 and mode == 'shadow':
            status = live.get('adaptive_detection', {})
            if status.get('guard'):
                report['guard'] = status['guard']
            if status.get('status') in ('degraded', 'guard_stopped'):
                report.update(status=status['status'], fallback_reason=status.get('fallback_reason'))
    except (OSError, ValueError):
        pass
    try:
        with closing(sqlite3.connect((directory/'shadow.sqlite').resolve().as_uri()+'?mode=ro', uri=True, timeout=.1)) as db:
            report['events'] = []
            for encoded, clip in db.execute('SELECT metadata,clip FROM events ORDER BY utc DESC LIMIT 100'):
                event = json.loads(encoded)
                event['clip'] = clip if clip and (directory/'clips'/clip).is_file() else None
                report['events'].append(event)
    except (sqlite3.Error, ValueError):
        report['events'] = []
    return report


HTML = r'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Adaptive detection pilot</title><style>
body{font:16px system-ui;background:#f4f6f8;color:#172536;max-width:1100px;margin:32px auto;padding:0 20px}h1{font-size:30px}button,input,select{font:inherit;padding:8px;border:1px solid #b4bdc7;border-radius:6px}button{cursor:pointer}section{background:white;border-radius:12px;padding:20px;margin:18px 0}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:9px;border-bottom:1px solid #eee}small{color:#526275}.scroll{overflow:auto}audio{max-width:220px}#message{min-height:24px}pre{white-space:pre-wrap}
</style><h1>Adaptive detection pilot</h1><p>The existing fixed detector controls public recordings. Shadow mode compares passages with their local background and typical traffic.</p>
<section><label>Station admin code <input id="key" type="password" autocomplete="current-password"></label> <button id="load">Load report</button><p id="message" role="status"></p>
<select id="mode"><option value="off">Off — fixed detection</option><option value="shadow">Shadow — learn and compare</option></select> <button id="save">Set mode</button> <button id="reset">Mic changed / station moved: relearn</button></section>
<section><h2 id="status">Waiting for access</h2><p id="reference"></p><p id="quality"></p><p id="guard"></p><details><summary>Automatic stop limits</summary><p>Free disk below 256 MiB; no new audio for 15 seconds; active low voltage or throttling continuously for 2 minutes; less than 90% technical coverage, more than 1% queue loss, or more than 60% of one CPU core over 5 minutes; process memory more than 64 MiB above startup for 2 minutes; any analysis worker error.</p><p>Checks run every 10 seconds. CPU and memory include the whole detector process. A stop disables shadow learning and saves its reason; the fixed recorder keeps running. Resolve the cause, then select Shadow to resume. Sparse traffic and weather screening do not themselves stop the trial.</p></details><div class="scroll"><table><thead><tr><th>Local period</th><th>Valid hours</th><th>Screened passages</th><th>Traffic reference (dBFS A)</th></tr></thead><tbody id="periods"></tbody></table></div><p><small>Each period needs 2 valid hours and 50 screened passages across 2 dates before comparison is ready. Readiness does not enable adaptive recording. Road distance and floor do not enter this comparison.</small></p></section>
<section><h2>Candidate comparisons</h2><p id="counts"></p><p><small>Up to 10,000 passages / 7 days, not unique vehicles. “Fixed” means the passage overlapped a fixed recording. Learning and uncertain sound labels are not accuracy measurements. Private clips are a capped review sample.</small></p><div class="scroll"><table><thead><tr><th>Local time</th><th>Comparison</th><th>Heuristic label</th><th>Above traffic</th><th>Private sample</th></tr></thead><tbody id="events"></tbody></table></div></section>
<script>
const el=id=>document.getElementById(id);let urls=[];
const text=(tag,value)=>{const node=document.createElement(tag);node.textContent=value;return node};
async function api(path,body){const r=await fetch(path,{method:body?'POST':'GET',headers:{'X-Admin-Key':el('key').value,...(body?{'Content-Type':'application/json'}:{})},body:body?JSON.stringify(body):undefined});if(!r.ok)throw Error(r.status===403?'Enter the station admin code.':'Request failed: '+r.status);return r.json()}
function showGuard(g){
 if(!g){el('guard').textContent='Guardian: waiting for first report.';return}
 if(g.trip){el('guard').textContent='Learning stopped automatically: '+g.trip.reason.replaceAll('_',' ')+' at '+new Date(g.trip.utc*1000).toLocaleString()+'. '+(g.trip.persisted_mode_off?'Off mode saved.':'Off mode could not be saved; see station logs.')+' Fixed recording continues. Resolve the cause before resuming.';return}
 const m=g.metrics||{};el('guard').textContent='Guardian armed - Free disk: '+(m.free_mib?.toFixed(0)??'unknown')+' MiB - Technical coverage: '+(m.technical_coverage==null?'measuring first 5 minutes':(100*m.technical_coverage).toFixed(1)+'%')+' - Process CPU: '+(m.process_cpu_fraction==null?'measuring':(100*m.process_cpu_fraction).toFixed(1)+'% of one core')+' - Current power flags: '+(m.power_flags==null?'unavailable':'0x'+m.power_flags.toString(16));
}
async function load(){try{const r=await api('/api/adaptive');el('message').textContent='Report loaded';el('mode').value=r.mode;el('status').textContent=r.mode+' · '+r.status;const ref=r.reference||{};el('reference').textContent='Period: '+(ref.period||'—')+' · Background: '+(ref.background_dbfs_a?.toFixed(1)??'learning')+' · Traffic: '+(ref.traffic_dbfs_a?.toFixed(1)??'learning')+' · Proposed trigger: '+(ref.trigger_dbfs_a?.toFixed(1)??'learning')+' dBFS A';el('quality').textContent='Reason: '+(r.fallback_reason||'none')+' · Missing analysis blocks: '+(r.dropped_blocks||0)+' · Audio gaps: '+(r.audio_gaps||0)+' · Gain observed: '+Boolean(r.gain_observed);showGuard(r.guard);el('periods').replaceChildren();for(const [p,s] of Object.entries(r.periods||{})){const row=text('tr','');[p,(s.valid_seconds/3600).toFixed(2),s.screened_candidates,s.accepted_traffic_dbfs_a?.toFixed(1)??'learning'].forEach(v=>row.append(text('td',v)));el('periods').append(row)}el('counts').textContent=Object.entries(r.candidate_comparisons||{}).map(([k,v])=>k+': '+v).join(' · ')||'No completed passages yet.';urls.forEach(URL.revokeObjectURL);urls=[];el('events').replaceChildren();for(const e of r.events||[]){const row=text('tr','');[new Date(e.start_utc*1000).toLocaleString('en-CA',{timeZone:'America/Edmonton'}),e.comparison,e.classification,e.traffic_excess_db==null?'learning':e.traffic_excess_db.toFixed(1)+' dB'].forEach(v=>row.append(text('td',v)));const cell=text('td','');if(e.clip){const b=text('button','Play sample');b.onclick=async()=>{try{const response=await fetch('/api/adaptive/clip/'+encodeURIComponent(e.clip),{headers:{'X-Admin-Key':el('key').value}});if(!response.ok)throw Error('Sample unavailable');const u=URL.createObjectURL(await response.blob());urls.push(u);const a=document.createElement('audio');a.controls=true;a.src=u;cell.replaceChildren(a);a.play().catch(()=>{})}catch(error){el('message').textContent=error.message}};cell.append(b)}else cell.textContent='Summary only';row.append(cell);el('events').append(row)}}catch(error){el('message').textContent=error.message}}
el('load').onclick=load;el('save').onclick=async()=>{try{await api('/api/adaptive',{mode:el('mode').value});el('message').textContent='Mode saved; allow a few seconds for the worker.'}catch(e){el('message').textContent=e.message}};el('reset').onclick=async()=>{if(!confirm('Start fresh learning for a moved station or changed microphone? Calibration and public clips stay as they are.'))return;try{await api('/api/adaptive',{reset:true});el('message').textContent='Relearning requested; allow up to a minute.'}catch(e){el('message').textContent=e.message}};
</script></html>'''
