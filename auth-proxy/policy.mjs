import { createHash } from 'node:crypto';

export class Denied extends Error {
  constructor(code = 403) {
    super('Request denied');
    this.code = code;
  }
}

const apiRoutes = new Map([
  ['/chat/completions', 'POST'], ['/v1/chat/completions', 'POST'],
  ['/responses', 'POST'], ['/v1/responses', 'POST'],
  ['/embeddings', 'POST'], ['/v1/embeddings', 'POST'],
]);
const adminReadRoutes = new Set(['/model/info', '/team/info', '/key/info']);
const adminWriteRoutes = new Set(['/key/block', '/key/unblock']);
const fields = new Set([
  'model', 'messages', 'input', 'instructions', 'stream', 'stream_options',
  'max_tokens', 'max_completion_tokens', 'max_output_tokens', 'temperature',
  'top_p', 'tools', 'tool_choice', 'parallel_tool_calls', 'response_format',
  'text', 'reasoning', 'store', 'encoding_format',
  'dimensions', 'user',
]);

export function validateConfig(config) {
  const uuid = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;
  if (/REPLACE_|example\.com/.test(JSON.stringify(config))) throw new Error('Unresolved deployment configuration');
  if (!uuid.test(config.tenantId) || !uuid.test(config.adminClientId)) throw new Error('Invalid tenant/client ID');
  if (!/^llm-api\.[a-z0-9.-]+$/.test(config.apiHost) ||
      config.adminHost !== config.apiHost.replace(/^llm-api\./, 'llm-admin.')) throw new Error('Invalid paired hosts');
  if (!config.apiAudience || !config.apiClientIds?.length || !config.apiClientIds.every(value => uuid.test(value))) throw new Error('Missing API audience/client allowlist');
  if (config.apiAudience === config.adminClientId || config.apiAudience === `api://${config.adminClientId}`) throw new Error('API and admin applications must be distinct');
  const identities = new Set();
  const keyFiles = new Set();
  for (const binding of config.bindings) {
    const identity = `${binding.plane}:${binding.oid}`;
    if (!uuid.test(binding.oid) || identities.has(identity)) throw new Error('Invalid/duplicate binding');
    identities.add(identity);
    if (binding.principalType !== undefined &&
      (!['User', 'ServicePrincipal'].includes(binding.principalType) ||
       (binding.plane === 'admin' && binding.principalType !== 'User'))) throw new Error('Invalid principal type');
    if (binding.clientIds !== undefined &&
      (binding.plane !== 'api' || binding.principalType !== 'User' || !Array.isArray(binding.clientIds) ||
       !binding.clientIds.length || !binding.clientIds.every(value => config.apiClientIds.includes(value)))) throw new Error('Invalid delegated client scope');
    if (binding.role === 'audit_reader') {
      if (binding.plane !== 'admin' || binding.keyFile || binding.models?.length) throw new Error('Audit readers cannot carry LiteLLM backend privileges');
      continue;
    }
    if (!['api', 'admin'].includes(binding.plane) || !/^[a-z0-9-]+$/.test(binding.keyFile)) throw new Error('Invalid binding');
    if (keyFiles.has(`${binding.plane}:${binding.keyFile}`)) throw new Error('Shared identity credential is forbidden');
    keyFiles.add(`${binding.plane}:${binding.keyFile}`);
    if (!['internal_user', 'proxy_admin_viewer', 'proxy_admin'].includes(binding.role) ||
        (binding.plane === 'api') !== (binding.role === 'internal_user')) throw new Error('Invalid role');
    if (!Array.isArray(binding.models) || !binding.models.length || binding.models.includes('*')) throw new Error('Explicit model ACL required');
    if (binding.audit?.capture && (binding.plane !== 'api' || !/^[a-zA-Z0-9-]{1,128}$/.test(binding.audit.teamId ?? ''))) throw new Error('Audited identity requires a trusted team mapping');
  }
  return config;
}

export function subjectTag(tenantId, oid) {
  return createHash('sha256').update(`${tenantId}:${oid}`).digest('hex');
}

export function bindingFor(config, plane, claims) {
  if (claims.tid !== config.tenantId || typeof claims.oid !== 'string') throw new Denied();
  const binding = config.bindings.find(item => item.plane === plane && item.oid === claims.oid);
  if (!binding || binding.disabled === true) throw new Denied();
  if (plane === 'api') {
    if (claims.ver !== '2.0' || !config.apiClientIds.includes(claims.azp)) throw new Denied();
    const delegated = typeof claims.scp === 'string' && claims.scp.split(' ').includes('llm.invoke');
    const application = claims.idtyp === 'app' && Array.isArray(claims.roles) && claims.roles.includes('Llm.Invoke');
    if (binding.principalType === 'User' && (!delegated || claims.idtyp === 'app')) throw new Denied();
    if (binding.principalType === 'ServicePrincipal' && !application) throw new Denied();
    if (binding.clientIds && !binding.clientIds.includes(claims.azp)) throw new Denied();
    if (!delegated && !application) throw new Denied();
  } else if (!Array.isArray(claims.roles) || !claims.roles.includes(binding.role)) {
    throw new Denied();
  }
  return binding;
}

export function authorizeRoute(plane, method, rawUrl, binding) {
  if (binding.role === 'audit_reader') throw new Denied();
  const path = rawUrl.split('?')[0];
  if (/[\\%;]|\/\/|\/\.{1,2}(\/|$)/.test(path)) throw new Denied();
  if (plane === 'api') {
    if (apiRoutes.get(path) !== method || rawUrl.includes('?')) throw new Denied();
  } else {
    if (method === 'GET' && adminReadRoutes.has(path)) {
      const allowedQuery = { '/model/info': ['model'], '/team/info': ['team_id'], '/key/info': ['key'] };
      const query = new URL(rawUrl, 'http://local.invalid').searchParams;
      if ([...query.keys()].some(key => !allowedQuery[path].includes(key) || query.getAll(key).length !== 1)) throw new Denied();
      return;
    }
    if (method === 'POST' && binding.role === 'proxy_admin' && adminWriteRoutes.has(path) && !rawUrl.includes('?')) return;
    throw new Denied();
  }
}

export function sanitizeBody(body, binding, subject) {
  if (!body || typeof body !== 'object' || Array.isArray(body) || Object.keys(body).some(key => !fields.has(key))) throw new Denied(400);
  if (!binding.models.includes(body.model)) throw new Denied();
  const pending = [body];
  while (pending.length) {
    const value = pending.pop();
    if (!value || typeof value !== 'object') continue;
    if ('file_id' in value || 'encrypted_content' in value || ['item_reference', 'input_file', 'file'].includes(value.type)) throw new Denied();
    pending.push(...Object.values(value));
  }
  if (body.store === true) throw new Denied();
  if (body.tools && (!Array.isArray(body.tools) || body.tools.some(tool => tool?.type !== 'function'))) throw new Denied();
  return { ...body, user: subject };
}

export function cleanHeaders(headers, key) {
  const allowed = new Set(['content-type', 'accept', 'accept-encoding']);
  const result = Object.fromEntries(Object.entries(headers).filter(([name]) => allowed.has(name.toLowerCase())));
  result.authorization = `Bearer ${key}`;
  return result;
}