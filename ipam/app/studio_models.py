"""Evidence-based switch profiles and interface classification.

Port identity comes from interface names or explicit audited overrides; ifIndex is
never interpreted as a front-panel position.
"""
import re
N3='https://www.cisco.com/c/en/us/products/collateral/switches/nexus-3000-series-switches/data_sheet_c78-729483.html'
N9='https://www.cisco.com/c/en/us/td/docs/switches/datacenter/nexus9000/hw/n92160ycx_hig/guide/b_n92160ycx_nxos_mode_hardware_install_guide_chapter_01.html'
SG='https://www.cisco.com/c/en/us/products/collateral/switches/350x-series-stackable-managed-switches/datasheet-c78-735986.html'
PROFILES={
 'N3K-C3172TQ':{'groups':[(1,48,'RJ45'),(49,54,'QSFP+')],'sections':[{'media':'RJ45','label':'10GBASE-T Downlinks','first':1,'last':48,'columns':24},{'media':'QSFP+','label':'QSFP+ Uplinks','first':49,'last':54,'columns':6}],'source_url':N3,'layout':'n3k-3172tq'},
 'C92160YC-X':{'groups':[(1,48,'SFP+'),(49,54,'QSFP+')],'sections':[{'media':'SFP+','label':'SFP+ / SFP28 Downlinks','first':1,'last':48,'columns':24},{'media':'QSFP+','label':'QSFP+ Uplinks','first':49,'last':54,'columns':6}],'source_url':N9,'layout':'n9k-92160ycx','note':'The 48 downlink cages support SFP/SFP+ and SFP28 optics; four of the six QSFP+ uplink ports support QSFP28 100G optics. Actual transceiver and negotiated speed are not inferred.'},
 'SG350X-24':{'groups':[(1,24,'RJ45'),(1,4,'SFP+')],'sections':[{'media':'RJ45','label':'Gigabit Ethernet Downlinks','first':1,'last':24,'columns':24},{'media':'SFP+','label':'10G Uplinks','first':1,'last':4,'columns':4}],'source_url':SG,'layout':'sg350x-24','note':'Two uplink positions are combo copper/SFP+ and two are SFP+; active medium is unknown without transceiver evidence.'},
 'SG350X-48':{'groups':[(1,48,'RJ45'),(1,4,'SFP+')],'sections':[{'media':'RJ45','label':'Gigabit Ethernet Downlinks','first':1,'last':48,'columns':24},{'media':'SFP+','label':'10G Uplinks','first':1,'last':4,'columns':4}],'source_url':SG,'layout':'sg350x-48','note':'Two uplink positions are combo copper/SFP+ and two are SFP+; active medium is unknown without transceiver evidence.'},
}

# Exact sysObjectID mappings observed in Cisco documentation/inventory. Unknown
# or family-level OIDs deliberately remain generic instead of inventing an SKU.
SYSOBJECT_MODELS={
 '.1.3.6.1.4.1.9.12.3.1.3.1389':'N3K-C3172TQ',
 '.1.3.6.1.4.1.9.12.3.1.3.1744':'C92160YC-X',
 '.1.3.6.1.4.1.9.6.1.94.24.1':'SG350X-24',
 '.1.3.6.1.4.1.9.6.1.94.48.1':'SG350X-48',
}

LOGICAL_RE=re.compile(r'^(?:vlan\d|vl\d|loopback|lo\d|port-channel|portchannel|po\d|lag\d|tunnel\d|nve\d|null\d|br\d|bdi\d|svi\d)',re.I)
MANAGEMENT_RE=re.compile(r'^(?:mgmt\d|management\d|oob\d|console\d)',re.I)
PHYSICAL_RE=re.compile(r'^(?:ethernet|eth|et|e|gigabitethernet|gig|gi|tengigabitethernet|tengige|te|fastethernet|fa|twentyfivegige|twe|fortygigabitethernet|fo|hundredgigabitethernet|hu)(\d+(?:/\d+){0,3})$',re.I)

def hardware_profile(device,metadata,source):
    model=str(metadata.get('model') or '')
    evidence='MANUAL' if model and source=='VERIFIED' else None
    if not(model in PROFILES and source=='VERIFIED'):
        oid=str(device.get('sys_object_id') or '').strip()
        if oid: oid='.'+oid.lstrip('.')
        model=SYSOBJECT_MODELS.get(oid)
        evidence='SYSOBJECTID' if model else None
        if not model:
            text=str(device.get('sys_descr') or '')
            # Only exact, product-specific tokens produce a profile.
            model=next((m for m in PROFILES if re.search(r'(?<![A-Za-z0-9-])'+re.escape(m)+r'(?![A-Za-z0-9-])',text,re.I)),None)
            evidence='SYSDESCR' if model else None
    family=None
    if not model:
        text=' '.join(str(device.get(k) or '') for k in ('sys_descr','model'))
        family='Catalyst 2960XR' if re.search(r'catalyst\s*2960xr|c2960xr',text,re.I) else 'Catalyst 2960XL' if re.search(r'catalyst\s*2960xl|c2960xl',text,re.I) else 'Catalyst 2960' if re.search(r'catalyst\s*2960|c2960',text,re.I) else 'SG350' if re.search(r'\bSG350\b',text,re.I) else None
        return {'model':model or metadata.get('model') or 'Unknown','family':family,'verified':False,'evidence':None,'groups':[],'layout':'generic','catalog':sorted(PROFILES)}
    return {'model':model,'family':model,'verified':True,'evidence':evidence,**PROFILES[model],'catalog':sorted(PROFILES)}

def classify_interface(name):
    value=str(name or '').strip()
    if MANAGEMENT_RE.match(value): return 'management'
    if LOGICAL_RE.match(value): return 'logical'
    if PHYSICAL_RE.fullmatch(value): return 'physical'
    # Numeric ifName values and vendor-specific names are not safely placeable.
    return 'unknown'

def physical_name(name):
    return classify_interface(name)=='physical'

def port_media(name,profile):
    if classify_interface(name)=='management':return 'Management'
    if not profile.get('verified'):return 'Unknown'
    value=str(name or '')
    match=PHYSICAL_RE.fullmatch(value)
    if not match:return 'Unknown'
    prefix=value[:len(value)-len(match.group(1))].lower()
    # Cisco Small Business interface names identify uplinks by interface type.
    if profile.get('model','').startswith('SG350X'):
        if prefix in ('te','tengige','tengigabitethernet'): return 'SFP+'
        if prefix in ('gi','gig','gigabitethernet'):
            parts=match.group(1).split('/')
            copper_count=24 if profile.get('model','').endswith('-24') else 48
            if int(parts[-1])>copper_count:return 'Unknown'
            return 'RJ45'
    parts=match.group(1).split('/')
    number=int(parts[-2] if len(parts)>=3 and profile.get('model','').startswith(('N3K','C92160')) else parts[-1])
    for first,last,media in profile['groups']:
        if first<=number<=last:return media
    return 'Unknown'

def port_key(name):
    """Canonical comparison only; never rewrite the stored SNMP/legacy port label."""
    value=re.sub(r'\s+','',str(name or '')).lower()
    for prefix,replacement in ((r'^(?:tengigabitethernet|tengige|te)(?=\d)','te'),(r'^(?:gigabitethernet|gig|gi)(?=\d)','gi'),(r'^(?:fastethernet|fa)(?=\d)','fa'),(r'^(?:ethernet|eth|et|e)(?=\d)','eth'),(r'^(?:port-channel|portchannel|po)(?=\d)','po')):
        normalized=re.sub(prefix,replacement,value)
        if normalized!=value:return normalized
    return value
