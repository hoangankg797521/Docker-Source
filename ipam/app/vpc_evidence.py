"""CISCO-VPC-MIB evidence; keepalive routing never implies a physical cable."""
import json,os,tempfile,fcntl
from pathlib import Path
from datetime import datetime,timezone
ROOT=(1,3,6,1,4,1,9,9,807,1)

def parse(rows,ifmap):
    columns={}
    for oid,value in rows:
        if tuple(oid[:len(ROOT)])!=ROOT:continue
        columns[tuple(oid[len(ROOT):])]=value
    def number(value):
        import re
        m=re.fullmatch(r'\d+',str(value or '').strip());return int(m.group()) if m else 0
    peers=[];hosts=[];keepalive=[]
    for index,value in columns.items():
        if index[:4]==(4,1,1,2) and len(index)==5:
            name=ifmap.get(number(value));
            if name:peers.append({'domain':index[4],'interface':name,'if_index':number(value)})
        if index[:4]==(4,2,1,3) and len(index)==6:
            name=ifmap.get(number(value))
            if name:hosts.append({'domain':index[4],'vpc_id':index[5],'interface':name,'if_index':number(value)})
        if index[:4]==(1,2,1,6) and len(index)==5:
            name=ifmap.get(number(value))
            if name:keepalive.append({'domain':index[4],'interface':name,'if_index':number(value),'status':str(columns.get((1,2,1,2,index[4]),'Unknown'))})
    return {'source':'CISCO-VPC-MIB','observed_at':datetime.now(timezone.utc).isoformat(),'peer_links':peers,'host_links':hosts,'keepalive_transport':keepalive}

def path():return Path(os.getenv('DB_PATH','/data/ipam.db')).parent/'studio-vpc-evidence.json'

def save(did,evidence):
    target=path();lock=target.with_suffix('.lock')
    with lock.open('a') as handle:
        os.chmod(lock,0o600);fcntl.flock(handle,fcntl.LOCK_EX)
        try:current=json.loads(target.read_text())
        except (OSError,ValueError):current={}
        current[str(did)]=evidence
        fd,name=tempfile.mkstemp(prefix='vpc-',dir=target.parent)
        with os.fdopen(fd,'w') as output:json.dump(current,output)
        os.chmod(name,0o600);os.replace(name,target)

def load():
    try:
        data=json.loads(path().read_text());now=datetime.now(timezone.utc)
        return {k:v for k,v in data.items() if (now-datetime.fromisoformat(v['observed_at'])).total_seconds()<86400}
    except (OSError,ValueError,KeyError,TypeError):return {}

def annotate(links,evidence,normalize):
    for link in links:
        for side in ('source','target'):
            row=evidence.get(str(link[side+'_device_id']),{});name=normalize(link[side+'_port']);channel=normalize(link.get(side+'_port_channel'))
            role='Unknown'
            if any(normalize(p['interface']) in (name,channel) for p in row.get('peer_links',[])):role='peer-link member' if link.get('link_type')=='PHYSICAL' else 'peer-link'
            elif any(normalize(p['interface'])==name for p in row.get('keepalive_transport',[])):role='keepalive transport (logical reachability)'
            elif any(normalize(p['interface']) in (name,channel) for p in row.get('host_links',[])):role='vPC host-link member' if link.get('link_type')=='PHYSICAL' else 'vPC host-link'
            link[side+'_vpc_role']=role
