import argparse, csv, datetime, json, os, socket, struct, threading, time, secrets, tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

p=argparse.ArgumentParser()
p.add_argument('--device',default='192.168.1.142');p.add_argument('--port',type=int,default=502)
p.add_argument('--unit',type=int,default=1);p.add_argument('--web-port',type=int,default=8088)
p.add_argument('--interval',type=int,default=15);p.add_argument('--output',default='hcv400_history.csv')
a=p.parse_args()
FIELDS={'supply_rpm':(100,'float','RPM'),'extract_rpm':(102,'float','RPM'),
'outdoor_temp':(132,'float','°C'),'supply_temp':(134,'float','°C'),
'extract_temp':(136,'float','°C'),'exhaust_temp':(138,'float','°C'),
'humidity':(196,'uint','% (verify)'),'bypass':(198,'uint','raw'),
'fan_level':(324,'uint','level'),'operation_mode':(472,'uint','raw'),
'filter_days':(554,'uint','days')}
state={'status':'Starting','device':a.device,'last_update':None,'values':{},'errors':{},'last_command':None}
lock=threading.RLock(); bus_lock=threading.Lock(); csrf=secrets.token_urlsafe(32)
CONFIG_PATH=os.path.join(os.path.dirname(os.path.abspath(a.output)),'hcv400_automation.json')
DEFAULT_CONFIG={'enabled':False,'schedule':[{'time':'07:00','level':2},{'time':'22:00','level':1}],
                'humidity_enabled':False,'humidity_below':42,'humidity_release':45,
                'minimum_command_seconds':120}
config=dict(DEFAULT_CONFIG); config['schedule']=[dict(x) for x in DEFAULT_CONFIG['schedule']]
auto={'humidity_active':False,'desired_level':None,'reason':'Disabled','last_action':None,'last_error':None,
      'last_attempt_monotonic':0,'last_applied_level':None}

def validate_config(d):
    if not isinstance(d,dict):raise ValueError('Settings must be an object')
    if set(d)!=set(DEFAULT_CONFIG):raise ValueError('Unexpected or missing settings fields')
    for k in ('enabled','humidity_enabled'):
        if type(d[k]) is not bool:raise ValueError(k+' must be true or false')
    if type(d['humidity_below']) not in (int,float) or not 20<=d['humidity_below']<=75:raise ValueError('Humidity threshold must be 20–75')
    if type(d['humidity_release']) not in (int,float) or not d['humidity_below']+2<=d['humidity_release']<=85:raise ValueError('Release must be at least 2% above threshold')
    if type(d['minimum_command_seconds']) is not int or not 60<=d['minimum_command_seconds']<=3600:raise ValueError('Minimum interval must be 60–3600 seconds')
    rows=d['schedule']
    if not isinstance(rows,list) or not 1<=len(rows)<=24:raise ValueError('Schedule requires 1–24 entries')
    seen=set()
    for row in rows:
        if not isinstance(row,dict) or set(row)!={'time','level'}:raise ValueError('Invalid schedule entry')
        t=row['time']
        if not isinstance(t,str) or len(t)!=5 or t[2]!=':' or not t[:2].isdigit() or not t[3:].isdigit() or int(t[:2])>23 or int(t[3:])>59:raise ValueError('Use 24-hour HH:MM time')
        if t in seen:raise ValueError('Duplicate schedule time')
        seen.add(t)
        if type(row['level']) is not int or row['level'] not in (1,2,3,4):raise ValueError('Only fan levels 1–4')
    return {**d,'schedule':sorted(rows,key=lambda x:x['time'])}

def save_config(d):
    folder=os.path.dirname(CONFIG_PATH)
    fd,path=tempfile.mkstemp(prefix='.hcv400-',suffix='.tmp',dir=folder)
    try:
        with os.fdopen(fd,'w',encoding='utf8') as f:
            json.dump(d,f,indent=2);f.flush();os.fsync(f.fileno())
        os.replace(path,CONFIG_PATH)
    finally:
        if os.path.exists(path):os.unlink(path)

if os.path.exists(CONFIG_PATH):
    try:
        with open(CONFIG_PATH,encoding='utf8') as f:config=validate_config(json.load(f))
    except Exception as e:
        print('Automation config invalid; automation disabled:',e)
        config=dict(DEFAULT_CONFIG);config['schedule']=[dict(x) for x in DEFAULT_CONFIG['schedule']]

def set_fan(level, source):
    # Serialize reads/writes as well as manual/automatic decisions.
    with command_lock:
        before=read32(324)
        exchange(16,324,[level&0xffff,(level>>16)&0xffff])
        time.sleep(.4)
        after=read32(324)
        success=after==level
        result={'message':('Fan level '+str(level)+' confirmed' if success else 'Write acknowledged but readback differs: '+str(after)),
                'before':before,'after':after,'requested':level,'verified':success,'source':source}
        with lock:state['last_command']={**result,'timestamp':datetime.datetime.now(datetime.timezone.utc).isoformat()}
        return result

command_lock=threading.Lock()

def schedule_level(rows, now):
    current=now.strftime('%H:%M')
    previous=[r for r in rows if r['time']<=current]
    return (previous[-1] if previous else rows[-1])['level']

def automation_step():
    with lock:
        cfg={**config,'schedule':[dict(x) for x in config['schedule']]}
        snapshot=dict(state['values']);stamp=state['last_update']
    if not cfg['enabled']:
        with lock:auto.update(reason='Disabled',desired_level=None,humidity_active=False)
        return
    # No stale readings, no guessed humidity units, and no automation on communication loss.
    try: age=(datetime.datetime.now(datetime.timezone.utc)-datetime.datetime.fromisoformat(stamp)).total_seconds()
    except (TypeError,ValueError):age=99999
    if age>max(60,3*a.interval) or 'fan_level' not in snapshot:
        with lock:auto.update(reason='Waiting for fresh fan readings',desired_level=None)
        return
    target=schedule_level(cfg['schedule'],datetime.datetime.now().astimezone())
    reason='Daily schedule'
    if cfg['humidity_enabled']:
        reading=snapshot.get('humidity',{}).get('value')
        if type(reading) not in (int,float) or not 0<=reading<=100:
            with lock:auto.update(reason='Humidity unavailable or invalid; writes paused',desired_level=None)
            return
        # Hold low speed until humidity reaches release threshold.
        with lock:active=auto['humidity_active']
        active=(reading<cfg['humidity_release']) if active else (reading<cfg['humidity_below'])
        with lock:auto['humidity_active']=active
        if active:target=1;reason='Low humidity protection'
    else:
        with lock:auto['humidity_active']=False
    with lock:auto.update(desired_level=target,reason=reason)
    current=snapshot['fan_level']['value']
    if current==target:return
    with lock:
        elapsed=time.monotonic()-auto['last_attempt_monotonic']
        if elapsed<cfg['minimum_command_seconds']:return
        auto['last_attempt_monotonic']=time.monotonic()
    try:
        result=set_fan(target,'automation: '+reason)
        with lock:
            auto['last_action']=state['last_command']
            auto['last_error']=None if result['verified'] else result['message']
            auto['last_applied_level']=target if result['verified'] else None
    except Exception as e:
        with lock:auto['last_error']=str(e)

def automation_loop():
    while True:
        try:automation_step()
        except Exception as e:
            with lock:auto['last_error']=str(e)
        time.sleep(10)


def exchange(fc, address, quantity_or_words):
    tx=secrets.randbelow(65536)
    if fc==3: pdu=struct.pack('>BHH',3,address,quantity_or_words)
    else: pdu=struct.pack('>BHHB',16,address,len(quantity_or_words),len(quantity_or_words)*2)+struct.pack('>'+('H'*len(quantity_or_words)),*quantity_or_words)
    packet=struct.pack('>HHHB',tx,0,len(pdu)+1,a.unit)+pdu
    with bus_lock:
        with socket.create_connection((a.device,a.port),timeout=4) as s:
            s.settimeout(4);s.sendall(packet)
            def recv(n):
                b=b''
                while len(b)<n:
                    c=s.recv(n-len(b))
                    if not c:raise ConnectionError('Device closed connection')
                    b+=c
                return b
            header=recv(7);tid,proto,length,uid=struct.unpack('>HHHB',header)
            if tid!=tx or proto or uid!=a.unit or length<2 or length>254:raise ValueError('Invalid Modbus response header')
            response=recv(length-1)
    if response[0]==fc|0x80:raise ValueError('Modbus exception '+str(response[1]))
    if response[0]!=fc:raise ValueError('Unexpected function code')
    if fc==3:
        if len(response)!=2+2*quantity_or_words or response[1]!=2*quantity_or_words:raise ValueError('Invalid register payload')
        return response[2:]
    if len(response)!=5 or struct.unpack('>HH',response[1:])!=(address,len(quantity_or_words)):raise ValueError('Write acknowledgement mismatch')

def read32(address,kind='uint'):
    b=exchange(3,address,2)
    return struct.unpack('>f' if kind=='float' else '>I',b[2:4]+b[0:2])[0]

def poll():
    while True:
        values={};errors={}
        for name,(addr,kind,unit) in FIELDS.items():
            try:
                v=read32(addr,kind)
                if kind=='float' and (not -100000<v<100000):raise ValueError('Implausible float')
                values[name]={'value':round(v,2) if kind=='float' else v,'unit':unit,'address':addr}
            except Exception as e:errors[name]=str(e)
        now=datetime.datetime.now(datetime.timezone.utc).isoformat()
        with lock:state.update(status='Connected' if values else 'No readings',last_update=now,values=values,errors=errors)
        if values:
            try:
                exists=os.path.exists(a.output) and os.path.getsize(a.output)>0
                with open(a.output,'a',newline='') as f:
                    w=csv.writer(f)
                    if not exists:w.writerow(['timestamp_utc','sensor','value','unit','register'])
                    for name,v in values.items():w.writerow([now,name,v['value'],v['unit'],v['address']])
            except OSError as e:
                with lock:state['errors']['csv']=str(e)
        time.sleep(max(10,a.interval))

HTML=r'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Dantherm HCV 400</title><style>body{font:16px system-ui;background:#0b1118;color:#edf4f7;max-width:1000px;margin:auto;padding:24px}h1{color:#8de4b4}.muted{color:#aab9c4}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px}.card{background:#19252f;border:1px solid #334650;border-radius:14px;padding:18px}.value{font-size:30px;font-weight:700;margin:10px 0}button{background:#2e6e58;color:white;border:1px solid #76b69b;border-radius:10px;padding:13px 20px;margin:5px;cursor:pointer;font-size:16px}button:disabled{opacity:.4;cursor:wait}button.selected{outline:3px solid #b0efc7}a{color:#8de4b4}small{color:#b5c2cc}</style></head><body><h1>Dantherm HCV 400</h1><p class="muted">Local LAN dashboard · Modbus TCP · community register mapping</p><div class="card" id="status">Connecting…</div><h2>Fan speed</h2><div class="card"><p>Current level: <strong id="level">—</strong> · Supply: <span id="supply">—</span> RPM · Extract: <span id="extract">—</span> RPM</p><div id="buttons"><button data-level="1">1 · Low</button><button data-level="2">2 · Normal</button><button data-level="3">3 · High</button><button data-level="4">4 · Boost</button></div><p><label><input type="checkbox" id="confirm"> Enable manual fan commands (levels 1–4 only)</label></p><p class="muted">Commands write register 324 and verify by reading it back. The device may override manual commands in automatic modes. Level 0 (off) is deliberately unavailable.</p><p id="result" role="status"></p></div><h2>Daily schedule & humidity automation</h2><div class="card"><p><label><input type="checkbox" id="auto_enabled"> Enable automatic control</label></p><p><label><input type="checkbox" id="hum_enabled"> Reduce to level 1 when humidity is low</label></p><p>Reduce below <input id="hum_below" type="number" min="20" max="75" step="1" value="42" style="width:65px"> % · Resume scheduled speed at <input id="hum_release" type="number" min="22" max="85" step="1" value="45" style="width:65px"> %</p><p class="muted">The humidity sensor must report percentage values. Verify against the official app before enabling. Hysteresis prevents repeated switching. Humidity control overrides the daily schedule.</p><p>Daily schedule (local PC time):</p><div id="schedule_rows"></div><button id="add_row" type="button">+ Add time</button><p><button id="save_auto" type="button">Save automation settings</button></p><p id="auto_status" role="status"></p><p class="muted">Settings are saved locally. This computer and app must stay running. Manual fan changes may be overridden by the next automation cycle. Automatic control is OFF by default.</p></div><h2>Live measurements</h2><div class="grid" id="cards"></div><p class="muted">Raw bypass and operation codes are not interpreted. Readings are logged to CSV on this PC.</p><a href="/api/readings">JSON readings API</a><script>
const labels={outdoor_temp:'Outdoor temperature',supply_temp:'Supply temperature',extract_temp:'Extract temperature',exhaust_temp:'Exhaust temperature',humidity:'Humidity',bypass:'Bypass (raw)',fan_level:'Fan level',operation_mode:'Operation mode (raw)',filter_days:'Filter remaining',supply_rpm:'Supply fan RPM',extract_rpm:'Extract fan RPM'};
let token='';let configLoaded=false;let lastAutoConfig=null;
function addRow(t='07:00',level=2){const d=document.createElement('div');d.className='schedule-row';d.innerHTML='<input type="time" value="'+t+'"> <select>'+[1,2,3,4].map(n=>'<option value="'+n+'" '+(n===level?'selected':'')+'>Fan '+n+'</option>').join('')+'</select> <button type="button" class="remove-row">Remove</button>';d.querySelector('button').onclick=()=>d.remove();document.getElementById('schedule_rows').appendChild(d)}
document.getElementById('add_row').onclick=()=>addRow();
async function loadConfig(){const r=await fetch('/api/automation',{cache:'no-store'});const j=await r.json();lastAutoConfig=j.config;if(!configLoaded){document.getElementById('auto_enabled').checked=j.config.enabled;document.getElementById('hum_enabled').checked=j.config.humidity_enabled;document.getElementById('hum_below').value=j.config.humidity_below;document.getElementById('hum_release').value=j.config.humidity_release;document.getElementById('schedule_rows').innerHTML='';j.config.schedule.forEach(x=>addRow(x.time,x.level));configLoaded=true}document.getElementById('auto_status').textContent='Automation: '+j.status.reason+' · Desired level: '+(j.status.desired_level??'—')+(j.status.last_error?' · Error: '+j.status.last_error:'')}
document.getElementById('save_auto').onclick=async()=>{try{const cfg={...lastAutoConfig,enabled:document.getElementById('auto_enabled').checked,humidity_enabled:document.getElementById('hum_enabled').checked,humidity_below:Number(document.getElementById('hum_below').value),humidity_release:Number(document.getElementById('hum_release').value),schedule:[...document.querySelectorAll('.schedule-row')].map(r=>({time:r.querySelector('input').value,level:Number(r.querySelector('select').value)}))};if(cfg.enabled&&!confirm('Enable automatic fan-speed changes? This will control your ventilation while the app runs.'))return;const r=await fetch('/api/automation',{method:'POST',headers:{'Content-Type':'application/json','X-Local-Token':token},body:JSON.stringify(cfg)});const j=await r.json();if(!r.ok)throw Error(j.error);lastAutoConfig=j.config;document.getElementById('auto_status').textContent='Saved successfully.'}catch(e){document.getElementById('auto_status').textContent='Save failed: '+e.message}};
async function update(){try{const s=await(await fetch('/api/readings',{cache:'no-store'})).json();token=s.token;document.getElementById('status').textContent=s.status+' · '+s.device+':502 · Last update: '+(s.last_update||'waiting')+' · Errors: '+Object.keys(s.errors).length;const v=s.values;await loadConfig();document.getElementById('level').textContent=v.fan_level?.value??'—';document.getElementById('supply').textContent=v.supply_rpm?.value??'—';document.getElementById('extract').textContent=v.extract_rpm?.value??'—';document.getElementById('cards').innerHTML=Object.entries(labels).map(([k,l])=>{let x=v[k];return '<div class="card"><small>'+l+'</small><div class="value">'+(x?x.value:'—')+'</div><small>'+(x?x.unit+' · register '+x.address:(s.errors[k]||'Waiting'))+'</small></div>'}).join('');document.querySelectorAll('button[data-level]').forEach(b=>b.classList.toggle('selected',Number(b.dataset.level)===v.fan_level?.value));}catch(e){document.getElementById('status').textContent=String(e)}}
document.querySelectorAll('button[data-level]').forEach(b=>b.addEventListener('click',async()=>{if(!document.getElementById('confirm').checked){document.getElementById('result').textContent='Tick the enable checkbox first.';return}const level=Number(b.dataset.level);if(!window.confirm('Set Dantherm fan speed to level '+level+'?'))return;document.querySelectorAll('button').forEach(x=>x.disabled=true);try{const r=await fetch('/api/fan',{method:'POST',headers:{'Content-Type':'application/json','X-Local-Token':token},body:JSON.stringify({level})});const j=await r.json();document.getElementById('result').textContent=j.message||j.error||'Unknown response';await update()}catch(e){document.getElementById('result').textContent=String(e)}finally{document.querySelectorAll('button').forEach(x=>x.disabled=false)}}));update();setInterval(update,5000);
</script></body></html>'''
class Handler(BaseHTTPRequestHandler):
    def send_json(self,code,obj):
        body=json.dumps(obj).encode();self.send_response(code);self.send_header('Content-Type','application/json');self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    def do_GET(self):
        if urlsplit(self.path).path=='/api/readings':
            with lock:payload=dict(state);payload['values']=dict(state['values']);payload['errors']=dict(state['errors']);payload['token']=csrf
            return self.send_json(200,payload)
        if urlsplit(self.path).path=='/api/automation':
            with lock:payload={'config':{**config,'schedule':[dict(x) for x in config['schedule']]},'status':dict(auto)}
            payload['status'].pop('last_attempt_monotonic',None)
            return self.send_json(200,payload)
        if self.path=='/':
            b=HTML.encode();self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b);return
        self.send_error(404)
    def do_POST(self):
        if self.path not in ('/api/fan','/api/automation'):return self.send_json(404,{'error':'Not found'})
        if self.headers.get('X-Local-Token')!=csrf:return self.send_json(403,{'error':'Invalid local token'})
        if self.headers.get('Content-Type','').split(';')[0]!='application/json':return self.send_json(415,{'error':'JSON required'})
        try:
            n=int(self.headers.get('Content-Length','0'))
            if n<1 or n>8192:raise ValueError('Invalid body length')
            data=json.loads(self.rfile.read(n))
            if self.path=='/api/automation':
                new=validate_config(data)
                with lock:
                    save_config(new)
                    config.clear();config.update(new)
                    auto['humidity_active']=False
                    auto['last_attempt_monotonic']=time.monotonic() if new['enabled'] else 0
                return self.send_json(200,{'message':'Automation saved','config':new})
            level=data.get('level')
            if type(level)!=int or level not in (1,2,3,4):raise ValueError('Only levels 1–4 allowed')
            result=set_fan(level,'manual')
            success=result['verified']
            return self.send_json(200 if success else 409,result)
        except (ValueError,KeyError,TypeError) as e:return self.send_json(400,{'error':str(e)})
        except Exception as e:return self.send_json(502,{'error':'Modbus command failed: '+str(e)})
    def log_message(self,*args):pass
if __name__=='__main__':
    threading.Thread(target=poll,daemon=True).start()
    threading.Thread(target=automation_loop,daemon=True).start()
    print('Dashboard: http://localhost:'+str(a.web_port))
    ThreadingHTTPServer(('127.0.0.1',a.web_port),Handler).serve_forever()
