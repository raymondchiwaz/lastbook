#!/usr/bin/env python3
"""Run real-library checks inside the actual GTK/WebKit desktop shell (no HTTP port).

Development-only harness; does not alter production navigation or network rules.
  .venv-low-vram/bin/python app/tests/linux_readiness.py --data /path/to/LastbookData --evidence /path/to/results
Checks real documents, app routes, search, PDF drawing, offline maps and local AI.
Leaves a report and an actual WebKit map screenshot, then closes its test window.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
ap = argparse.ArgumentParser(description=__doc__)
ap.add_argument('--data', required=True, type=Path)
ap.add_argument('--evidence', required=True, type=Path)
ap.add_argument('--no-chat', action='store_true')
args = ap.parse_args()
args.evidence.mkdir(parents=True, exist_ok=True)
os.environ.setdefault('LASTBOOK_AI_PROFILE', 'low-vram')
os.environ.setdefault('LASTBOOK_AI_MODEL', 'qwen3.5-2b-abliterated')
os.environ.setdefault('LASTBOOK_LOCAL_AI', '1')
os.environ.setdefault('WEBKIT_DISABLE_DMABUF_RENDERER', '1')
os.environ.setdefault('GGML_VK_VISIBLE_DEVICES', (ROOT/'derived/llm/vulkan-device').read_text().strip())
os.environ['GST_PLUGIN_PATH_1_0'] = str(ROOT/'derived/gst')
os.environ['WEBKIT_GST_ALLOWED_URI_PROTOCOLS'] = 'lastbook'
sys.path[:0] = [str(ROOT/'app'), str(ROOT)]

records = json.loads((args.data/'library-data/catalog.json').read_text())
if isinstance(records, dict):
    records = records['records']
missing = [r['path'] for r in records if not (args.data/'library-data'/r['path']).is_file()]
if missing:
    raise RuntimeError(f'{len(missing)} catalog files missing: {missing[:5]}')
pdf = next(r for r in records if r.get('kind') == 'pdf')
video = next((r for r in records if r.get('kind') in ('video', 'mp4', 'webm')), None)

import shell_linux
from gi.repository import GLib, WebKit2
import main

SCRIPT = r'''(() => {
window.__readiness = {state:'running', checks:[], errors:[]};
const result = window.__readiness;
const record = (name, ok, detail) => {result.checks.push({name,ok:!!ok,detail}); if(!ok) throw Error(name + ': ' + JSON.stringify(detail))};
window.addEventListener('error', e => result.errors.push(e.message));
window.addEventListener('unhandledrejection', e => result.errors.push(String(e.reason)));
const wait = (fn, ms=60000) => new Promise((resolve,reject) => {
  const until=performance.now()+ms; const step=()=>{let r;try{r=fn()}catch(e){} if(r)return resolve(r); if(performance.now()>until)return reject(Error('Timed out waiting for UI: '+fn));setTimeout(step,100)};step();
});
const go=async(hash, selector)=>{
  const old=document.querySelector('#main').firstElementChild;
  const changed=location.hash!==hash;
  location.hash=hash;
  await wait(()=>location.hash===hash&&document.querySelector(selector)&&(!changed||document.querySelector('#main').firstElementChild!==old));
  await new Promise(requestAnimationFrame);
  if(document.body.classList.contains('v-error'))throw Error(document.querySelector('#main').innerText);
};
const get=async(url)=>{const r=await fetch(url);if(!r.ok)throw Error(url+': '+r.status);return r.json()};
(async()=>{
  await wait(()=>document.querySelector('#main h1'));
  record('desktop_custom_scheme',location.protocol==='lastbook:',location.href);
  record('home_route',document.querySelector('#main').innerText.length>100,document.title);
  const lib=await get('/api/library');record('real_library',lib.resources>=300,lib);
  for(const [hash,sel] of [['#/library','#main h1'],['#/t/water','#main h1'],['#/search?q=water','#main h1'],['#/videos','#main h1'],['#/encyclopedias','#main h1']]){
    await go(hash,sel); record('route_'+hash,!document.querySelector('#main .notice.err')&&!document.body.classList.contains('v-error'),document.querySelector('#main').innerText.slice(0,250));
  }
  const search=await get('/api/find?q=water');record('offline_fulltext_search',(search.results||[]).length>0,{results:search.results?.length});
  await go('#/read/'+PDF_ID+'?p=1','img.pageimg');
  const image=await wait(()=>{const i=document.querySelector('img.pageimg');return i&&i.naturalWidth>0&&i});
  record('actual_pdf_render',image.naturalWidth>100,{width:image.naturalWidth,height:image.naturalHeight});
  if(VIDEO_ID){
    await go('#/watch/'+VIDEO_ID,'#vid');const v=document.querySelector('#vid');v.muted=true;
    await wait(()=>v.readyState>=1||v.error,60000);record('video_metadata',!v.error,{width:v.videoWidth,duration:v.duration,error:v.error?.code});
    await v.play();await wait(()=>v.currentTime>.5||v.error,30000);record('video_playback',!v.error&&v.currentTime>.5,{time:v.currentTime,width:v.videoWidth});v.pause();
  }
  await go('#/map?lat=-17.83&lon=31.05&z=14','#mapc');
  const info=await get('/api/map/info');record('zimbabwe_detail_installed',info.regional?.regions?.some(r=>r.title==='Zimbabwe'),info.regional?.regions?.map(r=>r.title));
  await wait(()=>document.querySelector('#near button'));
  record('nearest_services_visible',document.querySelectorAll('#near button').length>0,document.querySelector('#near').innerText.slice(0,500));
  const tile=await get('/api/map/tile/14/9605/9016');
  record('detailed_road_geometry',!!tile.layers.road?.length,{layers:Object.fromEntries(Object.entries(tile.layers).map(([k,v])=>[k,v.length]))});
  const hospitals=await get('/api/map/near?lat=-17.83&lon=31.05&cls=hospital&limit=3');
  record('hospital_search',hospitals.length>0,hospitals.map(p=>({name:p.name,km:p.km,source:p.source})));
  document.getElementById('layers-btn').click();await wait(()=>document.querySelector('[data-p="Survival"]'));
  document.querySelector('[data-p="Water"]').click();record('water_layers_toggle',document.querySelector('input[value="waterpoint"]')?.checked,true);
  document.querySelector('[data-p="Survival"]').click();document.querySelector('#layers .x').click();
  document.querySelector('#near button').click();await wait(()=>!document.getElementById('card').hidden);
  record('service_detail_card',document.getElementById('card').innerText.length>50,document.getElementById('card').innerText.slice(0,1000));
  document.querySelector('#card .x').click();
  const testPlace=await get('/api/places?q=Harare');record('offline_place_search',testPlace.some(p=>p.name==='Harare'),testPlace.slice(0,2));
  document.getElementById('map-africa').click();record('africa_control',!!document.getElementById('map-africa'),true);
  document.getElementById('map-asia').click();record('asia_control',!!document.getElementById('map-asia'),true);
  document.getElementById('map-zw').click();
  if(CHAT){
    await go('#/ask','#chatIn');
    let ai;
    const until=performance.now()+120000;
    do {ai=await get('/api/assistant/status');if(ai.online)break;await new Promise(resolve=>setTimeout(resolve,500))} while(performance.now()<until);
    record('local_ai_status',ai.online,ai);
    const r=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({messages:[{role:'user',content:'Find a saved reference about water treatment. Quote one short sentence from it and cite the source.'}]})});
    const s=await r.text();const events=s.split('\n').filter(x=>x.startsWith('data: ')).map(x=>JSON.parse(x.slice(6)));
    const done=events.find(e=>e.type==='done');record('local_ai_answer',!!done&&!!done.answer?.trim(),done||events);
    record('local_ai_citation',!!done.sources?.length&&!done.invalid?.length,{sources:done.sources,invalid:done.invalid,latency:done.latency});
  }
  await go('#/map?lat=-17.83&lon=31.05&z=14','#mapc');await wait(()=>document.querySelector('#near button'));
  record('javascript_errors',result.errors.length===0,result.errors);
  result.state='done';
})().catch(error=>{result.state='failed';result.error=String(error);result.stack=error.stack});
return true;
})()'''
SCRIPT = ('const PDF_ID='+json.dumps(pdf['id'])+', VIDEO_ID='+json.dumps(video and video['id'])+', CHAT='+json.dumps(not args.no_chat)+';\n'+SCRIPT)

original = shell_linux.Shell.__init__
started = False
finished = False
deadline = time.monotonic()+1200
report = None


def installed(self, *a, **kw):
    original(self, *a, **kw)

    def loaded(view, event):
        global started
        if event == WebKit2.LoadEvent.FINISHED and not started:
            started = True
            self.js(SCRIPT)
            GLib.timeout_add(500, check, self)
    self.view.connect('load-changed', loaded)


def check(shell):
    if finished:
        return False
    if time.monotonic()>deadline:
        end(shell, {'state':'failed','error':'Desktop readiness timeout'})
        return False

    def checked(view, task, _):
        try:
            value = view.evaluate_javascript_finish(task)
            result = json.loads(value.to_string())
            if result and result.get('state') in ('done','failed'):
                end(shell, result)
        except Exception as error:
            print('READINESS_CALLBACK',error,flush=True)
    shell.view.evaluate_javascript('JSON.stringify(window.__readiness || null)', -1, None, None, None, checked, None)
    return True


def end(shell, result):
    global finished, report
    if finished:
        return
    finished = True
    report = result
    (args.evidence/'desktop-readiness.json').write_text(json.dumps(result, indent=2)+'\n')
    print('DESKTOP_READINESS',json.dumps(result),flush=True)

    def saved(view, task, _):
        try:
            surface=view.get_snapshot_finish(task)
            surface.write_to_png(str(args.evidence/'desktop-map.png'))
        except Exception as error:
            print('SCREENSHOT_ERROR',error,flush=True)
        shell_linux.quit_app()
    shell.view.get_snapshot(WebKit2.SnapshotRegion.VISIBLE,WebKit2.SnapshotOptions.NONE,None,saved,None)


shell_linux.Shell.__init__ = installed
sys.argv = [str(ROOT/'app/main.py'), '--data', str(args.data)]
code = main.main()
sys.exit(code or (0 if report and report.get('state')=='done' else 1))
