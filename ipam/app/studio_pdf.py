"""Compact, read-only network documentation PDF renderer (V4)."""
import io
import json
import math
import os
import copy
from datetime import datetime
from zoneinfo import ZoneInfo

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, A3, A2, A1, landscape, portrait
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, KeepTogether
from xml.sax.saxutils import escape


V5_COLORS = {
    'navy': '#0F172A', 'blue': '#2563EB', 'emerald': '#059669',
    'violet': '#7C3AED', 'cyan': '#0891B2', 'amber': '#D97706',
    'red': '#DC2626', 'slate': '#64748B',
}
V5_ROLE_COLORS = {
    'Firewall': ('#FEF3C7', V5_COLORS['amber']),
    'Core': ('#E2E8F0', V5_COLORS['navy']),
    'Distribution': ('#EDE9FE', V5_COLORS['violet']),
    'Access': ('#CFFAFE', V5_COLORS['cyan']),
    'Downstream': ('#F1F5F9', V5_COLORS['slate']),
}


def presentation_diagram(diagram, devices):
    """Return an export-only layered diagram copy; never mutate saved layout."""
    from .export_normalization import normalize_export_diagram
    result = normalize_export_diagram(diagram, devices)
    document = result.setdefault('document', {})
    nodes, edges = document.setdefault('nodes', []), document.setdefault('edges', [])
    by_id = {d.get('id'): d for d in devices}
    by_node = {n.get('id'): n for n in nodes}
    roles = {}
    adjacency = {n.get('id'): set() for n in nodes}
    for edge in edges:
        a, b = edge.get('source'), edge.get('target')
        if a in adjacency and b in adjacency:
            adjacency[a].add(b); adjacency[b].add(a)
    for node in nodes:
        device = by_id.get(node.get('device_id')) or {}
        role = _v5_device_role(device, node)
        roles[node.get('id')] = role
    # An otherwise unknown switch connected below an Access device is a cascade.
    for node in nodes:
        if roles[node.get('id')] == 'Downstream' and any(roles.get(peer) == 'Access' for peer in adjacency.get(node.get('id'), ())):
            roles[node.get('id')] = 'Downstream'
    rank = {'Firewall': 0, 'Core': 1, 'Distribution': 2, 'Access': 3, 'Downstream': 4}
    node_w, node_h, gap_x, gap_y, max_cols = 160, 58, 40, 72, 7
    ordered = sorted(nodes, key=lambda n: (rank[roles.get(n.get('id'), 'Downstream')],
                    str((by_id.get(n.get('device_id')) or {}).get('name') or '').casefold(), str(n.get('id') or '')))
    buckets = {}
    for node in ordered:
        buckets.setdefault(roles.get(node.get('id'), 'Downstream'), []).append(node)
    canvas_w = max_cols * node_w + (max_cols - 1) * gap_x
    y = 0
    for role in ('Firewall', 'Core', 'Distribution', 'Access', 'Downstream'):
        members = buckets.get(role, [])
        for row_start in range(0, len(members), max_cols):
            row = members[row_start:row_start + max_cols]
            row_w = len(row) * node_w + max(0, len(row) - 1) * gap_x
            x = (canvas_w - row_w) / 2
            for i, node in enumerate(row):
                node['x'], node['y'] = x + i * (node_w + gap_x), y
                node['width'], node['height'] = node_w, node_h
                node['group'] = role
                fill, stroke = _v5_node_colors(by_id.get(node.get('device_id')) or {}, role)
                node['color'], node['presentation_stroke'], node['presentation_role'] = fill, stroke, role
            y += node_h + gap_y
    from .export_routing import route_export_edges
    route_export_edges(document)
    document['viewport'] = {**(document.get('viewport') or {}), 'layout': 'presentation-v5'}
    result['export_layout'] = 'presentation'
    return result


def _v5_device_role(device, node=None):
    if (node or {}).get('group') in V5_ROLE_COLORS:return node['group']
    meta = (device or {}).get('metadata') or {}
    text = ' '.join(str(x or '') for x in ((device or {}).get('name'), (device or {}).get('device_type'),
                meta.get('role'), meta.get('function'), meta.get('custom_group'), (node or {}).get('group'))).casefold()
    if any(x in text for x in ('firewall', 'sophos', 'fortigate', 'palo alto')): return 'Firewall'
    if 'core' in text: return 'Core'
    if 'nexus' in text or 'distribution' in text or 'dist-' in text: return 'Distribution'
    if any(x in text for x in ('access', 'sg350', 'sg300', 'catalyst', 'switch', 'sw-')): return 'Access'
    return 'Downstream'


def _v5_node_colors(device, role):
    if (device or {}).get('documentation_theme')=='pastel-v6':
        from .topology_theme import THEME
        layer=next((x for x in THEME['layers'] if x['name']==role or role=='Firewall' and x['name']=='Firewall HA'),THEME['layers'][-1])
        return layer['background'],layer['border']
    name = str((device or {}).get('name') or '').casefold()
    if 'nexus-n9k-01' in name: return '#D1FAE5', V5_COLORS['emerald']
    if 'nexus-n9k-02' in name: return '#DBEAFE', V5_COLORS['blue']
    return V5_ROLE_COLORS.get(role, V5_ROLE_COLORS['Downstream'])


def _orient_link(link, source_id):
    """Orient a view row from the card's switch without changing the record."""
    row = dict(link)
    if link.get('source_device_id') == source_id:
        return row
    row['source_device_id'], row['target_device_id'] = link.get('target_device_id'), link.get('source_device_id')
    for left, right in (('source_port', 'target_port'), ('source_port_channel', 'target_port_channel'),
                        ('source_speed_bps', 'target_speed_bps'), ('source_members', 'target_members')):
        row[left], row[right] = link.get(right), link.get(left)
    return row


def _v5_access_mapping(physical, devices_by_id):
    """Return access cards and the remaining physical records exactly once."""
    owned, remainder = {}, []
    for link in physical:
        source, target = link.get('source_device_id'), link.get('target_device_id')
        src_role = _v5_device_role(devices_by_id.get(source))
        dst_role = _v5_device_role(devices_by_id.get(target))
        owner = source if src_role == 'Access' else target if dst_role == 'Access' else None
        if owner is None:
            remainder.append(link)
        elif src_role == dst_role == 'Access' and owner != source:
            remainder.append(link)
        else:
            owned.setdefault(owner, []).append(_orient_link(link, owner))
    cards = []
    for source_id, links in owned.items():
        lag_groups, standalone = {}, {}
        for link in links:
            po = str(link.get('source_port_channel') or '').strip()
            if po:
                lag_groups.setdefault(po.casefold(), {'label': _channel_label(po), 'links': [], 'verified': True}).get('links').append(link)
            else:
                standalone.setdefault(link.get('target_device_id'), []).append(link)
        groups = list(lag_groups.values())
        standalone_targets = list(standalone)
        dist_targets = [tid for tid in standalone_targets if _v5_device_role(devices_by_id.get(tid)) == 'Distribution']
        if len(dist_targets) > 1:
            dual = []
            for target_id in dist_targets:
                dual.extend(standalone.pop(target_id))
            groups.append({'label': 'Dual uplink · relationship unverified', 'links': dual, 'verified': False})
        for target_id, rows in standalone.items():
            groups.append({'label': 'Additional connection · no aggregate asserted', 'links': rows, 'verified': False})
        groups.sort(key=lambda g: (not g['verified'], str(g['label']).casefold()))
        cards.append({'source_id': source_id, 'links': links, 'groups': groups})
    cards.sort(key=lambda c: str((devices_by_id.get(c['source_id']) or {}).get('name') or '').casefold())
    return cards, remainder


def render_pdf_v5(diagrams, devices, links, paper='A3', orientation='landscape', scope='all',
                  include=None, full_technical=False, vpc_evidence=None, layout_mode='presentation', unresolved=(), server_rows=None, ha_data=None):
    """Compact V5 vector report with visual uplink cards and export-only layout."""
    include = set(include or ('topology', 'devices', 'mapping', 'channels', 'ha'))
    if layout_mode not in ('presentation', 'saved'):
        raise ValueError('Invalid topology layout mode')
    from .export_normalization import normalize_export_diagram
    diagrams = [normalize_export_diagram(d, devices) for d in diagrams]
    if layout_mode == 'presentation' and diagrams:
        diagrams = [presentation_diagram(diagrams[0], devices)]
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_CENTER
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, KeepTogether
    from reportlab.lib.pagesizes import A3, A4, A2, A1, landscape, portrait
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas
    import io, os

    font = 'Helvetica'
    font_path = os.getenv('IPAM_PDF_FONT') or '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
    if os.path.exists(font_path):
        if 'StudioSansV5' not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont('StudioSansV5', font_path))
        font = 'StudioSansV5'
    page = {'A4': A4, 'A3': A3, 'A2': A2, 'A1': A1}[paper]
    page_size = (landscape if orientation == 'landscape' else portrait)(page)
    width, height = page_size
    margin = 10 * mm if paper == 'A4' else 12 * mm
    usable_w = width - 2 * margin
    by_id = {d.get('id'): d for d in devices}
    diagram = diagrams[0] if diagrams else {}
    physical = [l for l in links if l.get('link_type') == 'PHYSICAL']
    channels = [l for l in links if l.get('link_type') == 'PORT_CHANNEL']
    confirmed = [l for l in physical if str(l.get('verification_status') or l.get('status') or '').upper() in ('CONFIRMED','VERIFIED')]
    now = datetime.now(ZoneInfo('Asia/Ho_Chi_Minh')).strftime('%d/%m/%Y %H:%M %Z')
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name='V5Title', parent=styles['Title'], fontName=font, fontSize=18, leading=21,
                              textColor=colors.HexColor(V5_COLORS['navy']), alignment=0, spaceAfter=2))
    styles.add(ParagraphStyle(name='V5Sub', parent=styles['Normal'], fontName=font, fontSize=7.5, leading=9,
                              textColor=colors.HexColor(V5_COLORS['slate'])))
    styles.add(ParagraphStyle(name='V5Section', parent=styles['Heading2'], fontName=font, fontSize=12, leading=14,
                              textColor=colors.HexColor(V5_COLORS['navy']), spaceBefore=1, spaceAfter=4))
    styles.add(ParagraphStyle(name='V5CardTitle', parent=styles['Normal'], fontName=font, fontSize=8.5, leading=10,
                              textColor=colors.HexColor(V5_COLORS['navy'])) )
    styles.add(ParagraphStyle(name='V5Small', parent=styles['Normal'], fontName=font, fontSize=7.0, leading=8.2,
                              textColor=colors.HexColor('#334155')))
    styles.add(ParagraphStyle(name='V5PeerTitle', parent=styles['Normal'], fontName=font, fontSize=7.2, leading=8.5,
                              textColor=colors.HexColor(V5_COLORS['navy'])))
    styles.add(ParagraphStyle(name='V5Stat', parent=styles['Normal'], fontName=font, fontSize=8, leading=10,
                              alignment=TA_CENTER, textColor=colors.HexColor(V5_COLORS['navy'])))
    styles.add(ParagraphStyle(name='V5Cell', parent=styles['Normal'], fontName=font, fontSize=6.4, leading=7.6,
                              textColor=colors.HexColor('#1e293b')))
    def p(value, style='V5Cell'):
        return Paragraph(escape(str(value if value not in (None, '') else 'Unknown')), styles[style])
    def footer(c, doc):
        c.saveState(); c.setStrokeColor(colors.HexColor('#CBD5E1')); c.line(margin, 8*mm, width-margin, 8*mm)
        c.setFont(font, 6.5); c.setFillColor(colors.HexColor(V5_COLORS['slate']))
        c.drawString(margin, 5*mm, f'IPAM Network Documentation V{6 if server_rows is not None or (ha_data and ha_data.get('clusters')) else 5} · {now}')
        c.drawRightString(width-margin, 5*mm, f'{doc.page}')
        c.restoreState()
    def heading(title, detail=''):
        result=[Paragraph(escape(title),styles['V5Section'])]
        if detail: result.append(Paragraph(escape(detail),styles['V5Sub']))
        return result
    output=io.BytesIO()
    doc=SimpleDocTemplate(output,pagesize=page_size,leftMargin=margin,rightMargin=margin,
                          topMargin=10*mm,bottomMargin=12*mm,title=f'IPAM Network Documentation V{6 if server_rows is not None or (ha_data and ha_data.get('clusters')) else 5}',
                          author='IPAM Network Topology Studio')
    story=[]
    story.append(Paragraph('IPAM NETWORK DOCUMENTATION',styles['V5Title']))
    story.append(Paragraph(f'{escape(scope.title())} scope · {escape(now)} · {escape(str(layout_mode).title())} layout',styles['V5Sub']))
    story.append(Spacer(1,3*mm))
    stat_names=[('Devices',len(devices)),('Physical links',len(physical)),('Port-channels',len(channels)),
                ('Confirmed',len(confirmed)),('Unverified',len(physical)-len(confirmed))]
    stats=Table([[Paragraph(f'<b>{v}</b><br/>{escape(k)}',styles['V5Stat']) for k,v in stat_names]],
                colWidths=[usable_w/5]*5,rowHeights=[11*mm])
    stats.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,-1),colors.HexColor('#F1F5F9')),
        ('BOX',(0,0),(-1,-1),.5,colors.HexColor('#CBD5E1')),('INNERGRID',(0,0),(-1,-1),.35,colors.white),
        ('VALIGN',(0,0),(-1,-1),'MIDDLE')]))
    story.extend([stats,Spacer(1,3*mm)])
    if 'topology' in include:
        story.extend(heading('Network Overview', diagram.get('title') or 'Saved network diagram'))
        if ha_data and ha_data.get('clusters'):
            for cluster in ha_data['clusters']:story.extend(heading(cluster['name']+' — '+str(len(cluster['members']))+' nodes',cluster['mode']+' · Cluster IP '+(cluster['cluster_ip'] or 'Unknown')+' · Live roles / health Unknown'))
        normalization=diagram.get('export_normalization',{})
        if normalization.get('diagnostics'):
            story.append(Paragraph(escape('Export identity check: '+str(len(normalization['diagnostics']))+' duplicate visual instances consolidated; saved diagram unchanged.'),styles['V5Sub']))
        _draw_topology(story,diagram,by_id,width,height,margin,font,'hybrid', max_height_ratio=.48, extra_bottom=28)
        legend=[]
        for role in ('Firewall','Core','Distribution','Access','Downstream'):
            sample=next((d for d in devices if _v5_device_role(d)==role and d.get('documentation_theme')),None)
            fill,stroke=_v5_node_colors(sample,role) if sample else V5_ROLE_COLORS[role]
            legend.append(Paragraph(f'<font color="{stroke}">■</font> {role}',styles['V5Small']))
        legend.append(Paragraph('Saved link colors/routes retain their source meaning.',styles['V5Small']))
        tbl=Table([legend],colWidths=[usable_w/len(legend)]*len(legend))
        tbl.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'MIDDLE'),('LEFTPADDING',(0,0),(-1,-1),2),
                                 ('RIGHTPADDING',(0,0),(-1,-1),2),('TOPPADDING',(0,0),(-1,-1),2)]))
        story.extend([Spacer(1,2*mm),tbl])
        integrity=_saved_diagram_integrity(diagram,links)
        story.append(Paragraph(escape(integrity),styles['V5Sub']))
    if 'mapping' in include:
        story.append(PageBreak())
        story.extend(heading('Visual Port Mapping',f'{len(physical)} physical links · each physical record appears once in the mapping view'))
        cards,remainder=_v5_access_mapping(physical,by_id)
        # Nested ReportLab tables do not subtract a parent cell's padding from
        # their explicit column widths. Reserve that space at each nesting
        # level so peer panels stay inside the switch card and the grid column.
        grid_column_width=usable_w*.44
        card_width=grid_column_width-4
        inner_width=card_width-12
        def speed_label(link):
            vals=[v for v in (link.get('source_speed_bps'),link.get('target_speed_bps')) if v and v!=4294967295]
            return f'{min(vals)/1e9:g}G' if vals and min(vals)>=1e9 else f'{min(vals)/1e6:g}M' if vals else (link.get('metadata') or {}).get('bandwidth') or 'Unknown'
        def status_color(link):
            value=str(link.get('verification_status') or link.get('status') or 'Unverified').upper()
            return {'CONFIRMED':V5_COLORS['emerald'],'VERIFIED':V5_COLORS['emerald'],
                    'ONE_SIDED':V5_COLORS['amber'],'CONFLICT':V5_COLORS['red'],'STALE':V5_COLORS['slate']}.get(value,V5_COLORS['slate'])
        def card_for(card):
            src=by_id.get(card['source_id']) or {}; title=src.get('name') or 'Unknown'; ip=src.get('management_ip') or ''
            role=_v5_device_role(src)
            title_color=V5_ROLE_COLORS.get(role,V5_ROLE_COLORS['Downstream'])[1]
            head=Table([[Paragraph(f'<b>{escape(str(title))}</b>',styles['V5CardTitle']),
                         Paragraph(escape(str(ip)),styles['V5Small'])]],colWidths=[inner_width/2,inner_width/2])
            head.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'MIDDLE'),('LEFTPADDING',(0,0),(-1,-1),1),
                ('RIGHTPADDING',(0,0),(-1,-1),1),('BOTTOMPADDING',(0,0),(-1,-1),2)]))
            content=[[head]]
            for group in card['groups']:
                po_links=group['links']; values=[speed_label(x) for x in po_links]
                summary=(f"{group['label']} · {len(po_links)} link"+('s' if len(po_links)!=1 else '')+
                         (f" · {', '.join(sorted(set(values)))} per link" if values else ''))
                content.append([Paragraph(escape(summary),styles['V5Small'])])
                peers={}
                for link in po_links: peers.setdefault(link.get('target_device_id'),[]).append(link)
                panels=[]
                for peer_id, peer_links in sorted(peers.items(),key=lambda x:str((by_id.get(x[0]) or {}).get('name') or '').casefold()):
                    peer=by_id.get(peer_id) or {}; peer_name=str(peer.get('name') or 'Unknown')
                    peer_role=_v5_device_role(peer); _,peer_color=_v5_node_colors(peer,peer_role)
                    peer_ink=peer.get('diagram_accent') if peer.get('documentation_theme') else peer_color
                    rows=[[Paragraph(f'<font color="{peer_ink}"><b>{escape(peer_name)}</b></font>',styles['V5PeerTitle'])]]
                    for link in sorted(peer_links,key=lambda l:_if_key(l.get('source_port'))):
                        speed=speed_label(link); status=str(link.get('verification_status') or link.get('status') or 'Unverified')
                        text=f"{escape(str(link.get('source_port') or 'Unknown'))} → {escape(str(link.get('target_port') or 'Unknown'))}<br/><font color=\"{status_color(link)}\">{escape(speed)} · {escape(status)}</font>"
                        rows.append([Paragraph(text,styles['V5Small'])])
                    panel=Table(rows,colWidths=[inner_width/max(1,len(peers))])
                    panel.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor(_v5_node_colors(peer,peer_role)[0])),
                        ('BOX',(0,0),(-1,-1),.55,colors.HexColor(peer_color)),('LEFTPADDING',(0,0),(-1,-1),3),
                        ('RIGHTPADDING',(0,0),(-1,-1),3),('TOPPADDING',(0,0),(-1,-1),2),
                        ('BOTTOMPADDING',(0,0),(-1,-1),2)]))
                    panels.append(panel)
                peer_row=Table([panels],colWidths=[inner_width/max(1,len(panels))]*len(panels)) if panels else p('No destination data')
                peer_row.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),1),
                                              ('RIGHTPADDING',(0,0),(-1,-1),1),('TOPPADDING',(0,0),(-1,-1),1)]))
                content.append([peer_row])
            card_table=Table(content,colWidths=[card_width])
            card_table.setStyle(TableStyle([('BOX',(0,0),(-1,-1),.7,colors.HexColor(title_color)),
                ('BACKGROUND',(0,0),(-1,0),colors.HexColor('#FFFFFF')),('LEFTPADDING',(0,0),(-1,-1),4),
                ('RIGHTPADDING',(0,0),(-1,-1),4),('TOPPADDING',(0,0),(-1,-1),2),
                ('BOTTOMPADDING',(0,0),(-1,-1),2),('VALIGN',(0,0),(-1,-1),'TOP')]))
            return card_table
        card_rows=[]
        for index in range(0,len(cards),2):
            row=[card_for(cards[index])]
            row.append(card_for(cards[index+1]) if index+1<len(cards) else '')
            card_rows.append(row)
        if card_rows:
            grid=Table(card_rows,colWidths=[grid_column_width,grid_column_width],hAlign='LEFT',splitByRow=1)
            grid.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),2),
                ('RIGHTPADDING',(0,0),(-1,-1),2),('TOPPADDING',(0,0),(-1,-1),2),('BOTTOMPADDING',(0,0),(-1,-1),3)]))
            story.append(grid)
        if remainder:
            story.append(Spacer(1,2*mm))
            rows=[[p('DEVICE'),p('PORT'),p('CONNECTED DEVICE'),p('PORT'),p('SPEED · STATUS')]]
            for link in sorted(remainder,key=lambda l:(str((by_id.get(l.get('source_device_id')) or {}).get('name') or '').casefold(),_if_key(l.get('source_port')))):
                src=by_id.get(link.get('source_device_id')) or {}; dst=by_id.get(link.get('target_device_id')) or {}
                rows.append([p(src.get('name')),p(link.get('source_port')),p(dst.get('name')),p(link.get('target_port')),
                             p(f"{speed_label(link)} · {link.get('verification_status') or link.get('status') or 'Unverified'}")])
            rest=_table(rows,[usable_w*x for x in (.22,.18,.23,.18,.19)],1,font)
            story.append(KeepTogether([Paragraph('Core / Distribution / Firewall Connections',styles['V5Section']),rest]))
        mapped_ids={str(l.get('id')) for c in cards for l in c['links']}
        mapped_ids.update(str(l.get('id')) for l in remainder)
        if len(mapped_ids)!=len(physical):
            raise ValueError('V5 mapping view contains duplicate or missing physical records')
    else:
        remainder=physical
    if 'devices' in include or 'channels' in include or full_technical or ('exceptions' in include and any(str(l.get('verification_status') or l.get('status') or '').upper() not in ('CONFIRMED','VERIFIED') for l in physical)):
        story.extend([Spacer(1,3*mm),Paragraph('Technical Details',styles['V5Section'])])
        inv=None
        if 'devices' in include:
            inventory=sorted(devices,key=lambda d:(_v5_device_role(d),str(d.get('name') or '').casefold()))
            has_model=any(d.get('model') or (d.get('metadata') or {}).get('model') for d in inventory)
            rows=[[p('DEVICE'),p('IP'),*([p('MODEL')] if has_model else []),p('ROLE')]]
            for d in inventory:
                rows.append([p(d.get('name')),p(d.get('management_ip')),
                    *([p(d.get('model') or (d.get('metadata') or {}).get('model') or 'N/A')] if has_model else []),p(_v5_device_role(d))])
            widths=[usable_w*.35,usable_w*.25,usable_w*.24,usable_w*.16] if has_model else [usable_w*.46,usable_w*.30,usable_w*.24]
            inv=_table(rows,widths,1,font)
        if 'channels' in include and channels:
            rows=[[p('DEVICE'),p('PORT-CHANNEL'),p('MEMBER PORTS'),p('PEER')]]
            for ch in sorted(channels,key=lambda l:str((by_id.get(l.get('source_device_id')) or {}).get('name') or '').casefold()):
                src=by_id.get(ch.get('source_device_id')) or {}; dst=by_id.get(ch.get('target_device_id')) or {}
                pairs=_member_pairs(ch,physical)
                local=', '.join(dict.fromkeys(x[0] for x in pairs if x[0]!='Unknown')) or 'Unknown'
                remote=', '.join(dict.fromkeys(x[1] for x in pairs if x[1]!='Unknown')) or 'Unknown'
                rows.append([p(src.get('name')),p(_channel_label(ch.get('source_port'))),p(local+' ↔ '+remote),p(dst.get('name'))])
            channel_table=_table(rows,[usable_w*.24,usable_w*.18,usable_w*.38,usable_w*.20],1,font)
            if inv:
                # Full width tables stay readable and avoid clipping nested table columns.
                story.extend([inv, Spacer(1,2*mm), channel_table]); inv=None
            else: story.append(channel_table)
        if inv: story.append(inv)
        if full_technical:
            story.extend([Spacer(1,2*mm),p(f'{len(physical)} physical records · detailed link IDs and cable labels', 'V5Sub')])
            rows=[[p('LINK ID'),p('SOURCE'),p('DESTINATION'),p('CABLE / LAST SEEN')]]
            for link in physical:
                src=by_id.get(link.get('source_device_id')) or {}; dst=by_id.get(link.get('target_device_id')) or {}; meta=link.get('metadata') or {}
                rows.append([p(link.get('id')),p(f"{src.get('name')} {link.get('source_port')}"),
                    p(f"{dst.get('name')} {link.get('target_port')}"),p(f"{meta.get('cable_label') or 'N/A'} / {link.get('last_seen') or 'N/A'}")])
            story.append(_table(rows,[usable_w*.18,usable_w*.28,usable_w*.28,usable_w*.26],1,font))
        if 'exceptions' in include:
            bad=[l for l in physical if str(l.get('verification_status') or l.get('status') or '').upper() not in ('CONFIRMED','VERIFIED')]
            if bad:
                story.extend([Spacer(1,2*mm),Paragraph('Exceptions',styles['V5Section'])])
                rows=[[p('DEVICE'),p('LOCAL PORT'),p('REMOTE DEVICE'),p('REMOTE PORT'),p('STATUS')]]
                for link in bad:
                    rows.append([p((by_id.get(link.get('source_device_id')) or {}).get('name')),p(link.get('source_port')),
                                 p((by_id.get(link.get('target_device_id')) or {}).get('name')),p(link.get('target_port')),
                                 p(link.get('verification_status') or link.get('status'))])
                story.append(_table(rows,[usable_w*x for x in (.22,.17,.23,.18,.20)],1,font))
            if unresolved:
                story.append(p(f'{len(unresolved)} unmatched observations are omitted from physical links and remain unverified.', 'V5Sub'))
    if 'ha' in include and ha_data and ha_data.get('clusters'):
        from .ha_documents import ha_flowables
        story.append(PageBreak())
        story.extend(ha_flowables(ha_data,usable_w,styles))
    if 'server_connections' in include and server_rows is not None:
        from .server_documents import server_flowables
        story.append(PageBreak() if server_rows else Spacer(1,10))
        story.extend(server_flowables(server_rows,usable_w,styles,font,height-2*margin-20,detailed=full_technical))
    doc.build(story,onFirstPage=footer,onLaterPages=footer)
    return output.getvalue()


def render_pdf(diagrams, devices, links, paper='A3', orientation='landscape', mode='fit',
               scope='all', view_mode='physical', unresolved=(), include=None, full_technical=False,
               vpc_evidence=None):
    """Render a saved-layout topology and evidence-grouped port mapping.

    `links` is the already correlated Port Mapping API snapshot. No raw discovery
    observations are promoted to links here and no source record is modified.
    """
    include = set(include or ('topology', 'devices', 'mapping', 'channels'))
    font = 'Helvetica'
    font_path = os.getenv('IPAM_PDF_FONT') or '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
    if os.path.exists(font_path):
        if 'StudioSans' not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont('StudioSans', font_path))
        font = 'StudioSans'
    pages = {'A4': A4, 'A3': A3, 'A2': A2, 'A1': A1}
    page_size = (landscape if orientation == 'landscape' else portrait)(pages[paper])
    width, height = page_size
    margin = 12 * mm if paper != 'A4' else 10 * mm
    by_id = {d.get('id'): d for d in devices}
    inferred_roles = _topology_roles(diagrams[0] if diagrams else {}, by_id)
    now = datetime.now(ZoneInfo('Asia/Ho_Chi_Minh')).strftime('%d/%m/%Y %H:%M %Z')
    physical = [l for l in links if l.get('link_type') == 'PHYSICAL']
    channels = [l for l in links if l.get('link_type') == 'PORT_CHANNEL']
    confirmed = [l for l in physical if str(l.get('verification_status') or l.get('status') or '').upper() in ('CONFIRMED', 'VERIFIED')]
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name='V3Title', parent=styles['Title'], fontName=font, fontSize=20, leading=24, textColor=colors.HexColor('#102a43'), spaceAfter=5))
    styles.add(ParagraphStyle(name='V3Sub', parent=styles['Normal'], fontName=font, fontSize=8, leading=10, textColor=colors.HexColor('#64748b')))
    styles.add(ParagraphStyle(name='V3Head', parent=styles['Heading1'], fontName=font, fontSize=13, leading=16, textColor=colors.HexColor('#102a43'), spaceBefore=0, spaceAfter=7))
    styles.add(ParagraphStyle(name='V3Cell', parent=styles['Normal'], fontName=font, fontSize=7.5 if paper != 'A4' else 7, leading=9, textColor=colors.HexColor('#1e293b')))
    styles.add(ParagraphStyle(name='V3CellSmall', parent=styles['Normal'], fontName=font, fontSize=6.5, leading=8, textColor=colors.HexColor('#1e293b')))
    styles.add(ParagraphStyle(name='V3CoverStat', parent=styles['Normal'], fontName=font, fontSize=10, leading=13, alignment=TA_CENTER, textColor=colors.HexColor('#075985')))

    def p(value, style='V3Cell'):
        if value in (None, ''):
            value = 'Unknown'
        return Paragraph(escape(str(value)), styles[style])

    def footer(c, doc):
        c.saveState()
        c.setStrokeColor(colors.HexColor('#dbe4ef'))
        c.line(margin, 8 * mm, width - margin, 8 * mm)
        c.setFont(font, 7)
        c.setFillColor(colors.HexColor('#64748b'))
        c.drawString(margin, 5 * mm, f'IPAM Network Documentation V4 · {now}')
        c.drawRightString(width - margin, 5 * mm, f'{doc.page}')
        c.restoreState()

    output = io.BytesIO()
    doc = SimpleDocTemplate(output, pagesize=page_size, leftMargin=margin,
                            rightMargin=margin, topMargin=12 * mm, bottomMargin=13 * mm,
                            title='IPAM Network Documentation V4', author='IPAM Network Topology Studio')
    story = []
    usable_w = width - 2 * margin

    def section(title, subtitle=None):
        group = [Paragraph(escape(title), styles['V3Head'])]
        if subtitle:
            group.append(Paragraph(escape(subtitle), styles['V3Sub']))
            group.append(Spacer(1, 3 * mm))
        return group

    # The overview page combines the cover metrics and exactly one saved diagram.
    story.append(Paragraph('IPAM Network Documentation', styles['V3Title']))
    story.append(Paragraph(f'Compact Grouped V4 · {escape(scope.title())} scope · Exported {escape(now)}', styles['V3Sub']))
    story.append(Spacer(1, 4 * mm))
    stats = [('Devices', len(devices)), ('Physical links', len(physical)), ('Port-channels', len(channels)), ('Confirmed links', len(confirmed)), ('Unverified', len(physical) - len(confirmed))]
    stat_table = Table([[Paragraph(f'<b>{escape(str(v))}</b><br/>{escape(str(k))}', styles['V3CoverStat']) for k, v in stats]], colWidths=[usable_w / 5] * 5, rowHeights=[13 * mm])
    stat_table.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#edf4fa')), ('BOX', (0, 0), (-1, -1), .5, colors.HexColor('#dbe4ef')), ('INNERGRID', (0, 0), (-1, -1), .4, colors.white), ('VALIGN', (0, 0), (-1, -1), 'MIDDLE')]))
    story.append(stat_table)
    story.append(Spacer(1, 5 * mm))
    if 'topology' in include and diagrams:
        story.extend(section('Network Topology', diagrams[0].get('title') or 'Saved topology layout'))
        _draw_topology(story, diagrams[0], by_id, width, height, margin, font, view_mode)
        integrity = _saved_diagram_integrity(diagrams[0], links)
        story.append(Paragraph(escape(integrity), styles['V3Sub']))
    elif 'topology' in include:
        story.extend(section('Network Topology', 'No saved diagram is available in this scope.'))
    if 'devices' in include:
        story.append(PageBreak())
        story.extend(section('Device Inventory', f'{len(devices)} devices · sorted by role and hostname'))
        inventory = sorted(devices, key=lambda d: (str(_role(d, inferred_roles)).casefold(), str(d.get('name') or '').casefold()))
        has_model = any((d.get('metadata') or {}).get('model') or d.get('model') for d in inventory)
        rows = [[p('Hostname'), p('IP Address'), *([p('Model')] if has_model else []), p('Role')]]
        for d in inventory:
            row = [p(d.get('name') or 'Unknown'), p(d.get('management_ip') or 'Unknown')]
            if has_model:
                row.append(p((d.get('metadata') or {}).get('model') or d.get('model') or 'N/A'))
            row.append(p(_role(d, inferred_roles)))
            rows.append(row)
        widths = ([usable_w * .29, usable_w * .19, usable_w * .30, usable_w * .22]
                  if has_model else [usable_w * .42, usable_w * .28, usable_w * .30])
        story.append(_table(rows, widths, repeat=1, font=font))
    if 'mapping' in include:
        story.append(PageBreak())
        ordered = sorted(physical, key=lambda l: (
            str((by_id.get(l.get('source_device_id')) or {}).get('name') or '').casefold(),
            _if_key(l.get('source_port')), str((by_id.get(l.get('target_device_id')) or {}).get('name') or '').casefold(),
            _if_key(l.get('target_port')), str(l.get('id') or '')))
        groups = _mapping_groups(ordered, vpc_evidence or {})
        story.extend(section('Grouped Port Mapping', f'{len(physical)} physical records · {len(groups)} evidence-based groups'))
        per_source = {}
        for group in groups:
            per_source.setdefault(group['source_device_id'], []).append(group)
        for source_id, source_groups in per_source.items():
            src = by_id.get(source_id) or {}
            title = f"{src.get('name') or 'Unknown'}  ·  {src.get('management_ip') or ''}"
            total = sum(len(g['links']) for g in source_groups)
            # Keep each switch in one compact card; unusually large source groups
            # are continued in bounded chunks so a 1,000-link export still paginates.
            chunks=[]
            if total <= 32:
                chunks=[source_groups]
            else:
                current=[]; count=0
                for group in source_groups:
                    for offset in range(0,len(group['links']),32):
                        part={**group,'links':group['links'][offset:offset+32]}
                        if count and count+len(part['links'])>32:
                            chunks.append(current); current=[]; count=0
                        current.append(part); count+=len(part['links'])
                if current: chunks.append(current)
            for chunk_no, source_chunk in enumerate(chunks,1):
                card_title=title+(f' · continued {chunk_no}' if len(chunks)>1 and chunk_no>1 else '')
                contents=[[Paragraph(escape(card_title),styles['V3Cell'])]]
                for group in source_chunk:
                    po=group.get('source_port_channel')
                    group_name=(_channel_label(po)+' · local member evidence' if po else 'Additional connections · group unverified')
                    if group.get('vpc_verified'): group_name+=' · vPC peers verified'
                    elif group.get('multi_peer'): group_name+=' · dual-uplink relation unverified'
                    contents.append([Paragraph(escape(group_name),styles['V3Sub'])])
                    rows=[[p('SOURCE PORT'),p('DESTINATION SWITCH'),p('DESTINATION PORT'),p('SPEED / STATUS')]]
                    for link in group['links']:
                        dst=by_id.get(link.get('target_device_id')) or {}
                        speeds=[v for v in (link.get('source_speed_bps'),link.get('target_speed_bps')) if v and v!=4294967295]
                        speed=(f'{min(speeds)/1e9:g}G' if speeds and min(speeds)>=1e9 else
                               f'{min(speeds)/1e6:g}M' if speeds else (link.get('metadata') or {}).get('bandwidth') or 'Unknown')
                        status=str(link.get('verification_status') or link.get('status') or 'Unverified')
                        rows.append([p(link.get('source_port') or 'Unknown'),p(dst.get('name') or 'Unknown'),
                                     p(link.get('target_port') or 'Unknown'),p(f'{speed} · {status}')])
                    contents.append([_table(rows,[usable_w*.24,usable_w*.29,usable_w*.23,usable_w*.24],repeat=1,font=font)])
                    contents.append([Paragraph(escape(f"{len(group['links'])} physical link"+('s' if len(group['links'])!=1 else '')),
                                                styles['V3Sub'])])
                card=Table(contents,colWidths=[usable_w],hAlign='LEFT')
                card.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#edf4fa')),
                    ('BOX',(0,0),(-1,-1),.5,colors.HexColor('#cbd5e1')),
                    ('LEFTPADDING',(0,0),(-1,-1),5),('RIGHTPADDING',(0,0),(-1,-1),5),
                    ('TOPPADDING',(0,0),(-1,-1),3),('BOTTOMPADDING',(0,0),(-1,-1),3)]))
                story.append(KeepTogether([card,Spacer(1,3*mm)]))
        if not physical:
            story.append(p('No physical link records are available in this scope.'))
    if full_technical and 'mapping' in include:
        story.append(PageBreak())
        story.extend(section('Full Technical Link Details', f'{len(physical)} physical records · raw discovery observations are not included'))
        rows = [[p('Link ID'), p('Source device / port'), p('Destination device / port'),
                 p('Speed / VLAN'), p('Port-channel / status'), p('Cable / last seen')]]
        for link in ordered:
            src = by_id.get(link.get('source_device_id')) or {}
            dst = by_id.get(link.get('target_device_id')) or {}
            meta = link.get('metadata') or {}
            speeds = [v for v in (link.get('source_speed_bps'), link.get('target_speed_bps')) if v and v != 4294967295]
            speed = (f'{min(speeds) / 1e9:g}G' if speeds and min(speeds) >= 1e9 else
                     f'{min(speeds) / 1e6:g}M' if speeds else meta.get('bandwidth') or 'Unknown')
            po = link.get('port_channel') or link.get('source_port_channel') or link.get('target_port_channel') or 'N/A'
            status = link.get('verification_status') or link.get('status') or 'Unverified'
            rows.append([p(link.get('id') or 'Unknown', 'V3CellSmall'),
                         p(f"{src.get('name') or 'Unknown'} / {link.get('source_port') or 'Unknown'}", 'V3CellSmall'),
                         p(f"{dst.get('name') or 'Unknown'} / {link.get('target_port') or 'Unknown'}", 'V3CellSmall'),
                         p(f"{speed} / {meta.get('vlan') or 'N/A'}", 'V3CellSmall'),
                         p(f'{po} / {status}', 'V3CellSmall'),
                         p(f"{meta.get('cable_label') or 'N/A'} / {link.get('last_seen') or link.get('updated_at') or 'N/A'}", 'V3CellSmall')])
        story.append(_table(rows, [usable_w * x for x in (.13, .20, .20, .13, .17, .17)], repeat=1, font=font))
    if 'channels' in include and channels:
        member_rows = [(link, _member_pairs(link, physical)) for link in channels]
        has_members = any(pairs for _, pairs in member_rows)
        if has_members:
            story.extend(section('Port-channel Members', f'{len(channels)} logical aggregates · physical member links remain in the mapping table'))
        else:
            story.append(Paragraph(f'{len(channels)} Port-channels · member interfaces are not present in this snapshot.', styles['V3Sub']))
        if has_members:
            rows = [[p('DEVICE'), p('PORT-CHANNEL'), p('MEMBER INTERFACE'), p('PEER'), p('PEER MEMBER')]]
            for link in sorted(channels, key=lambda l: (str((by_id.get(l.get('source_device_id')) or {}).get('name') or '').casefold(), str(l.get('source_port') or ''))):
                src, dst = by_id.get(link.get('source_device_id')) or {}, by_id.get(link.get('target_device_id')) or {}
                pairs = _member_pairs(link, physical)
                for sm, tm in pairs:
                    rows.append([p(src.get('name')), p(_channel_label(link.get('source_port'))), p(sm), p(dst.get('name')), p(tm)])
                if not pairs and not link.get('source_members') and not link.get('target_members'):
                    rows.append([p(src.get('name')), p(_channel_label(link.get('source_port'))), p('Unknown'), p(dst.get('name')), p('Unknown')])
            widths = [usable_w * .23, usable_w * .16, usable_w * .21, usable_w * .22, usable_w * .18]
            story.append(_table(rows, widths, repeat=1, font=font))
    if 'exceptions' in include:
        bad = [l for l in physical if str(l.get('verification_status') or l.get('status') or '').upper() not in ('CONFIRMED', 'VERIFIED')]
        unresolved_rows = []
        for l in bad:
            src = by_id.get(l.get('source_device_id')) or {}
            dst = by_id.get(l.get('target_device_id')) or {}
            unresolved_rows.append([p(src.get('name')), p(l.get('source_port') or 'Unknown'), p(dst.get('name')), p(l.get('target_port') or 'Unknown'), p(l.get('verification_status') or l.get('status') or 'Unverified')])
        for u in unresolved or ():
            unresolved_rows.append([p((by_id.get(u.get('device_id')) or {}).get('name') or u.get('device_name') or 'Unknown'), p(u.get('local_port') or u.get('local_if_name') or 'Unknown'), p(u.get('remote_device') or u.get('remote_ip') or 'Unknown'), p(u.get('remote_port') or 'Unknown'), p(u.get('reason') or u.get('status') or 'Unverified observation')])
        if unresolved_rows:
            story.extend(section('Exceptions', f'{len(unresolved_rows)} incomplete or unverified records; observations are not promoted to physical links'))
            story.append(_table([[p('SOURCE'), p('SOURCE PORT'), p('REMOTE DEVICE'), p('REMOTE PORT'), p('REASON / STATUS')]] + unresolved_rows,
                                [usable_w * .22, usable_w * .17, usable_w * .23, usable_w * .17, usable_w * .21], repeat=1, font=font))
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return output.getvalue()


def _role(device, inferred=None):
    metadata = device.get('metadata') or {}
    return metadata.get('role') or metadata.get('function') or (inferred or {}).get(device.get('id')) or device.get('device_type') or 'Unknown'


def _topology_roles(diagram, by_id):
    """Use explicit roles first, then a deterministic graph-distance grouping."""
    doc = diagram.get('document') or {}
    nodes = doc.get('nodes') or []
    by_node = {n.get('id'): n for n in nodes}
    adjacency = {n.get('id'): set() for n in nodes}
    for edge in doc.get('edges') or []:
        if edge.get('source') in adjacency and edge.get('target') in adjacency:
            adjacency[edge['source']].add(edge['target'])
            adjacency[edge['target']].add(edge['source'])
    labels = {0: 'Firewall', 1: 'Core', 2: 'Distribution', 3: 'Access', 4: 'Endpoints'}
    ranks = {}
    for node in nodes:
        device = by_id.get(node.get('device_id')) or {}
        metadata = device.get('metadata') or {}
        role = str(metadata.get('role') or metadata.get('function') or node.get('group') or '').casefold()
        kind = str(device.get('device_type') or '').casefold()
        if 'firewall' in role + ' ' + kind:
            ranks[node['id']] = 0
        elif 'core' in role:
            ranks[node['id']] = 1
        elif 'distribution' in role:
            ranks[node['id']] = 2
        elif 'access' in role:
            ranks[node['id']] = 3
        elif any(x in role + ' ' + kind for x in ('server', 'storage', 'client')):
            ranks[node['id']] = 4
    queue = [(node_id, 0) for node_id, rank in ranks.items() if rank == 0]
    if not queue and nodes:
        root = max(nodes, key=lambda n: (len(adjacency[n['id']]), str((by_id.get(n.get('device_id')) or {}).get('name') or '')))
        ranks.setdefault(root['id'], 1)
        queue = [(root['id'], ranks[root['id']])]
    seen = {node_id for node_id, _ in queue}
    cursor = 0
    while cursor < len(queue):
        node_id, rank = queue[cursor]
        cursor += 1
        for peer in adjacency.get(node_id, ()):
            if peer in seen:
                continue
            seen.add(peer)
            ranks.setdefault(peer, min(3, rank + 1))
            queue.append((peer, ranks[peer]))
    for node in nodes:
        ranks.setdefault(node['id'], 4)
    return {by_node[nid].get('device_id'): labels[rank] for nid, rank in ranks.items() if by_node[nid].get('device_id') is not None}


def _if_key(value):
    import re
    return tuple(int(x) if x.isdigit() else x.casefold() for x in re.split(r'(\d+)', str(value or '')))


def _channel_label(value):
    import re
    value = str(value or '')
    match = re.match(r'^(?:port[- ]?channel|po)(\d+)$', value, re.IGNORECASE)
    return f'Po{match.group(1)}' if match else value


def _member_pairs(link, physical_links=()):
    source = sorted({str(m.get('member_if_name')) for m in link.get('source_members') or [] if m.get('member_if_name')}, key=_if_key)
    target = sorted({str(m.get('member_if_name')) for m in link.get('target_members') or [] if m.get('member_if_name')}, key=_if_key)
    from .port_mapping import normalize_interface
    src_id, dst_id = link.get('source_device_id'), link.get('target_device_id')
    src_po, dst_po = normalize_interface(link.get('source_port')), normalize_interface(link.get('target_port'))
    pairs = []
    for member in physical_links:
        if member.get('link_type') != 'PHYSICAL': continue
        if member.get('source_device_id') == src_id and member.get('target_device_id') == dst_id:
            sp, tp = member.get('source_port'), member.get('target_port')
            source_channel = normalize_interface(member.get('source_port_channel'))
            target_channel = normalize_interface(member.get('target_port_channel'))
        elif member.get('source_device_id') == dst_id and member.get('target_device_id') == src_id:
            sp, tp = member.get('target_port'), member.get('source_port')
            source_channel = normalize_interface(member.get('target_port_channel'))
            target_channel = normalize_interface(member.get('source_port_channel'))
        else: continue
        if source_channel == src_po and target_channel == dst_po:
            pairs.append((sp, tp))
    if pairs:
        return sorted(set(pairs), key=lambda pair: (_if_key(pair[0]), _if_key(pair[1])))
    # Keep member inventories distinct when no exact physical mapping connects them.
    return [('Unknown', s) for s in source] + [('Unknown', t) for t in target]


def _mapping_groups(links, vpc_evidence=None):
    """Group by exact source LAG membership; report dual-peer evidence separately."""
    from .port_mapping import normalize_interface
    vpc_evidence = vpc_evidence or {}
    grouped = {}
    for link in links:
        source_po = link.get('source_port_channel')
        source_port = normalize_interface(link.get('source_port'))
        # The API sets source_port_channel by exact interface membership lookup.
        proven = bool(source_po and source_port)
        key = (link.get('source_device_id'), normalize_interface(source_po)) if proven else (link.get('source_device_id'), 'standalone')
        group = grouped.setdefault(key, {'source_device_id': link.get('source_device_id'),
                                         'source_port_channel': source_po if proven else None, 'links': []})
        group['links'].append(link)
    for group in grouped.values():
        targets = sorted({str(l.get('target_device_id')) for l in group['links'] if l.get('target_device_id') is not None})
        group['multi_peer'] = len(targets) > 1
        if len(targets) <= 1:
            continue
        peer_sets = [{(str(p.get('domain')), normalize_interface(p.get('interface')))
                      for p in (vpc_evidence.get(did) or {}).get('peer_links') or []} for did in targets]
        group['vpc_verified'] = bool(peer_sets and all(peer_sets) and set.intersection(*peer_sets))
    return list(grouped.values())


def _saved_route_points(edge):
    route = edge.get('route')
    if isinstance(route, str):
        try: route = json.loads(route)
        except (ValueError, TypeError): return []
    if isinstance(route, dict): route = route.get('points') or route.get('route')
    points=[]
    if isinstance(route, (list, tuple)):
        for point in route:
            try:
                if isinstance(point, dict): points.append((float(point['x']), float(point['y'])))
                elif isinstance(point, (list, tuple)) and len(point) >= 2: points.append((float(point[0]), float(point[1])))
            except (KeyError, TypeError, ValueError): return []
    return points if len(points) >= 2 else []


def _saved_diagram_integrity(diagram, links):
    document=diagram.get('document') or {}
    edges=document.get('edges') or []
    refs={str(e.get('physical_link_id')):e for e in edges if e.get('physical_link_id')}
    records={str(l.get('id')):l for l in links if l.get('id')}
    absent=sorted(set(records)-set(refs)); orphaned=sorted(set(refs)-set(records))
    node_ids={n.get('id'):n.get('device_id') for n in document.get('nodes') or []}
    endpoint_mismatch=[]
    for lid,edge in refs.items():
        row=records.get(lid)
        if not row: continue
        endpoints={node_ids.get(edge.get('source')),node_ids.get(edge.get('target'))}
        if endpoints!={row.get('source_device_id'),row.get('target_device_id')}:
            endpoint_mismatch.append(lid)
    unrouted=[str(e.get('id') or 'Unknown') for e in edges if not _saved_route_points(e)]
    return (f"Saved diagram ↔ Port Mapping: {len(refs)} edge references / {len(records)} records; "
            f"missing {len(absent)}, orphan {len(orphaned)}, endpoint mismatches {len(endpoint_mismatch)}, "
            f"saved routes missing {len(unrouted)}. "
            + (f"Review IDs: {', '.join((absent+orphaned+endpoint_mismatch+unrouted)[:8])}." if absent or orphaned or endpoint_mismatch or unrouted else 'Identifiers and endpoints match.'))


def _table(rows, widths, repeat, font):
    table = Table(rows, colWidths=widths, repeatRows=repeat, splitByRow=1, hAlign='LEFT')
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#e8eef6')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.HexColor('#102a43')),
        ('FONTNAME', (0, 0), (-1, -1), font), ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('GRID', (0, 0), (-1, -1), .35, colors.HexColor('#dbe4ef')),
        ('LEFTPADDING', (0, 0), (-1, -1), 4), ('RIGHTPADDING', (0, 0), (-1, -1), 4),
        ('TOPPADDING', (0, 0), (-1, -1), 3), ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f8fafc')]),
    ]))
    return table


def _draw_topology(story, diagram, by_id, width, height, margin, font, view_mode, max_height_ratio=.65, extra_bottom=0):
    """Draw the stored node positions and edge routes without rebuilding a graph."""
    from reportlab.graphics.shapes import Drawing, Line, Rect, String, PolyLine, Polygon
    from reportlab.platypus import Flowable
    from reportlab.lib.styles import ParagraphStyle

    document = diagram.get('document') or {}
    nodes = document.get('nodes') or []
    edges = document.get('edges') or []
    # Saved diagram is authoritative; view filters must not discard its edges.
    if not nodes:
        story.append(Paragraph('No saved topology layout is available.', getSampleStyleSheet()['Normal']))
        return
    themed=any(d.get('documentation_theme')=='pastel-v6' for d in by_id.values())
    min_x = min(float(n.get('x') or 0) for n in nodes) - (18 if themed else 0)
    min_y = min(float(n.get('y') or 0) for n in nodes) - (28 if themed else 0)
    max_x = max(float(n.get('x') or 0) + float(n.get('width') or 180) for n in nodes)
    max_y = max(float(n.get('y') or 0) + float(n.get('height') or 66) for n in nodes)
    if themed:max_x+=18;max_y+=18
    routed=[p for edge in edges for p in _saved_route_points(edge)]
    if routed:
        min_x=min(min_x,min(p[0] for p in routed)-8);max_x=max(max_x,max(p[0] for p in routed)+8)
        min_y=min(min_y,min(p[1] for p in routed)-8);max_y=max(max_y,max(p[1] for p in routed)+8)
    raw_w, raw_h = max(1, max_x - min_x), max(1, max_y - min_y)
    # Reserve at most about half an A3 landscape page for the diagram; keep the
    # overview compact while preserving every saved node position.
    max_h = height * max_height_ratio
    scale = min((width - 2 * margin) / raw_w, max_h / raw_h, 1.0)
    draw_w, draw_h = raw_w * scale, raw_h * scale + extra_bottom * scale
    drawing = Drawing(draw_w, draw_h)
    positions = {n['id']: (float(n.get('x') or 0), float(n.get('y') or 0), float(n.get('width') or 180), float(n.get('height') or 66)) for n in nodes}
    # Saved group names define presentation boundaries; no group is inferred from connectivity.
    groups = {}
    for node in nodes:
        group = str(node.get('group') or (_v5_device_role(by_id.get(node.get('device_id')) or {},node) if themed else '')).strip()
        if group:
            groups.setdefault(group, []).append(node)
    for group, members in groups.items():
        gx = min(float(n.get('x') or 0) for n in members) - 12
        gy = min(float(n.get('y') or 0) for n in members) - 24
        gr = max(float(n.get('x') or 0) + float(n.get('width') or 180) for n in members) + 12
        gb = max(float(n.get('y') or 0) + float(n.get('height') or 66) for n in members) + 12
        group_device=by_id.get(members[0].get('device_id')) or {}
        group_fill,group_border=_v5_node_colors(group_device,group if group in V5_ROLE_COLORS else _v5_device_role(group_device)) if group_device.get('documentation_theme') else ('#f8fafc','#cbd5e1')
        drawing.add(Rect((gx-min_x)*scale, draw_h-(gb-min_y)*scale, (gr-gx)*scale, (gb-gy)*scale,
                         fillColor=colors.HexColor(group_fill), strokeColor=colors.HexColor(group_border),
                         strokeWidth=.7, rx=5, ry=5))
        drawing.add(String((gx-min_x+5)*scale, draw_h-(gy-min_y+13)*scale, group[:48], fontName=font,
                           fontSize=max(6, 8*scale), fillColor=colors.HexColor('#475569')))
    missing_routes = []
    for edge in edges:
        if edge.get('source') not in positions or edge.get('target') not in positions:
            continue
        points = _saved_route_points(edge)
        if len(points) < 2:
            missing_routes.append(str(edge.get('id') or 'Unknown'))
            continue
        coords=[]
        for x,y in points:
            coords.extend([(x-min_x)*scale, draw_h-(y-min_y)*scale])
        raw_color = str(edge.get('color') or '#64748b')
        try: stroke = colors.HexColor(raw_color)
        except ValueError: stroke = colors.HexColor('#64748b')
        path = PolyLine(coords, strokeColor=stroke, strokeWidth=max(.65, 1.25*scale), fillColor=None)
        if str(edge.get('style') or '').casefold() in ('dashed','dash','dotted'):
            path.strokeDashArray = [4*scale, 3*scale]
        drawing.add(path)
        if edge.get('link_type')=='HA_HEARTBEAT':
            lx,ly=points[len(points)//2];drawing.add(String((lx-min_x)*scale,draw_h-(ly-min_y)*scale+5,'HA Sync',fontName=font,fontSize=max(5,8*scale),textAnchor='middle',fillColor=colors.HexColor('#7B659A')))
    for node in nodes:
        x, y, nw, nh = positions[node['id']]
        rx, ry = (x - min_x) * scale, draw_h - (y - min_y + nh) * scale
        rw, rh = nw * scale, nh * scale
        device = by_id.get(node.get('device_id')) or {}
        role = node.get('presentation_role') or _v5_device_role(device, node)
        fill, stroke = _v5_node_colors(device, role)
        if device.get('documentation_theme'):fill,stroke='#FFFFFF','#DCE4ED'
        drawing.add(Rect(rx, ry, rw, rh, rx=4, ry=4, fillColor=colors.HexColor(fill),
                         strokeColor=colors.HexColor(stroke), strokeWidth=1.0))
        name = device.get('display_name') or device.get('name') or node.get('name') or node.get('kind') or 'Device'
        ip = (device.get('ha_management_ip') or 'Node IP: Unknown') if device.get('ha_cluster_id') else device.get('management_ip') or ''
        group = ('Configured '+str(device.get('ha_configured_role'))+' / Live Unknown') if device.get('ha_cluster_id') else (device.get('metadata') or {}).get('role') or device.get('device_type') or ''
        if device.get('documentation_theme') and nh>=90:
            # Compact vector appliance: same blue chassis and visible ports as UI.
            iw,ih=124*scale,32*scale;ix=rx+(rw-iw)/2;iy=ry+rh-45*scale
            drawing.add(Polygon([ix,iy+ih,ix+12*scale,iy+ih+9*scale,ix+iw-10*scale,iy+ih+9*scale,ix+iw,iy+ih],fillColor=colors.HexColor('#32A8CF'),strokeColor=None))
            drawing.add(Rect(ix,iy,iw,ih,fillColor=colors.HexColor('#075478'),strokeColor=colors.HexColor('#2588B5'),strokeWidth=.5))
            for row in range(2):
                for col in range(6):drawing.add(Rect(ix+(8+col*15)*scale,iy+(6+row*13)*scale,10*scale,6*scale,fillColor=colors.HexColor('#123D54'),strokeColor=colors.HexColor('#9CC5D8'),strokeWidth=.35))
            if role=='Firewall':drawing.add(String(ix+iw-15*scale,iy+10*scale,'S',fontName=font,fontSize=12*scale,fillColor=colors.white))
            else:
                cx,cy=ix+iw-14*scale,iy+ih/2
                for dx,dy in [(0,7),(7,0),(5,5),(5,-5)]:drawing.add(Line(cx-dx*scale,cy-dy*scale,cx+dx*scale,cy+dy*scale,strokeColor=colors.white,strokeWidth=.7))
            center=rx+rw/2;fs=max(6,11*scale)
            drawing.add(String(center,ry+rh-64*scale,str(name),textAnchor='middle',fontName=font,fontSize=fs,fillColor=colors.HexColor('#24364B')))
            if ip:drawing.add(String(center,ry+rh-79*scale,str(ip),textAnchor='middle',fontName=font,fontSize=max(5,9*scale),fillColor=colors.HexColor('#64748B')))
            if group:drawing.add(String(center,ry+7*scale,str(group),textAnchor='middle',fontName=font,fontSize=max(4.5,8*scale),fillColor=colors.HexColor('#64748B')))
            continue
        fs = max(6, min(9, 9 * scale))
        drawing.add(String(rx + 5, ry + rh - 12 * scale, str(name)[:40], fontName=font, fontSize=fs, fillColor=colors.HexColor('#102a43')))
        if ip:
            drawing.add(String(rx + 5, ry + rh - 25 * scale, str(ip), fontName=font, fontSize=max(5.5, fs - 1), fillColor=colors.HexColor('#475569')))
        if group:
            drawing.add(String(rx + 5, ry + 5 * scale, str(group)[:28], fontName=font, fontSize=max(5, fs - 1.5), fillColor=colors.HexColor('#64748b')))
    class ScaledDrawing(Flowable):
        def __init__(self, graphic):
            super().__init__(); self.graphic = graphic; self.width = graphic.width; self.height = graphic.height
        def draw(self): self.graphic.drawOn(self.canv, 0, 0)
    story.append(ScaledDrawing(drawing))
    if missing_routes:
        story.append(Paragraph(escape(f"Topology review: {len(missing_routes)} saved edge(s) have no usable stored route and were omitted rather than redrawn: " + ', '.join(missing_routes[:20])),
                               ParagraphStyle(name='V4TopologyDiagnostic', parent=getSampleStyleSheet()['Normal'],
                                              fontName=font, fontSize=7.5, leading=9,
                                              textColor=colors.HexColor('#64748b'))))
