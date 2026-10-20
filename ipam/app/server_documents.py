"""V6 server section using the V5 palette, type scale and compact peer cards."""
from collections import defaultdict
from html import escape
from io import BytesIO
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet,ParagraphStyle
from reportlab.platypus import SimpleDocTemplate,Paragraph,Spacer,Table,TableStyle,KeepTogether,PageBreak
from reportlab.lib.pagesizes import A3,A4,A2,A1,landscape,portrait

def server_flowables(servers,width,styles,font,max_height=500,detailed=False):
    from .studio_pdf import _v5_node_colors
    def p(text,style='V5Cell'):return Paragraph(escape(str(text)),styles[style])
    out=[p('Server Connection','V5Title'),p('Manual documentation · Verification is not link UP/DOWN','V5Sub'),Spacer(1,10)]
    if not servers:out.append(p('No servers documented.'));return out
    for s in servers:
        ns={n['id']:n for n in s['interfaces']};links=s.get('connections',[])
        # Bound each continuation card; a large server never becomes an unprintable giant row.
        chunks=[links[i:i+6] for i in range(0,len(links),6)] or [[]]
        for part,chunk in enumerate(chunks):
            title=s['hostname']+(' (continued)' if part else '')
            speeds={c.get('link_speed') for c in links}
            count=str(len(links))+' connections'
            if len(speeds)==1 and next(iter(speeds)):
                count=str(len(links))+' × '+str(next(iter(speeds)))
            identity=title+(' · '+', '.join(s.get('management_ips',[])) if s.get('management_ips') else '')
            header=Table([[p(identity,'V5CardTitle'),p(' · '.join([str(s[k]) for k in ('status','server_type') if s.get(k)]+[count]),'V5Small')]],colWidths=[(width-16)*.68,(width-16)*.32]);header.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),0),('RIGHTPADDING',(0,0),(-1,-1),0)]))
            card_rows=[[header]]
            # Each physical connection gets one card. Last row shares the full
            # available width, even for 4/5/7 members or repeated destination IDs.
            columns=3 if width>=700 else 2 if width>=400 else 1
            for first in range(0,len(chunk),columns):
                row=chunk[first:first+columns];gap=8;inner=width-16
                cell_width=(inner-gap*(len(row)-1))/len(row);cells=[];widths=[]
                for c in row:
                    name=c['switch_name'];fill,stroke=_v5_node_colors({'name':name},'Core' if 'core' in name.lower() else 'Distribution')
                    content=[]
                    nic=ns.get(c['nic_id'],{});endpoint=str(nic.get('name','NIC'))+' → '+(c.get('switch_port') or 'Unknown')
                    reported=str(c.get('vlan_mode') or '').strip().upper();vlan=reported if reported in ('TRUNK','ACCESS','HYBRID') else 'UNKNOWN'
                    vid=str(c.get('vlan') or '').strip()
                    if vlan=='ACCESS' and vid.isdigit() and 0<int(vid)<4095:vlan+=' · VLAN '+vid
                    badge_colors={'TRUNK':('#FEF3C7','#92400E'),'ACCESS':('#166534','#FFFFFF'),'HYBRID':('#EDE9FE','#6D28D9')}
                    bg,ink=badge_colors.get(vlan.split(' · ')[0],('#E2E8F0','#475569'))
                    badge=Table([[Paragraph('<font color="'+ink+'">'+escape(vlan)+'</font>',styles['V5Cell'])]],colWidths=[min(98,cell_width*.38)]);badge.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,-1),colors.HexColor(bg)),('TEXTCOLOR',(0,0),(-1,-1),colors.HexColor(ink)),('LEFTPADDING',(0,0),(-1,-1),4),('RIGHTPADDING',(0,0),(-1,-1),4)]))
                    main=Table([[p(name,'V5PeerTitle'),badge]],colWidths=[cell_width-16-min(98,cell_width*.38),min(98,cell_width*.38)])
                    main.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),0),('RIGHTPADDING',(0,0),(-1,-1),3),('TOPPADDING',(0,0),(-1,-1),0),('BOTTOMPADDING',(0,0),(-1,-1),2)]));content.append(main)
                    if detailed and c.get('switch_ip'):content.append(p(c['switch_ip'],'V5Small'))
                    content.append(p(endpoint,'V5Small'))
                    values=[]
                    for k in ('link_speed','destination_port_channel','connection_type','server_bond_mode','switch_aggregation','source','verification_status'):
                        value=str(c.get(k) or '').strip()
                        if value.lower() in ('lacp','lacp / 802.3ad'):value='LACP'
                        if value and value.upper()!='MANUAL' and value.casefold() not in {v.casefold() for v in values}:values.append(value)
                    content.append(p(' · '.join(values)))
                    peer=Table([[content]],colWidths=[cell_width]);peer.setStyle(TableStyle([('BOX',(0,0),(-1,-1),.7,colors.HexColor(stroke)),('BACKGROUND',(0,0),(-1,-1),colors.HexColor(fill)),('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),8),('RIGHTPADDING',(0,0),(-1,-1),8),('TOPPADDING',(0,0),(-1,-1),5),('BOTTOMPADDING',(0,0),(-1,-1),5)]))
                    if cells:cells.append('');widths.append(gap)
                    cells.append(peer);widths.append(cell_width)
                grid=Table([cells],colWidths=widths);grid.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),0),('RIGHTPADDING',(0,0),(-1,-1),0)]));card_rows.append([[grid]])
            if not chunk:card_rows.append([[p('No manual connections.')]])
            card=Table(card_rows,colWidths=[width]);card.setStyle(TableStyle([('BOX',(0,0),(-1,-1),.6,colors.HexColor('#CBD5E1')),('BACKGROUND',(0,0),(0,0),colors.HexColor('#F1F5F9')),('LEFTPADDING',(0,0),(-1,-1),8),('RIGHTPADDING',(0,0),(-1,-1),8),('TOPPADDING',(0,0),(-1,-1),3),('BOTTOMPADDING',(0,0),(-1,-1),3)]))
            # Very long field values use a single-member continuation card if needed.
            if card.wrap(width,max_height)[1]>max_height and len(chunk)>1:
                for c in chunk:
                    one={**s,'connections':[c]};out.extend(server_flowables([one],width,styles,font,max_height,detailed=detailed)[3:])
            else:out.append(KeepTogether([card,Spacer(1,6)]))
        if detailed:
            # Full fields remain readable/splittable instead of clipping a compact card.
            out.append(p(s['hostname']+' · Details','V5CardTitle'))
            for k in ('server_type','status','management_ips','os','vendor','model','location','rack','description','created_by','created_at','updated_by','updated_at'):
                v=s.get(k)
                if v:out.append(p(k.replace('_',' ')+': '+(', '.join(v) if isinstance(v,list) else str(v))))
            for n in s['interfaces']:
                out.append(p('NIC '+n['name']+(' · MAC '+n['mac'] if n.get('mac') else '')+(' · IP '+', '.join(n['ips']) if n.get('ips') else '')))
            for c in links:
                nic=ns.get(c['nic_id'],{});out.append(p(str(nic.get('name','NIC'))+' → '+c['switch_name']+' / '+c['switch_port'],'V5Small'))
                for k in ('destination_port_channel','destination_interface_description','link_speed','connection_type','server_bond_mode','switch_aggregation','bond_name','vlan_mode','vlan','cable_label','notes','source','verification_status','oper_status','created_by','created_at','updated_by','updated_at'):
                    if c.get(k):out.append(p(k.replace('_',' ')+': '+str(c[k])))
                for warning in c.get('warnings',[]):out.append(p('Warning: '+warning['code']))
            out.append(Spacer(1,10))
    return out

def render_server_pdf(servers,paper='A3',orientation='landscape',detailed=False):
    import os
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    font='Helvetica';path=os.getenv('IPAM_PDF_FONT','/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')
    if os.path.exists(path):
        font='ServerV6Sans'
        if font not in pdfmetrics.getRegisteredFontNames():pdfmetrics.registerFont(TTFont(font,path))
    styles=getSampleStyleSheet()
    for name,size in [('V5Title',18),('V5Sub',8),('V5Cell',7),('V5Small',8),('V5CardTitle',11),('V5PeerTitle',9)]:styles.add(ParagraphStyle(name=name,fontName=font,fontSize=size,leading=size*1.3,textColor=colors.HexColor('#0F172A')))
    page=(landscape if orientation=='landscape' else portrait)({'A3':A3,'A4':A4,'A2':A2,'A1':A1}[paper]);out=BytesIO();doc=SimpleDocTemplate(out,pagesize=page,leftMargin=30,rightMargin=30,topMargin=25,bottomMargin=30,title='IPAM Server Connection V6',author='IPAM Network Topology Studio')
    def footer(c,d):c.setFont(font,7);c.drawString(30,15,'IPAM Network Documentation V6 · Manual Server Connection');c.drawRightString(page[0]-30,15,str(d.page))
    doc.build(server_flowables(servers,page[0]-60,styles,font,page[1]-75,detailed=detailed),onFirstPage=footer,onLaterPages=footer);return out.getvalue()
