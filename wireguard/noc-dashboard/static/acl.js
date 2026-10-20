'use strict';
let aclTimer=null;
let aclOpenGeneration=0;
const aclCache=new Map();
let aclRequest=null;
function normalizeAclPort(value){const port=String(value??'').trim();return /^any$/i.test(port)?'':port}
function aclLabel(p){return p.legacy?'Allow All':({allow_all:'Allow All',custom:'Custom ACL',deny_all:'Deny All'})[p.mode]||'Chưa rõ'}
function aclStatus(a){return ({applied:'Applied',pending:'Pending',error:'Error'})[a?.status]||a?.status||'Chưa xác định'}
function aclSummary(id){
 const c=aclCache.get(String(id));
 if(!c)return '<span class="acl-skeleton" aria-label="Chưa tải policy"></span>';
 const status=aclStatus(c.item.apply),bad=!['applied','pending'].includes(c.item.apply?.status);
 return `<span>${esc(aclLabel(c.item.policy))}</span> <span class="acl-state ${bad?'is-error':''}" title="${esc(c.item.apply?.error||status)}">${esc(status)}</span>${c.error?`<span class="acl-stale" role="status" title="${esc(c.error)}">⚠ Chưa cập nhật</span>`:''}`;
}
function paintAcl(){document.querySelectorAll('[data-acl-summary]').forEach(el=>{el.innerHTML=aclSummary(el.dataset.aclSummary)})}
async function wireAcl(){
 document.querySelectorAll('[data-acl]').forEach(b=>b.onclick=()=>openAcl(b.dataset.acl));
 if(!document.querySelector('[data-acl-summary]'))return;
 paintAcl();
 if(!aclRequest)aclRequest=(async()=>{try{const data=await api('/api/acl/clients');for(const item of data.items)aclCache.set(String(item.client.id),{item,error:null})}catch(e){document.querySelectorAll('[data-acl-summary]').forEach(el=>{const c=aclCache.get(el.dataset.aclSummary);if(c)c.error=e.message;else el.innerHTML=`<span class="acl-stale" role="alert" title="${esc(e.message)}">⚠ ACL không khả dụng</span>`});throw e}finally{aclRequest=null}})();
 try{await aclRequest;paintAcl()}catch{document.querySelectorAll('[data-acl-summary]').forEach(el=>{if(aclCache.has(el.dataset.aclSummary))el.innerHTML=aclSummary(el.dataset.aclSummary)})}
}
function finishAclSave(result,id){
 aclCache.set(String(id),{item:result,error:null});paintAcl();
 if(result.apply.status!=='applied')throw Error(`ACL ${aclStatus(result.apply)}${result.apply.error?' · '+result.apply.error:''}`);
 closeModal();toast(`Đã lưu và áp dụng ACL${result.warnings?.length?' · '+result.warnings.join('; '):''}`);wireAcl();
}
async function openAcl(id,editMode=false){
 clearInterval(aclTimer);
 const generation=++aclOpenGeneration;
 const title=editMode?'Sửa Client':'Access Control';
 modal(title,'<div class="acl-opening" role="status"><span class="loading-spinner" aria-hidden="true"></span>Đang tải policy…</div>','<button class="btn ghost" data-close>Hủy</button>');
 $('#modal-root .modal').classList.add('acl-editor');
 try{
  let d=await api(`/api/acl/clients/${encodeURIComponent(id)}`);
  if(generation!==aclOpenGeneration)return;
  let rules=structuredClone(d.policy.rules);
  modal(editMode?'Sửa Client':'Access Control',`${editMode?`<label class="form-field">Tên Client<input id="acl-client-name" maxlength="64" value="${esc(d.client.name)}"></label><button class="btn ghost" id="acl-save-name">Lưu tên Client</button>`:''}<p>${esc(d.client.name)} · ${esc(d.client.ip)} · Revision ${d.policy.revision}</p><p id="acl-apply" role="status">Apply Status: ${esc(aclStatus(d.apply))}${d.apply.error?' · '+esc(d.apply.error):''}</p>${d.policy.legacy?'<p class="note-box">Đang giữ nguyên quyền của peer hiện hữu. Lưu sẽ chuyển peer sang chính sách ACL trong phạm vi được duyệt.</p>':''}<label class="form-field">Policy Mode<select id="acl-mode"><option value="allow_all">Allow All (approved scope)</option><option value="custom">Custom ACL</option><option value="deny_all">Deny All</option></select></label><details class="acl-scope"><summary>Phạm vi được duyệt</summary><p>${esc(d.scope.join(', '))}</p></details><div class="acl-rule-head" aria-hidden="true"><span>IP / Subnet</span><span>Protocol</span><span>Destination Port</span><span>Enable</span><span></span></div><div id="acl-rules"></div><datalist id="acl-port-options"><option value="Any">Tất cả port</option></datalist><button class="btn ghost" id="acl-add">＋ Thêm ACL Rule</button><p id="acl-error" role="alert"></p><p>Effective Policy: ${esc(d.effective)}</p><details class="acl-coverage"><summary>Client Route Coverage</summary><p>Client AllowedIPs (route): ${esc(d.routeCoverage.clientRoutes.join(", "))}</p><p>Cấu hình trên thiết bị chưa được xác minh; đối chiếu cấu hình server hiện xuất. Lưu ACL không đổi key, Address, Endpoint và không restart tunnel.</p>${d.routeCoverage.destinations.map(r=>`<p>${esc(r.destination)} · ${esc(r.coverage)} · ${esc(r.effectiveAccess)} ${esc(r.warning||"")}</p>`).join("")}</details><details class="acl-audit"><summary>ACL Audit Log</summary><pre id="acl-audit" style="white-space:pre-wrap;max-height:220px;overflow:auto"></pre></details>`,`<button class="btn ghost" data-close>Hủy</button><button class="btn primary" id="acl-save">Save Policy</button>`);
  $('#modal-root .modal').classList.add('acl-editor');
  $('#acl-mode').value=d.policy.mode;
  if(editMode)$('#acl-save-name').onclick=()=>busyAction($('#acl-save-name'),async()=>{try{await api(`/api/clients/${encodeURIComponent(id)}`,{method:'POST',body:JSON.stringify({name:$('#acl-client-name').value.trim()})});toast('Đã lưu tên Client');if(generation===aclOpenGeneration)await openAcl(id,true)}catch(e){if(generation===aclOpenGeneration&&$('#acl-error'))$('#acl-error').textContent=e.message}},'Đang lưu…');
  function draw(){const root=$('#acl-rules');root.innerHTML=rules.map((r,i)=>`<div class="acl-rule" data-rule="${i}"><label class="form-field">IP/Subnet được phép<input data-field="destination" value="${esc(r.destination)}" placeholder="IPv4/CIDR"></label><label class="form-field">Protocol<select data-field="protocol">${['any','tcp','udp','icmp'].map(p=>`<option value="${p}" ${p===r.protocol?'selected':''}>${p==='any'?'Any':p.toUpperCase()}</option>`).join('')}</select></label><label class="form-field">Destination Port<input data-field="ports" value="${esc(r.ports||'Any')}" list="acl-port-options" placeholder="Any hoặc 443, 8000-8080" aria-label="Destination Port rule ${i+1}" autocomplete="off"></label><label class="acl-enable"><span>Enable</span><input type="checkbox" data-field="enabled" aria-label="Bật rule ${i+1}" ${r.enabled?'checked':''}></label><button type="button" class="acl-delete" data-remove="${i}" aria-label="Xóa rule ${i+1}" title="Xóa rule"><svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7"/></svg></button></div>`).join('');root.querySelectorAll('[data-field]').forEach(el=>el.onchange=()=>{const i=Number(el.closest('[data-rule]').dataset.rule);rules[i][el.dataset.field]=el.type==='checkbox'?el.checked:el.dataset.field==='ports'?normalizeAclPort(el.value):el.value});root.querySelectorAll('[data-remove]').forEach(el=>el.onclick=()=>{rules.splice(Number(el.dataset.remove),1);draw()})}
  draw();$('#acl-add').onclick=()=>{$('#acl-mode').value='custom';rules.push({destination:'',protocol:'any',ports:'',enabled:true});draw()};
  $('#acl-save').onclick=async()=>{const b=$('#acl-save');b.disabled=true;b.textContent='Đang lưu…';b.setAttribute('aria-busy','true');try{const result=await api(`/api/acl/clients/${encodeURIComponent(id)}`,{method:'POST',body:JSON.stringify({revision:d.policy.revision,mode:$('#acl-mode').value,rules})});if(generation!==aclOpenGeneration)return;d=result;finishAclSave(result,id)}catch(e){if(generation===aclOpenGeneration&&$('#acl-error'))$('#acl-error').textContent=e.message}finally{if(b.isConnected){b.disabled=false;b.textContent='Save Policy';b.removeAttribute('aria-busy')}}};
  // Audit is loaded only on demand; it never delays editor interaction.
  const auditBox=$('.acl-audit');let auditLoading=false;
  auditBox.ontoggle=async()=>{if(!auditBox.open||auditLoading||auditBox.dataset.loaded)return;auditLoading=true;const out=$('#acl-audit');out.textContent='Đang tải lịch sử…';try{const audit=await api(`/api/acl/clients/${encodeURIComponent(id)}/audit`);if(generation!==aclOpenGeneration)return;out.textContent=JSON.stringify(audit.items,null,2);auditBox.dataset.loaded='true'}catch(e){if(out.isConnected)out.textContent=e.message}finally{auditLoading=false}};
  aclTimer=setInterval(async()=>{if(generation!==aclOpenGeneration||!$('#acl-apply')){clearInterval(aclTimer);return}if($('#acl-save')?.disabled)return;try{const now=await api(`/api/acl/clients/${encodeURIComponent(id)}`);if(generation!==aclOpenGeneration||!$('#acl-apply'))return;$('#acl-apply').textContent=`Apply Status: ${aclStatus(now.apply)}${now.apply.error?' · '+now.apply.error:''}`;if(now.policy.revision!==d.policy.revision){$('#acl-error').textContent='Policy đã thay đổi ở giao diện khác. Đóng và mở lại trước khi lưu.';$('#acl-save').disabled=true}}catch(e){if(generation!==aclOpenGeneration||!$('#acl-apply'))return;$('#acl-apply').textContent='⚠ Không cập nhật Apply Status: '+e.message}},5000);
 }catch(e){if(generation!==aclOpenGeneration)return;const body=$('#modal-root .modal-body');if(body)body.innerHTML=`<p role="alert">${esc(e.message)}</p><button class="btn ghost" id="acl-retry">Thử lại</button>`;$('#acl-retry').onclick=()=>openAcl(id,editMode)}
}
