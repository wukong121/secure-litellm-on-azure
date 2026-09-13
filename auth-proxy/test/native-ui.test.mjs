import test from 'node:test';
import assert from 'node:assert/strict';
import { decodeJwt } from 'jose';
import { createServer, request as httpRequest } from 'node:http';
import { once } from 'node:events';
import { createGateway } from '../proxy.mjs';
import { createSessions } from '../auth.mjs';
import { authorizeNativeUiRead, nativeUiClaims, nativeUiCookie, nativeUiEnabled, nativeUiKeyLookup, verifyNativeUiRequest } from '../native-ui.mjs';

test('native UI metadata carries only a paired-session challenge, never a backend key', async () => {
  const binding = { plane: 'admin', role: 'proxy_admin', keyFile: 'key-' + 'a'.repeat(48) };
  const session = { csrf: 'b'.repeat(64), exp: Math.floor(Date.now() / 1000) + 300 };
  const claims = nativeUiClaims(binding, session, 'synthetic-subject');
  const sessions = createSessions(Buffer.alloc(32, 7), 'llm-admin.synthetic.invalid');
  const token = await sessions.uiToken(claims);
  assert.equal(decodeJwt(token).key, session.csrf);
  assert.equal(decodeJwt(token).user_id, 'llmgw-admin-' + 'a'.repeat(48));
  await assert.rejects(sessions.open(token, 'session'));
  assert.ok(nativeUiCookie(token, 300).includes('Secure; SameSite=Lax'));
  assert.ok(!nativeUiCookie(token, 300).includes('HttpOnly'));
  assert.equal(nativeUiEnabled({ nativeUi: true }, binding), true);
  assert.equal(nativeUiEnabled({}, binding), false);
  assert.equal(nativeUiEnabled({ nativeUi: true }, { ...binding, role: 'audit_reader' }), false);
});

test('native UI challenge requires one matching header and same-origin writes', () => {
  const origin = 'https://llm-admin.synthetic.invalid';
  const session = { csrf: 'a'.repeat(64) };
  const request = { method: 'POST', rawHeaders: ['Authorization', 'Bearer ' + session.csrf], headers: { authorization: 'Bearer ' + session.csrf, origin } };
  verifyNativeUiRequest(request, session, origin);
  for (const invalid of [
    { ...request, rawHeaders: [...request.rawHeaders, ...request.rawHeaders] },
    { ...request, headers: { ...request.headers, authorization: 'Bearer ' + 'b'.repeat(64) } },
    { ...request, headers: { ...request.headers, origin: 'https://other.invalid' } },
    { ...request, method: 'GET', headers: { ...request.headers, 'sec-fetch-site': 'cross-site' } },
  ]) assert.throws(() => verifyNativeUiRequest(invalid, session, origin));
  assert.throws(() => verifyNativeUiRequest(request, {}, origin));
});

test('native content read is explicit, bounded and cannot expose arbitrary management routes', () => {
  const reader = { nativeAuditRead: true };
  assert.equal(authorizeNativeUiRead('GET', '/spend/logs/ui?page=1&page_size=50', reader), true);
  assert.equal(authorizeNativeUiRead('GET', '/spend/logs/ui/chatcmpl-synthetic?start_date=2026-09-13%2000:00:00', reader), true);
  assert.equal(authorizeNativeUiRead('POST', '/spend/logs/ui', reader), false);
  assert.equal(authorizeNativeUiRead('GET', '/config/yaml', reader), false);
  for (const path of ['/spend/logs/ui?page_size=1000', '/spend/logs/ui?page=1&page=2', '/spend/logs/ui?api_key=sk-synthetic', '/spend/logs/ui?unexpected=true']) assert.throws(() => authorizeNativeUiRead('GET', path, reader));
  assert.throws(() => authorizeNativeUiRead('GET', '/spend/logs/ui', {}));
  assert.throws(() => authorizeNativeUiRead('GET', '/spend/logs/ui/chatcmpl-synthetic', { nativeAuditRead: false }));
  assert.equal(authorizeNativeUiRead('POST', '/v2/key/info', reader), true);
  assert.equal(authorizeNativeUiRead('POST', '/key/generate', reader), false);
  const lookup = nativeUiKeyLookup({ keys: ['a'.repeat(64)] }, { csrf: 'a'.repeat(64) }, 'synthetic-backend-key');
  assert.notEqual(lookup.keys[0], 'a'.repeat(64));
  assert.ok(!JSON.stringify(lookup).includes('synthetic-backend-key'));
  assert.throws(() => nativeUiKeyLookup({ keys: ['sk-raw-key'] }, {}, 'synthetic-backend-key'));
});

test('native UI assets require the enterprise session and never receive its cookies or keys', async context => {
  const binding = { plane: 'admin', oid: '22222222-2222-4222-8222-222222222222', role: 'proxy_admin', keyFile: 'key-' + 'a'.repeat(48) };
  const config = { nativeUi: true, tenantId: '11111111-1111-4111-8111-111111111111', adminHost: 'llm-admin.synthetic.invalid', bindings: [binding] };
  const sessions = createSessions(Buffer.alloc(32, 9), config.adminHost);
  const csrf = 'b'.repeat(64);
  const token = await sessions.seal({ tid: config.tenantId, oid: binding.oid, roles: [binding.role], csrf }, 'session', Math.floor(Date.now() / 1000) + 300);
  const received = [];
  const upstream = createServer((request, response) => {
    received.push(request.headers);
    response.writeHead(200, { 'content-type': 'text/html', 'set-cookie': 'unsafe-backend-cookie=value' });
    response.end('<!doctype html><title>Native fixture</title>');
  });
  upstream.listen(0, '127.0.0.1');
  await once(upstream, 'listening');
  const gateway = createGateway({ plane: 'admin', getConfig: () => config, sessions, keyFor: () => assert.fail('Static UI must not read a backend key'), target: `http://127.0.0.1:${upstream.address().port}`, audit: () => {} });
  gateway.listen(0, '127.0.0.1');
  await once(gateway, 'listening');
  context.after(() => { gateway.closeAllConnections(); gateway.close(); upstream.closeAllConnections(); upstream.close(); });
  const call = (path, headers = {}) => new Promise((resolve, reject) => {
    const request = httpRequest({ host: '127.0.0.1', port: gateway.address().port, path, headers: { host: config.adminHost, ...headers } }, response => {
      response.resume();
      response.on('end', () => resolve({ status: response.statusCode, headers: response.headers }));
    });
    request.on('error', reject);
    request.end();
  });
  assert.equal((await call('/ui/')).status, 302);
  assert.equal((await call('/ui/_next/static/file.js')).status, 401);
  const response = await call('/ui/', { cookie: '__Host-llm-admin=' + token });
  assert.equal(response.status, 200);
  assert.equal(response.headers['set-cookie'], undefined);
  assert.ok(response.headers['content-security-policy'].includes("frame-ancestors 'none'"));
  assert.equal(received[0].cookie, undefined);
  assert.equal(received[0].authorization, undefined);
  assert.equal((await call('/litellm-asset-prefix/_next/static/chunks/native.js', { cookie: '__Host-llm-admin=' + token })).status, 200);
  assert.equal((await call('/litellm-asset-prefix/_next/static/chunks/native.js')).status, 401);
  assert.equal((await call('/ui/%2e%2e/login', { cookie: '__Host-llm-admin=' + token })).status, 403);
  assert.equal((await call('/key/info', { cookie: '__Host-llm-admin=' + token, authorization: 'Bearer ' + 'c'.repeat(64) })).status, 401);
});