'use strict';
const fs=require('node:fs'),path=require('node:path');
function seed({config=process.env.CONFIG_DIR||'/config',state=process.env.STATE_DIR||'/state',defaults=process.env.DEFAULTS_DIR||'/defaults',ownership=true}={}) {
  for(const dir of [config,state])fs.mkdirSync(dir,{recursive:true});
  for(const file of fs.readdirSync(defaults)){
    const target=path.join(config,file);
    if(fs.existsSync(target))continue;
    fs.copyFileSync(path.join(defaults,file),target,fs.constants.COPYFILE_EXCL);
    fs.chmodSync(target,0o644);
    if(ownership)fs.chownSync(target,1000,1000);
  }
  for(const dir of [config,state]){
    if(ownership)fs.chownSync(dir,1000,1000);
    fs.chmodSync(dir,dir===state?0o700:0o755);
  }
  console.log('Presentation defaults initialized; existing configuration preserved.');
}
if(require.main===module)seed();
module.exports={seed};
