import argparse, csv, datetime, json, os, socket, struct, threading, time, secrets, tempfile, logging, sys, traceback, ipaddress, concurrent.futures
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

p=argparse.ArgumentParser()
p.add_argument('--device',default='auto');p.add_argument('--port',type=int,default=502)
p.add_argument('--unit',type=int,default=1);p.add_argument('--web-port',type=int,default=8088)
p.add_argument('--interval',type=int,default=15);p.add_argument('--output',default='hcv400_history.csv')
a=p.parse_args()
# Log failures even when packaged as a windowless Windows executable.
DATA_DIR=os.path.join(os.environ.get('LOCALAPPDATA',os.path.dirname(os.path.abspath(a.output))), 'AirNest-HCV400')
os.makedirs(DATA_DIR,exist_ok=True)
LOG_PATH=os.path.join(DATA_DIR,'monitor.log')
logging.basicConfig(filename=LOG_PATH,level=logging.INFO,format='%(asctime)s %(levelname)s %(threadName)s %(message)s')
def thread_exception(args):
    logging.error('Background thread failed: %s',args.thread.name,exc_info=(args.exc_type,args.exc_value,args.exc_traceback))
threading.excepthook=thread_exception

FIELDS={'supply_rpm':(100,'float','RPM'),'extract_rpm':(102,'float','RPM'),
'outdoor_temp':(132,'float','°C'),'supply_temp':(134,'float','°C'),
'extract_temp':(136,'float','°C'),'exhaust_temp':(138,'float','°C'),
'humidity':(196,'uint','% (verify)'),'bypass':(198,'uint','raw'),
'fan_level':(324,'uint','level'),'operation_mode':(472,'uint','raw'),
'filter_days':(554,'uint','days'),'filter_period':(556,'uint','days'),'alarm_status':(516,'uint','raw'),'runtime_hours':(624,'uint','hours'),'preheater_duty':(160,'uint','raw'),'air_quality':(430,'uint','raw'),'co2':(574,'uint','ppm')}
state={'status':'Starting','device':a.device,'last_update':None,'values':{},'errors':{},'last_command':None}
lock=threading.RLock(); bus_lock=threading.Lock(); csrf=secrets.token_urlsafe(32)
DEVICE_PATH=os.path.join(DATA_DIR,'device.json')
discovery={'running':False,'candidates':[],'last_scan':None,'error':None}
device_confirmed=False
try:
    with open(DEVICE_PATH,encoding='utf-8') as f:
        saved_device=json.load(f).get('ip')
    if saved_device and ipaddress.ip_address(saved_device).is_private and a.device=='auto':a.device=saved_device
except (OSError,ValueError,TypeError,AttributeError):pass
state['device']=a.device
# A register signature is a compatibility check, NOT a cryptographic device identity.
def probe_host(ip,timeout=.65):
    try:
        with socket.create_connection((ip,a.port),timeout=timeout) as sock:
            sock.settimeout(timeout)
            tx=42;packet=struct.pack('>HHHB',tx,0,6,a.unit)+struct.pack('>BHH',3,132,2)
            sock.sendall(packet)
            def read_exact(n):
                buf=b''
                while len(buf)<n:
                    part=sock.recv(n-len(buf))
                    if not part:raise ConnectionError('Closed')
                    buf+=part
                return buf
            header=read_exact(7);tid,proto,length,uid=struct.unpack('>HHHB',header)
            if (tid,proto,uid)!=(tx,0,a.unit) or length!=7:return False
            payload=read_exact(length-1)
            if payload[:2]!=b'\x03\x04':return False
            outdoor=struct.unpack('>f',payload[4:6]+payload[2:4])[0]
            return -45<=outdoor<=60
    except (OSError,ValueError,struct.error):return False

def local_subnet():
    # Use the active IPv4 route. Avoid scanning arbitrary networks or VPN ranges.
    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sock:
        sock.connect(('1.1.1.1',80))
        ip=ipaddress.ip_address(sock.getsockname()[0])
    if not ip.is_private or ip.is_loopback:return None
    return ipaddress.ip_network(str(ip)+'/24',strict=False)

def scan_worker():
    with lock:discovery.update(running=True,error=None,candidates=[])
    try:
        net=local_subnet()
        if net is None:raise ValueError('No private IPv4 LAN detected')
        with concurrent.futures.ThreadPoolExecutor(max_workers=24) as pool:
            results=list(pool.map(lambda ip:(str(ip),probe_host(str(ip))),net.hosts()))
        matches=[{'ip':ip,'description':'Compatible Modbus register response (identity unverified)'} for ip,ok in results if ok]
        with lock:discovery.update(candidates=matches,last_scan=datetime.datetime.now(datetime.timezone.utc).isoformat())
    except Exception as e:
        logging.exception('Discovery failed')
        with lock:discovery['error']=str(e)
    finally:
        with lock:discovery['running']=False

def select_device(ip):
    global device_confirmed
    with lock:
        if ip not in [item['ip'] for item in discovery['candidates']]:raise ValueError('Scan and select a discovered device')
        # Pause automation after changing device, until operator enables it again.
        config['enabled']=False;config['night_enabled']=False;config['vacation_enabled']=False;config['sleep_until']=None
        save_config(config)
        a.device=ip;state['device']=ip;state['status']='Reconnecting';state['values']={};state['last_update']=None
        device_confirmed=True
    fd,tmp=tempfile.mkstemp(dir=DATA_DIR,prefix='device-',suffix='.tmp')
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:json.dump({'ip':ip},f)
        os.replace(tmp,DEVICE_PATH)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)
    logging.info('User selected LAN device %s',ip)

CONFIG_PATH=os.path.join(DATA_DIR,'hcv400_automation.json')
DEFAULT_CONFIG={'enabled':False,'schedule':[{'time':'07:00','level':2},{'time':'22:00','level':1}],
                'humidity_enabled':False,'humidity_below':42,'humidity_release':45,
                'minimum_command_seconds':120,'night_enabled':False,'night_start':'23:00','night_end':'06:30','vacation_enabled':False,'sleep_until':None}
config=dict(DEFAULT_CONFIG); config['schedule']=[dict(x) for x in DEFAULT_CONFIG['schedule']]
auto={'humidity_active':False,'desired_level':None,'reason':'Disabled','last_action':None,'last_error':None,
      'last_attempt_monotonic':0,'last_applied_level':None}

def validate_config(d):
    if not isinstance(d,dict):raise ValueError('Settings must be an object')
    if set(d)!=set(DEFAULT_CONFIG):raise ValueError('Unexpected or missing settings fields')
    for k in ('enabled','humidity_enabled','night_enabled','vacation_enabled'):
        if type(d[k]) is not bool:raise ValueError(k+' must be true or false')
    if type(d['humidity_below']) not in (int,float) or not 20<=d['humidity_below']<=75:raise ValueError('Humidity threshold must be 20–75')
    if type(d['humidity_release']) not in (int,float) or not d['humidity_below']+2<=d['humidity_release']<=85:raise ValueError('Release must be at least 2% above threshold')
    if type(d['minimum_command_seconds']) is not int or not 60<=d['minimum_command_seconds']<=3600:raise ValueError('Minimum interval must be 60–3600 seconds')
    for k in ('night_start','night_end'):
        t=d[k]
        if not isinstance(t,str) or len(t)!=5 or t[2]!=':' or not t[:2].isdigit() or not t[3:].isdigit() or int(t[:2])>23 or int(t[3:])>59:raise ValueError('Invalid night time')
    until=d['sleep_until']
    if until is not None:
        if not isinstance(until,str):raise ValueError('Invalid sleep timer')
        try:dt=datetime.datetime.fromisoformat(until)
        except ValueError:raise ValueError('Invalid sleep timer')
        if dt.tzinfo is None:raise ValueError('Sleep timer needs timezone')
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
    if not device_confirmed:raise ValueError('Confirm the device in Network Settings before sending commands')
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
    active_override=cfg['vacation_enabled'] or cfg['night_enabled'] or cfg['sleep_until'] is not None
    if not device_confirmed:
        with lock:auto.update(reason='Device not confirmed; automatic writes disabled',desired_level=None)
        return
    if not cfg['enabled'] and not active_override:
        with lock:auto.update(reason='Disabled',desired_level=None,humidity_active=False)
        return
    # No stale readings, no guessed humidity units, and no automation on communication loss.
    try: age=(datetime.datetime.now(datetime.timezone.utc)-datetime.datetime.fromisoformat(stamp)).total_seconds()
    except (TypeError,ValueError):age=99999
    if age>max(60,3*a.interval) or 'fan_level' not in snapshot:
        with lock:auto.update(reason='Waiting for fresh fan readings',desired_level=None)
        return
    target=schedule_level(cfg['schedule'],datetime.datetime.now().astimezone()) if cfg['enabled'] else snapshot['fan_level']['value']
    reason='Daily schedule'
    if cfg['enabled'] and cfg['humidity_enabled']:
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
    # Local convenience modes, not the Dantherm native operating-mode bits.
    now=datetime.datetime.now().astimezone()
    hm=now.strftime('%H:%M')
    start,end=cfg['night_start'],cfg['night_end']
    night=(start<=hm<end if start<end else hm>=start or hm<end) if start!=end else False
    if cfg['night_enabled'] and night:target=1;reason='Night interval'
    if cfg['vacation_enabled']:target=1;reason='Vacation (local override)'
    if cfg['sleep_until']:
        try:
            until=datetime.datetime.fromisoformat(cfg['sleep_until'])
            if now<until:target=1;reason='Sleep timer'
            else:
                with lock:
                    config['sleep_until']=None
                    save_config(config)
        except Exception as e:
            with lock:auto['last_error']='Sleep timer: '+str(e)
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
            logging.exception('Automation step failed')
            with lock:auto['last_error']=str(e)
        time.sleep(10)


def exchange(fc, address, quantity_or_words):
    tx=secrets.randbelow(65536)
    if fc==3: pdu=struct.pack('>BHH',3,address,quantity_or_words)
    else: pdu=struct.pack('>BHHB',16,address,len(quantity_or_words),len(quantity_or_words)*2)+struct.pack('>'+('H'*len(quantity_or_words)),*quantity_or_words)
    packet=struct.pack('>HHHB',tx,0,len(pdu)+1,a.unit)+pdu
    with bus_lock:
        if a.device=='auto':raise ConnectionError('No device selected; use Scan Network')
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
                logging.warning('CSV write failed: %s',e)
        time.sleep(max(10,a.interval))

HTML=r'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>AirNest · Your Home · HCV 400</title><style>
:root{color-scheme:dark;--bg:#08111d;--card:#111f30;--line:#273a50;--text:#f1f7fb;--muted:#a4b5c8;--mint:#65e3bd;--blue:#66baff;--orange:#ffb36c}*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at 70% 0%,#1b3048 0%,var(--bg) 55%);color:var(--text);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}.wrap{max-width:1160px;margin:auto;padding:26px 20px 70px}header{display:flex;align-items:center;justify-content:space-between;gap:14px;flex-wrap:wrap;margin-bottom:22px}.brand{display:flex;align-items:center;gap:13px}.logo{width:53px;height:53px;border-radius:16px;background:linear-gradient(145deg,#183d52,#174d4b);display:grid;place-items:center;border:1px solid #4d8e9d}.logo svg{width:34px}h1{font-size:25px;margin:0;letter-spacing:-.6px}h2{font-size:18px;margin:0 0 13px}h3{font-size:15px;margin:0 0 8px}.muted,small{color:var(--muted)}.pill{padding:7px 12px;border-radius:40px;border:1px solid #3d6171;color:#b8e8e1;background:#142d36;font-size:13px}.hero{display:grid;grid-template-columns:1.65fr 1fr;gap:17px}.card{background:linear-gradient(155deg,#15263a,#101b2a);border:1px solid var(--line);border-radius:19px;padding:21px;box-shadow:0 12px 35px #0002}.visual{position:relative;overflow:hidden;padding:0}.visual img{width:100%;display:block;aspect-ratio:1.56;object-fit:cover}.visual .overlay{padding:13px 20px;background:#101c2be8;display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap}.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#66db9c;margin-right:7px}.statgrid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.stat{background:#0b1929;border:1px solid #294057;border-radius:13px;padding:13px}.stat strong{font-size:25px;display:block;margin-top:3px;font-variant-numeric:tabular-nums}.section{margin-top:20px}.twocol{display:grid;grid-template-columns:1fr 1fr;gap:17px}.row{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.space{justify-content:space-between}button,input,select{font:inherit}button{border:1px solid #41647a;background:#21374b;color:#f2f7fc;padding:10px 14px;border-radius:11px;cursor:pointer}button:hover{border-color:var(--mint)}button:disabled{opacity:.55;cursor:not-allowed}.primary{background:#176951;border-color:#2a9774}.active{background:#176951;border-color:#7bf4cd}.danger{background:#57303a;border-color:#995b69}input[type=time],input[type=number],select{background:#0c1a2b;border:1px solid #40566b;color:white;padding:9px;border-radius:9px;max-width:160px}input[type=checkbox]{accent-color:#5bdfb6;width:17px;height:17px}.field{display:flex;align-items:center;gap:9px;flex-wrap:wrap;margin:12px 0}.field label{color:#c8d5e0}.speed{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin:15px 0}.speed button{padding:15px 5px}.speed b{font-size:22px;display:block}.modegrid{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}.modegrid button{text-align:left;min-height:78px}.modegrid b{display:block}.modegrid small{display:block}.schedule-row{display:flex;align-items:center;gap:9px;margin:8px 0}.schedule-row button{padding:8px}.divider{height:1px;background:var(--line);margin:20px 0}.notice{border-left:3px solid var(--orange);padding:8px 12px;background:#4033214a;color:#f7d7a9;font-size:13px;border-radius:4px}#message,#auto_status,#filter_status{font-size:13px;min-height:22px;color:#9fe9d2}.measurements{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}.measurements .stat strong{font-size:21px}.tag{color:var(--mint);font-size:12px}.legend{display:flex;gap:12px;flex-wrap:wrap;font-size:12px}.legend span:before{content:'●';margin-right:5px}.blue:before{color:#66baff}.teal:before{color:#4de3c9}.orange:before{color:#ffb36c}.gray:before{color:#b1c4d4}@media(max-width:800px){.hero,.twocol{grid-template-columns:1fr}.measurements{grid-template-columns:repeat(2,1fr)}}@media(max-width:450px){.speed{grid-template-columns:repeat(2,1fr)}.modegrid{grid-template-columns:1fr}.wrap{padding:14px}.card{padding:15px}}

/* AirNest v6 — house-specific design */
:root{--bg:#061321;--card:#0b2233;--line:#20516b;--mint:#34e5b2;--blue:#3ab9ff;--orange:#ffae51}
.wrap{max-width:1440px;padding:22px 22px 70px}
header{background:linear-gradient(120deg,#071a2a,#09283a);padding:17px 22px;border:1px solid #1c435a;border-radius:20px}
.logo{background:#0b3035;border-color:#247e75}
.card{background:linear-gradient(150deg,#0d263a,#091b2b);border-color:#1d4c64;box-shadow:0 16px 40px #0004}
.hero{grid-template-columns:minmax(0,2.25fr) minmax(280px,1fr)}
.visual img{aspect-ratio:1.48;object-fit:contain;background:#061522}
.visual .overlay{background:#071d2e;align-items:center}
.stat{background:#092033;border-color:#20536c}
button{transition:background .15s,border-color .15s,transform .15s}
button:hover:not(:disabled){transform:translateY(-1px)}
@media(max-width:800px){.hero{grid-template-columns:1fr}.visual img{aspect-ratio:auto}.wrap{padding:12px 12px 40px}header{padding:12px}}
</style></head><body><div class="wrap"><header><div class="brand"><div class="logo"><svg viewBox="0 0 64 64" fill="none"><path d="M8 30 32 10 56 30v23H8V30Z" stroke="#83f0cf" stroke-width="4" stroke-linejoin="round"/><path d="M14 37h36M32 23v25" stroke="#83f0cf" stroke-width="3"/><path d="m20 39 8-5-8-5M44 35l-8 5 8 5" stroke="#78caff" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/></svg></div><div><h1>AirNest <span class="muted">/ HCV 400</span></h1><small>Your home · local ventilation monitoring & control</small></div></div><div class="pill" id="connection"><span class="dot"></span>Connecting to 192.168.1.142</div></header>
<section class="card section"><h2>Network & device discovery</h2><p>Find your HCV 400 after changing Wi-Fi, router or IP address. Discovery scans your current local /24 IPv4 network, and never changes device settings.</p><div class="row"><button id="scan_network">Scan Network</button><select id="device_list"><option value="">No devices scanned</option></select><button id="select_device">Use selected device</button></div><small id="scan_status">A newly discovered device must be confirmed before fan or automation commands are enabled.</small></section><div class="hero"><section class="card visual"><img src="/house_airflow.png" alt="Airflow visualization inspired by your house: outdoor intake blue, indoor extract orange, supply teal, exhaust grey"><div class="overlay"><b>Your home · Airflow overview</b><div class="legend"><span class="blue">Blue · intake</span><span class="teal">Teal · bedroom/living supply</span><span class="orange">Orange · stairs/kitchen extract</span><span class="gray">Grey · exhaust outdoors</span></div></div></section><section class="card"><h2>Home climate</h2><div class="statgrid"><div class="stat"><small>Outdoor</small><strong id="outdoor">—</strong></div><div class="stat"><small>Supply</small><strong id="supplytemp">—</strong></div><div class="stat"><small>Indoor extract</small><strong id="extracttemp">—</strong></div><div class="stat"><small>Humidity</small><strong id="humidity">—</strong></div></div><div class="divider"></div><div class="row space"><span>Current fan</span><b id="currentfan">—</b></div><div class="row space"><span>Supply / extract RPM</span><b id="rpms">—</b></div><div class="row space"><span>Filter remaining</span><b id="filterdays">—</b></div><small id="lastupdate">Waiting for device…</small></section></div>
<div class="twocol section"><section class="card"><h2>Fan speed</h2><div class="speed" id="speeds"><button data-level="1"><b>1</b>Quiet</button><button data-level="2"><b>2</b>Normal</button><button data-level="3"><b>3</b>High</button><button data-level="4"><b>4</b>Boost</button></div><div class="field"><input type="checkbox" id="manual_confirm"><label for="manual_confirm">Allow manual fan changes</label></div><div id="message" role="status"></div><small>Fan writes use community register 324. Verify changes with the official Dantherm controller. Manual commands can be overridden by active automations.</small></section><section class="card"><h2>Quick modes</h2><div class="modegrid"><button id="sleep"><b>☾ Sleep</b><small id="sleep_label">Fan 1 for 8 hours</small></button><button id="vacation"><b>✈ Vacation</b><small>Keep fan at 1 until disabled</small></button></div><div class="field"><label>Sleep duration</label><select id="sleep_hours"><option value="2">2 hours</option><option value="4">4 hours</option><option value="8" selected>8 hours</option><option value="12">12 hours</option></select></div><div class="notice">These are app-managed fan overrides, not the unit's built-in Sleep/Away modes. The PC must stay on.</div></section></div>
<div class="twocol section"><section class="card"><h2>Schedule & night mode</h2><div class="field"><input type="checkbox" id="auto_enabled"><label>Enable daily schedule</label></div><div id="schedule_rows"></div><button id="add_row">+ Add time</button><div class="divider"></div><div class="field"><input type="checkbox" id="night_enabled"><label>Night mode · force fan level 1</label></div><div class="field"><label>From</label><input type="time" id="night_start"><label>until</label><input type="time" id="night_end"></div><small>Night interval works across midnight and takes priority over the daily schedule.</small></section><section class="card"><h2>Humidity automation</h2><div class="field"><input type="checkbox" id="hum_enabled"><label>Reduce to fan 1 when air is dry</label></div><div class="field"><label>Below</label><input type="number" id="hum_below" min="20" max="75"><span>%</span><label>Resume at</label><input type="number" id="hum_release" min="22" max="85"><span>%</span></div><small>Humidity override requires the daily schedule to be enabled. Night, vacation and sleep overrides have higher priority.</small><div class="divider"></div><button class="primary" id="save_auto">Save settings</button><p id="auto_status" role="status"></p><div class="notice">Automation starts disabled by default. These local controls cannot replace minimum ventilation requirements or the unit's safety protections.</div></section></div>
<section class="card section"><h2>Filter maintenance</h2><div class="row space"><span>Filter time remaining</span><b id="filter_remaining">—</b></div><div class="row space"><span>Current filter period</span><b id="filter_period">—</b></div><div class="field"><label for="period_days">Change filter reminder period</label><select id="period_days"><option value="90">90 days</option><option value="180" selected>180 days</option><option value="270">270 days</option><option value="365">365 days</option></select><button id="set_period">Apply to unit</button></div><div class="field"><button id="filter_reset" disabled>Reset filter timer · not yet enabled</button></div><div id="filter_status" role="status"></div><small>Filter-period writes use community register 556 with readback verification. The filter reset register 558 is documented but the reset command value is not verified, so remote reset is disabled. After inspecting/replacing filters, use the official Dantherm control panel to reset the timer.</small></section>
<section class="card section"><h2>More device readings</h2><div class="measurements" id="measurements"></div><p><a style="color:var(--mint)" href="/api/readings">Open JSON readings API</a></p><small>Values may depend on installed sensors. This dashboard does not alter factory safety settings.</small></section></div>
<script>
let token='',cfg=null,loaded=false,busy=false;
const $=id=>document.getElementById(id);const show=(id,text)=>$(id).textContent=text;
async function api(url,options={}){const controller=new AbortController();const timeout=setTimeout(()=>controller.abort(),12000);let r;try{r=await fetch(url,{cache:'no-store',signal:controller.signal,...options})}catch(e){throw Error(e.name==='AbortError'?'Request timed out — check whether the local server is responding':'Cannot reach local server. Restart AirNest and check /api/health')}finally{clearTimeout(timeout)}const raw=await r.text();let j;try{j=JSON.parse(raw)}catch{throw Error('Unexpected server response ('+r.status+')')}if(!r.ok)throw Error(j.error||j.message||'HTTP '+r.status);return j}
function row(t='07:00',level=2){const d=document.createElement('div');d.className='schedule-row';const input=document.createElement('input');input.type='time';input.value=t;const sel=document.createElement('select');for(let n=1;n<=4;n++){const opt=document.createElement('option');opt.value=n;opt.textContent='Fan '+n;sel.appendChild(opt)}sel.value=level;const rm=document.createElement('button');rm.textContent='Remove';rm.onclick=()=>d.remove();d.append(input,sel,rm);$('schedule_rows').appendChild(d)}
function renderConfig(c){cfg=c;if(loaded)return;loaded=true;for(const key of ['auto_enabled','hum_enabled','night_enabled'])$(key).checked=c[{auto_enabled:'enabled',hum_enabled:'humidity_enabled',night_enabled:'night_enabled'}[key]];for(const key of ['hum_below','hum_release','night_start','night_end'])$(key).value=c[{hum_below:'humidity_below',hum_release:'humidity_release',night_start:'night_start',night_end:'night_end'}[key]];$('schedule_rows').innerHTML='';c.schedule.forEach(x=>row(x.time,x.level))}
function collect(){return {...cfg,enabled:$('auto_enabled').checked,humidity_enabled:$('hum_enabled').checked,humidity_below:Number($('hum_below').value),humidity_release:Number($('hum_release').value),night_enabled:$('night_enabled').checked,night_start:$('night_start').value,night_end:$('night_end').value,schedule:[...document.querySelectorAll('.schedule-row')].map(r=>({time:r.querySelector('input').value,level:Number(r.querySelector('select').value)}))}}
async function saveConfig(c){const j=await api('/api/automation',{method:'POST',headers:{'Content-Type':'application/json','X-Local-Token':token},body:JSON.stringify(c)});cfg=j.config;return j}
$('add_row').onclick=()=>row();$('save_auto').onclick=async()=>{try{if(!cfg)throw Error('Waiting for settings');const c=collect();if((c.enabled||c.night_enabled||c.vacation_enabled||c.sleep_until)&&!confirm('Enable automatic fan changes? The monitor must stay running.'))return;await saveConfig(c);show('auto_status','Settings saved.')}catch(e){show('auto_status','Save failed: '+e.message)}};
async function quick(kind){if(!cfg)return;try{let c=collect();if(kind==='vacation'){c.vacation_enabled=!cfg.vacation_enabled;if(c.vacation_enabled&&!confirm('Vacation mode will hold fan level 1 until you turn it off. Continue?'))return}if(kind==='sleep'){const active=cfg.sleep_until&&Date.parse(cfg.sleep_until)>Date.now();c.sleep_until=active?null:new Date(Date.now()+Number($('sleep_hours').value)*3600000).toISOString();if(!active&&!confirm('Start sleep mode at fan level 1?'))return}await saveConfig(c);show('auto_status','Mode saved.');await refresh()}catch(e){show('auto_status',e.message)}}$('sleep').onclick=()=>quick('sleep');$('vacation').onclick=()=>quick('vacation');
for(const b of document.querySelectorAll('[data-level]'))b.onclick=async()=>{if(busy)return;if(!$('manual_confirm').checked){show('message','First check “Allow manual fan changes”.');return}if(!confirm('Set fan to level '+b.dataset.level+'?'))return;busy=true;document.querySelectorAll('[data-level]').forEach(x=>x.disabled=true);try{const j=await api('/api/fan',{method:'POST',headers:{'Content-Type':'application/json','X-Local-Token':token},body:JSON.stringify({level:Number(b.dataset.level)})});show('message',j.message||'Command sent');await refresh()}catch(e){show('message','Fan command error: '+e.message+' — check if the server is still running.')}finally{busy=false;document.querySelectorAll('[data-level]').forEach(x=>x.disabled=false)}};
$('set_period').onclick=async()=>{const days=Number($('period_days').value);if(!confirm('Change the device filter reminder interval to '+days+' days? This writes register 556.'))return;try{const j=await api('/api/filter-period',{method:'POST',headers:{'Content-Type':'application/json','X-Local-Token':token},body:JSON.stringify({days,confirm:'CHANGE FILTER PERIOD'})});show('filter_status',j.message);await refresh()}catch(e){show('filter_status','Period change failed: '+e.message)}};
const labels={exhaust_temp:'Exhaust temperature',bypass:'Bypass status (raw)',operation_mode:'Operation mode (raw)',alarm_status:'Alarm status (raw)',runtime_hours:'Runtime',preheater_duty:'Preheater duty (raw)',air_quality:'Air quality (raw)',co2:'CO₂ (optional)',filter_period:'Filter period',supply_rpm:'Supply fan',extract_rpm:'Extract fan'};
$('scan_network').onclick=async()=>{try{const j=await api('/api/discovery',{method:'POST',headers:{'Content-Type':'application/json','X-Local-Token':token},body:'{}'});show('scan_status',j.message); }catch(e){show('scan_status',e.message)}};
$('select_device').onclick=async()=>{const ip=$('device_list').value;if(!ip)return; if(!confirm('Connect to '+ip+'? Automatic fan control will be disabled until re-enabled. Confirm this is your Dantherm device.'))return;try{const j=await api('/api/device',{method:'POST',headers:{'Content-Type':'application/json','X-Local-Token':token},body:JSON.stringify({ip})});show('scan_status',j.message);await refresh()}catch(e){show('scan_status',e.message)}};
async function refresh(){try{const [s,a]=await Promise.all([api('/api/readings'),api('/api/automation')]);token=s.token;renderConfig(a.config);cfg=a.config;show('connection',s.status+' · '+s.device+':502'+(s.device_confirmed?'':' · controls locked'));const d=await api('/api/discovery');if(d.running)show('scan_status','Scanning local network…');else if(d.error)show('scan_status',d.error);else if(d.last_scan)show('scan_status',d.candidates.length+' compatible response(s) found. Select your device to enable controls.');const sel=$('device_list');const old=sel.value;sel.innerHTML='<option value="">Choose device</option>'+d.candidates.map(x=>'<option value="'+x.ip+'">'+x.ip+' (identity unverified)</option>').join('');sel.value=old;const v=s.values||{};const val=k=>v[k]?.value;for(const [id,k,unit] of [['outdoor','outdoor_temp','°C'],['supplytemp','supply_temp','°C'],['extracttemp','extract_temp','°C'],['humidity','humidity','%']])show(id,val(k)??'—'+unit);show('currentfan',val('fan_level')??'—');show('rpms',(val('supply_rpm')??'—')+' / '+(val('extract_rpm')??'—'));show('filterdays',(val('filter_days')??'—')+' days');show('filter_remaining',(val('filter_days')??'—')+' days');show('filter_period',(val('filter_period')??'—')+' days');show('lastupdate','Last update: '+(s.last_update||'waiting')+' · Errors: '+Object.keys(s.errors||{}).length);document.querySelectorAll('[data-level]').forEach(b=>b.classList.toggle('active',Number(b.dataset.level)===val('fan_level')));$('vacation').classList.toggle('active',cfg.vacation_enabled);const sleepActive=cfg.sleep_until&&Date.parse(cfg.sleep_until)>Date.now();$('sleep').classList.toggle('active',!!sleepActive);show('sleep_label',sleepActive?'Active until '+new Date(cfg.sleep_until).toLocaleTimeString():'Fan 1 for selected hours');show('auto_status','Automation: '+a.status.reason+(a.status.last_error?' · '+a.status.last_error:''));$('measurements').innerHTML=Object.entries(labels).map(([k,l])=>'<div class="stat"><small>'+l+'</small><strong>'+((val(k)??'—'))+'</strong><small>Register '+(v[k]?.address??'—')+'</small></div>').join('')}catch(e){show('connection','Disconnected · '+e.message)}}
refresh();setInterval(refresh,6000);
</script></body></html>
'''
class Handler(BaseHTTPRequestHandler):
    def send_json(self,code,obj):
        body=json.dumps(obj).encode();self.send_response(code);self.send_header('Content-Type','application/json');self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    def do_GET(self):
        if urlsplit(self.path).path=='/api/discovery':
            with lock:return self.send_json(200,dict(discovery))
        if urlsplit(self.path).path=='/api/health':
            with lock:
                return self.send_json(200,{'server':'running','device_status':state['status'],'last_update':state['last_update'],'log_path':LOG_PATH,'last_command':state['last_command']})
        if urlsplit(self.path).path=='/api/readings':
            with lock:payload=dict(state);payload['values']=dict(state['values']);payload['errors']=dict(state['errors']);payload['token']=csrf;payload['device_confirmed']=device_confirmed
            return self.send_json(200,payload)
        if urlsplit(self.path).path=='/api/automation':
            with lock:payload={'config':{**config,'schedule':[dict(x) for x in config['schedule']]},'status':dict(auto)}
            payload['status'].pop('last_attempt_monotonic',None)
            return self.send_json(200,payload)
        if self.path=='/house_airflow.png':
            import sys
            root=getattr(sys,'_MEIPASS',os.path.dirname(os.path.abspath(__file__)))
            path=os.path.join(root,'house_airflow.png')
            try:
                with open(path,'rb') as f:b=f.read()
            except OSError:return self.send_error(404)
            self.send_response(200);self.send_header('Content-Type','image/png');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b);return
        if self.path=='/':
            b=HTML.encode();self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b);return
        self.send_error(404)
    def do_POST(self):
        if self.path not in ('/api/fan','/api/automation','/api/filter-period','/api/discovery','/api/device'):return self.send_json(404,{'error':'Not found'})
        if self.headers.get('X-Local-Token')!=csrf:return self.send_json(403,{'error':'Invalid local token'})
        if self.headers.get('Content-Type','').split(';')[0]!='application/json':return self.send_json(415,{'error':'JSON required'})
        try:
            n=int(self.headers.get('Content-Length','0'))
            if n<1 or n>8192:raise ValueError('Invalid body length')
            data=json.loads(self.rfile.read(n))
            if self.path=='/api/discovery':
                with lock:
                    if discovery['running']:return self.send_json(200,{'message':'Scan already running'})
                    discovery['running']=True
                threading.Thread(target=scan_worker,daemon=True).start()
                return self.send_json(200,{'message':'Network scan started; results appear shortly'})
            if self.path=='/api/device':
                select_device(data.get('ip'))
                return self.send_json(200,{'message':'Device selected; automation disabled until you enable it again'})
            if self.path=='/api/automation':
                new=validate_config(data)
                with lock:
                    save_config(new)
                    config.clear();config.update(new)
                    auto['humidity_active']=False
                    auto['last_attempt_monotonic']=time.monotonic() if new['enabled'] else 0
                return self.send_json(200,{'message':'Automation saved','config':new})
            if self.path=='/api/filter-period':
                if not device_confirmed:raise ValueError('Confirm the device in Network Settings first')
                days=data.get('days')
                if type(days)!=int or not 90<=days<=365:raise ValueError('Filter period must be 90–365 days')
                if data.get('confirm')!='CHANGE FILTER PERIOD':raise ValueError('Confirmation required')
                with command_lock:
                    before=read32(556)
                    exchange(16,556,[days&65535,days>>16])
                    time.sleep(.3)
                    after=read32(556)
                return self.send_json(200 if after==days else 409,{'verified':after==days,'before':before,'after':after,'message':'Filter period updated' if after==days else 'Write readback did not match'})
            level=data.get('level')
            if type(level)!=int or level not in (1,2,3,4):raise ValueError('Only levels 1–4 allowed')
            result=set_fan(level,'manual')
            success=result['verified']
            return self.send_json(200 if success else 409,result)
        except (ValueError,KeyError,TypeError) as e:return self.send_json(400,{'error':str(e)})
        except Exception as e:
            logging.exception('Command failed on %s',self.path)
            return self.send_json(502,{'error':'Device command failed: '+str(e)})
    def log_message(self,fmt,*args):
        logging.info('%s %s',self.address_string(),fmt % args)
    def handle_one_request(self):
        try:super().handle_one_request()
        except (BrokenPipeError,ConnectionResetError):logging.info('Browser disconnected during request')
        except Exception:
            logging.exception('Unexpected HTTP handler failure')
            raise
if __name__=='__main__':
    threading.Thread(target=poll,daemon=True).start()
    threading.Thread(target=scan_worker,daemon=True).start()
    threading.Thread(target=automation_loop,daemon=True).start()
    print('Dashboard: http://localhost:'+str(a.web_port))
    logging.info('Starting AirNest on 127.0.0.1:%s, device %s:%s',a.web_port,a.device,a.port)
    server=ThreadingHTTPServer(('127.0.0.1',a.web_port),Handler)
    server.daemon_threads=True
    try:server.serve_forever()
    except Exception:
        logging.exception('HTTP server stopped unexpectedly')
        raise
