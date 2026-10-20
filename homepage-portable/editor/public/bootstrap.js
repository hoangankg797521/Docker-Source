// Loaded by Homepage's native custom.js as well as the gateway HTML injection.
// This also covers domain tabs with cached HTML from before the editor existed.
(()=>{
 function load(){
  if(document.getElementById('hp-edit')||document.querySelector('script[data-hp-editor-loader]'))return;
  const version='20261016.2';
  if(!document.querySelector('link[data-hp-editor-style]')){const css=document.createElement('link');css.rel='stylesheet';css.href='/editor/editor.css?v='+version;css.dataset.hpEditorStyle='';document.head.append(css);}
  const script=document.createElement('script');script.src='/editor/editor.js?v='+version;script.dataset.hpEditorLoader='';script.defer=true;script.onerror=()=>script.remove();document.body.append(script);
 }
 if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',load,{once:true});else load();
})();
