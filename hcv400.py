#!/usr/bin/env python3
"""Read-only HCV 400 monitor; community register map, verify against your firmware."""
import argparse, csv, datetime as dt, json, socket, struct, threading, time, os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
p=argparse.ArgumentParser()
p.add_argument('--device',default='192.168.1.142');p.add_argument('--port',type=int,default=502)
p.add_argument('--unit',type=int,default=1);p.add_argument('--web-port',type=int,default=8088)
p.add_argument('--interval',type=int,default=30);p.add_argument('--output',default='hcv400_history.csv')
a=p.parse_args()
# Addresses are zero-based, community-sourced, not guaranteed for every firmware.
FIELDS={'outdoor_temp':(132,'float','°C'),'supply_temp':(134,'float','°C'),
'extract_temp':(136,'float','°C'),'exhaust_temp':(138,'float','°C'),
'humidity_raw':(196,'uint','raw'),'bypass_raw':(198,'uint','raw'),
'fan_state_raw':(324,'uint','raw'),'operation_mode_raw':(472,'uint','raw'),
'filter_days_raw':(554,'uint','days')}
state={'status':'Starting','device':a.device,'last_update':None,'values':{},'errors':{}}
lock=threading.Lock()
def read_pair(addr):
    txid=int(time.monotonic_ns()&65535)
    packet=struct.pack('>HHHBBHH',txid,0,6,a.unit,3,addr,2)
    with socket.create_connection((a.device,a.port),timeout=3) as s:
        s.settimeout(3);s.sendall(packet)
        def recv(n):
            b=b''
            while len(b)<n:
                part=s.recv(n-len(b))
                if not part:raise ConnectionError('Connection closed')
                b+=part
            return b
        head=recv(7);tid,proto,length,unit=struct.unpack('>HHHB',head)
        if tid!=txid or proto!=0 or unit!=a.unit or not 2<=length<=254:raise ValueError('Bad Modbus header')
        payload=recv(length-1)
        if payload[0]==0x83:raise ValueError('Modbus exception '+str(payload[1]))
        if len(payload)!=6 or payload[:2]!=b'\x03\x04':raise ValueError('Unexpected response')
        # Swap 16-bit words as documented by HCV community configurations.
        return payload[4:6]+payload[2:4]
def poll():
    while True:
        values={};errors={}
        for name,(addr,kind,unit) in FIELDS.items():
            try:
                raw=read_pair(addr)
                value=struct.unpack('>f' if kind=='float' else '>I',raw)[0]
                if kind=='float' and not -100<=value<=150:raise ValueError('Implausible float')
                values[name]={'value':round(value,2) if kind=='float' else value,'unit':unit,'address':addr}
            except Exception as e:errors[name]=str(e)
        now=dt.datetime.now(dt.timezone.utc).isoformat()
        with lock:state.update(status='Connected' if values else 'No readings',last_update=now,values=values,errors=errors)
        if values:
            exists=os.path.exists(a.output) and os.path.getsize(a.output)>0
            with open(a.output,'a',newline='') as f:
                w=csv.writer(f)
                if not exists:w.writerow(['timestamp_utc','sensor','value','unit','register'])
                for name,v in values.items():w.writerow([now,name,v['value'],v['unit'],v['address']])
        time.sleep(max(10,a.interval))
HTML='''<!doctype html><html lang="en"><meta name="viewport" content="width=device-width,initial-scale=1"><meta charset="utf-8"><title>HCV 400 Monitor</title><style>body{font:16px system-ui;background:#0b1015;color:#e9f2f4;max-width:900px;margin:auto;padding:24px}h1{color:#8ce4b1}.muted{color:#9cabb6}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px}.card{background:#17212a;border:1px solid #30414c;border-radius:16px;padding:20px}.value{font-size:30px;font-weight:700;margin:10px 0}a{color:#8ce4b1}small{color:#9cabb6}</style><h1>Dantherm HCV 400</h1><p class="muted">Local read-only monitor · Modbus TCP · community register mapping</p><div class="card" id="status">Connecting…</div><h2>Live measurements</h2><div class="grid" id="cards"></div><p class="muted">Raw mode and bypass codes are intentionally not interpreted until validated on your unit. CSV history is saved on the computer running this app.</p><p><a href="/api/readings">JSON readings API</a></p><script>const labels={outdoor_temp:'Outdoor temperature',supply_temp:'Supply temperature',extract_temp:'Extract temperature',exhaust_temp:'Exhaust temperature',humidity_raw:'Humidity (raw)',bypass_raw:'Bypass (raw)',fan_state_raw:'Fan state (raw)',operation_mode_raw:'Operation mode (raw)',filter_days_raw:'Filter remaining'};async function update(){try{let r=await fetch('/api/readings',{cache:'no-store'}),s=await r.json();document.getElementById('status').textContent=s.status+' · '+s.device+':502 · Last update: '+(s.last_update||'waiting')+' · Errors: '+Object.keys(s.errors).length;document.getElementById('cards').innerHTML=Object.entries(labels).map(([k,label])=>{let v=s.values[k];return '<div class="card"><small>'+label+'</small><div class="value">'+(v?v.value:'—')+'</div><small>'+(v?v.unit+' · register '+v.address:(s.errors[k]||'Waiting'))+'</small></div>'}).join('')}catch(e){document.getElementById('status').textContent=String(e)}}update();setInterval(update,5000)</script></html>'''
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path=='/api/readings':
            with lock:body=json.dumps(state).encode()
            mime='application/json'
        elif self.path=='/':body=HTML.encode();mime='text/html; charset=utf-8'
        else:self.send_error(404);return
        self.send_response(200);self.send_header('Content-Type',mime);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    def log_message(self,*args):pass
if __name__=='__main__':
    threading.Thread(target=poll,daemon=True).start()
    print('Dashboard: http://localhost:'+str(a.web_port))
    ThreadingHTTPServer(('0.0.0.0',a.web_port),Handler).serve_forever()
