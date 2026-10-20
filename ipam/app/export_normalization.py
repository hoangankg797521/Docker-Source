"""Canonical visual identities for read-only PDF and HTML export.

Diagram instance IDs remain separate from physical device IDs. Saved documents,
physical records and HA membership are never written by this module.
"""
import copy
import json
from collections import Counter


def normalize_export_diagram(diagram, devices=()):
    result = copy.deepcopy(diagram or {})
    document = result.setdefault('document', {})
    nodes = document.setdefault('nodes', [])
    edges = document.setdefault('edges', [])
    aliases = {}
    for device in devices:
        canonical = str(device.get('canonical_id', device['id']))
        aliases[str(device['id'])] = canonical
        if device.get('network_device_id') is not None:
            aliases[str(device['network_device_id'])] = canonical
    degree = Counter(endpoint for edge in edges for endpoint in (edge.get('source'), edge.get('target')))
    instances, groups = set(), {}
    for node in nodes:
        if node['id'] in instances:
            raise ValueError('Duplicate diagram instance ID: '+str(node['id']))
        instances.add(node['id'])
        did = node.get('device_id')
        # Explicit instances are retained; hostname and IP never establish identity.
        if did is None or node.get('export_multi_instance') is True:
            continue
        groups.setdefault(aliases.get(str(did), str(did)), []).append(node)
    remap, retained, diagnostics = {}, set(instances), []
    originals = {n['id']: copy.deepcopy(n) for n in nodes}
    for did, members in groups.items():
        canonical = max(members, key=lambda n: degree[n['id']])
        for duplicate in members:
            if duplicate is canonical:
                continue
            remap[duplicate['id']] = canonical['id']
            retained.remove(duplicate['id'])
            # Fill missing metadata; retain complete original instances for review.
            for key, value in duplicate.items():
                if key in {'id','device_id','x','y','width','height','color','presentation_stroke','presentation_role'}:
                    continue
                if key not in canonical or canonical[key] is None:
                    canonical[key] = copy.deepcopy(value)
            diagnostics.append({'code':'DUPLICATE_DEVICE_INSTANCE','device_id':did,
                'canonical_node_id':canonical['id'],'duplicate_node_id':duplicate['id'],
                'original_instance':copy.deepcopy(duplicate)})
    normalized_edges, suppressed = [], []
    for edge in edges:
        old_source, old_target = edge.get('source'), edge.get('target')
        if old_source not in instances or old_target not in instances:
            raise ValueError('Export edge has missing endpoint: '+str(edge.get('id')))
        edge['source'], edge['target'] = remap.get(old_source,old_source), remap.get(old_target,old_target)
        if edge['source'] == edge['target'] and old_source != old_target:
            suppressed.append({'code':'DUPLICATE_INSTANCE_SELF_LOOP','edge':copy.deepcopy(edge),
                               'original_endpoints':[old_source,old_target]})
            continue
        # Preserve all interior route points. Translate only an endpoint moved by
        # canonical remapping; untouched routes retain their exact saved value.
        if old_source != edge['source'] or old_target != edge['target']:
            route = edge.get('route'); encoded = isinstance(route,str)
            try: parsed = json.loads(route) if encoded else copy.deepcopy(route)
            except (ValueError,TypeError): parsed = None
            points = parsed if isinstance(parsed,list) else (parsed or {}).get('points') if isinstance(parsed,dict) else None
            if points:
                for old,new,index in ((old_source,edge['source'],0),(old_target,edge['target'],-1)):
                    if old == new:continue
                    a,b=originals[old],originals[new];dx=float(b.get('x',0))-float(a.get('x',0));dy=float(b.get('y',0))-float(a.get('y',0));p=points[index]
                    if isinstance(p,dict):p['x']+=dx;p['y']+=dy
                    elif isinstance(p,list) and len(p)>=2:p[0]+=dx;p[1]+=dy
                edge['route']=json.dumps(parsed) if encoded else parsed
        normalized_edges.append(edge)
    document['nodes']=[n for n in nodes if n['id'] in retained]
    document['edges']=normalized_edges
    if not diagnostics and not suppressed and result.get('export_normalization',{}).get('version') == 1:
        return result
    result['export_normalization']={'version':1,'input_nodes':len(nodes),
        'output_nodes':len(document['nodes']),'remapped_nodes':remap,
        'diagnostics':diagnostics,'suppressed_visual_edges':suppressed}
    return result
