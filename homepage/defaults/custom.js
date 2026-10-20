/* Restore the saved palette before initializing any portal widgets. */
(() => {
 let theme = 'light';
 try { if (localStorage.getItem('homepage-portal-theme') === 'dark') theme = 'dark'; } catch (_) {}
 document.documentElement.dataset.vhTheme = theme;
})();
/* Local service filter: no external search or API calls. */
(() => {
 const normalize = s => s.toLowerCase().normalize('NFD').replace(/[\u0300-\u036f]/g,'').replace(/đ/g,'d');
 function install() {
  const host = document.getElementById('widgets-wrap');
  if (!host || document.getElementById('vh-search')) return;
  const wrap = document.createElement('div'); wrap.id = 'vh-search';
  const input = document.createElement('input'); input.type = 'search'; input.placeholder = 'Tìm dịch vụ: Proxmox, Wi-Fi, sao lưu…'; input.setAttribute('aria-label','Tìm dịch vụ'); input.autocomplete = 'off';
  const status = document.createElement('div'); status.id = 'vh-search-status'; status.setAttribute('role','status');
  wrap.append(input,status); host.append(wrap);
  input.addEventListener('input', () => {
   const query = normalize(input.value.trim()); let found = 0;
   document.querySelectorAll('.services-group').forEach(group => {
    let count = 0;
    group.querySelectorAll('.services-list > .service').forEach(item => {
     const match = !query || normalize(item.textContent).includes(query);
     item.style.display = match ? 'flex' : 'none'; if (match) count++;
    });
    group.style.display = count ? '' : 'none'; found += count;
   });
   status.textContent = query ? (found ? found + ' dịch vụ phù hợp' : 'Không tìm thấy dịch vụ. Hãy thử từ khóa khác.') : '';
  });
 }
 const observer = new MutationObserver(install); observer.observe(document.documentElement,{childList:true,subtree:true}); install();
})();

/* Persisted light/dark preference, scoped to this portal. */
(() => {
 const key = 'homepage-portal-theme';
 let theme = 'light';
 try { if (localStorage.getItem(key) === 'dark') theme = 'dark'; } catch (_) {}
 function apply() {
  document.documentElement.dataset.vhTheme = theme;
  const button = document.getElementById('vh-theme-toggle');
  if (button) {
   button.textContent = theme === 'dark' ? '☀' : '☾';
   button.setAttribute('aria-label', theme === 'dark' ? 'Chuyển sang giao diện sáng' : 'Chuyển sang giao diện tối');
   button.setAttribute('aria-pressed', String(theme === 'dark'));
   button.title = button.getAttribute('aria-label');
  }
 }
 function install() {
  const host = document.querySelector('.information-widget-resource')?.parentElement;
  if (!host || document.getElementById('vh-theme-toggle')) return;
  const button = document.createElement('button'); button.id = 'vh-theme-toggle'; button.type = 'button';
  button.addEventListener('click', () => {
   theme = theme === 'dark' ? 'light' : 'dark';
   try { localStorage.setItem(key, theme); } catch (_) {}
   apply();
  });
  host.append(button); apply();
 }
 apply();
 new MutationObserver(install).observe(document.documentElement, {childList:true,subtree:true}); install();
})();

/* Design credit, preserving the built-in footer controls. */
(() => {
 function installCredit() {
  const footer = document.getElementById('information-widgets-right');
  if (!footer || document.getElementById('vh-design-credit')) return;
  const credit = document.createElement('div');
  credit.id = 'vh-design-credit';
  credit.append('Design by ');
  const name = document.createElement('span'); name.textContent = 'Le Hoang An'; credit.append(name);
  footer.append(credit);
 }
 new MutationObserver(installCredit).observe(document.documentElement, {childList:true, subtree:true});
 installCredit();
})();

/* Original resource presentation using this installation’s native metrics. */
(() => {
 const originalFetch = window.fetch.bind(window);
 let metrics = null, pending = null;
 async function readMetrics() {
  if(pending)return pending;
  pending=(async()=>{
   const load=async type=>{const r=await originalFetch('/api/widgets/resources?type='+type+'&target=%2F',{cache:'no-store',signal:AbortSignal.timeout(8000)});if(!r.ok)throw Error('Native metrics unavailable');return r.json();};
   const [c,m,d]=await Promise.all(['cpu','memory','disk'].map(load));
   metrics={timestamp:Date.now()/1000,cpu:c.cpu.usage,memory:{used:m.memory.active??m.memory.used,total:m.memory.total},disk:{used:d.drive.used,total:d.drive.size}};
   return metrics;
  })().finally(()=>pending=null);
  return pending;
 }
 const format = bytes => (bytes / 1073741824).toLocaleString('vi-VN',{maximumFractionDigits:2}) + ' GiB';
 function render() {
  const nodes = document.querySelectorAll('.information-widget-resource');
  if (nodes.length !== 3) return;
  const fresh = metrics && Date.now()/1000-metrics.timestamp <=20;
  const values = fresh ? [metrics.cpu.toFixed(1)+'%', format(metrics.memory.used)+' / '+format(metrics.memory.total), format(metrics.disk.used)+' / '+format(metrics.disk.total)] : ['—','—','—'];
  const percents = fresh ? [metrics.cpu,100*metrics.memory.used/metrics.memory.total,100*metrics.disk.used/metrics.disk.total] : [0,0,0];
  nodes.forEach((node,i) => {
   const left=node.querySelector('.pl-0\\.5'), right=node.querySelector('.pr-1'), bar=node.querySelector('.resource-usage > div');
   if (left && left.textContent!==values[i]) left.textContent=values[i];
   const label=['CPU','RAM','Disk'][i]; if(right && right.textContent!==label) right.textContent=label;
   const width=Math.max(0,Math.min(100,percents[i]))+'%'; if(bar && Math.abs((parseFloat(bar.style.width)||0)-parseFloat(width))>0.01) bar.style.width=width;
   const title=fresh?'Tài nguyên máy đang chạy · cập nhật mỗi 5 giây':'Không có dữ liệu tài nguyên mới'; if(node.title!==title) node.title=title;
  });
 }
 // Watch only the resource row, not every mutation across the entire page.
 let queued=false, watched=null;
 const resourceObserver=new MutationObserver(() => {
  if(!queued){queued=true; setTimeout(()=>{queued=false;render();},100);}
 });
 function watchResources(){
  const row=document.querySelector('.information-widget-resource')?.parentElement;
  if(row && row!==watched){resourceObserver.disconnect();watched=row;resourceObserver.observe(row,{childList:true,subtree:true,characterData:true});}
 }
 async function refresh(){watchResources();try{await readMetrics();}catch(_){metrics=null;}render();}
 refresh(); setInterval(refresh,5000);
})();

/* Homepage visual editor bootstrap v2 */
// Loaded by Homepage's native custom.js as well as the gateway HTML injection.
// This also covers domain tabs with cached HTML from before the editor existed.
(()=>{
 function load(){
  if(document.getElementById('hp-edit')||document.querySelector('script[data-hp-editor-loader]'))return;
  const version='portable-2';
  if(!document.querySelector('link[data-hp-editor-style]')){const css=document.createElement('link');css.rel='stylesheet';css.href='/editor/editor.css?v='+version;css.dataset.hpEditorStyle='';document.head.append(css);}
  const script=document.createElement('script');script.src='/editor/editor.js?v='+version;script.dataset.hpEditorLoader='';script.defer=true;script.onerror=()=>script.remove();document.body.append(script);
 }
 if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',load,{once:true});else load();
})();
