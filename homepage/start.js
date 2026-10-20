'use strict';
const http=require('node:http');
const upstream=new URL(process.env.UPSTREAM||'http://homepage:3000');
const origin=(process.env.PUBLIC_ORIGINS||'http://localhost:3000').split(',')[0].trim();
function revalidate(){return new Promise((resolve,reject)=>{
 const req=http.get(new URL('/api/revalidate',upstream),{headers:{Host:new URL(origin).host}},res=>{
  res.resume();res.on('end',()=>res.statusCode===200?resolve():reject(Error('Homepage cache initialization HTTP '+res.statusCode)));
 });
 req.setTimeout(15000,()=>req.destroy(Error('Homepage cache initialization timeout')));req.on('error',reject);
});}
(async()=>{for(let i=0;i<3;i++){try{await revalidate();require('./server');return;}catch(e){if(i===2)throw e;await new Promise(r=>setTimeout(r,1000));}}})().catch(e=>{console.error(e.message);process.exit(1);});
