'use strict';
const {test,after}=require('node:test'),assert=require('node:assert/strict'),fs=require('fs'),os=require('os'),path=require('path'),crypto=require('crypto');
const tmp=fs.mkdtempSync(path.join(os.tmpdir(),'hp-editor-test-'));process.env.CONFIG_DIR=path.join(tmp,'config');process.env.STATE_DIR=path.join(tmp,'state');process.env.PORT='0';process.env.PUBLIC_ORIGINS='http://test';fs.mkdirSync(process.env.CONFIG_DIR);fs.mkdirSync(process.env.STATE_DIR);
const raw={'services.yaml':`# dashboard comment\n- Monitoring:\n    # card comment\n    - Metrics:\n        href: http://monitor:3000\n        description: Original\n        siteMonitor: http://monitor:3000\n        widget:\n          type: grafana\n          key: TOP_SECRET_DO_NOT_SEND\n        custom: {keep: true}\n- Storage:\n    - NAS:\n        href: https://nas.local\n        icon: synology-dsm.svg\n`,'widgets.yaml':'# widget comment\n- greeting:\n    text: Hospital\n    text_size: xl\n- greeting:\n    text: IT Portal\n    text_size: sm\n- resources:\n    cpu: true\n    memory: true\n    disk: /\n- datetime:\n    locale: vi-VN\n','settings.yaml':'title: Original\nlayout:\n  Monitoring:\n    style: row\n    columns: 4\n  Storage:\n    style: row\n    columns: 2\n'};
for(const [n,v]of Object.entries(raw))fs.writeFileSync(path.join(process.env.CONFIG_DIR,n),v);
const salt=crypto.randomBytes(16).toString('hex');fs.writeFileSync(path.join(process.env.STATE_DIR,'auth.json'),JSON.stringify({username:'admin',salt,hash:crypto.scryptSync('test-pass',salt,64).toString('hex')}));
const app=require('./server');let cookie,csrf;async function call(p,data,headers={}){await new Promise(resolve=>app.server.listening?resolve():app.server.once('listening',resolve));const r=await fetch(`http://127.0.0.1:${app.server.address().port}/editor/api/${p}`,{method:data?'POST':'GET',headers:{...(data?{'Content-Type':'application/json',Origin:'http://test'}:{}),...headers},body:data?JSON.stringify(data):undefined});return {r,b:await r.json()};}
after(()=>{app.server.close();fs.rmSync(tmp,{recursive:true,force:true});});
test('YAML edit and move retains advanced fields, secrets, comments and layout',()=>{const m=app.snapshot(raw);assert(!JSON.stringify(m).includes('TOP_SECRET'));const c=m.groups[0].cards[0];c.name='New Metrics';c.href='http://new-monitor:3000';m.groups[0].cards=[];m.groups[1].cards.push(c);m.groups.reverse();m.groups[0].name='New Storage';const next=app.build(m,raw);assert.match(next['services.yaml'],/# dashboard comment/);assert.match(next['services.yaml'],/# card comment/);assert.match(next['services.yaml'],/TOP_SECRET_DO_NOT_SEND/);assert.match(next['services.yaml'],/siteMonitor: http:\/\/monitor:3000/);assert.match(next['services.yaml'],/keep: true/);assert.match(next['settings.yaml'],/New Storage:\n    style: row\n    columns: 2/);assert.equal(app.snapshot(next).groups[0].cards[1].name,'New Metrics');});
test('malformed data and duplicate IDs rejected',()=>{const m=app.snapshot(raw);m.groups[1].cards.push(m.groups[0].cards[0]);assert.throws(()=>app.build(m,raw));m.groups[1].cards.pop();m.groups[0].cards[0].href='javascript:alert(1)';assert.throws(()=>app.build(m,raw));assert.throws(()=>app.validate({...raw,'services.yaml':'- A: bad'}));});
test('unauthorized writes and hostile origins are blocked',async()=>{assert.equal((await call('config',{})).r.status,401);assert.equal((await call('login',{username:'admin',password:'test-pass'},{Origin:'http://evil'})).r.status,403);});
test('login and CSRF, atomic save, conflicts, restore and rollback',async()=>{const login=await call('login',{username:'admin',password:'test-pass'});assert.equal(login.r.status,200);cookie=login.r.headers.get('set-cookie').split(';')[0];csrf=login.b.csrf;const headers={Cookie:cookie,'X-CSRF-Token':csrf};const m=(await call('config',undefined,headers)).b;assert(!JSON.stringify(m).includes('TOP_SECRET'));assert.equal((await call('config',m,{Cookie:cookie})).r.status,403);m.groups[0].cards.push({name:'New Card',href:'http://new.local',description:'Added',icon:'mdi-server',target:'_blank'});const saved=await call('config',m,headers);assert.equal(saved.r.status,200);assert.equal(saved.b.groups[0].cards.length,2);assert.equal((await call('config',m,headers)).r.status,409);const backups=(await call('backups',undefined,headers)).b;assert.equal(backups.length,1);const restored=await call('restore',{id:backups[0].id,revision:saved.b.revision},headers);assert.equal(restored.r.status,200);assert.equal(restored.b.groups[0].cards.length,1);const before=app.read();process.env.FAIL_AFTER_WRITE='1';const update=app.snapshot();update.groups[0].name='Fail test';assert.throws(()=>app.commit(before,app.build(update,before),'admin'));delete process.env.FAIL_AFTER_WRITE;assert.deepEqual(app.read(),before);assert(!fs.existsSync(path.join(process.env.STATE_DIR,'journal.json')));assert.match(fs.readFileSync(path.join(process.env.STATE_DIR,'audit.jsonl'),'utf8'),/rollback/);});
test('icon catalog API searches every Homepage icon family and validates custom URLs',async()=>{
 assert.equal(app.iconValue('https://icons.example/icon.svg?color=blue'),'https://icons.example/icon.svg?color=blue');
 assert.equal(app.iconValue('/icons/custom.svg'),'/icons/custom.svg');
 assert.throws(()=>app.iconValue('javascript:alert(1)'));
 assert.throws(()=>app.iconValue('ftp://icons.example/icon.svg'));
 assert.throws(()=>app.iconValue('https://user:pass@icons.example/icon.svg'));
 const originalFetch=global.fetch;
 global.fetch=async (url,options)=>{
  if(String(url).startsWith('http://127.0.0.1:'))return originalFetch(url,options);
  const u=new URL(url);
  if(u.hostname==='api.iconify.design')return new Response(JSON.stringify({icons:[u.searchParams.get('prefix')+':server-outline']}),{status:200});
  if(u.pathname.endsWith('/metadata.json'))return new Response(JSON.stringify({proxmox:{aliases:['virtualization'],categories:['server']},zabbix:{aliases:['monitoring'],categories:['network']}}),{status:200});
  if(u.pathname.endsWith('/index.json'))return new Response(JSON.stringify([{Name:'Proxmox',Reference:'proxmox',SVG:'Yes',Category:'Self-Hosted'},{Name:'Zabbix',Reference:'zabbix',SVG:'Yes',Category:'Monitoring'}]),{status:200});
  throw Error('Unexpected icon catalog URL '+url);
 };
 try{
  const unauthorized=await call('icons?source=mdi&q=server');assert.equal(unauthorized.r.status,401);
  for(const [source,q,expected] of [['dashboard','virtualization','proxmox.svg'],['mdi','server','mdi-server-outline'],['si','server','si-server-outline'],['selfhst','proxmox','sh-proxmox.svg']]){
   const {r,b}=await call('icons?source='+source+'&q='+q,undefined,{Cookie:cookie});assert.equal(r.status,200);assert.equal(b.items[0].value,expected);
  }
 }finally{global.fetch=originalFetch;}
});
test('gateway invalidates stale domain HTML and versions editor assets',async()=>{
 const http=require('http');let received;
 const upstream=http.createServer((req,res)=>{received=req.headers;res.writeHead(200,{'Content-Type':'text/html','Cache-Control':'s-maxage=31536000',ETag:'old-dashboard','Last-Modified':'Wed, 01 Jan 2025 00:00:00 GMT',Expires:'Thu, 01 Jan 2037 00:00:00 GMT'});res.end('<html><body>Homepage</body></html>');});
 await new Promise(resolve=>upstream.listen(0,'127.0.0.1',resolve));process.env.UPSTREAM=`http://127.0.0.1:${upstream.address().port}`;
 try{const res=await fetch(`http://127.0.0.1:${app.server.address().port}/`,{headers:{'If-None-Match':'old-dashboard','If-Modified-Since':'Wed, 01 Jan 2025 00:00:00 GMT'}});assert.equal(res.status,200);assert.equal(res.headers.get('cache-control'),'no-store, max-age=0');assert.equal(res.headers.get('etag'),null);assert.equal(res.headers.get('last-modified'),null);assert.equal(res.headers.get('expires'),null);assert.equal(received['if-none-match'],undefined);assert.equal(received['if-modified-since'],undefined);assert.match(await res.text(),/data-hp-editor-loader src="\/editor\/editor.js\?v=[a-f0-9]{12}"/);}finally{upstream.close();delete process.env.UPSTREAM;}
});
test('layout columns and header edits persist while monitoring widgets and advanced layout remain intact',()=>{
 const YAML=require('./vendor/yaml'),m=app.snapshot(raw);
 assert.deepEqual(m.headers,{title:'Hospital',subtitle:'IT Portal'});assert.equal(m.groups[1].columns,2);
 const custom={...raw,'settings.yaml':raw['settings.yaml'].replace('    columns: 4','    columns: 4\n    icon: mdi-server\n    initiallyCollapsed: false')};
 m.headers={title:'MY DASHBOARD',subtitle:'My IT Portal'};m.groups[0].columns=3;m.groups[1].columns=6;m.groups[1].name='Storage renamed';
 const next=app.build(m,custom),again=app.snapshot(next),beforeWidgets=YAML.parse(raw['widgets.yaml']),afterWidgets=YAML.parse(next['widgets.yaml']);
 assert.deepEqual(again.headers,m.headers);assert.deepEqual(again.groups.map(g=>g.columns),[3,6]);assert.deepEqual(afterWidgets.slice(2),beforeWidgets.slice(2));assert.equal(afterWidgets[0].greeting.text_size,'xl');assert.match(next['widgets.yaml'],/# widget comment/);assert.match(next['settings.yaml'],/icon: mdi-server/);assert.match(next['settings.yaml'],/initiallyCollapsed: false/);
 for(const columns of [0,9,1.5,'3',null]){const bad=structuredClone(m);bad.groups[0].columns=columns;assert.throws(()=>app.build(bad,raw),/Số cột/);}
 for(const title of ['', 'x'.repeat(121),123]){const bad=structuredClone(m);bad.headers.title=title;assert.throws(()=>app.build(bad,raw));}
 const legacy=app.snapshot(raw);delete legacy.headers;legacy.groups.forEach(g=>delete g.columns);assert.equal(app.build(legacy,raw)['widgets.yaml'],raw['widgets.yaml']);
 const noLayout={...raw,'settings.yaml':'title: Original\n'};assert.equal(app.snapshot(app.build(app.snapshot(noLayout),noLayout)).groups[0].columns,4);
});
test('saving layout and titles creates a three-file backup; restore and failed writes protect all widgets',async()=>{
 const headers={Cookie:cookie,'X-CSRF-Token':csrf},before=app.read(),m=app.snapshot();m.headers={title:'Header edited',subtitle:'Subtitle edited'};m.groups[0].columns=5;
 const saved=await call('config',m,headers);assert.equal(saved.r.status,200);assert.equal(saved.b.headers.title,'Header edited');assert.equal(saved.b.groups[0].columns,5);
 const list=(await call('backups',undefined,headers)).b,backup=list.find(b=>{const r=JSON.parse(fs.readFileSync(path.join(process.env.STATE_DIR,b.id)));return r['widgets.yaml']===before['widgets.yaml'];});assert(backup);
 const restored=await call('restore',{id:backup.id,revision:saved.b.revision},headers);assert.equal(restored.r.status,200);assert.deepEqual(app.read(),before);
 const failing=app.snapshot();failing.headers.title='Must roll back';failing.groups[0].columns=8;process.env.FAIL_AFTER_WRITE='1';assert.throws(()=>app.commit(before,app.build(failing,before),'admin'));delete process.env.FAIL_AFTER_WRITE;assert.deepEqual(app.read(),before);
});
test('restoring legacy backups without widgets.yaml retains current titles and monitoring configuration',async()=>{
 const before=app.read();fs.writeFileSync(path.join(process.env.STATE_DIR,'backup-legacy.json'),JSON.stringify({time:new Date().toISOString(),'services.yaml':before['services.yaml'],'settings.yaml':before['settings.yaml']}));
 const result=await call('restore',{id:'backup-legacy.json',revision:app.snapshot().revision},{Cookie:cookie,'X-CSRF-Token':csrf});assert.equal(result.r.status,200);assert.equal(app.read()['widgets.yaml'],before['widgets.yaml']);
});
