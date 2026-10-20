<template>
  <section class="acl-box">
    <h2>Access Control</h2>
    <p v-if="error" role="alert">{{ error }}</p>
    <template v-if="data">
      <p>{{ data.client.name }} · {{ data.client.ip }} · Revision {{ data.policy.revision }}</p>
      <p>Apply Status: <strong>{{ data.apply.status }}</strong></p>
      <p v-if="data.policy.legacy">Client hiện hữu: đang giữ nguyên quyền trước chuyển đổi. Lưu chính sách sẽ chuyển sang phạm vi ACL.</p>
      <label>Policy Mode <select v-model="mode"><option value="allow_all">Allow All (approved scope)</option><option value="custom">Custom ACL</option><option value="deny_all">Deny All</option></select></label>
      <p>Phạm vi được phê duyệt: {{ data.scope.join(', ') }}</p>
      <div v-for="(r, i) in rules" :key="i" class="acl-row">
        <input v-model="r.destination" aria-label="IP/Subnet" placeholder="IP/CIDR, ví dụ 172.16.20.50/32" />
        <select v-model="r.protocol" aria-label="Protocol"><option>any</option><option>tcp</option><option>udp</option><option>icmp</option></select>
        <input v-model="r.ports" aria-label="Destination port" placeholder="Port / range" />
        <label><input v-model="r.enabled" type="checkbox" /> Enable</label>
        <button type="button" @click="rules.splice(i, 1)">Remove Rule</button>
      </div>
      <div class="acl-row">
        <button type="button" @click="add">Add IP / Subnet / Port</button>
        <button type="button" :disabled="busy" @click="save">Save Policy</button>
        <button type="button" @click="load">Reload</button>
      </div>
      <p>Effective Policy: {{ data.effective }}</p>
      <h3>Client Route Coverage</h3>
      <p>Client AllowedIPs (định tuyến): {{ data.routeCoverage.clientRoutes.join(", ") }}</p>
      <p>Cấu hình trên thiết bị chưa được xác minh; dùng AllowedIPs mà server hiện xuất. ACL lưu tại server không đổi cấu hình hoặc restart tunnel.</p>
      <p v-for="item in data.routeCoverage.destinations" :key="item.destination">{{ item.destination }} · {{ item.coverage }} · {{ item.effectiveAccess }} {{ item.warning }}</p>
      <p v-if="notice">{{ notice }}</p>
      <details><summary>ACL Audit Log</summary><pre>{{ audit }}</pre></details>
    </template>
  </section>
</template>
<script setup lang="ts">
const props = defineProps<{ clientId: string | number }>();
type Rule = { destination: string; protocol: string; ports: string; enabled: boolean };
const data = ref<any>(null), error = ref(''), notice = ref(''), busy = ref(false);
const mode = ref('deny_all'), rules = ref<Rule[]>([]), audit = ref<any>([]);
async function load() {
  try {
    data.value = await $fetch(`/api/acl/clients/${props.clientId}`);
    mode.value = data.value.policy.mode;
    rules.value = data.value.policy.rules.map((rule: Rule) => ({ ...rule }));
    audit.value = (await $fetch<any>(`/api/acl/clients/${props.clientId}/audit`)).items;
    error.value = '';
  } catch (e: any) { error.value = e.data?.statusMessage || 'ACL chưa sẵn sàng / cần quyền admin'; }
}
function add() { mode.value = 'custom'; rules.value.push({ destination: '', protocol: 'any', ports: '', enabled: true }); }
async function save() {
  busy.value = true;
  try {
    const result = await $fetch<any>(`/api/acl/clients/${props.clientId}`, { method: 'POST', headers: { 'X-ACL-Intent': 'policy-edit' }, body: { mode: mode.value, rules: rules.value, revision: data.value.policy.revision } });
    notice.value = `Apply: ${result.apply.status}. ${(result.warnings || []).join('; ')}`;
    await load();
  } catch (e: any) { error.value = e.data?.statusMessage || 'Lưu thất bại; tải lại nếu revision bị thay đổi'; }
  finally { busy.value = false; }
}
onMounted(load);
let timer: ReturnType<typeof setInterval>;
onMounted(() => { timer = setInterval(async () => {
  try { const current = await $fetch<any>(`/api/acl/clients/${props.clientId}`); data.value.apply = current.apply; if (current.policy.revision !== data.value.policy.revision) notice.value = 'Policy đã thay đổi ở giao diện khác. Reload trước khi lưu.'; } catch { if (data.value) data.value.apply.status = 'unavailable'; }
}, 5000); });
onUnmounted(() => clearInterval(timer));
</script>
<style scoped>
.acl-box { margin-top:24px; padding:24px; border:1px solid #94a3b8; border-radius:12px; font-size:16px; }
h2 { font-size:22px; font-weight:700; } p { margin:12px 0; overflow-wrap:anywhere; } .acl-row { display:flex; flex-wrap:wrap; gap:10px; margin:12px 0; align-items:center; } input,select,button { padding:10px; border:1px solid #94a3b8; border-radius:6px; background:transparent; color:inherit; } button { cursor:pointer; } pre { white-space:pre-wrap; font-size:14px; max-height:300px; overflow:auto; }
</style>
