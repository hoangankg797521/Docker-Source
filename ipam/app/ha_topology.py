"""Hybrid identities and declared HA relationships. Never polls or configures hardware."""
import json,uuid,ipaddress
from contextlib import contextmanager
from urllib.parse import urlparse
from fastapi import APIRouter,HTTPException,Request
from pydantic import BaseModel,Field
from .topology_theme import THEME
from .studio_models import physical_name
from .port_mapping import normalize_interface
class DeviceIn(BaseModel):
 display_name:str=Field(min_length=1,max_length=200)
 device_type:str=Field(default='Firewall',max_length=100)
 management_ip:str=''
 serial_number:str=Field(default='',max_length=200)
 network_device_id:int|None=None
 identity_evidence:str=Field(default='',max_length=2000)
 description:str=Field(default='',max_length=4000)
 version:int=0
class ClusterIn(BaseModel):
 name:str=Field(min_length=1,max_length=200)
 mode:str='Active-Passive'
 cluster_ip:str=''
 cluster_ip_verified:bool=False
 description:str=Field(default='',max_length=4000)
 version:int=0
class MemberIn(BaseModel):
 device_id:int
 configured_role:str='Unknown'
 verify_configured_role:bool=False
 description:str=Field(default='',max_length=4000)
 version:int=0
class ConnectionIn(BaseModel):
 source_device_id:int
 target_device_id:int|None=None
 target_network_id:int|None=None
 kind:str='UPLINK'
 source_port:str=Field(default='',max_length=100)
 target_port:str=Field(default='',max_length=100)
 speed:str=Field(default='',max_length=100)
 media_type:str=Field(default='',max_length=100)
 vlan:str=Field(default='',max_length=500)
 lag:str=Field(default='',max_length=100)
 description:str=Field(default='',max_length=4000)
 verification:str='UNVERIFIED'
 version:int=0

def valid_ip(v):
 if not v:return ''
 try:return str(ipaddress.ip_address(v.strip()))
 except ValueError:raise HTTPException(422,'Invalid IPv4/IPv6 address')
def decoded(v):return json.loads(v) if isinstance(v,str) else v

def snapshot(c):
 devices=[dict(r) for r in c.execute('SELECT * FROM studio_hybrid_devices WHERE NOT deleted ORDER BY display_name')]
 ns={r['id']:dict(r) for r in c.execute('SELECT id,name,management_ip,serial_number,status,last_discovery FROM network_devices')}
 for d in devices:
  snmp=ns.get(d['network_device_id']);d['discovery_type']='Hybrid' if snmp else 'Manual';d['monitoring']='SNMP' if snmp else 'Not Monitored';d['live_role']='Unknown';d['live_status']='Unknown';d['snmp_address']=snmp['management_ip'] if snmp else '';d['original_name']=snmp['name'] if snmp else '';d['last_discovery']=snmp['last_discovery'] if snmp else None
 clusters=[dict(r) for r in c.execute('SELECT * FROM studio_ha_clusters ORDER BY name')]
 members=[dict(r) for r in c.execute('SELECT * FROM studio_ha_members ORDER BY configured_role,id')]
 connections=[dict(r) for r in c.execute('SELECT * FROM studio_ha_connections ORDER BY kind,id')]
 byid={d['id']:d for d in devices};mapped={d['network_device_id']:d for d in devices if d['network_device_id'] is not None}
 for m in members:m['device']=byid.get(m['device_id']);m['live_role']='Unknown';m['live_status']='Unknown'
 for l in connections:
  a=byid[l['source_device_id']];b=byid.get(l['target_device_id']) if l['target_device_id'] is not None else mapped.get(l['target_network_id']) or ns.get(l['target_network_id']);l['source_canonical_id']=a['canonical_id'];l['target_canonical_id']=b.get('canonical_id',b.get('id')) if b else None;l['source_name']=a['display_name'];l['target_name']=b.get('display_name',b.get('name')) if b else 'Unknown';l['ports_known']=bool(l['source_port'] and l['target_port']);l['aggregate_only']=l['ports_known'] and (not physical_name(l['source_port']) or not physical_name(l['target_port']));l['mapping_status']='Aggregate only / Physical ports pending' if l['aggregate_only'] else 'Mapped' if l['ports_known'] else 'Port Mapping Pending';l['oper_status']='Unknown';l['source']='MANUAL';l['computed_verification']=l['verification'];l['conflicting_links']=[]
 for cl in clusters:cl['members']=[m for m in members if m['cluster_id']==cl['id']];cl['connections']=[l for l in connections if l['cluster_id']==cl['id']];cl['live_status']='Unknown'
 return {'devices':devices,'clusters':clusters,'theme':THEME}

def inventory_overlay(c,devices):
 snap=snapshot(c);out=[dict(d) for d in devices];byid={d['id']:d for d in out}
 for h in snap['devices']:
  existing=byid.get(h['network_device_id']);d=dict(existing) if existing else {'id':h['canonical_id'],'name':h['display_name'],'management_ip':h['management_ip'],'device_type':h['device_type'],'vendor':'','status':'Unknown','last_discovery':None,'total_interfaces':0,'metadata':{},'hardware_profile':{'verified':False,'groups':[]}}
  d.update(id=h['canonical_id'],serial_number=h['serial_number'] or d.get('serial_number',''),device_type=h['device_type'],description=h['description'],display_name=h['display_name'],hybrid_identity_id=h['id'],discovery_type=h['discovery_type'],ha_management_ip=h['management_ip'],ha_monitoring=h['monitoring'],ha_live_role='Unknown')
  member=next((m for cl in snap['clusters'] for m in cl['members'] if m['device_id']==h['id']),None)
  if member:d.update(ha_cluster_id=member['cluster_id'],ha_configured_role=member['configured_role'],ha_role_verified_at=member['role_verified_at'])
  if existing:out.remove(existing)
  out.append(d)
 for d in out:
  text=(str(d.get('device_type',''))+' '+str(d.get('name',''))).lower();rank=0 if 'sophos' in text or 'firewall' in text else 1 if 'core' in text else 2 if 'nexus' in text or 'distribution' in text else 3
  d.update(documentation_theme='pastel-v6',diagram_background=THEME['layers'][rank]['background'],diagram_border=THEME['layers'][rank]['border'],diagram_accent=THEME['layers'][rank]['accent'])
 return out

def overlay_mapping(c,result):
 snap=snapshot(c);identities={d['network_device_id']:d['canonical_id'] for d in snap['devices'] if d['network_device_id'] is not None}
 result['ha']=snap;result['ha_pending_mappings']=sum(l['mapping_status']!='Mapped' for cl in snap['clusters'] for l in cl['connections'] if l['kind']=='UPLINK');result['ha_heartbeat_connections']=sum(l['kind']=='HEARTBEAT' for cl in snap['clusters'] for l in cl['connections'])
 for l in result['links']:
  for k in ('source_device_id','target_device_id'):l[k]=identities.get(l.get(k),l.get(k))
 for cl in snap['clusters']:
  for l in cl['connections']:
   # A declaration with no physical ports does not become a discovered/physical cable.
   pair={l['source_canonical_id'],l['target_canonical_id']}
   existing=[e for e in result['links'] if {e['source_device_id'],e['target_device_id']}==pair]
   if l['kind']=='UPLINK' and not l['ports_known'] and existing:
    for e in existing:e['ha_relationship_id']=l['id'];e['ha_mapping_status']=l['mapping_status'];e['ha_cluster_id']=cl['id']
    continue
   physical=l['ports_known'] and not l['aggregate_only'] and l['kind']!='HEARTBEAT'
   def endpoints(edge):
    if edge['source_device_id']==l['source_canonical_id']:return edge.get('source_port'),edge.get('target_port')
    return edge.get('target_port'),edge.get('source_port')
   exact=[edge for edge in existing if l['ports_known'] and tuple(normalize_interface(v) for v in endpoints(edge))==(normalize_interface(l['source_port']),normalize_interface(l['target_port']))]
   if exact:
    proof=[edge.get('verification_status') or edge.get('status') for edge in exact];l['computed_verification']='CONFLICT' if 'CONFLICT' in proof else 'CONFIRMED' if 'CONFIRMED' in proof else l['verification'];l['evidence_links']=[edge['id'] for edge in exact]
    for edge in exact:edge['ha_relationship_id']=l['id'];edge['ha_mapping_status']=l['mapping_status'];edge['ha_cluster_id']=cl['id']
    continue
   conflicts=[]
   if physical:
    for edge in result['links']:
     if edge['link_type']!='PHYSICAL':continue
     for device_id,port,peer_id in ((l['source_canonical_id'],l['source_port'],l['target_canonical_id']),(l['target_canonical_id'],l['target_port'],l['source_canonical_id'])):
      if edge['source_device_id']==device_id and normalize_interface(edge.get('source_port'))==normalize_interface(port):conflicts.append(edge['id'])
      if edge['target_device_id']==device_id and normalize_interface(edge.get('target_port'))==normalize_interface(port):conflicts.append(edge['id'])
   state='CONFLICT' if conflicts else l['verification'];l['computed_verification']=state;l['conflicting_links']=list(dict.fromkeys(conflicts))
   e={'id':'ha:'+l['id'],'source_device_id':l['source_canonical_id'],'target_device_id':l['target_canonical_id'],'source_port':l['source_port'] or None,'target_port':l['target_port'] or None,'link_type':'PHYSICAL' if physical else 'PORT_CHANNEL' if l['aggregate_only'] and l['kind']!='HEARTBEAT' else 'HA_HEARTBEAT' if l['kind']=='HEARTBEAT' else 'MANUAL_PENDING','source':'MANUAL','status':state,'verification_status':state,'conflicting_links':list(dict.fromkeys(conflicts)),'oper_status':'Unknown','ha_cluster_id':cl['id'],'ha_relationship_id':l['id'],'ha_mapping_status':l['mapping_status'],'metadata':{'bandwidth':l['speed'],'media_type':l['media_type'],'vlan':l['vlan'],'port_channel':l['lag'],'description':l['description']},'last_seen':None,'port_channel':l['lag'] or None,'logical_observation':not physical and not l['aggregate_only'],'source_members':[],'target_members':[]}
   result['links'].append(e)
   if physical:result['physical_links']+=1
   if e['link_type']=='PORT_CHANNEL':
    result['port_channels']=result.get('port_channels',0)+1
    if state not in ('CONFIRMED','VERIFIED'):result['unverified_port_channels']=result.get('unverified_port_channels',0)+1
   if conflicts:result['conflict_links']=result.get('conflict_links',0)+1
 roles={}
 for d in inventory_overlay(c,[dict(r) for r in c.execute('SELECT id,name,device_type,sys_descr FROM network_devices')]):
  text=(str(d.get('device_type',''))+' '+str(d.get('name',''))).lower()
  roles[d['id']]='firewall' if 'firewall' in text or 'sophos' in text else 'core' if 'core' in text else 'distribution' if 'nexus' in text or 'distribution' in text else 'access'
 for e in result['links']:
  a,b=roles.get(e['source_device_id']),roles.get(e['target_device_id']);kind='heartbeat' if e['link_type']=='HA_HEARTBEAT' else 'unknown' if e['link_type'] in ('MANUAL_PENDING','LOGICAL_OBSERVATION') or e.get('ha_mapping_status')=='Port Mapping Pending' else 'firewall' if 'firewall' in (a,b) else 'core' if 'core' in (a,b) else 'distribution' if 'distribution' in (a,b) else 'access'
  e['diagram_color']=THEME['links'][kind];e['diagram_style']='dashed' if kind in ('heartbeat','unknown') else 'solid';e['source_layer']=a;e['target_layer']=b
 return result

def register(app,db,auth,has_permission,audit,mapping_provider=None):
 router=APIRouter(prefix='/api/studio/ha',tags=['Hybrid Device / HA Management'])
 def user(r,write=False):
  u=auth(r)
  if not has_permission(u,'discovery.write' if write else 'discovery.read'):raise HTTPException(403,'Missing discovery permission')
  if write and r.headers.get('origin') and urlparse(r.headers['origin']).netloc!=r.headers.get('host'):raise HTTPException(403,'Cross-origin write rejected')
  return u
 @contextmanager
 def tx(write=False):
  c=db()
  try:
   if write:c.execute('SELECT pg_advisory_xact_lock(620007)')
   yield c;c.commit()
  except Exception:c.rollback();raise
  finally:c.close()
 def log(c,typ,eid,action,actor,before,after):
  c.execute('INSERT INTO studio_ha_audit(entity_type,entity_id,action,actor,before_state,after_state) VALUES(?,?,?,?,?,?)',(typ,str(eid),action,actor,json.dumps(before,default=str) if before else None,json.dumps(after,default=str) if after else None));audit('HA_'+action,str(eid),actor,conn=c)
 def row(c,table,rid,lock=False):
  r=c.execute('SELECT * FROM '+table+' WHERE id=?'+(' FOR UPDATE' if lock else ''),(rid,)).fetchone()
  if not r:raise HTTPException(404,'Record not found')
  return dict(r)
 def device_write(c,x,actor,did=None):
  d=x.model_dump(exclude={'version'});d['management_ip']=valid_ip(d['management_ip']);d['display_name']=d['display_name'].strip();d['serial_number']=d['serial_number'].strip()
  if not d['display_name']:raise HTTPException(422,'Display name required')
  if d['network_device_id'] is not None:
   if not d['identity_evidence'].strip():raise HTTPException(422,'Mapping SNMP identity requires explicit verification evidence')
   if not c.execute('SELECT 1 FROM network_devices WHERE id=?',(d['network_device_id'],)).fetchone():raise HTTPException(422,'SNMP device not found')
   if c.execute('SELECT 1 FROM studio_hybrid_devices WHERE network_device_id=? AND id<>? AND NOT deleted',(d['network_device_id'],did or 0)).fetchone():raise HTTPException(409,'SNMP device already mapped')
  if d['serial_number'] and c.execute('SELECT 1 FROM studio_hybrid_devices WHERE lower(serial_number)=lower(?) AND id<>? AND NOT deleted',(d['serial_number'],did or 0)).fetchone():raise HTTPException(409,'Serial already assigned')
  if c.execute('SELECT 1 FROM studio_hybrid_devices WHERE lower(display_name)=lower(?) AND id<>? AND NOT deleted',(d['display_name'],did or 0)).fetchone():raise HTTPException(409,'Display name already exists')
  old=row(c,'studio_hybrid_devices',did,True) if did is not None else None
  if old and old['version']!=x.version:raise HTTPException(409,'Device changed; reload')
  if old and old['canonical_id']>0 and d['network_device_id']!=old['network_device_id']:raise HTTPException(409,'Established SNMP identity cannot be reassigned or detached; its canonical inventory ID must remain stable')
  if old:c.execute('UPDATE studio_hybrid_devices SET '+','.join(k+'=?' for k in d)+',updated_by=?,updated_at=now(),version=version+1 WHERE id=?',[*d.values(),actor,did])
  else:
   did=c.execute("SELECT nextval('studio_hybrid_device_seq') id").fetchone()['id'];canonical=d['network_device_id'] or did
   c.execute('INSERT INTO studio_hybrid_devices(id,canonical_id,'+','.join(d)+',created_by,updated_by) VALUES('+','.join('?' for _ in range(len(d)+4))+')',[did,canonical,*d.values(),actor,actor])
  out=row(c,'studio_hybrid_devices',did);log(c,'device',did,'DEVICE_UPDATE' if old else 'DEVICE_CREATE',actor,old,out);return out
 @router.get('')
 def get(request:Request):
  u=user(request)
  if mapping_provider:s=mapping_provider(request)['ha']
  else:
   with tx() as c:s=snapshot(c)
  return {**s,'can_write':has_permission(u,'discovery.write')}
 @router.post('/devices')
 def add_device(x:DeviceIn,request:Request):
  u=user(request,True)
  with tx(True) as c:return device_write(c,x,u['username'])
 @router.put('/devices/{did}')
 def edit_device(did:int,x:DeviceIn,request:Request):
  u=user(request,True)
  with tx(True) as c:return device_write(c,x,u['username'],did)
 @router.post('/clusters')
 def add_cluster(x:ClusterIn,request:Request):
  u=user(request,True)
  with tx(True) as c:
   if x.mode not in ('Active-Passive','Active-Active','Other'):raise HTTPException(422,'Invalid HA mode')
   if c.execute('SELECT 1 FROM studio_ha_clusters WHERE lower(name)=lower(?)',(x.name.strip(),)).fetchone():raise HTTPException(409,'Cluster already exists')
   cid=str(uuid.uuid4());d=x.model_dump(exclude={'version'});d['cluster_ip']=valid_ip(d['cluster_ip']);d['name']=d['name'].strip()
   if not d['name']:raise HTTPException(422,'Cluster name required')
   c.execute('INSERT INTO studio_ha_clusters(id,'+','.join(d)+',created_by,updated_by) VALUES('+','.join('?' for _ in range(len(d)+3))+')',[cid,*d.values(),u['username'],u['username']]);out=row(c,'studio_ha_clusters',cid);log(c,'cluster',cid,'CLUSTER_CREATE',u['username'],None,out);return out
 @router.put('/clusters/{cid}')
 def edit_cluster(cid:str,x:ClusterIn,request:Request):
  u=user(request,True)
  with tx(True) as c:
   old=row(c,'studio_ha_clusters',cid,True)
   if old['version']!=x.version:raise HTTPException(409,'Cluster changed; reload')
   if x.mode not in ('Active-Passive','Active-Active','Other'):raise HTTPException(422,'Invalid mode')
   if c.execute('SELECT 1 FROM studio_ha_clusters WHERE lower(name)=lower(?) AND id<>?',(x.name.strip(),cid)).fetchone():raise HTTPException(409,'Cluster already exists')
   d=x.model_dump(exclude={'version'});d['cluster_ip']=valid_ip(d['cluster_ip']);d['name']=d['name'].strip()
   if not d['name']:raise HTTPException(422,'Cluster name required')
   c.execute('UPDATE studio_ha_clusters SET '+','.join(k+'=?' for k in d)+',updated_by=?,updated_at=now(),version=version+1 WHERE id=?',[*d.values(),u['username'],cid]);out=row(c,'studio_ha_clusters',cid);log(c,'cluster',cid,'CLUSTER_UPDATE',u['username'],old,out);return out
 def member_write(c,cid,x,actor,mid=None):
  row(c,'studio_ha_clusters',cid);row(c,'studio_hybrid_devices',x.device_id)
  if x.configured_role not in ('Active','Passive','Unknown'):raise HTTPException(422,'Invalid configured role')
  if c.execute('SELECT 1 FROM studio_ha_members WHERE device_id=? AND id<>?',(x.device_id,mid or '')).fetchone():raise HTTPException(409,'Device already belongs to an HA cluster')
  old=row(c,'studio_ha_members',mid,True) if mid else None
  if old and (old['cluster_id']!=cid or old['version']!=x.version):raise HTTPException(409,'Member changed; reload')
  if old and old['device_id']!=x.device_id and c.execute('SELECT 1 FROM studio_ha_connections WHERE source_device_id=? OR target_device_id=?',(old['device_id'],old['device_id'])).fetchone():raise HTTPException(409,'Member has connections; edit its device information instead of replacing its identity')
  mid=mid or str(uuid.uuid4());verified=c.execute('SELECT now() verified').fetchone()['verified'] if x.verify_configured_role else None
  if old:c.execute('UPDATE studio_ha_members SET device_id=?,configured_role=?,description=?,role_verified_at=?,role_verified_by=?,version=version+1 WHERE id=?',(x.device_id,x.configured_role,x.description,verified,actor if verified else None,mid))
  else:c.execute('INSERT INTO studio_ha_members(id,cluster_id,device_id,configured_role,description,role_verified_at,role_verified_by) VALUES(?,?,?,?,?,?,?)',(mid,cid,x.device_id,x.configured_role,x.description,verified,actor if verified else None))
  out=row(c,'studio_ha_members',mid);log(c,'member',mid,'MEMBER_UPDATE' if old else 'MEMBER_CREATE',actor,old,out);return out
 @router.post('/clusters/{cid}/members')
 def add_member(cid:str,x:MemberIn,request:Request):
  u=user(request,True)
  with tx(True) as c:return member_write(c,cid,x,u['username'])
 @router.put('/clusters/{cid}/members/{mid}')
 def edit_member(cid:str,mid:str,x:MemberIn,request:Request):
  u=user(request,True)
  with tx(True) as c:return member_write(c,cid,x,u['username'],mid)
 def connection_write(c,cid,x,actor,lid=None):
  row(c,'studio_ha_clusters',cid);source_identity=row(c,'studio_hybrid_devices',x.source_device_id)
  if x.target_network_id is not None and x.target_network_id==source_identity['network_device_id']:raise HTTPException(422,'A mapped identity cannot connect to itself')
  if not c.execute('SELECT 1 FROM studio_ha_members WHERE cluster_id=? AND device_id=?',(cid,x.source_device_id)).fetchone():raise HTTPException(422,'Source must be a cluster member')
  if (x.target_device_id is None)==(x.target_network_id is None):raise HTTPException(422,'Choose exactly one destination')
  if x.target_device_id is not None:
   row(c,'studio_hybrid_devices',x.target_device_id)
   if x.target_device_id==x.source_device_id:raise HTTPException(422,'Self link rejected')
  elif not c.execute('SELECT 1 FROM network_devices WHERE id=?',(x.target_network_id,)).fetchone():raise HTTPException(422,'Destination inventory device not found')
  if x.kind not in ('UPLINK','HEARTBEAT','OTHER') or x.verification not in ('MANUAL','VERIFIED','UNVERIFIED'):raise HTTPException(422,'Invalid connection option')
  for field in ('source_port','target_port'):
   port=getattr(x,field).strip();setattr(x,field,'' if port.casefold() in ('unknown','unverified','pending','port mapping pending','n/a','none','-','—','?') else port)
  if x.verification=='VERIFIED' and not (x.source_port.strip() and x.target_port.strip()):raise HTTPException(422,'Verified port mapping requires both actual ports')
  old=row(c,'studio_ha_connections',lid,True) if lid else None
  if old and (old['cluster_id']!=cid or old['version']!=x.version):raise HTTPException(409,'Connection changed; reload')
  for other in c.execute('SELECT * FROM studio_ha_connections WHERE cluster_id=? AND id<>?',(cid,lid or '')):
   if other['source_device_id']==x.source_device_id and other['target_device_id']==x.target_device_id and other['target_network_id']==x.target_network_id and other['kind']==x.kind and normalize_interface(other['source_port'])==normalize_interface(x.source_port) and normalize_interface(other['target_port'])==normalize_interface(x.target_port):raise HTTPException(409,'Exact HA mapping already exists; edit it or use distinct actual ports for parallel links')
  d=x.model_dump(exclude={'version'});d['source_port']=d['source_port'].strip();d['target_port']=d['target_port'].strip();lid=lid or str(uuid.uuid4())
  if old:c.execute('UPDATE studio_ha_connections SET '+','.join(k+'=?' for k in d)+',updated_by=?,updated_at=now(),version=version+1 WHERE id=?',[*d.values(),actor,lid])
  else:c.execute('INSERT INTO studio_ha_connections(id,cluster_id,'+','.join(d)+',created_by,updated_by) VALUES('+','.join('?' for _ in range(len(d)+4))+')',[lid,cid,*d.values(),actor,actor])
  out=row(c,'studio_ha_connections',lid);log(c,'connection',lid,'CONNECTION_UPDATE' if old else 'CONNECTION_CREATE',actor,old,out);return out
 @router.post('/clusters/{cid}/connections')
 def add_connection(cid:str,x:ConnectionIn,request:Request):
  u=user(request,True)
  with tx(True) as c:return connection_write(c,cid,x,u['username'])
 @router.put('/clusters/{cid}/connections/{lid}')
 def edit_connection(cid:str,lid:str,x:ConnectionIn,request:Request):
  u=user(request,True)
  with tx(True) as c:return connection_write(c,cid,x,u['username'],lid)
 @router.get('/audit')
 def history(request:Request):
  user(request)
  with tx() as c:return {'audit':[dict(r) for r in c.execute('SELECT * FROM studio_ha_audit ORDER BY id DESC LIMIT 1000')]}
 @router.post('/bootstrap-sophos')
 def bootstrap(request:Request):
  u=user(request,True);actor=u['username']
  with tx(True) as c:
   name='SOPHOS FIREWALL HA CLUSTER'
   existing=c.execute('SELECT id FROM studio_ha_clusters WHERE name=?',(name,)).fetchone()
   if existing:return snapshot(c)
   candidates=[dict(r) for r in c.execute('SELECT id,name,management_ip,device_type,sys_descr FROM network_devices WHERE management_ip=?',('172.16.10.254',)) if 'sophos' in (r['name']+' '+r['device_type']+' '+r['sys_descr']).lower()]
   if len(candidates)!=1:raise HTTPException(409,'Cannot uniquely map existing Sophos SNMP identity; choose it in HA Management')
   cores=[]
   for core_name in ('Core-Primary','Core-Secondary'):
    rows=[dict(r) for r in c.execute('SELECT id FROM network_devices WHERE name=?',(core_name,))]
    if len(rows)!=1:raise HTTPException(409,'Core identity is ambiguous: '+core_name)
    cores.append(rows[0]['id'])
   cid=str(uuid.uuid4());c.execute('INSERT INTO studio_ha_clusters(id,name,mode,cluster_ip,description,created_by,updated_by) VALUES(?,?,?,?,?,?,?)',(cid,name,'Active-Passive','172.16.10.254','HA membership / four core relationships declared by IT. Cluster-IP ownership and live roles remain unverified.',actor,actor));log(c,'cluster',cid,'CLUSTER_CREATE',actor,None,row(c,'studio_ha_clusters',cid))
   members=[]
   for suffix,role,network_id in (('01','Active',candidates[0]['id']),('02','Passive',None)):
    d=device_write(c,DeviceIn(display_name='Sophos FW-'+suffix,network_device_id=network_id,identity_evidence='Existing IPAM Sophos SNMP identity; HA membership declared by IT' if network_id else '',description='Configured '+role+'; live role Unknown until supported by verified evidence'),actor)
    member_write(c,cid,MemberIn(device_id=d['id'],configured_role=role),actor);members.append(d)
    for target in cores:connection_write(c,cid,ConnectionIn(source_device_id=d['id'],target_network_id=target),actor)
   connection_write(c,cid,ConnectionIn(source_device_id=members[0]['id'],target_device_id=members[1]['id'],kind='HEARTBEAT',description='Declared HA synchronization relationship; physical ports and live status Unknown'),actor)
   return snapshot(c)
 app.include_router(router)
