"""Deterministic, obstacle-aware routing for export copies only.

Saved diagrams remain authoritative and untouched. No connectivity or status is
inferred here: only the routes of existing presentation edges are replaced.
"""
from collections import defaultdict
from heapq import heappop, heappush
import math


def _hits(a, b, boxes):
    if a[0] == b[0]:
        lo, hi = sorted((a[1], b[1]))
        return any(x < a[0] < r and max(lo, y) < min(hi, t) for x, y, r, t in boxes)
    lo, hi = sorted((a[0], b[0]))
    return any(y < a[1] < t and max(lo, x) < min(hi, r) for x, y, r, t in boxes)


def route_export_edges(document):
    """Mutate only a caller-owned export document; retain all edge metadata.

    Ports are allocated per node side, including reverse-direction parallel
    records. A rectilinear visibility grid avoids padded node rectangles; A*
    penalizes bends and shared lanes. Failed routes retain their original data
    and are explicitly reported instead of inventing a new connection.
    """
    nodes = {n['id']: n for n in document.get('nodes', [])}
    edges = document.get('edges', [])
    rects = {k: (float(n.get('x', 0)), float(n.get('y', 0)),
                 float(n.get('width') or 160), float(n.get('height') or 58)) for k, n in nodes.items()}
    sides = defaultdict(list)
    def center(k):
        x, y, w, h = rects[k]
        return x+w/2, y+h/2
    for i, e in enumerate(edges):
        if e.get('source') not in nodes or e.get('target') not in nodes: continue
        for end, other in [('source', 'target'), ('target', 'source')]:
            k, peer = e[end], e[other]
            x, y = center(k); px, py = center(peer)
            # Prefer vertical anchors in a layered topology.
            side = ('bottom' if py > y else 'top') if abs(py-y) > 20 else ('right' if px > x else 'left')
            sides[k, side].append((px if side in ('top','bottom') else py, str(peer), str(e.get('id', i)), i, end))
    anchors = {}
    for (k, side), entries in sides.items():
        x, y, w, h = rects[k]
        for j, (_, _, _, i, end) in enumerate(sorted(entries)):
            f = (j+1)/(len(entries)+1)
            if side in ('top', 'bottom'):
                a = (round(x+12+(w-24)*f, 3), y if side == 'top' else y+h)
                b = (a[0], a[1]+(-12 if side == 'top' else 12))
            else:
                a = (x if side == 'left' else x+w, round(y+8+(h-16)*f, 3))
                b = (a[0]+(-12 if side == 'left' else 12), a[1])
            anchors[i, end] = a, b
    boxes = [(x-7,y-7,x+w+7,y+h+7) for x,y,w,h in rects.values()]
    xs, ys = set(), set()
    for x,y,w,h in rects.values():
        xs.update((x-12,x+w+12));ys.update((y-12,y+h+12))
    # Independent lanes in the free inter-row/inter-column corridors avoid a
    # common bus impression. The coordinate grid still scales with node count.
    for axis,values in ((0,xs),(1,ys)):
        spans=sorted(set((r[axis],r[axis]+r[axis+2]) for r in rects.values()))
        boundaries=sorted({v for pair in spans for v in pair})
        for lo,hi in zip(boundaries,boundaries[1:]):
            if hi-lo>20 and not any(a<(lo+hi)/2<b for a,b in spans):
                steps=min(18,max(1,int((hi-lo-16)//4)))
                values.update(round(lo+8+(hi-lo-16)*j/steps,3) for j in range(steps+1))
    for a,b in anchors.values(): xs.add(b[0]);ys.add(b[1])
    if not xs or not ys: return {'algorithm':'orthogonal-v64','routed':0,'unrouted':[]}
    # Extra outside lanes also serve edges that span several device layers.
    for j in range(1,9):
        xs.update((min(x for x,_,_,_ in rects.values())-12-j*7,max(x+w for x,_,w,_ in rects.values())+12+j*7))
        ys.update((min(y for _,y,_,_ in rects.values())-12-j*7,max(y+h for _,y,_,h in rects.values())+12+j*7))
    xs,ys=sorted(xs),sorted(ys);xi={v:i for i,v in enumerate(xs)};yi={v:i for i,v in enumerate(ys)}
    blocked={(i,j) for i,x in enumerate(xs) for j,y in enumerate(ys) if any(l<x<r and t<y<b for l,t,r,b in boxes)}
    used=defaultdict(int);adj={}
    def neighbours(k):
        if k not in adj:
            i,j=k;a=(xs[i],ys[j]);out=[]
            for ni,nj,d in ((i-1,j,1),(i+1,j,1),(i,j-1,2),(i,j+1,2)):
                if 0<=ni<len(xs) and 0<=nj<len(ys) and (ni,nj) not in blocked:
                    b=(xs[ni],ys[nj])
                    if not _hits(a,b,boxes):out.append(((ni,nj),d,abs(a[0]-b[0])+abs(a[1]-b[1])))
            adj[k]=out
        return adj[k]
    report={'algorithm':'orthogonal-v64','routed':0,'unrouted':[]}
    for i,e in enumerate(edges):
        if (i,'source') not in anchors:report['unrouted'].append(e.get('id'));continue
        a,start=anchors[i,'source'];z,end=anchors[i,'target'];s=(xi[start[0]],yi[start[1]]);t=(xi[end[0]],yi[end[1]])
        q=[(0,0,s,0)];cost={(s,0):0};prev={};finish=None
        while q:
            _,g,k,d=heappop(q)
            if g!=cost.get((k,d)):continue
            if k==t:finish=(k,d);break
            for nk,nd,length in neighbours(k):
                segment=tuple(sorted((k,nk)));ng=g+length+(18 if d and d!=nd else 0)+used[segment]*length*5
                state=(nk,nd)
                if ng<cost.get(state,math.inf):
                    cost[state]=ng;prev[state]=(k,d)
                    h=abs(xs[nk[0]]-end[0])+abs(ys[nk[1]]-end[1]);heappush(q,(ng+h,ng,nk,nd))
        if finish is None:
            report['unrouted'].append(e.get('id'));continue
        keys=[];state=finish
        while state in prev:keys.append(state[0]);state=prev[state]
        keys.append(s);keys.reverse()
        for k,nk in zip(keys,keys[1:]):used[tuple(sorted((k,nk)))]+=1
        points=[a]+[(xs[k[0]],ys[k[1]]) for k in keys]+[z]
        compact=[]
        for point in points:
            if compact and compact[-1]==point:continue
            while len(compact)>1 and (compact[-2][0]==compact[-1][0]==point[0] or compact[-2][1]==compact[-1][1]==point[1]):compact.pop()
            compact.append(point)
        e['route']=[{'x':x,'y':y} for x,y in compact];report['routed']+=1
    document['export_routing']=report
    return report
