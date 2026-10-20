const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
let stopped = 0, draws = 0, saved = [], alerts = [], supported = true, failStart = false;
const elements = {Desk:{width:1921,height:1081,captureStream(){}},DeskRecordButtonImage:{},DeskRecordButton:{appendChild(el){elements[el.id]=el;}}};
const stream = {getTracks:()=>[{stop(){stopped++;}}]};
class Recorder {
  static isTypeSupported(type){return supported && type.includes('avc1');}
  constructor(s,options){this.state='inactive';this.mimeType=options.mimeType;}
  start(){if(failStart)throw Error('encoder unavailable');this.state='recording';}
  stop(){this.state='inactive';this.ondataavailable({data:new Blob(['final'])});this.onstop();}
}
const ctx = {Blob,Date,URL:{createObjectURL:()=> 'blob:test',revokeObjectURL(){}},MediaRecorder:Recorder,features2:0,currentNode:{name:'Win/Test'},desktop:{State:3},Q:id=>elements[id],messagebox:(title,text)=>alerts.push(text),saveAs:(blob,name)=>saved.push({blob,name}),setInterval:()=>1,clearInterval(){},window:{addEventListener(){}},document:{createElement(type){return type==='canvas'?{getContext:()=>({fillRect(){},drawImage(){draws++;}}),captureStream(){assert.equal(this.width%2,0);assert.equal(this.height%2,0);assert(this.width<=1920&&this.height<=1080);return stream;}}:{style:{}};}}};
vm.createContext(ctx);vm.runInContext(fs.readFileSync(path.join(__dirname,'fixtures','desktop-video-mp4.js'),'utf8'),ctx);
ctx.deskRecordSession();assert(ctx.deskVideoRecording);assert.equal(ctx.deskVideoRecording.recorder.state,'recording');assert(draws>0);
ctx.deskVideoRecording.recorder.ondataavailable({data:new Blob(['first'])});
ctx.desktop.State=0;ctx.deskRecordSession();assert.equal(ctx.deskVideoRecording,null);assert.equal(saved.length,1);assert.equal(saved[0].blob.size,10);assert(saved[0].blob.type.startsWith('video/mp4'));assert(saved[0].name.startsWith('Desktop-Win_Test-'));assert(saved[0].name.endsWith('.mp4'));assert.equal(stopped,1);
ctx.deskVideoStop();assert.equal(saved.length,1);
ctx.desktop.State=3;supported=false;ctx.deskRecordSession();assert.equal(ctx.deskVideoRecording,null);assert(alerts.pop().includes('MP4/H.264'));assert.equal(saved.length,1);
supported=true;failStart=true;ctx.deskRecordSession();assert.equal(ctx.deskVideoRecording,null);assert.equal(stopped,2);assert.equal(saved.length,1);assert(alerts.pop().includes('encoder unavailable'));
ctx.features2=0x400;failStart=false;ctx.deskRecordSession();assert.equal(ctx.deskVideoRecording,null);
console.log('PASS: H.264 MP4 selection, even bounded dimensions, final chunks, disconnect stop, one download, cleanup, unsupported encoder, start failure, recording policy.');
