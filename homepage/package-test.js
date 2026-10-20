'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {seed}=require('./init'),Y=require('./editor/vendor/yaml');
test('fresh install seeds original presentation and dummy data; subsequent init preserves edits',()=>{
 const root=fs.mkdtempSync(path.join(os.tmpdir(),'homepage-seed-'));
 try{
  const config=path.join(root,'config'),state=path.join(root,'state');
  const opts={config,state,defaults:path.join(__dirname,'defaults'),ownership:false};seed(opts);
  const s=Y.parse(fs.readFileSync(path.join(config,'settings.yaml'),'utf8'));
  assert.equal(s.headerStyle,'boxed');assert.equal(s.theme,'light');assert.equal(s.useEqualHeights,true);
  assert(Object.values(s.layout).every(g=>g.columns===4&&g.style==='row'));
  const services=Y.parse(fs.readFileSync(path.join(config,'services.yaml'),'utf8'));assert.equal(services.length,4);
  for(const g of services)for(const c of Object.values(g)[0])assert(Object.values(c)[0].href.startsWith('https://example.com/'));
  const urls=services.flatMap(g=>Object.values(g)[0].map(c=>Object.values(c)[0].href));assert.equal(new Set(urls).size,16);
  assert(fs.statSync(path.join(config,'custom.css')).size>15000);assert(fs.statSync(path.join(config,'custom.js')).size>5000);
  assert(!fs.existsSync(path.join(state,'auth.json')));
  fs.writeFileSync(path.join(config,'services.yaml'),'# user change\n[]\n');seed(opts);
  assert.equal(fs.readFileSync(path.join(config,'services.yaml'),'utf8'),'# user change\n[]\n');
 }finally{fs.rmSync(root,{recursive:true,force:true});}
});
