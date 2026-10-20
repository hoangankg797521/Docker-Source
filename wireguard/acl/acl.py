"""Shared ACL authority. No WireGuard keys, passwords, Docker socket or shell execution."""
import hashlib
import hmac
import ipaddress
import json
import os
import re
import sqlite3
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

def configured_scope():
    values = os.environ.get('ACL_SCOPE', '192.168.50.0/24').split(',')
    networks = [ipaddress.ip_network(v.strip(), strict=True) for v in values]
    if not networks or any(n.version != 4 or n.prefixlen == 0 for n in networks):
        raise ValueError('ACL_SCOPE requires explicit canonical IPv4 subnets; /0 is forbidden')
    return [str(n) for n in networks]

SCOPE = configured_scope()
TABLE = 'wg_unified_acl'
LOCK = threading.RLock()

class Invalid(Exception):
    pass

class Conflict(Exception):
    pass

def validate(body):
    if body.get('mode') not in ('allow_all','custom','deny_all'):
        raise Invalid('Invalid policy mode')
    rules = body.get('rules', [])
    if not isinstance(rules, list) or len(rules) > 64:
        raise Invalid('Maximum 64 rules')
    clean, warnings = [], []
    for r in rules:
        try:
            net = ipaddress.ip_network(r['destination'], strict=True)
        except (KeyError, ValueError, TypeError):
            raise Invalid('Destination must be a canonical IPv4/CIDR')
        if net.version != 4 or not any(net.subnet_of(ipaddress.ip_network(s)) for s in SCOPE):
            raise Invalid('Destination outside approved IPv4 scope; IPv6 is disabled')
        proto = r.get('protocol', 'any')
        if proto not in ('any','tcp','udp','icmp'):
            raise Invalid('Protocol must be any/tcp/udp/icmp')
        port = str(r.get('ports','')).strip()
        if port:
            if proto not in ('tcp','udp') or not re.fullmatch(r'[0-9]{1,5}(-[0-9]{1,5})?', port):
                raise Invalid('Ports require TCP/UDP and one port or range')
            lo, _, hi = port.partition('-')
            if not 1 <= int(lo) <= int(hi or lo) <= 65535:
                raise Invalid('Invalid port range')
            port = str(int(lo)) + ('-' + str(int(hi)) if hi and int(hi) != int(lo) else '')
        if not isinstance(r.get('enabled', True), bool):
            raise Invalid('Rule enabled must be boolean')
        row = dict(destination=str(net), protocol=proto, ports=port, enabled=r.get('enabled',True))
        if row in clean:
            raise Invalid('Duplicate rule')
        for old in clean:
            if net.overlaps(ipaddress.ip_network(old['destination'])) and row['enabled'] and old['enabled']:
                warnings.append('Overlapping destination: '+str(net)+' / '+old['destination'])
        clean.append(row)
    return dict(mode=body['mode'], rules=clean, legacy=False), sorted(set(warnings))

def route_coverage(policy, client):
    routes = [ipaddress.ip_network(v, strict=False) for v in client.get('routes', [])]
    v4 = list(ipaddress.collapse_addresses([v for v in routes if v.version == 4]))
    destinations = SCOPE if policy['mode'] == 'allow_all' else [r['destination'] for r in policy['rules'] if r['enabled']] if policy['mode'] == 'custom' else []
    result = []
    for destination in sorted(set(destinations)):
        net = ipaddress.ip_network(destination)
        covered = any(net.subnet_of(r) for r in v4)
        partial = not covered and any(net.overlaps(r) for r in v4)
        result.append(dict(destination=destination, coverage='covered' if covered else 'partial' if partial else 'missing', effectiveAccess='server_allowed_route_covered' if covered else 'server_allowed_route_incomplete', warning=None if covered else 'ACL cho phép nhưng cấu hình xuất hiện tại thiếu route; cần bổ sung route trên thiết bị nếu cần truy cập.'))
    return dict(source='wg-easy exported configuration; installed client configuration not verified', clientRoutes=client.get('routes',[]), installedRoutesVerified=False, destinations=result)


def candidate(state):
    lines = [f'table inet {TABLE} {{']
    for reverse in (False, True):
        lines.append('chain reverse {' if reverse else 'chain access {')
        for cid, c in sorted(state['clients'].items()):
            policy = state['policies'][cid]
            addr = str(ipaddress.IPv4Address(c['ip']))
            client_match = f'ip {"daddr" if reverse else "saddr"} {addr}'
            if policy.get('legacy'):
                lines.append(f'{client_match} counter accept comment "legacy-{cid}"')
            else:
                rules = [dict(destination=dest,protocol='any',ports='',enabled=True) for dest in SCOPE] if policy['mode']=='allow_all' else policy['rules'] if policy['mode']=='custom' else []
                for r in rules:
                    if not r['enabled']: continue
                    rule = f'{client_match} ip {"saddr" if reverse else "daddr"} {r["destination"]}'
                    if reverse: rule += ' ct state established,related'
                    if r['protocol'] != 'any': rule += f' ip protocol {r["protocol"]}'
                    if r['ports']: rule += f' {r["protocol"]} {"sport" if reverse else "dport"} {r["ports"]}'
                    lines.append(rule+' counter accept')
            lines.append(f'{client_match} counter drop comment "client-{cid}-default-deny"')
        lines += ['counter drop comment "unknown-client-or-ipv6-deny"','}']
    lines += [
        'chain input { type filter hook input priority -10; policy accept; iifname "wg0" jump access; }',
        'chain output { type filter hook output priority -10; policy accept; oifname "wg0" jump reverse; }',
        'chain forward { type filter hook forward priority -10; policy accept; iifname "wg0" jump access; oifname "wg0" jump reverse; }', '}']
    return '\n'.join(lines) + '\n'


def normalize(value):
    if isinstance(value, dict):
        return {k:normalize(v) for k,v in value.items() if k not in ('handle','packets','bytes','metainfo')}
    if isinstance(value, list):
        return [normalize(v) for v in value if not isinstance(v,dict) or 'metainfo' not in v]
    return value

class Firewall:
    def run(self, args, data=None):
        p = subprocess.run(['nft']+args, input=data, text=True, capture_output=True, timeout=10)
        if p.returncode:
            raise RuntimeError(p.stderr.strip()[:1500])
        return p.stdout
    def fingerprint(self):
        try:
            data = normalize(json.loads(self.run(['-j','list','table','inet',TABLE])))
            return hashlib.sha256(json.dumps(data,sort_keys=True).encode()).hexdigest()
        except RuntimeError:
            return None
    def apply(self, state):
        script = candidate(state)
        if self.fingerprint() is not None:
            script = f'delete table inet {TABLE}\n' + script
        self.run(['-c','-f','-'],script)
        self.run(['-f','-'],script)
        result = self.fingerprint()
        if not result:
            raise RuntimeError('Ruleset verification failed')
        return result

class Authority:
    def __init__(self, path, firewall=None, legacy_ids=()):
        self.path, self.fw = path, firewall or Firewall()
        self.legacy_ids = set(legacy_ids)
        self.error = None
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL, fingerprint TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY, ts INTEGER, actor TEXT, client TEXT, revision INTEGER, previous TEXT, new TEXT, result TEXT, error TEXT)')
            db.execute('INSERT OR IGNORE INTO state VALUES (1, ?, NULL)', (json.dumps(dict(clients={},policies={},generation=0)),))
        self.reconcile()
    def db(self):
        return sqlite3.connect(self.path)
    def load(self):
        with self.db() as db:
            row = db.execute('SELECT body,fingerprint FROM state WHERE id=1').fetchone()
        return json.loads(row[0]), row[1]
    def status(self):
        state, expected = self.load()
        actual = self.fw.fingerprint()
        return dict(status='applied' if expected and actual == expected and not self.error else 'drift', error=self.error, generation=state['generation'], fingerprint=actual, expectedFingerprint=expected)
    def reconcile(self):
        with LOCK:
            state, fingerprint = self.load()
            if not fingerprint or fingerprint != self.fw.fingerprint():
                drift = fingerprint is not None
                if drift:
                    with self.db() as db:
                        db.execute('INSERT INTO audit(ts,actor,client,revision,previous,new,result,error) VALUES(?,?,?,?,?,?,?,?)',(int(time.time()),'acl-reconciler','registry',state['generation'],json.dumps({'fingerprint':fingerprint}),json.dumps({'fingerprint':self.fw.fingerprint()}),'drift_detected',None))
                try:
                    fingerprint = self.fw.apply(state)
                    with self.db() as db:
                        db.execute('UPDATE state SET fingerprint=? WHERE id=1',(fingerprint,))
                        if drift:
                            db.execute('INSERT INTO audit(ts,actor,client,revision,previous,new,result,error) VALUES(?,?,?,?,?,?,?,?)',(int(time.time()),'acl-reconciler','registry',state['generation'],'{}','{}','reconciled',None))
                    self.error = None
                except Exception as e:
                    self.error = str(e)
    def commit(self, state, actor, cid, previous, new):
        old, _ = self.load()
        try:
            fingerprint = self.fw.apply(state)
            with self.db() as db:
                db.execute('UPDATE state SET body=?,fingerprint=? WHERE id=1',(json.dumps(state),fingerprint))
                db.execute('INSERT INTO audit(ts,actor,client,revision,previous,new,result,error) VALUES(?,?,?,?,?,?,?,?)',(int(time.time()),actor,cid,new.get('revision',0),json.dumps(previous),json.dumps(new),'applied',None))
            self.error = None
        except Exception as e:
            try:
                self.fw.apply(old)
            except Exception:
                self.error = 'Apply and rollback failed; fail-closed gate requires operator review'
            with self.db() as db:
                db.execute('INSERT INTO audit(ts,actor,client,revision,previous,new,result,error) VALUES(?,?,?,?,?,?,?,?)',(int(time.time()),actor,cid,new.get('revision',0),json.dumps(previous),json.dumps(new),'failed',str(e)))
            raise
    def sync(self, clients, actor):
        with LOCK:
            state, _ = self.load()
            registry = {}
            for c in clients:
                cid = str(c['id'])
                if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',cid):
                    raise Invalid('Invalid client ID')
                ip = str(ipaddress.IPv4Address(c['ip']))
                if ipaddress.IPv4Address(ip) not in ipaddress.ip_network('10.255.66.0/24') or ip.endswith('.0') or ip.endswith('.1') or ip.endswith('.255'):
                    raise Invalid('Client IP outside VPN subnet')
                if ip in [v['ip'] for v in registry.values()]:
                    raise Invalid('Duplicate VPN IP')
                routes = c.get('routes',[])
                if not isinstance(routes,list) or len(routes)>128: raise Invalid('Invalid client routes')
                routes = [str(ipaddress.ip_network(v,strict=False)) for v in routes]
                registry[cid] = dict(id=cid,name=str(c['name'])[:128],ip=ip,routes=routes)
            if registry == state['clients']:
                return
            for cid in registry:
                if cid not in state['policies']:
                    state['policies'][cid] = dict(mode='allow_all' if cid in self.legacy_ids and state['generation']==0 else 'deny_all',rules=[],legacy=cid in self.legacy_ids and state['generation']==0,revision=1,updatedAt=int(time.time()),updatedBy=actor)
            state['policies'] = {cid:state['policies'][cid] for cid in registry}
            state['clients'] = registry
            state['generation'] += 1
            self.commit(state,actor,'registry',{},dict(revision=state['generation'],clients=registry))
    def get(self, cid):
        with LOCK:
            state,_ = self.load()
            if cid not in state['clients']:
                raise KeyError('Client not found')
            return dict(client=state['clients'][cid],policy=state['policies'][cid],scope=SCOPE,apply=self.status(),routeCoverage=route_coverage(state['policies'][cid],state['clients'][cid]),effective=('Legacy: giữ nguyên quyền trước chuyển đổi' if state['policies'][cid].get('legacy') else 'Rules enabled trong scope; mặc định chặn IPv4 khác và IPv6'))
    def save(self,cid,body,actor):
        with LOCK:
            state,_ = self.load()
            if cid not in state['clients']:
                raise KeyError('Client not found')
            old=state['policies'][cid]
            if type(body.get('revision')) is not int or body['revision'] != old['revision']:
                raise Conflict('Policy changed; reload before saving')
            policy,warnings=validate(body)
            policy.update(revision=old['revision']+1,updatedAt=int(time.time()),updatedBy=actor)
            state['policies'][cid]=policy
            state['generation']+=1
            self.commit(state,actor,cid,old,policy)
            return dict(**self.get(cid),warnings=warnings)
    def audit(self,cid):
        with self.db() as db:
            db.row_factory=sqlite3.Row
            rows=db.execute('SELECT * FROM audit WHERE client=? OR client=? ORDER BY id DESC LIMIT 100',(cid,'registry')).fetchall()
        return [dict(r) for r in rows]

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):
        pass
    def send(self,status,data):
        raw=json.dumps(data,ensure_ascii=False).encode()
        self.send_response(status); self.send_header('Content-Type','application/json');self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
    def dispatch(self,write=False):
        try:
            if self.path=='/health' and not write:
                s=AUTHORITY.status(); self.send(200 if s['status']=='applied' else 503,s);return
            supplied=self.headers.get('Authorization','').removeprefix('Bearer ')
            role=next((name for name,token in TOKENS.items() if hmac.compare_digest(supplied,token)),None)
            if not role:
                self.send(401,{'error':'unauthorized'});return
            actor=self.headers.get('X-ACL-Actor','')
            if not actor or len(actor)>128:
                raise Invalid('Administrator identity required')
            body={}
            if write:
                size=int(self.headers.get('Content-Length','0'))
                if not 0<size<=65536: raise Invalid('Invalid body size')
                body=json.loads(self.rfile.read(size))
                if not isinstance(body,dict): raise Invalid('JSON object required')
            if self.path=='/v1/sync' and write:
                if role!='wg': self.send(403,{'error':'registry restricted to wg-easy'});return
                AUTHORITY.sync(body['clients'],actor);self.send(200,AUTHORITY.status());return
            if self.path=='/v1/clients' and not write:
                state,_=AUTHORITY.load();self.send(200,{'items':[AUTHORITY.get(cid) for cid in state['clients']],'apply':AUTHORITY.status()});return
            match=re.fullmatch(r'/v1/clients/([A-Za-z0-9_-]{1,64})(/audit)?',self.path)
            if not match: self.send(404,{'error':'not_found'});return
            cid=match[1]
            if match[2]:
                if write: raise Invalid('Audit is read-only')
                self.send(200,{'items':AUTHORITY.audit(cid)});return
            self.send(200,AUTHORITY.save(cid,body,actor) if write else AUTHORITY.get(cid))
        except Conflict as e: self.send(409,{'error':str(e)})
        except KeyError: self.send(404,{'error':'client_not_found'})
        except (Invalid,ValueError,TypeError) as e: self.send(400,{'error':str(e)})
        except Exception: self.send(503,{'error':'ACL apply/backend failure; check audit and status'})
    def do_GET(self): self.dispatch()
    def do_POST(self): self.dispatch(True)

def monitor():
    while True:
        time.sleep(5)
        AUTHORITY.reconcile()

if __name__=='__main__':
    os.umask(0o077)
    root=Path(os.getenv('DATA_DIR','/data'));root.mkdir(parents=True,exist_ok=True)
    TOKENS={role:Path('/run/secrets/'+role+'_token').read_text().strip() for role in ('wg','noc')}
    if any(len(t)<32 for t in TOKENS.values()): raise RuntimeError('Service token too short')
    legacy=Path('/config/legacy-ids.json')
    AUTHORITY=Authority(str(root/'acl.sqlite3'),legacy_ids=json.loads(legacy.read_text()) if legacy.exists() else [])
    threading.Thread(target=monitor,daemon=True).start()
    ThreadingHTTPServer(('0.0.0.0',8089),Handler).serve_forever()
