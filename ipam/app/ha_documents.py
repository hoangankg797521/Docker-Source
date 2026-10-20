"""Read-only HA documentation section; pending relations are never physical links."""
from html import escape
from reportlab.lib import colors
from reportlab.platypus import Paragraph,Table,TableStyle,Spacer,KeepTogether
from .topology_theme import THEME

def ha_flowables(data,width,styles):
 def p(x,style='V5Cell'):return Paragraph(escape(str(x)),styles[style])
 out=[p('HA Management · Hybrid Discovery','V5Title'),p('Configured roles are documentation. Live role / cluster health remain Unknown without evidence.','V5Sub'),Spacer(1,8)]
 for c in data.get('clusters',[]):
  header=p(c['name']+' — '+str(len(c['members']))+' nodes','V5CardTitle')
  detail=p(c['mode']+' · Cluster IP '+(c['cluster_ip'] or 'Unknown')+(' (IT verified)' if c['cluster_ip_verified'] else ' (ownership unverified)')+' · Live status Unknown','V5Small')
  peer=[]
  for m in c['members']:
   d=m['device'];content=[p(d['display_name'],'V5CardTitle'),p('Configured '+m['configured_role']+' · Live Role Unknown'),p(d['discovery_type']+' · '+d['monitoring']),p('Node Management IP: '+(d['management_ip'] or 'Unknown'))]
   if d.get('snmp_address'):content.append(p('SNMP target: '+d['snmp_address']))
   if d.get('serial_number'):content.append(p('Serial: '+d['serial_number']))
   content.append(p('Last IT role verification: '+str(m.get('role_verified_at') or 'Not verified')))
   t=Table([[content]],colWidths=[width/2-10]);t.setStyle(TableStyle([('BOX',(0,0),(-1,-1),.6,colors.HexColor('#8CB9DA')),('BACKGROUND',(0,0),(-1,-1),colors.HexColor('#EAF4FB')),('LEFTPADDING',(0,0),(-1,-1),8),('RIGHTPADDING',(0,0),(-1,-1),8)]));peer.append(t)
  out.extend([header,detail,Spacer(1,6)])
  for i in range(0,len(peer),2):
   cells=peer[i:i+2]+([''] if len(peer[i:i+2])==1 else []);t=Table([cells],colWidths=[width/2]*2);t.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),0),('RIGHTPADDING',(0,0),(-1,-1),4)]));out.append(KeepTogether([t,Spacer(1,6)]))
  rows=[[p(x) for x in ('Source / Port','Destination / Port','Kind / Metadata','Verification / Link status')]]
  for l in c['connections']:
   rows.append([p(l['source_name']+' / '+(l['source_port'] or 'Port Mapping Pending')),p(l['target_name']+' / '+(l['target_port'] or 'Port Mapping Pending')),p(' · '.join(str(l[k]) for k in ('kind','speed','media_type','vlan','lag') if l.get(k))),p(l['mapping_status']+' · '+str(l.get('computed_verification') or l['verification'])+' · IT declaration '+l['verification']+' · MANUAL · Oper Unknown')])
  t=Table(rows,colWidths=[width*.23,width*.23,width*.31,width*.23],repeatRows=1,splitByRow=1);t.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#EAF1FB')),('GRID',(0,0),(-1,-1),.35,colors.HexColor('#DCE4ED')),('VALIGN',(0,0),(-1,-1),'TOP'),('TOPPADDING',(0,0),(-1,-1),5),('BOTTOMPADDING',(0,0),(-1,-1),5)]));out.extend([t,Spacer(1,10)])
  for key in ('description','created_by','created_at','updated_by','updated_at'):
   if c.get(key):out.append(p(key+': '+str(c[key])))
  for member in c['members']:
   d=member['device']
   for key in ('description','identity_evidence','created_by','created_at','updated_by','updated_at'):
    if d.get(key):out.append(p(d['display_name']+' · '+key+': '+str(d[key])))
   if member.get('description'):out.append(p(d['display_name']+' · Member description: '+member['description']))
  for link in c['connections']:
   if link.get('description'):out.append(p(link['source_name']+' → '+link['target_name']+' · Description: '+link['description']))
 out.append(p('Legend: Firewall–Core '+THEME['links']['firewall']+' · Core–Distribution '+THEME['links']['core']+' · Distribution–Access '+THEME['links']['distribution']+' · Access–Access '+THEME['links']['access']+' · HA Sync dashed '+THEME['links']['heartbeat']+' · Pending / Unknown dashed '+THEME['links']['unknown'],'V5Sub'))
 return out
