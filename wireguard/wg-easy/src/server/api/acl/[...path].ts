import { createError, getRequestURL, readBody, getHeader } from 'h3';
import { definePermissionEventHandler } from '#server/utils/handler';
import { aclRequest } from '#server/utils/unifiedAcl';

export default definePermissionEventHandler('admin', 'any', async ({ event, user }) => {
  const url = getRequestURL(event);
  if (event.method !== 'GET' && event.method !== 'POST') throw createError({ statusCode: 405 });
  if (event.method === 'POST' && getHeader(event, 'x-acl-intent') !== 'policy-edit') {
    throw createError({ statusCode: 403, statusMessage: 'ACL request header required' });
  }
  const path = url.pathname.replace(/^\/api\/acl/, '/v1');
  if (!/^\/v1\/clients(?:\/[A-Za-z0-9_-]+(?:\/audit)?)?$/.test(path)) throw createError({ statusCode: 404 });
  try {
    return await aclRequest(path, user.username, event.method === 'POST' ? await readBody(event) : undefined);
  } catch (err: any) {
    throw createError({ statusCode: err.statusCode || 503, statusMessage: err.data?.error || 'ACL backend unavailable' });
  }
});
