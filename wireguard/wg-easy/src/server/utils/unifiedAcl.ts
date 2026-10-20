import { readFile } from 'node:fs/promises';
import Database from '#server/utils/Database';

export async function aclRequest(path: string, actor: string, body?: unknown) {
  const url = process.env.ACL_BACKEND_URL;
  if (!url) throw new Error('Unified ACL backend is not configured');
  const token = (await readFile(process.env.ACL_TOKEN_FILE || '/run/secrets/wg_token', 'utf8')).trim();
  return await $fetch<Record<string, unknown>>(`${url}${path}`, {
    method: body === undefined ? 'GET' : 'POST',
    headers: { Authorization: `Bearer ${token}`, 'X-ACL-Actor': actor },
    body: body as Record<string, unknown> | undefined,
    timeout: 15000,
    retry: 0,
  });
}

export async function syncUnifiedAcl() {
  if (!process.env.ACL_BACKEND_URL) return;
  const iface = await Database.interfaces.get();
  if (iface.firewallEnabled || iface.name !== 'wg0' || iface.ipv4Cidr !== '10.255.66.0/24') {
    throw new Error('Unified ACL requires wg0, VPN 10.255.66.0/24, native firewall disabled');
  }
  const clients = await Database.clients.getAllPublic({});
  const defaults = await Database.userConfigs.get();
  if (clients.some(c => c.serverAllowedIps.length > 0)) {
    throw new Error('Unified ACL requires exclusive /32 peer source addresses; routed peers are not supported');
  }
  const result = await aclRequest('/v1/sync', 'wg-easy-system', {
    clients: clients.map(c => ({ id: String(c.id), name: c.name, ip: c.ipv4Address, routes: c.allowedIps ?? defaults.defaultAllowedIps })),
  });
  if (result.status !== 'applied') throw new Error('ACL not ready: WireGuard activation blocked');
}
