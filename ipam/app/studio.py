"""Additive Topology Studio. No writes to legacy topology, DHCP or discovery tables."""
import csv
import io
import json
import math
import os
import re
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from .studio_models import hardware_profile, port_media, physical_name, port_key, classify_interface
from .port_mapping import correlate as correlate_port_mappings, normalize_interface

VERSION = '2.0.4'
META_FIELDS = {'model','location','rack','function','role','zone','description','notes','port_count','building','floor','department','custom_group','vlan','subnet'}
LINK_FIELDS = {'cable_type','cable_label','bandwidth','vlan','port_channel','description','notes','link_type','status','remote_hostname','remote_ip','remote_interface','function'}
NODE_FIELDS = {'id','device_id','name','kind','x','y','width','height','color','group','notes','detail_diagram_id','label_position'}
EDGE_FIELDS = {'id','source','target','label','color','style','link_type','vlan','bandwidth','notes','physical_link_id','data_source','last_seen','legacy_link_id','route'}

class DiagramIn(BaseModel):
    title: str = Field(min_length=1,max_length=160)
    kind: str = 'logical'
    document: dict = Field(default_factory=dict)
    revision: int = 0

class LinkIn(BaseModel):
    source_device_id: int
    source_port: str = Field(min_length=1,max_length=100)
    target_device_id: int
    target_port: str = Field(min_length=1,max_length=100)
    metadata: dict = Field(default_factory=dict)
    source: str = 'MANUAL'

class MetadataIn(BaseModel):
    metadata: dict
    source: str = 'MANUAL'


def clean_fields(value, allowed):
    if not isinstance(value,dict):
        raise HTTPException(422,'Object required')
    out={}
    for k,v in value.items():
        if k not in allowed: continue
        if not isinstance(v,(str,int,float,bool,type(None))):
            raise HTTPException(422,'Scalar field required: '+k)
        if isinstance(v,str) and len(v)>4000: raise HTTPException(422,'Field too long: '+k)
        if isinstance(v,float) and not math.isfinite(v): raise HTTPException(422,'Nonfinite number')
        out[k]=v
    return out


def validate_document(doc, device_ids):
    if len(json.dumps(doc))>4_000_000: raise HTTPException(413,'Diagram exceeds 4 MB')
    nodes=doc.get('nodes',[]); edges=doc.get('edges',[])
    if not isinstance(nodes,list) or not isinstance(edges,list) or len(nodes)>1000 or len(edges)>10000:
        raise HTTPException(422,'Maximum 1000 nodes / 10000 edges')
    ids=set(); normalized=[]
    for n in nodes:
        n=clean_fields(n,NODE_FIELDS)
        nid=n.get('id')
        if not isinstance(nid,str) or not nid or len(nid)>100 or nid in ids: raise HTTPException(422,'Duplicate or invalid node ID')
        ids.add(nid)
        did=n.get('device_id')
        if did is not None:
            if type(did) is not int or did not in device_ids: raise HTTPException(422,'Missing inventory device')
            # Shared identity is resolved at read/export time, never snapshotted in a node.
            n.pop('name',None)
        for field in ('x','y'):
            v=n.get(field,0)
            if not isinstance(v,(int,float)) or not math.isfinite(v) or abs(v)>1_000_000: raise HTTPException(422,'Invalid position')
            n[field]=v
        normalized.append(n)
    edge_ids=set(); normalized_edges=[]
    for e in edges:
        e=clean_fields(e,EDGE_FIELDS)
        eid=e.get('id')
        if not isinstance(eid,str) or not eid or eid in edge_ids: raise HTTPException(422,'Duplicate or invalid edge ID')
        edge_ids.add(eid)
        if e.get('source') not in ids or e.get('target') not in ids or e.get('source')==e.get('target'): raise HTTPException(422,'Invalid edge endpoints')
        normalized_edges.append(e)
    viewport=doc.get('viewport',{})
    viewport=clean_fields(viewport,{'x','y','zoom','grid','layout','view_mode','show_labels','edge_grouping','color_by','manual_positions','direction','node_style','node_size','group_by','presentation_theme'})
    choices={'direction':('tb','lr'),'node_style':('compact','standard','detailed'),'node_size':('0.9','1','1.2','1.5'),'group_by':('role','building','floor','department','vlan','subnet','custom_group')}
    for key,values in choices.items():
        if key in viewport and viewport[key] not in values:raise HTTPException(422,'Invalid viewport option: '+key)
    return {'nodes':normalized,'edges':normalized_edges,'viewport':viewport}


def register(app, db, auth, has_permission, audit):
    router=APIRouter(prefix='/api/studio')
    from .server_connections import register as register_servers
    server_snapshot=register_servers(app,db,auth,has_permission,audit)
    from .ha_topology import register as register_ha, inventory_overlay, overlay_mapping, snapshot as ha_snapshot
    register_ha(app,db,auth,has_permission,audit,lambda req:port_mapping(req))

    def user(request,write=False):
        row=auth(request)
        if not has_permission(row,'discovery.write' if write else 'discovery.read'):
            raise HTTPException(403,'Missing discovery permission')
        if write and request.headers.get('origin') and request.headers['origin'].rstrip('/')!=str(request.base_url).rstrip('/'):
            # Reverse proxies may use http upstream; compare host, while cookie SameSite remains enforced.
            from urllib.parse import urlparse
            if urlparse(request.headers['origin']).netloc!=request.headers.get('host'):
                raise HTTPException(403,'Cross-origin write rejected')
        return row

    @contextmanager
    def connection():
        c=db()
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback(); raise
        finally: c.close()

    def dictionary(row):
        return dict(row) if row else None

    def doc_value(value):
        return json.loads(value) if isinstance(value,str) else value

    def diagram(c,did,lock=False):
        r=c.execute('SELECT * FROM studio_diagrams WHERE id=? AND NOT deleted'+(' FOR UPDATE' if lock else ''),(did,)).fetchone()
        if not r: raise HTTPException(404,'Diagram not found')
        r=dict(r); r['document']=doc_value(r['document']); return r

    def validate(c,doc):
        ids={int(r[0]) for r in c.execute('SELECT id FROM network_devices').fetchall()}
        ids.update(int(r[0]) for r in c.execute('SELECT canonical_id FROM studio_hybrid_devices WHERE NOT deleted'))
        return validate_document(doc,ids)

    def snapshot(c,r,actor):
        c.execute('INSERT INTO studio_revisions(diagram_id,revision,title,document,actor) VALUES(?,?,?,?,?)',
                  (r['id'],r['revision'],r['title'],json.dumps(r['document']),actor))

    @router.get('/health')
    def health(request:Request):
        user(request)
        with connection() as c: c.execute('SELECT id FROM studio_diagrams LIMIT 1')
        return {'ok':True,'version':VERSION,
                'advanced_views':os.getenv('IPAM_STUDIO_ADVANCED_VIEWS','0')=='1',
                'hierarchical_views':os.getenv('IPAM_STUDIO_ADVANCED_VIEWS','0')=='1'}

    @router.get('/features')
    def features(request:Request):
        user(request)
        enabled=os.getenv('IPAM_STUDIO_ADVANCED_VIEWS','0')=='1'
        return {'hierarchical_views':enabled,'multi_link_routing':enabled,'multi_link_data':True}

    @router.get('/inventory')
    def inventory(request:Request):
        user(request)
        with connection() as c:
            devices=[dict(r) for r in c.execute('SELECT d.id,d.name,d.management_ip,d.vendor,d.device_type,d.sys_descr,d.sys_object_id,d.serial_number,d.status,d.last_discovery,d.updated_at,(SELECT count(*) FROM network_interfaces i WHERE i.device_id=d.id) total_interfaces FROM network_devices d ORDER BY d.name').fetchall()]
            meta={int(r['device_id']):dict(r) for r in c.execute('SELECT * FROM studio_device_metadata').fetchall()}
            for d in devices:
                m=meta.get(d['id'],{}); d['metadata']=doc_value(m.get('metadata',{})); d['metadata_source']=m.get('source'); d['verified_at']=m.get('verified_at'); d['hardware_profile']=hardware_profile(d,d['metadata'],d['metadata_source'])
            devices=inventory_overlay(c,devices)
        return {'devices':devices,'source':'HYBRID','note':'Model and port layout are not inferred from hostname.'}

    @router.put('/inventory/{did}/metadata')
    def metadata(did:int,x:MetadataIn,request:Request):
        u=user(request,True)
        if x.source not in ('MANUAL','VERIFIED'): raise HTTPException(422,'Invalid source')
        with connection() as c:
            if not c.execute('SELECT id FROM network_devices WHERE id=?',(did,)).fetchone(): raise HTTPException(404,'Device not found')
            c.execute('INSERT INTO studio_device_metadata(device_id,metadata,source,verified_at) VALUES(?,?,?,?) ON CONFLICT(device_id) DO UPDATE SET metadata=excluded.metadata,source=excluded.source,verified_at=excluded.verified_at,updated_at=now()',
                (did,json.dumps(clean_fields(x.metadata,META_FIELDS)),x.source,datetime.now(timezone.utc) if x.source=='VERIFIED' else None))
            audit('STUDIO_DEVICE_METADATA',str(did),u['username'],conn=c)
        return {'ok':True}

    @router.get('/inventory/{did}/ports')
    def ports(did:int,request:Request):
        user(request)
        with connection() as c:
            hybrid=c.execute('SELECT network_device_id FROM studio_hybrid_devices WHERE canonical_id=? AND NOT deleted',(did,)).fetchone()
            if hybrid:
                if hybrid['network_device_id'] is None:return {'ports':[],'source':'MANUAL','hardware_profile':{'verified':False,'groups':[]},'layout':'Not Monitored / Unknown; no interfaces fabricated.'}
                did=hybrid['network_device_id']
            if not c.execute('SELECT id FROM network_devices WHERE id=?',(did,)).fetchone(): raise HTTPException(404,'Device not found')
            rows=[dict(r) for r in c.execute('SELECT if_index,if_name,if_descr,admin_status,oper_status,speed_bps,last_seen FROM network_interfaces WHERE device_id=? ORDER BY if_index',(did,)).fetchall()]
        with connection() as c:
            d=dict(c.execute('SELECT sys_descr,sys_object_id FROM network_devices WHERE id=?',(did,)).fetchone())
            m=c.execute('SELECT metadata,source FROM studio_device_metadata WHERE device_id=?',(did,)).fetchone()
            profile=hardware_profile(d,doc_value(m['metadata']) if m else {},m['source'] if m else None)
            annotations={r['if_name']:dict(r) for r in c.execute('SELECT * FROM studio_port_metadata WHERE device_id=?',(did,)).fetchall()}
            indexes=sorted({int(p['if_index']) for p in rows if p.get('if_index') is not None})
            mac_by_if={i:[] for i in indexes};arp_by_if={i:[] for i in indexes}
            if indexes:
                marks=','.join('?' for _ in indexes)
                # Bound response memory per interface; a trunk can learn many MACs.
                for r in c.execute(f'SELECT if_index,mac,vlan_id,last_seen FROM (SELECT if_index,mac,vlan_id,last_seen,ROW_NUMBER() OVER (PARTITION BY if_index ORDER BY last_seen DESC) AS row_num FROM network_mac_entries WHERE device_id=? AND if_index IN ({marks})) recent WHERE row_num<=100 ORDER BY if_index,last_seen DESC',(did,*indexes)).fetchall():
                    item=dict(r);mac_by_if.setdefault(int(item['if_index']),[]).append(item)
                for r in c.execute(f'SELECT if_index,ip,mac,last_seen FROM (SELECT if_index,ip,mac,last_seen,ROW_NUMBER() OVER (PARTITION BY if_index ORDER BY last_seen DESC) AS row_num FROM network_arp_entries WHERE device_id=? AND if_index IN ({marks})) recent WHERE row_num<=100 ORDER BY if_index,last_seen DESC',(did,*indexes)).fetchall():
                    item=dict(r);arp_by_if.setdefault(int(item['if_index']),[]).append(item)
        for p in rows:
            # RFC 2863 ifSpeed Gauge32 saturation is a sentinel, not a real 4.29-Gbps link.
            if p.get('speed_bps')==4294967295:
                p['speed_bps']=None;p['speed_note']='SNMP ifSpeed saturated; actual speed is unknown until ifHighSpeed is available'
            a=annotations.get(p['if_name'],{});p['annotation']=doc_value(a.get('metadata',{}));p['annotation_source']=a.get('source');p['annotation_updated_at']=a.get('updated_at');p['verified_at']=a.get('verified_at')
            kind=classify_interface(p['if_name'])
            position=p['annotation'].get('physical_port_number')
            manual_position=position is not None and p['annotation_source']=='VERIFIED'
            if kind=='unknown' and manual_position: kind='physical'
            p['interface_kind']=kind
            p['physical']=kind=='physical'
            p['media']=p['annotation'].get('media') or port_media(p['if_name'],profile)
            # Position is derived from the interface's own Cisco port label, never ifIndex.
            # Manual position overrides are stored in the existing audited metadata JSON.
            match=re.fullmatch(r'(?:Ethernet|Eth|Et|E|GigabitEthernet|Gig|Gi|TenGigabitEthernet|TenGigE|Te|FastEthernet|Fa|TwentyFiveGigE|Twe|FortyGigabitEthernet|Fo|HundredGigabitEthernet|Hu)(\d+(?:/\d+){0,3})',p['if_name'],re.I)
            if position is None and kind=='physical' and match and len(match.group(1).split('/'))<=2:
                position=int(match.group(1).split('/')[-1])
            p['physical_port_number']=position if kind=='physical' else None
            p['mapping_source']='MANUAL' if p['annotation'].get('physical_port_number') is not None and p['annotation_source']=='VERIFIED' else (profile.get('evidence') or 'OBSERVED_INTERFACE_NAME')
            p['mapping_verified']=bool(kind=='physical' and (profile.get('verified') or p['mapping_source']=='MANUAL'))
            p['mac_addresses']=mac_by_if.get(int(p['if_index']),[])[:100]
            p['ip_addresses']=arp_by_if.get(int(p['if_index']),[])[:100]
        return {'ports':rows,'source':'DISCOVERED','hardware_profile':profile,'layout':'Only observed, explicitly named physical interfaces are rendered; ifIndex and numeric ifName values are never treated as panel positions.'}

    @router.put('/inventory/{did}/ports/{if_name:path}')
    def annotate_port(did:int,if_name:str,x:MetadataIn,request:Request):
        u=user(request,True)
        if x.source not in ('MANUAL','VERIFIED'): raise HTTPException(422,'Invalid source')
        fields={'media','vlan','port_mode','remote_hostname','remote_ip','remote_interface','function','bandwidth','cable_label','description','notes','physical_port_number'}
        values=clean_fields(x.metadata,fields)
        if values.get('media') and values['media'] not in ('Unknown','RJ45','SFP','SFP+','SFP28','QSFP+','QSFP28','Management','Console'): raise HTTPException(422,'Invalid media')
        if values.get('physical_port_number') is not None:
            try: values['physical_port_number']=int(values['physical_port_number'])
            except (TypeError,ValueError): raise HTTPException(422,'Physical port number must be an integer')
            if not 1<=values['physical_port_number']<=512: raise HTTPException(422,'Physical port number out of range')
            if x.source!='VERIFIED': raise HTTPException(422,'Physical position override requires VERIFIED source')
        with connection() as c:
            if not c.execute('SELECT 1 FROM network_interfaces WHERE device_id=? AND if_name=?',(did,if_name)).fetchone(): raise HTTPException(404,'Interface not found')
            if values.get('physical_port_number') is not None and classify_interface(if_name) not in ('physical','unknown'):
                raise HTTPException(422,'Logical and management interfaces cannot map to a chassis port')
            c.execute('INSERT INTO studio_port_metadata(device_id,if_name,metadata,source,verified_at) VALUES(?,?,?,?,?) ON CONFLICT(device_id,if_name) DO UPDATE SET metadata=excluded.metadata,source=excluded.source,verified_at=excluded.verified_at,updated_at=now()',
              (did,if_name,json.dumps(values),x.source,datetime.now(timezone.utc) if x.source=='VERIFIED' else None))
            audit('STUDIO_PORT_ANNOTATION',str(did)+':'+if_name,u['username'],conn=c)
        return {'ok':True}


    @router.get('/diagrams')
    def diagrams(request:Request,include_archived:bool=False):
        user(request)
        with connection() as c:
            return [dict(r) for r in c.execute("SELECT id,title,kind,revision,updated_at,deleted,jsonb_array_length(document->'nodes') node_count FROM studio_diagrams WHERE (? OR NOT deleted) ORDER BY updated_at DESC",(include_archived,)).fetchall()]

    @router.post('/diagrams')
    def create(x:DiagramIn,request:Request):
        u=user(request,True)
        if x.kind not in ('logical','physical') or not x.title.strip(): raise HTTPException(422,'Invalid title or kind')
        with connection() as c:
            r={'id':str(uuid.uuid4()),'title':x.title.strip(),'kind':x.kind,'revision':1,'document':validate(c,x.document)}
            c.execute('INSERT INTO studio_diagrams(id,title,kind,document) VALUES(?,?,?,?)',(r['id'],r['title'],r['kind'],json.dumps(r['document'])))
            snapshot(c,r,u['username']); audit('STUDIO_CREATE',r['id'],r['title'],conn=c)
        return r

    @router.get('/diagrams/{did}')
    def get(did:str,request:Request):
        user(request)
        with connection() as c: return diagram(c,did)

    @router.put('/diagrams/{did}')
    def save(did:str,x:DiagramIn,request:Request):
        u=user(request,True)
        if not x.title.strip(): raise HTTPException(422,'Title required')
        with connection() as c:
            old=diagram(c,did,True)
            if x.revision!=old['revision']: raise HTTPException(409,'Diagram changed in another session; reload before saving')
            if x.kind!=old['kind']: raise HTTPException(422,'Diagram kind is immutable')
            r={**old,'title':x.title.strip(),'document':validate(c,x.document),'revision':old['revision']+1}
            c.execute('UPDATE studio_diagrams SET title=?,document=?,revision=?,updated_at=now() WHERE id=?',(r['title'],json.dumps(r['document']),r['revision'],did))
            snapshot(c,r,u['username']); audit('STUDIO_SAVE',did,str(r['revision']),conn=c)
        return r

    @router.delete('/diagrams/{did}')
    def remove(did:str,request:Request):
        user(request,True)
        with connection() as c:
            diagram(c,did,True); c.execute('UPDATE studio_diagrams SET deleted=true,updated_at=now() WHERE id=?',(did,)); audit('STUDIO_ARCHIVE',did,'Recoverable soft delete',conn=c)
        return {'ok':True}

    @router.post('/diagrams/{did}/unarchive')
    def unarchive(did:str,request:Request):
        user(request,True)
        with connection() as c:
            if not c.execute('SELECT id FROM studio_diagrams WHERE id=? FOR UPDATE',(did,)).fetchone(): raise HTTPException(404,'Diagram not found')
            c.execute('UPDATE studio_diagrams SET deleted=false,updated_at=now() WHERE id=?',(did,));audit('STUDIO_UNARCHIVE',did,'Restored to active library',conn=c)
            return diagram(c,did)

    @router.get('/diagrams/{did}/revisions')
    def revisions(did:str,request:Request):
        user(request)
        with connection() as c:
            diagram(c,did)
            return [dict(r) for r in c.execute('SELECT revision,title,actor,created_at FROM studio_revisions WHERE diagram_id=? ORDER BY revision DESC',(did,)).fetchall()]

    @router.post('/diagrams/{did}/restore/{revision}')
    def restore(did:str,revision:int,request:Request,expected_revision:int):
        u=user(request,True)
        with connection() as c:
            old=diagram(c,did,True)
            if expected_revision!=old['revision']: raise HTTPException(409,'Revision conflict')
            row=c.execute('SELECT title,document FROM studio_revisions WHERE diagram_id=? AND revision=?',(did,revision)).fetchone()
            if not row: raise HTTPException(404,'Revision not found')
            document=validate(c,doc_value(row['document']))
            r={**old,'revision':old['revision']+1,'title':row['title'],'document':document}
            c.execute('UPDATE studio_diagrams SET title=?,document=?,revision=?,updated_at=now() WHERE id=?',(r['title'],json.dumps(document),r['revision'],did))
            snapshot(c,r,u['username']); audit('STUDIO_RESTORE',did,str(revision),conn=c)
        return r

    def links(c):
        out=[]
        for r in c.execute('SELECT l.*,a.device_id source_device_id,a.if_name source_port,b.device_id target_device_id,b.if_name target_port FROM studio_links l JOIN studio_link_endpoints a ON a.link_id=l.id AND a.side=0 JOIN studio_link_endpoints b ON b.link_id=l.id AND b.side=1 ORDER BY l.updated_at DESC').fetchall():
            r=dict(r); r['metadata']=doc_value(r['metadata']); out.append(r)
        return out

    def link_state(c,lid):
        return next((l for l in links(c) if l['id']==lid),None)

    @router.get('/connections')
    def connections(request:Request):
        user(request)
        with connection() as c:
            return {'links':links(c),'legacy':[dict(r) for r in c.execute('SELECT * FROM topology_manual_links ORDER BY id').fetchall()],
                    'observations':[dict(r) for r in c.execute('SELECT device_id,local_if_name,protocol,remote_device,remote_ip,remote_port,last_seen FROM network_neighbors WHERE upper(protocol) IN (\'LLDP\',\'CDP\') ORDER BY device_id,local_if_name').fetchall()]}

    @router.get('/port-mapping')
    def port_mapping(request:Request):
        """Correlate cached LLDP/CDP evidence without running discovery scans."""
        user(request)
        with connection() as c:
            devices=[dict(r) for r in c.execute(
                'SELECT id,name,sys_name,management_ip,chassis_id,serial_number,device_type,status,last_discovery,sys_descr FROM network_devices ORDER BY name').fetchall()]
            interfaces=[dict(r) for r in c.execute(
                'SELECT device_id,if_index,if_name,if_descr,admin_status,oper_status,speed_bps,last_seen FROM network_interfaces ORDER BY device_id,if_index').fetchall()]
            observations=[dict(r) for r in c.execute(
                "SELECT device_id,local_if_index,local_if_name,protocol,remote_device,remote_port,remote_chassis_id,remote_port_id,remote_port_subtype,remote_ip,last_seen,link_speed_bps FROM network_neighbors WHERE upper(protocol) IN ('LLDP','CDP') ORDER BY device_id,local_if_index,protocol").fetchall()]
            lag_members=[dict(r) for r in c.execute(
                'SELECT device_id,aggregate_if_name,member_if_index,member_if_name,last_seen FROM network_lag_members ORDER BY device_id,aggregate_if_index,member_if_index').fetchall()]
            manual=links(c)
            legacy=[{**dict(r),'id':'legacy:'+str(r['id']),'legacy':True,'source':'MANUAL'} for r in c.execute('SELECT * FROM topology_manual_links WHERE enabled=1 ORDER BY id').fetchall()]
        result=correlate_port_mappings(devices,interfaces,observations,manual,legacy,now=datetime.now(timezone.utc))
        lag_index={}
        for member in lag_members:
            lag_index.setdefault((int(member['device_id']),str(member['aggregate_if_name']).casefold()),[]).append(member)
        membership={}
        for m in lag_members:
            membership[(int(m['device_id']),normalize_interface(m['member_if_name']))]=m['aggregate_if_name']
        for link in result['links']:
            if link.get('link_type')=='PORT_CHANNEL':
                link['source_members']=lag_index.get((int(link['source_device_id']),str(link['source_port']).casefold()),[])
                link['target_members']=lag_index.get((int(link['target_device_id']),str(link['target_port']).casefold()),[])
            else:
                link['source_port_channel']=membership.get((int(link['source_device_id']),normalize_interface(link['source_port'])))
                link['target_port_channel']=membership.get((int(link['target_device_id']),normalize_interface(link['target_port'])))
                link['port_channel']=link.get('port_channel') or link['source_port_channel'] or link['target_port_channel']
        result['lag_members']=lag_members
        from .vpc_evidence import load as load_vpc, annotate as annotate_vpc
        result['vpc_evidence']=load_vpc()
        annotate_vpc(result['links'],result['vpc_evidence'],normalize_interface)
        observed={int(o['device_id']) for o in observations}
        switches=[d for d in devices if any(k in str(d.get('device_type') or '').lower()+' '+str(d.get('sys_descr') or '').lower()
                    for k in ('switch','nexus','catalyst','sg350','n3k','n9k'))]
        result['switches_checked']=len(switches)
        result['devices_without_neighbor_data']=[{'device_id':d['id'],'name':d['name'],'device_type':d.get('device_type') or 'Unknown',
            'status':'NO_LLDP_CDP_DATA','last_discovery':d.get('last_discovery')}
            for d in switches if int(d['id']) not in observed]
        result['raw_observations']=len(observations)
        with connection() as c:result=overlay_mapping(c,result)
        return result

    @router.get('/validation')
    def validation(request:Request):
        user(request)
        issues=[]
        with connection() as c:
            valid={(int(r['device_id']),port_key(r['if_name'])) for r in c.execute('SELECT device_id,if_name FROM network_interfaces').fetchall()}
            used={}
            ls=links(c)
            legacy=[dict(r) for r in c.execute('SELECT * FROM topology_manual_links WHERE enabled=1').fetchall()]
            for l in ls:
                for side in ('source','target'):
                    key=(int(l[side+'_device_id']),port_key(l[side+'_port']))
                    if key not in valid: issues.append({'type':'MISSING_INTERFACE','link_id':l['id'],'device_id':key[0],'port':key[1]})
                    used.setdefault(key,[]).append(l['id'])
            for l in legacy:
                for key in ((int(l['source_device_id']),l['local_port']),(int(l['target_device_id']),l['remote_port'])):
                    if key[1]: used.setdefault((key[0],port_key(key[1])),[]).append('legacy:'+str(l['id']))
            for (did,port),ids in used.items():
                if len(ids)>1: issues.append({'type':'CONFLICTING_ENDPOINT' if physical_name(port) else 'LEGACY_LOGICAL_ENDPOINT_SHARED','device_id':did,'port':port,'links':ids})
            link_ids={l['id'] for l in ls}
            device_ids={int(r[0]) for r in c.execute('SELECT id FROM network_devices').fetchall()}
            for r in c.execute('SELECT id,document FROM studio_diagrams WHERE NOT deleted').fetchall():
                document=doc_value(r['document'])
                for n in document.get('nodes',[]):
                    if n.get('device_id') and n['device_id'] not in device_ids: issues.append({'type':'MISSING_DEVICE','diagram_id':r['id'],'node_id':n['id']})
                for e in document.get('edges',[]):
                    if e.get('physical_link_id') and e['physical_link_id'] not in link_ids: issues.append({'type':'ORPHAN_LINK','diagram_id':r['id'],'edge_id':e['id']})
        return {'issues':issues,'issue_count':len(issues),'checked_at':datetime.now(timezone.utc).isoformat(),'physical_links':len(ls),'legacy_links':len(legacy)}

    @router.get('/connections/{lid}/history')
    def connection_history(lid:str,request:Request):
        user(request)
        with connection() as c:
            rows=[dict(r) for r in c.execute('SELECT id,link_id,action,actor,before_state,after_state,created_at FROM studio_link_history WHERE link_id=? ORDER BY created_at DESC,id DESC',(lid,)).fetchall()]
        for row in rows:
            row['before_state']=doc_value(row['before_state']) if row['before_state'] else None
            row['after_state']=doc_value(row['after_state']) if row['after_state'] else None
        return {'history':rows}

    def put_link(c,x,lid,editing=False):
        if x.source not in ('MANUAL','VERIFIED'): raise HTTPException(422,'Invalid data source')
        if x.source_device_id==x.target_device_id: raise HTTPException(422,'Self physical link forbidden')
        # Serialize link changes, including old manual-link collision checks.
        c.execute('LOCK TABLE studio_link_endpoints IN SHARE ROW EXCLUSIVE MODE')
        if editing:
            if not c.execute('SELECT id FROM studio_links WHERE id=? FOR UPDATE',(lid,)).fetchone(): raise HTTPException(404,'Link not found')
            c.execute('DELETE FROM studio_link_endpoints WHERE link_id=?',(lid,))
        for did,port in ((x.source_device_id,x.source_port.strip()),(x.target_device_id,x.target_port.strip())):
            if not physical_name(port): raise HTTPException(422,'Choose a physical member interface, not a logical aggregate or VLAN')
            if not c.execute('SELECT id FROM network_devices WHERE id=?',(did,)).fetchone(): raise HTTPException(422,'Missing device')
            if not c.execute('SELECT 1 FROM network_interfaces WHERE device_id=? AND if_name=?',(did,port)).fetchone(): raise HTTPException(422,'Interface not found: '+port)
            if any(port_key(r['if_name'])==port_key(port) for r in c.execute('SELECT if_name FROM studio_link_endpoints WHERE device_id=?',(did,)).fetchall()): raise HTTPException(409,'Physical port is already assigned')
            legacy=c.execute('SELECT source_device_id,target_device_id,local_port,remote_port FROM topology_manual_links WHERE enabled=1 AND (source_device_id=? OR target_device_id=?)',(did,did)).fetchall()
            if any(port_key(r['local_port'] if int(r['source_device_id'])==did else r['remote_port'])==port_key(port) for r in legacy):
                raise HTTPException(409,'Port is used by a preserved legacy manual link; review it in legacy topology')
        m=clean_fields(x.metadata,LINK_FIELDS); verified=datetime.now(timezone.utc) if x.source=='VERIFIED' else None
        if editing: c.execute('UPDATE studio_links SET metadata=?,source=?,verified_at=?,updated_at=now() WHERE id=?',(json.dumps(m),x.source,verified,lid))
        else: c.execute('INSERT INTO studio_links(id,metadata,source,verified_at) VALUES(?,?,?,?)',(lid,json.dumps(m),x.source,verified))
        for side,did,port in ((0,x.source_device_id,x.source_port.strip()),(1,x.target_device_id,x.target_port.strip())):
            c.execute('INSERT INTO studio_link_endpoints(link_id,side,device_id,if_name) VALUES(?,?,?,?)',(lid,side,did,port))
        return lid

    @router.post('/connections')
    def new_link(x:LinkIn,request:Request):
        u=user(request,True)
        with connection() as c:
            lid=put_link(c,x,str(uuid.uuid4())); state=link_state(c,lid)
            c.execute('INSERT INTO studio_link_history(link_id,action,actor,after_state) VALUES(?,?,?,?)',(lid,'CREATE',u['username'],json.dumps(state,default=str)))
            audit('STUDIO_LINK_CREATE',lid,'Port-to-port',conn=c)
        return {'id':lid,'ok':True}

    @router.put('/connections/{lid}')
    def edit_link(lid:str,x:LinkIn,request:Request):
        u=user(request,True)
        with connection() as c:
            before=link_state(c,lid)
            if before is None: raise HTTPException(404,'Link not found')
            put_link(c,x,lid,True); after=link_state(c,lid)
            c.execute('INSERT INTO studio_link_history(link_id,action,actor,before_state,after_state) VALUES(?,?,?,?,?)',(lid,'UPDATE',u['username'],json.dumps(before,default=str),json.dumps(after,default=str)))
            audit('STUDIO_LINK_EDIT',lid,'Port-to-port',conn=c)
        return {'ok':True}

    @router.delete('/connections/{lid}')
    def delete_link(lid:str,request:Request):
        u=user(request,True)
        with connection() as c:
            before=link_state(c,lid)
            if before is None: raise HTTPException(404,'Link not found')
            c.execute('INSERT INTO studio_link_history(link_id,action,actor,before_state) VALUES(?,?,?,?)',(lid,'DELETE',u['username'],json.dumps(before,default=str)))
            c.execute('DELETE FROM studio_links WHERE id=?',(lid,)); audit('STUDIO_LINK_DELETE',lid,'Port-to-port',conn=c)
        return {'ok':True}

    @router.post('/connections/import')
    def import_links(payload:list[LinkIn],request:Request):
        u=user(request,True)
        if len(payload)>1000: raise HTTPException(422,'Maximum 1000 links per import')
        with connection() as c:
            ids=[]
            for x in payload:
                lid=put_link(c,x,str(uuid.uuid4())); ids.append(lid)
                state=link_state(c,lid)
                c.execute('INSERT INTO studio_link_history(link_id,action,actor,after_state) VALUES(?,?,?,?)',(lid,'CREATE',u['username'],json.dumps(state,default=str)))
            audit('STUDIO_LINK_IMPORT','studio',str(len(ids)),conn=c)
        return {'ids':ids,'ok':True}

    @router.get('/export/normalized-layout')
    def export_normalized_layout(request:Request,diagram_id:str='',layout_mode:str='saved'):
        user(request)
        if not diagram_id or layout_mode not in ('saved','presentation'):
            raise HTTPException(422,'Select a diagram and valid export layout')
        with connection() as c:
            saved=diagram(c,diagram_id)
        from .export_normalization import normalize_export_diagram
        normalized=normalize_export_diagram(saved,inventory(request)['devices'])
        if layout_mode=='presentation':
            from .studio_pdf import presentation_diagram
            return presentation_diagram(normalized,inventory(request)['devices'])
        return normalized

    @router.get('/export/presentation-layout')
    def export_presentation_layout(request:Request,diagram_id:str=''):
        user(request)
        if not diagram_id: raise HTTPException(422,'Select a topology diagram')
        with connection() as c:
            saved=diagram(c,diagram_id)
        from .studio_pdf import presentation_diagram
        return presentation_diagram(saved,inventory(request)['devices'])

    @router.get('/export/pdf')
    def pdf_export(request:Request,diagram_id:str='',paper:str='A3',orientation:str='landscape',mode:str='fit',scope:str='current',view_mode:str='physical',selected_device_ids:str='',group_name:str='',source_device_id:int=0,target_device_id:int=0,include:str='topology,devices,mapping,channels,ha',full_technical:bool=False,layout_mode:str='presentation',documentation_scope:str='network'):
        user(request)
        if documentation_scope not in ('network','servers','full'):raise HTTPException(422,'Invalid documentation scope')
        server_rows=None
        if documentation_scope!='network':
            with connection() as c:server_rows=server_snapshot(c)
        if paper not in ('A4','A3','A2','A1') or orientation not in ('landscape','portrait') or mode not in ('fit','multipage') or scope not in ('current','all','selected','group','pair','inventory','connections','complete') or view_mode not in ('hierarchical','physical','hybrid') or layout_mode not in ('presentation','saved'):
            raise HTTPException(422,'Invalid export option')
        if documentation_scope=='servers':
            from .server_documents import render_server_pdf
            return Response(render_server_pdf(server_rows,paper,orientation,detailed=full_technical),media_type='application/pdf',headers={'Cache-Control':'no-store','Content-Disposition':'attachment; filename=server-connections-v6.pdf'})
        try: selected={int(x) for x in selected_device_ids.split(',') if x.strip()}
        except ValueError: raise HTTPException(422,'Invalid selected device ID')
        if scope=='selected' and not selected: raise HTTPException(422,'Select at least one device')
        if scope=='group' and not group_name.strip(): raise HTTPException(422,'Select a device group')
        if scope=='pair' and (not source_device_id or not target_device_id or source_device_id==target_device_id): raise HTTPException(422,'Select two different switches')
        with connection() as c:
            if scope in ('all','complete','selected','group','pair'):
                ds=[{**dict(r),'document':doc_value(r['document'])} for r in c.execute('SELECT * FROM studio_diagrams WHERE NOT deleted ORDER BY title').fetchall()]
            else: ds=[diagram(c,diagram_id)] if diagram_id else []
            if scope=='current' and not ds: raise HTTPException(422,'Select a diagram')
            inv=inventory(request)['devices']
            if scope=='current':
                ids={n.get('device_id') for d in ds for n in d['document'].get('nodes',[]) if n.get('device_id')}
                inv=[d for d in inv if d['id'] in ids]
            elif scope=='selected': inv=[d for d in inv if d['id'] in selected]
            elif scope=='group': inv=[d for d in inv if str(d.get('metadata',{}).get('custom_group') or '').casefold()==group_name.strip().casefold()]
            elif scope=='pair': inv=[d for d in inv if d['id'] in (source_device_id,target_device_id)]
            inv=[{**d,'original_name':d['name'],'name':d.get('display_name') or d['name'],'management_ip':((('SNMP target '+d['management_ip']) if d.get('ha_monitoring')=='SNMP' and d['management_ip'] else 'Node IP: Unknown') if d.get('ha_cluster_id') and not d.get('ha_management_ip') else d['management_ip'])} for d in inv]
            saved_roles={n.get('device_id'):n.get('group') for d in ds if str(d.get('id'))==str(diagram_id) for n in d['document'].get('nodes',[]) if n.get('group') in ('Firewall','Core','Distribution','Access')}
            inv=[{**d,'metadata':{**(d.get('metadata') or {}),'role':saved_roles[d['id']]}} if d['id'] in saved_roles else d for d in inv]
            allowed={d['id'] for d in inv}
            if scope in ('selected','group','pair'):
                for d in ds:
                    node_ids={n['id'] for n in d['document'].get('nodes',[]) if n.get('device_id') in allowed}
                    d['document']['nodes']=[n for n in d['document'].get('nodes',[]) if n['id'] in node_ids]
                    d['document']['edges']=[e for e in d['document'].get('edges',[]) if e.get('source') in node_ids and e.get('target') in node_ids]
                ds=[d for d in ds if d['document'].get('nodes')]
            if scope=='pair': allowed={source_device_id,target_device_id}
            mapping=port_mapping(request)
            ls=[l for l in mapping['links'] if l['source_device_id'] in allowed and l['target_device_id'] in allowed]
            unresolved=[u for u in mapping.get('unresolved',[]) if u.get('device_id') in allowed]
        ha_for_export=json.loads(json.dumps(mapping.get('ha') or {},default=str))
        if scope not in ('all','complete','inventory','connections'):
            for cluster in ha_for_export.get('clusters',[]):
                cluster['members']=[m for m in cluster['members'] if m['device']['canonical_id'] in allowed]
                cluster['connections']=[l for l in cluster['connections'] if l['source_canonical_id'] in allowed and l['target_canonical_id'] in allowed]
            ha_for_export['clusters']=[cl for cl in ha_for_export.get('clusters',[]) if cl['members']]
        for l in ls:l.setdefault('metadata',{})
        # V3 exports one selected saved layout as the overview; the selected
        # diagram is presentation-only and does not alter stored topology.
        if ds:
            ds=[next((d for d in ds if str(d.get('id'))==str(diagram_id)),ds[0])]
        from .studio_pdf import render_pdf_v5
        sections={x.strip() for x in include.split(',') if x.strip()}
        if not sections.issubset({'topology','devices','mapping','channels','exceptions','server_connections','ha'}): raise HTTPException(422,'Invalid export section')
        try: data=render_pdf_v5(ds,inv,ls,paper,orientation,scope,sections,full_technical,
                                mapping.get('vpc_evidence') or {},layout_mode,unresolved,server_rows,ha_for_export)
        except ValueError as e: raise HTTPException(422,str(e))
        return Response(data,media_type='application/pdf',headers={'Content-Disposition':'attachment; filename="ipam-network-documentation.pdf"','Cache-Control':'no-store'})

    from concurrent.futures import ThreadPoolExecutor
    import threading
    import os
    import time
    workers=ThreadPoolExecutor(max_workers=2,thread_name_prefix='studio-export')
    slots=threading.BoundedSemaphore(4)
    jobs={}; jobs_lock=threading.Lock()

    def purge_jobs():
        now=time.time()
        if len(jobs)>8:
            for jid in list(jobs):
                if len(jobs)<=8: break
                if jobs[jid]['state'] not in ('queued','running'): jobs.pop(jid,None)
        for jid in list(jobs):
            if jobs[jid]['expires']<now and jobs[jid]['state'] not in ('queued','running'):
                jobs.pop(jid,None)

    @router.post('/export/jobs')
    def export_job(request:Request,diagram_id:str='',paper:str='A3',orientation:str='landscape',mode:str='fit',scope:str='current',view_mode:str='hybrid',selected_device_ids:str='',group_name:str='',source_device_id:int=0,target_device_id:int=0,include:str='topology,devices,mapping,channels,ha',full_technical:bool=False,layout_mode:str='presentation',documentation_scope:str='network'):
        u=user(request)
        if not slots.acquire(blocking=False): raise HTTPException(429,'Export queue is full; retry later')
        jid=str(uuid.uuid4())
        with jobs_lock:
            purge_jobs();jobs[jid]={'owner':u['id'],'state':'queued','expires':time.time()+1200}
        def work():
            with jobs_lock: jobs[jid]['state']='running'
            try:
                result=pdf_export(request,diagram_id,paper,orientation,mode,scope,view_mode,selected_device_ids,group_name,source_device_id,target_device_id,include,full_technical,layout_mode,documentation_scope)
                if len(result.body)>64_000_000: raise ValueError('Export exceeds 64 MB')
                with jobs_lock: jobs[jid].update(state='done',data=result.body)
            except Exception as exc:
                with jobs_lock: jobs[jid].update(state='failed',error=exc.detail if isinstance(exc,HTTPException) else 'Export could not be generated; review diagram bounds')
            finally: slots.release()
        future=workers.submit(work)
        with jobs_lock: jobs[jid]['future']=future
        return {'id':jid,'state':'queued'}

    def own_job(request,jid):
        u=user(request)
        with jobs_lock:
            purge_jobs();job=jobs.get(jid)
            if not job or job['owner']!=u['id']: raise HTTPException(404,'Export job expired or not found')
            return dict(job)

    @router.get('/export/jobs/{jid}')
    def job_status(jid:str,request:Request):
        job=own_job(request,jid)
        progress={'queued':0,'running':50,'done':100,'cancelled':100,'failed':100}.get(job['state'],0)
        return {'id':jid,'state':job['state'],'progress':progress,'error':job.get('error')}

    @router.delete('/export/jobs/{jid}')
    def cancel_job(jid:str,request:Request):
        own_job(request,jid)
        with jobs_lock:
            job=jobs.get(jid)
            if job['state']=='queued' and job.get('future') and job['future'].cancel():
                job.update(state='cancelled',error='Export cancelled before rendering');slots.release()
                return {'id':jid,'state':'cancelled'}
            if job['state'] in ('done','failed','cancelled'):
                return {'id':jid,'state':job['state']}
            raise HTTPException(409,'Export is already rendering and cannot be interrupted safely')

    @router.get('/export/jobs/{jid}/download')
    def job_download(jid:str,request:Request):
        job=own_job(request,jid)
        if job['state']!='done': raise HTTPException(409,'Export not complete')
        return Response(job['data'],media_type='application/pdf',headers={'Content-Disposition':'attachment; filename="ipam-network-documentation.pdf"','Cache-Control':'no-store'})

    @app.get('/favicon.ico',include_in_schema=False)
    def favicon():
        return Response(status_code=204)

    app.include_router(router)

    @app.get('/studio/{asset}',include_in_schema=False)
    def static_asset(asset:str):
        if asset not in {'studio.js','studio.css','topology-v2.js','topology-v2-worker.js','studio-export-v4.js','studio-export-v5.js','server-connections.js','ha-management.js','studio-export-ha-v6.js'}: raise HTTPException(404)
        return FileResponse(Path(__file__).parent/'static'/asset,headers={'Cache-Control':'no-cache, must-revalidate'})
