const fs = require('fs'), vm = require('vm'), assert = require('assert');
let messages = [], win = true, rights = 0xFFFFFFFF, timers = [];
const m = {State:3, stopInput:false, KeyAction:{DOWN:1,UP:2,EXDOWN:5,EXUP:4}, handleReleaseKeys(){}, SendStringUnicode(t){messages.push(['text',t]);}, SendKeyMsg(a,e){messages.push(['key',a,e.keyCode]);}, SendKeyMsgKC(a,k){if(Array.isArray(a)) a.forEach(x=>this.SendKeyMsgKC(...x)); else messages.push(['kc',a,k]);}, xxKeyDown(){messages.push(['original-down']);},xxKeyUp(){messages.push(['original-up']);},xxKeyPress(){messages.push(['original-press']);}};
const ctx = {toggleKvmControl(){},setTimeout:(f)=>timers.push(f),desktop:{State:3,contype:1,m}, desktopNode:{_id:'node1',osdesc:'macOS'},currentNode:{_id:'node1'},xxcurrentView:11,xxdialogMode:0,features2:0,GetNodeRights:()=>rights,isWindowsNode:()=>win,setSessionActivity(){},Q:()=>({checked:true}),CreateAgentRemoteDesktop:()=>m};
vm.createContext(ctx); vm.runInContext(fs.readFileSync(process.argv[2] || require('path').join(__dirname,'fixtures','ime-keyboard.js'),'utf8'),ctx);
ctx.deskIme.node='node1'; ctx.deskIme.enabled=true; ctx.deskIme.input={value:''};
function flush(){while(timers.length)timers.shift()();}
function reset(){flush(); messages=[];ctx.deskImeReset(); win=true;}
function event(key, more={}) {return {key,keyCode:65,code:'KeyA',stopPropagation(){},preventDefault(){this.prevented=true;},...more};}
reset(); ctx.deskIme.input.value='Tie'; ctx.deskImeSync(); assert.deepEqual(messages,[['text','Tie']]);
messages=[];ctx.deskIme.composing=true;ctx.deskIme.input.value='Tiếng';ctx.deskImeSync();assert.equal(messages.length,0);
ctx.deskIme.composing=false;ctx.deskImeSync();assert.deepEqual(messages,[['kc',1,8],['kc',2,8],['text','ếng']]);
messages=[];ctx.deskImeSync();assert.equal(messages.length,0);
reset();ctx.deskIme.input.value='Tiếng Việt';ctx.deskImeSync();assert.equal(messages[0][1],'Tiếng Việt');
reset();ctx.deskIme.input.value='không được gửi';rights=8|4096;ctx.deskImeSync();assert.equal(messages.length,0);assert.equal(ctx.deskIme.input.value,'');rights=0xFFFFFFFF;
reset();ctx.deskIme.input.value='không được gửi';ctx.desktopNode._id='other';ctx.deskImeSync();assert.equal(messages.length,0);ctx.desktopNode._id='node1';
reset();let e=event('Process',{keyCode:229,isComposing:true});ctx.deskImeKeyDown(e);assert.equal(messages.length,0);assert(!e.prevented);
reset();e=event('c',{keyCode:67,code:'KeyC',metaKey:true});ctx.deskImeKeyDown(e);assert.deepEqual(messages,[['kc',1,17],['key',1,67],['key',2,67],['kc',2,17]]);assert(e.prevented);
reset();win=false;e=event('c',{keyCode:67,code:'KeyC',metaKey:true});ctx.deskImeKeyDown(e);assert.equal(messages[0][2],91);flush();
reset();e=event('v',{keyCode:86,code:'KeyV',metaKey:true});ctx.deskImeKeyDown(e);assert.equal(messages.length,0);assert(!e.prevented);
reset();e=event('Enter',{keyCode:13,code:'Enter'});ctx.deskImeKeyDown(e);assert.deepEqual(messages,[['key',1,13],['key',2,13]]);
reset();ctx.deskIme.input.value='abc';e=event('Backspace',{keyCode:8,code:'Backspace'});ctx.deskImeKeyDown(e);assert(!e.prevented);assert.equal(messages.length,0);
reset();const wrapped=ctx.CreateAgentRemoteDesktop();wrapped.xxKeyDown(event('c',{code:'KeyC',keyCode:67,metaKey:true}));wrapped.xxKeyPress(event('c',{code:'KeyC',keyCode:67,metaKey:true}));wrapped.xxKeyUp(event('c',{code:'KeyC',keyCode:67,metaKey:false}));assert.deepEqual(messages,[['key',1,67],['key',2,67]]);
reset();wrapped.SendKeyMsgKC(5,91);wrapped.SendKeyMsgKC(4,91);assert.deepEqual(messages,[['kc',1,91],['kc',2,91]]);
reset();ctx.deskIme.input.value='Dòng 1\nDòng 2\tX';ctx.deskImeSync();assert.deepEqual(messages,[['text','Dòng 1'],['kc',1,13],['kc',2,13],['text','Dòng 2'],['kc',1,9],['kc',2,9],['text','X']]);
console.log('PASS: IME buffering, accent replacement, NFC, no duplicate commit, permissions, node isolation, Command mapping, paste, Enter, Backspace, legacy shortcuts, macOS extended keys.');
