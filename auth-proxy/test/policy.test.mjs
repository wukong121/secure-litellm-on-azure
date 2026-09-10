import test from 'node:test';
import assert from 'node:assert/strict';
import { authorizeRoute, bindingFor, cleanHeaders, sanitizeBody, validateConfig } from '../policy.mjs';

const tenant = '11111111-1111-1111-1111-111111111111';
const oid = '22222222-2222-2222-2222-222222222222';
const client = '33333333-3333-3333-3333-333333333333';
const binding = { oid, plane: 'api', role: 'internal_user', principalType: 'User' };
const config = { tenantId: tenant, adminClientId: client, apiHost: 'llm-api.test.invalid', adminHost: 'llm-admin.test.invalid', apiAudience: 'api-audience', apiClientIds: [client], bindings: [binding] };
const claims = { tid: tenant, oid, ver: '2.0', azp: client, scp: 'llm.invoke' };

test('strict host pairing and explicit non-wildcard mappings', () => {
  assert.equal(validateConfig(config), config);
  assert.throws(() => validateConfig({ ...config, apiAudience: config.adminClientId }));
  for (const update of [{ adminHost: config.apiHost }, { apiClientIds: [] }, { tenantId: 'REPLACE_TENANT' }, { bindings: [binding, binding] }, { bindings: [{ ...binding, keyFile: '../master-key' }] }, { bindings: [{ ...binding, models: ['*'] }] }]) {
    assert.throws(() => validateConfig({ ...config, ...update }));
  }
});

test('API identity requires tenant, object, client and delegated scope or application role', () => {
  assert.equal(bindingFor(config, 'api', claims), binding);
  assert.throws(() => bindingFor(config, 'api', { ...claims, scp: undefined, idtyp: 'app', roles: ['Llm.Invoke'] }));
  for (const update of [{ tid: 'other' }, { oid: 'unknown' }, { azp: 'unknown' }, { scp: 'other' }, { scp: undefined, roles: ['Llm.Invoke'] }, { ver: '1.0' }]) assert.throws(() => bindingFor(config, 'api', { ...claims, ...update }));
  assert.throws(() => bindingFor({ ...config, bindings: [{ ...binding, disabled: true }] }, 'api', claims));
});

test('admin requires a separate binding and matching role; viewer cannot write', () => {
  assert.throws(() => bindingFor(config, 'admin', claims));
  const admin = { ...binding, plane: 'admin', role: 'proxy_admin_viewer', keyFile: 'admin-key', models: ['approved-model'] };
  const adminConfig = { ...config, bindings: [admin] };
  assert.throws(() => bindingFor(adminConfig, 'admin', claims));
  assert.equal(bindingFor(adminConfig, 'admin', { ...claims, roles: ['proxy_admin_viewer'] }), admin);
  authorizeRoute('admin', 'GET', '/model/info', admin);
  assert.throws(() => authorizeRoute('admin', 'POST', '/key/block', admin));
});

test('typed bindings cannot exchange user and application credentials or delegated clients', () => {
  const user = { ...binding, principalType: 'User', clientIds: [client] };
  const typed = { ...config, bindings: [user] };
  assert.equal(validateConfig(typed), typed);
  assert.equal(bindingFor(typed, 'api', claims), user);
  assert.throws(() => bindingFor(typed, 'api', { ...claims, idtyp: 'app', roles: ['Llm.Invoke'] }));
  const application = { ...binding, principalType: 'ServicePrincipal' };
  assert.throws(() => bindingFor({ ...config, bindings: [application] }, 'api', claims));
  assert.equal(bindingFor({ ...config, bindings: [application] }, 'api', { ...claims, scp: undefined, idtyp: 'app', roles: ['Llm.Invoke'] }), application);
  assert.throws(() => validateConfig({ ...config, bindings: [{ ...application, plane: 'admin', role: 'proxy_admin' }] }));
  const otherClient = '44444444-4444-4444-8444-444444444444';
  assert.throws(() => bindingFor({ ...typed, apiClientIds: [client, otherClient] }, 'api', { ...claims, azp: otherClient }));
});

test('allowlisted methods and paths reject management and normalization bypasses', () => {
  authorizeRoute('api', 'POST', '/v1/responses', binding);
  for (const path of ['/ui', '/fallback/login', '/key/generate', '/v1/files', '/mcp', '/v1/responses/id', '/V1/responses', '/v1/responses/', '/v1/%72esponses', '//v1/responses', '/v1/../v1/responses', '/v1/responses?api_key=bad']) assert.throws(() => authorizeRoute('api', 'POST', path, binding));
  assert.throws(() => authorizeRoute('api', 'GET', '/v1/responses', binding));
});

test('model authorization belongs to LiteLLM while credentials, identity and upstream overrides remain denied', () => {
  const body = { model: 'approved-model', input: 'test', user: 'forged' };
  assert.equal(sanitizeBody(body, binding, 'trusted-subject').user, 'trusted-subject');
  assert.equal(sanitizeBody({ ...body, model: 'other' }, binding, 'trusted-subject').model, 'other');
  for (const update of [{ api_key: 'bad' }, { api_base: 'https://other.invalid' }, { extra_headers: {} }, { metadata: {} }, { model: '' }, { model: null }, { tools: [{ type: 'mcp' }] }, { input: [{ type: 'item_reference', id: 'other-object' }] }, { input: [{ encrypted_content: 'other-state' }] }, { input: [{ file_id: 'other-file' }] }, { store: true }]) assert.throws(() => sanitizeBody({ ...body, ...update }, binding, 'trusted-subject'));
});

test('forward only safe headers and a per-identity internal credential', () => {
  assert.deepEqual(cleanHeaders({ authorization: 'Bearer external', cookie: 'bad', 'x-team-id': 'forged', 'x-user-id': 'forged', 'x-forwarded-host': 'llm-admin.test.invalid', 'x-litellm-api-key': 'bad', 'content-type': 'application/json' }, 'synthetic-key'), { 'content-type': 'application/json', authorization: 'Bearer synthetic-key' });
});

test('audit reader is separate from backend administration and cannot carry backend keys', () => {
  const auditor = { oid, plane: 'admin', role: 'audit_reader', models: [] };
  const auditConfig = { ...config, bindings: [auditor] };
  assert.equal(validateConfig(auditConfig), auditConfig);
  assert.equal(bindingFor(auditConfig, 'admin', { ...claims, roles: ['audit_reader'] }), auditor);
  assert.throws(() => authorizeRoute('admin', 'GET', '/model/info', auditor));
  assert.throws(() => validateConfig({ ...config, bindings: [{ ...auditor, keyFile: 'admin-key' }] }));
  assert.throws(() => validateConfig({ ...config, bindings: [{ ...auditor, plane: 'api' }] }));
});