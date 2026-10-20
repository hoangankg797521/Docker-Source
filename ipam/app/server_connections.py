"""V6 manual server documentation. Never writes discovered topology/FDB tables."""
import ipaddress
import json
import re
import uuid
from contextlib import contextmanager
from urllib.parse import urlparse
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from .port_mapping import normalize_interface

class NIC(BaseModel):
    id: str = ''
    name: str = Field(min_length=1,max_length=100)
    mac: str = Field(default='',max_length=32)
    ips: list[str] = Field(default_factory=list,max_length=64)
class Server(BaseModel):
    hostname: str = Field(min_length=1,max_length=253)
    management_ips: list[str] = Field(default_factory=list,max_length=64)
    server_type: str = 'Physical'
    os: str = Field(default='',max_length=200)
    vendor: str = Field(default='',max_length=200)
    model: str = Field(default='',max_length=200)
    location: str = Field(default='',max_length=200)
    rack: str = Field(default='',max_length=200)
    description: str = Field(default='',max_length=4000)
    status: str = 'Unknown'
    interfaces: list[NIC] = Field(default_factory=list,max_length=256)
    version: int = 0
class Connection(BaseModel):
    nic_id: str
    switch_id: int
    switch_port: str = Field(min_length=1,max_length=100)
    link_speed: str = Field(default='',max_length=100)
    connection_type: str = Field(default='Standalone',max_length=100)
    bond_name: str = Field(default='',max_length=100)
    server_bond_mode: str = Field(default='',max_length=100)
    switch_aggregation: str = Field(default='',max_length=100)
    destination_port_channel: str = Field(default='',max_length=100)
    destination_interface_description: str = Field(default='',max_length=500)
    vlan_mode: str = Field(default='',max_length=40)
    vlan: str = Field(default='',max_length=500)
    cable_label: str = Field(default='',max_length=200)
    notes: str = Field(default='',max_length=4000)
    verification_status: str = 'MANUAL'
    acknowledge_conflicts: bool = False
    version: int = 0
class Duplicate(BaseModel):
    hostname: str

def documentation_verification(declared,port_exists):
    """Keep the declared state; missing interface inventory remains unverified."""
    return declared if port_exists else 'UNVERIFIED'

def ips(values):
    try: return list(dict.fromkeys(str(ipaddress.ip_address(v.strip())) for v in values))
    except ValueError: raise HTTPException(422,'Invalid IPv4/IPv6 address')
def normalize_server(x):
    d=x.model_dump();d['hostname']=d['hostname'].strip().rstrip('.')
    if not all(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?',s) for s in d['hostname'].split('.')):
        raise HTTPException(422,'Hostname must use DNS labels (letters, digits and hyphens)')
    if d['server_type'] not in ('Physical','VM','Other') or d['status'] not in ('Active','Inactive','Unknown'):
        raise HTTPException(422,'Invalid server type/status')
    d['management_ips']=ips(d['management_ips']);seen=set()
    for n in d['interfaces']:
        n['name']=n['name'].strip();key=n['name'].casefold()
        if not key or key in seen: raise HTTPException(422,'NIC names must be nonempty and unique within the server')
        seen.add(key);n['ips']=ips(n['ips'])
        if n['mac']:
            m=re.sub(r'[:.\-\s]','',n['mac']).lower()
            if not re.fullmatch('[0-9a-f]{12}',m): raise HTTPException(422,'Invalid NIC MAC address')
            n['mac']=':'.join(m[i:i+2] for i in range(0,12,2))
    return d

def value(v): return json.loads(v) if isinstance(v,str) else v
def register(app,db,auth,has_permission,audit):
    router=APIRouter(prefix='/api/studio/servers',tags=['Server Connection V6'])
    def user(r,write=False):
        u=auth(r)
        if not has_permission(u,'discovery.write' if write else 'discovery.read'):raise HTTPException(403,'Missing discovery permission')
        if write and r.headers.get('origin') and urlparse(r.headers['origin']).netloc!=r.headers.get('host'):
            raise HTTPException(403,'Cross-origin write rejected')
        return u
    @contextmanager
    def transaction(write=False):
        c=db()
        try:
            # Serialize manual uniqueness/conflict checks without touching SNMP locks.
            if write:c.execute('SELECT pg_advisory_xact_lock(620006)')
            yield c;c.commit()
        except Exception:c.rollback();raise
        finally:c.close()
    def server(c,sid,lock=False):
        r=c.execute('SELECT * FROM server_inventory WHERE id=? AND NOT deleted'+(' FOR UPDATE' if lock else ''),(sid,)).fetchone()
        if not r:raise HTTPException(404,'Server not found')
        d=dict(r);d['management_ips']=value(d['management_ips']);d['interfaces']=[{**dict(n),'ips':value(n['ips'])} for n in c.execute('SELECT * FROM server_network_interfaces WHERE server_id=? AND NOT deleted ORDER BY name',(sid,))]
        return d
    def record(c,sid,cid,action,actor,before,after):
        c.execute('INSERT INTO server_connection_audit(server_id,connection_id,action,actor,before_state,after_state) VALUES(?,?,?,?,?,?)',
                  (sid,cid,action,actor,json.dumps(before,default=str) if before is not None else None,json.dumps(after,default=str) if after is not None else None))
        audit('SERVER_CONNECTION_'+action,sid,actor,conn=c)
    def unique(c,d,sid=''):
        wanted=set(d['management_ips'])|{ip for n in d['interfaces'] for ip in n['ips']}
        macs={n['mac'] for n in d['interfaces'] if n['mac']}
        for row in c.execute('SELECT * FROM server_inventory WHERE NOT deleted AND id<>?',(sid,)):
            other=dict(row)
            if other['hostname'].casefold()==d['hostname'].casefold():raise HTTPException(409,'Hostname already exists')
            ns=[dict(n) for n in c.execute('SELECT mac,ips FROM server_network_interfaces WHERE server_id=? AND NOT deleted',(other['id'],))]
            if wanted.intersection(set(value(other['management_ips']))|{ip for n in ns for ip in value(n['ips'])}):raise HTTPException(409,'IP already assigned to another manual server')
            if macs.intersection(n['mac'] for n in ns if n['mac']):raise HTTPException(409,'MAC already assigned to another manual server')
    def write_nics(c,sid,ns):
        existing={r['id']:dict(r) for r in c.execute('SELECT * FROM server_network_interfaces WHERE server_id=? AND NOT deleted',(sid,))}
        keep={n['id'] for n in ns if n['id']}
        if len(keep)!=sum(bool(n['id']) for n in ns) or not keep.issubset(existing):raise HTTPException(422,'Invalid or duplicate NIC ID')
        for nid in existing.keys()-keep:
            if c.execute('SELECT 1 FROM server_connections WHERE nic_id=? AND NOT deleted',(nid,)).fetchone():raise HTTPException(409,'Remove the NIC connections before deleting this NIC')
        # Retire names first so two existing NICs may safely swap names.
        c.execute('UPDATE server_network_interfaces SET deleted=true WHERE server_id=?',(sid,))
        for n in ns:
            nid=n['id'] or str(uuid.uuid4())
            if nid in existing:c.execute('UPDATE server_network_interfaces SET name=?,mac=?,ips=?,deleted=false WHERE id=?',(n['name'],n['mac'],json.dumps(n['ips']),nid))
            else:c.execute('INSERT INTO server_network_interfaces(id,server_id,name,mac,ips) VALUES(?,?,?,?,?)',(nid,sid,n['name'],n['mac'],json.dumps(n['ips'])))
    fields=('hostname','management_ips','server_type','os','vendor','model','location','rack','description','status')
    def create(c,d,actor,action='CREATE'):
        unique(c,d);sid=str(uuid.uuid4());vals=[json.dumps(d[f]) if f=='management_ips' else d[f] for f in fields]
        c.execute('INSERT INTO server_inventory(id,'+','.join(fields)+',created_by,updated_by) VALUES('+','.join('?' for _ in range(len(fields)+3))+')',[sid,*vals,actor,actor])
        write_nics(c,sid,d['interfaces']);out=server(c,sid);record(c,sid,None,action,actor,None,out);return out
    def switches(c):
        return [dict(d) for d in c.execute('SELECT id,name,management_ip,device_type,sys_descr FROM network_devices ORDER BY name') if any(k in (str(d['device_type'])+' '+str(d['sys_descr'])).lower() for k in ('switch','nexus','catalyst','sg350','sg300','n3k','n9k'))]
    def enrich(c,cs):
        # Manual server documentation is authoritative. LLDP/transit observations
        # and reused NIC labels do not establish a conflict for this module.
        ports={(int(r['device_id']),normalize_interface(r['if_name'])):dict(r) for r in c.execute('SELECT * FROM network_interfaces')}
        ds={int(r['id']):dict(r) for r in c.execute('SELECT id,name,management_ip FROM network_devices')}
        for x in cs:
            key=(int(x['switch_id']),normalize_interface(x['switch_port']));p=ports.get(key);warnings=[]
            if not p:warnings.append({'code':'PORT_NOT_IN_INVENTORY','message':'Manually entered port; Unverified'})
            x['declared_verification']=x['verification_status'];x['source']='MANUAL';x['warnings']=warnings;x['port_exists']=bool(p)
            x['verification_status']=documentation_verification(x['verification_status'],bool(p))
            x['oper_status']=({'1':'up','2':'down','3':'testing','4':'unknown','5':'dormant','6':'notPresent','7':'lowerLayerDown'}.get(str(p['oper_status']),p['oper_status']) if p else 'Unknown');x['switch_name']=ds.get(int(x['switch_id']),{}).get('name','Unknown');x['switch_ip']=ds.get(int(x['switch_id']),{}).get('management_ip','')
            x['switch_interface_description']=(p.get('if_descr') or p.get('if_name') or '') if p else ''
            x['switch_port_status']=x['oper_status']
        return cs
    def snapshot(c):
        out=[server(c,r['id']) for r in c.execute('SELECT id FROM server_inventory WHERE NOT deleted ORDER BY lower(hostname)')]
        connections=enrich(c,[dict(r) for r in c.execute('SELECT x.* FROM server_connections x JOIN server_inventory s ON s.id=x.server_id WHERE NOT x.deleted AND NOT s.deleted ORDER BY x.created_at')])
        for s in out:s['connections']=[x for x in connections if x['server_id']==s['id']]
        return out
    @router.get('')
    def listing(request:Request,q:str='',status:str='',server_type:str=''):
        user(request)
        with transaction() as c:
            rows=snapshot(c);sw=switches(c)
        rows=[s for s in rows if (not q or q.casefold() in json.dumps(s,default=str,ensure_ascii=False).casefold()) and (not status or s['status']==status) and (not server_type or s['server_type']==server_type)]
        return {'servers':rows,'switches':sw,'source':'MANUAL','version':6,'can_write':has_permission(auth(request),'discovery.write')}
    @router.post('')
    def add(x:Server,request:Request):
        u=user(request,True)
        with transaction(True) as c:return create(c,normalize_server(x),u['username'])
    @router.get('/{sid}')
    def get(sid:str,request:Request):
        user(request)
        with transaction() as c:
            s=server(c,sid);s['connections']=enrich(c,[dict(r) for r in c.execute('SELECT * FROM server_connections WHERE server_id=? AND NOT deleted',(sid,))]);return s
    @router.put('/{sid}')
    def edit(sid:str,x:Server,request:Request):
        u=user(request,True);d=normalize_server(x)
        with transaction(True) as c:
            old=server(c,sid,True)
            if x.version!=old['version']:raise HTTPException(409,'Server changed; reload before saving')
            unique(c,d,sid);write_nics(c,sid,d['interfaces']);vals=[json.dumps(d[f]) if f=='management_ips' else d[f] for f in fields]
            c.execute('UPDATE server_inventory SET '+','.join(f+'=?' for f in fields)+',updated_by=?,updated_at=now(),version=version+1 WHERE id=?',[*vals,u['username'],sid]);out=server(c,sid);record(c,sid,None,'UPDATE',u['username'],old,out);return out
    @router.delete('/{sid}')
    def delete(sid:str,request:Request,version:int):
        u=user(request,True)
        with transaction(True) as c:
            old=server(c,sid,True)
            if version!=old['version']:raise HTTPException(409,'Server changed; reload')
            old['connections']=[dict(r) for r in c.execute('SELECT * FROM server_connections WHERE server_id=? AND NOT deleted',(sid,))]
            c.execute('UPDATE server_connections SET deleted=true WHERE server_id=?',(sid,));c.execute('UPDATE server_inventory SET deleted=true,updated_by=?,updated_at=now(),version=version+1 WHERE id=?',(u['username'],sid));record(c,sid,None,'DELETE',u['username'],old,None)
        return {'ok':True,'audit_retained':True}
    @router.post('/{sid}/duplicate')
    def duplicate(sid:str,x:Duplicate,request:Request):
        u=user(request,True)
        with transaction(True) as c:
            d=server(c,sid);d['hostname']=x.hostname;d['management_ips']=[];d['interfaces']=[{**n,'id':'','mac':'','ips':[]} for n in d['interfaces']]
            return create(c,normalize_server(Server(**d)),u['username'],'DUPLICATE')
    @router.get('/{sid}/audit')
    def history(sid:str,request:Request):
        user(request)
        with transaction() as c:
            return {'audit':[dict(r) for r in c.execute('SELECT * FROM server_connection_audit WHERE server_id=? ORDER BY id DESC LIMIT 1000',(sid,))]}
    def connection_write(c,sid,x,actor,cid=None):
        server(c,sid,True)
        if not c.execute('SELECT 1 FROM server_network_interfaces WHERE id=? AND server_id=? AND NOT deleted',(x.nic_id,sid)).fetchone():raise HTTPException(422,'Select a server NIC')
        if x.switch_id not in {s['id'] for s in switches(c)}:raise HTTPException(422,'Select a switch from current inventory')
        if x.verification_status not in ('MANUAL','VERIFIED','UNVERIFIED'):raise HTTPException(422,'Invalid verification status')
        if x.vlan_mode not in ('','Access','Trunk','Native','Routed','Unknown'):raise HTTPException(422,'Invalid VLAN mode')
        if not normalize_interface(x.switch_port):raise HTTPException(422,'Invalid switch port')
        old=None
        if cid:
            r=c.execute('SELECT * FROM server_connections WHERE id=? AND server_id=? AND NOT deleted FOR UPDATE',(cid,sid)).fetchone()
            if not r:raise HTTPException(404,'Connection not found')
            old=dict(r)
            if x.version!=old['version']:raise HTTPException(409,'Connection changed; reload')
        d=x.model_dump(exclude={'acknowledge_conflicts','version'});d.update(id=cid or str(uuid.uuid4()),server_id=sid)
        check=enrich(c,[dict(d)])[0]
        if not check['port_exists']:d['verification_status']='UNVERIFIED'
        if cid:c.execute('UPDATE server_connections SET '+','.join(k+'=?' for k in d if k not in ('id','server_id'))+',updated_by=?,updated_at=now(),version=version+1 WHERE id=?',[*[v for k,v in d.items() if k not in ('id','server_id')],actor,cid])
        else:c.execute('INSERT INTO server_connections('+','.join(d)+',created_by,updated_by) VALUES('+','.join('?' for _ in range(len(d)+2))+')',[*d.values(),actor,actor])
        c.execute('UPDATE server_inventory SET updated_by=?,updated_at=now(),version=version+1 WHERE id=?',(actor,sid));out=dict(c.execute('SELECT * FROM server_connections WHERE id=?',(d['id'],)).fetchone());record(c,sid,d['id'],'CONNECTION_UPDATE' if cid else 'CONNECTION_CREATE',actor,old,out);return enrich(c,[out])[0]
    @router.post('/{sid}/connections')
    def add_connection(sid:str,x:Connection,request:Request):
        u=user(request,True)
        with transaction(True) as c:return connection_write(c,sid,x,u['username'])
    @router.put('/{sid}/connections/{cid}')
    def edit_connection(sid:str,cid:str,x:Connection,request:Request):
        u=user(request,True)
        with transaction(True) as c:return connection_write(c,sid,x,u['username'],cid)
    @router.delete('/{sid}/connections/{cid}')
    def delete_connection(sid:str,cid:str,request:Request,version:int):
        u=user(request,True)
        with transaction(True) as c:
            server(c,sid,True);r=c.execute('SELECT * FROM server_connections WHERE id=? AND server_id=? AND NOT deleted FOR UPDATE',(cid,sid)).fetchone()
            if not r:raise HTTPException(404,'Connection not found')
            old=dict(r)
            if version!=old['version']:raise HTTPException(409,'Connection changed; reload')
            c.execute('UPDATE server_connections SET deleted=true,updated_by=?,updated_at=now(),version=version+1 WHERE id=?',(u['username'],cid));c.execute('UPDATE server_inventory SET updated_by=?,updated_at=now(),version=version+1 WHERE id=?',(u['username'],sid));record(c,sid,cid,'CONNECTION_DELETE',u['username'],old,None)
        return {'ok':True}
    app.include_router(router)
    return snapshot
