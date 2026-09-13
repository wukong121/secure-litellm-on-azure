import { createHash, timingSafeEqual } from 'node:crypto';
import { Denied } from './policy.mjs';
import routes from './native-ui-routes.json' with { type: 'json' };

export function nativeUiEnabled(config, binding) {
  return config.nativeUi === true && binding.plane === 'admin' && ['proxy_admin', 'proxy_admin_viewer'].includes(binding.role);
}

export function nativeUiAsset(method, rawUrl) {
  if (!['GET', 'HEAD'].includes(method)) return false;
  const path = rawUrl.split('?')[0];
  if (/[\\%;]|\/\/|\/\.{1,2}(\/|$)/.test(path)) return false;
  return ['/ui', '/get_image', '/get_favicon', '/favicon.ico', '/litellm/.well-known/litellm-ui-config'].includes(path) || path.startsWith('/ui/') || /^\/litellm-asset-prefix\/_next\/static\/[A-Za-z0-9_./-]+$/.test(path);
}

export function authorizeNativeUiRead(method, rawUrl, binding) {
  if (method === 'POST' && Object.hasOwn(routes.lookup, rawUrl)) return true;
  if (method !== 'GET') return false;
  const path = rawUrl.split('?')[0];
  if (/[\\%;]|\/\/|\/\.{1,2}(\/|$)/.test(path)) throw new Denied();
  const route = /^\/spend\/logs\/ui\/[A-Za-z0-9_-]{1,256}$/.test(path) ? '/spend/logs/ui/{request_id}' : path;
  const auditRoute = Object.hasOwn(routes.audit, route);
  const fields = routes.read[route] ?? routes.audit[route];
  if (!fields) return false;
  if (auditRoute && binding.nativeAuditRead !== true) throw new Denied();
  const query = new URL(rawUrl, 'https://local.invalid').searchParams;
  for (const [key, value] of query) {
    if (!fields.includes(key) || query.getAll(key).length !== 1 || value.length > 512 || /[\x00-\x1f\x7f]/.test(value)) throw new Denied(400);
    if (['size', 'page_size'].includes(key) && (!/^[0-9]+$/.test(value) || Number(value) < 1 || Number(value) > 100)) throw new Denied(400);
    if (key === 'page' && (!/^[0-9]+$/.test(value) || Number(value) < 1 || Number(value) > 10000)) throw new Denied(400);
    if (key === 'api_key' && !/^[a-f0-9]{64}$/.test(value)) throw new Denied(400);
    if (['start_date', 'end_date'].includes(key) && !/^\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z?)?$/.test(value)) throw new Denied(400);
  }
  return true;
}

export function nativeUiKeyLookup(body, session, backendKey) {
  if (!body || Object.keys(body).length !== 1 || !Array.isArray(body.keys) || body.keys.length < 1 || body.keys.length > 100 || body.keys.some(key => typeof key !== 'string' || !/^[a-f0-9]{64}$/.test(key))) throw new Denied(400);
  return { keys: body.keys.map(key => key === session.csrf ? createHash('sha256').update(backendKey).digest('hex') : key) };
}

export function nativeUiClaims(binding, session, subject) {
  if (!/^[a-f0-9]{64}$/.test(session.csrf ?? '') || !Number.isInteger(session.exp)) throw new Denied(401);
  if (!/^key-[a-f0-9]{48}$/.test(binding.keyFile ?? '')) throw new Denied();
  return {
    user_id: 'llmgw-admin-' + binding.keyFile.slice(4),
    user_role: binding.role,
    key: session.csrf,
    login_method: 'sso',
    premium_user: false,
    auth_header_name: 'Authorization',
    disabled_non_admin_personal_key_creation: true,
    server_root_path: '',
    subject,
    exp: session.exp,
  };
}

export function verifyNativeUiRequest(request, session, origin) {
  const headers = request.rawHeaders ?? [];
  const count = headers.filter((value, index) => index % 2 === 0 && value.toLowerCase() === 'authorization').length;
  const supplied = /^Bearer ([a-f0-9]{64})$/.exec(request.headers.authorization ?? '')?.[1];
  const expected = session.csrf;
  if (count !== 1 || !supplied || !/^[a-f0-9]{64}$/.test(expected ?? '') || !timingSafeEqual(Buffer.from(supplied), Buffer.from(expected))) throw new Denied(401);
  if (request.headers['sec-fetch-site'] === 'cross-site') throw new Denied();
  if (!['GET', 'HEAD'].includes(request.method) && request.headers.origin !== origin) throw new Denied();
}

export function nativeUiCookie(value, maxAge) {
  if (!/^[A-Za-z0-9_.-]+$/.test(value) || !Number.isInteger(maxAge) || maxAge < 0 || maxAge > 300) throw new Error('Invalid UI metadata cookie');
  return `token=${value}; Path=/; Secure; SameSite=Lax; Max-Age=${maxAge}`;
}