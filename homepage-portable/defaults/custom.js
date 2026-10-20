/* Restore the saved palette before initializing any portal widgets. */
(() => {
 let theme = 'dark';
 try { const saved=localStorage.getItem('homepage-portal-theme');if(saved==='dark'||saved==='light')theme=saved; } catch (_) {}
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
 let theme = 'dark';
 try { const saved=localStorage.getItem(key);if(saved==='dark'||saved==='light')theme=saved; } catch (_) {}
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

/* Homepage visual editor bootstrap v2 */
// Loaded by Homepage's native custom.js as well as the gateway HTML injection.
// This also covers domain tabs with cached HTML from before the editor existed.
(()=>{
 function load(){
  if(document.getElementById('hp-edit')||document.querySelector('script[data-hp-editor-loader]'))return;
  const version='portable-1';
  if(!document.querySelector('link[data-hp-editor-style]')){const css=document.createElement('link');css.rel='stylesheet';css.href='/editor/editor.css?v='+version;css.dataset.hpEditorStyle='';document.head.append(css);}
  const script=document.createElement('script');script.src='/editor/editor.js?v='+version;script.dataset.hpEditorLoader='';script.defer=true;script.onerror=()=>script.remove();document.body.append(script);
 }
 if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',load,{once:true});else load();
})();
