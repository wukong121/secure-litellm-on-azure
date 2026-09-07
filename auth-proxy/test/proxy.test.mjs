import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer, request as httpRequest } from 'node:http';
import { once } from 'node:events';
import { connect } from 'node:net';
import { createLocalJWKSet, exportJWK, generateKeyPair, SignJWT } from 'jose';
import * as oidc from 'openid-client';
import { createGateway } from '../proxy.mjs';
import { createSessions, createTokenVerifier } from '../auth.mjs';
import { subjectTag } from '../policy.mjs';
import { AuditWriter, objectName } from '../audit-capture.mjs';
import { MemoryAuditStore } from './audit-fixture.mjs';
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from '@opentelemetry/sdk-trace-base';

const tenantId = '11111111-1111-1111-1111-111111111111';
const oid = '22222222-2222-2222-2222-222222222222';
const clientId = '33333333-3333-3333-3333-333333333333';
const config = { tenantId, apiHost: 'llm-api.test.invalid', adminHost: 'llm-admin.test.invalid', apiAudience: 'api-test', apiClientIds: [clientId], bindings: [
  { plane: 'api', oid, role: 'internal_user', models: ['model-a'], keyFile: 'api-key' },
  { plane: 'admin', oid, role: 'proxy_admin', models: ['model-a'], keyFile: 'admin-key' },
] };
const { privateKey, publicKey } = await generateKeyPair('RS256');
const verifyToken = createTokenVerifier(config, createLocalJWKSet({ keys: [await exportJWK(publicKey)] }));
const token = await new SignJWT({ tid: tenantId, oid, azp: clientId, ver: '2.0', scp: 'llm.invoke' })
  .setProtectedHeader({ alg: 'RS256' }).setIssuer(`https://login.microsoftonline.com/${tenantId}/v2.0`)
  .setAudience(config.apiAudience).setIssuedAt().setNotBefore('0s').setExpirationTime('5m').sign(privateKey);

async function listen(server, context) {
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  context.after(() => { server.closeAllConnections(); server.close(); });
  return server.address().port;
}

function call(port, { path = '/v1/responses', method = 'POST', host = config.apiHost, headers = {}, body = { model: 'model-a', input: 'synthetic' } } = {}) {
  return new Promise((resolve, reject) => {
    const encoded = body === undefined ? undefined : JSON.stringify(body);
    const request = httpRequest({ host: '127.0.0.1', port, path, method, headers: { host, authorization: `Bearer ${token}`, 'content-type': 'application/json', ...(encoded ? { 'content-length': Buffer.byteLength(encoded) } : {}), ...headers } }, response => {
      const chunks = [];
      response.on('data', chunk => chunks.push(chunk));
      response.on('end', () => resolve({ code: response.statusCode, headers: response.headers, text: Buffer.concat(chunks).toString() }));
    });
    request.on('error', reject);
    request.end(encoded);
  });
}

test('real HTTP proxy forwards one internal key and trusted user, with no untrusted identity headers', { timeout: 5000 }, async context => {
  let received;
  const upstream = createServer(async (request, response) => {
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    received = { headers: request.headers, body: JSON.parse(Buffer.concat(chunks).toString()) };
    response.writeHead(200, { 'content-type': 'application/json', 'set-cookie': 'private=secret', location: 'http://internal.invalid/' });
    response.end('{"ok":true}');
  });
  const upstreamPort = await listen(upstream, context);
  const events = [];
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  context.after(() => provider.shutdown());
  const proxy = createGateway({ plane: 'api', getConfig: () => config, verifyToken, keyFor: binding => `synthetic-${binding.keyFile}`, target: `http://127.0.0.1:${upstreamPort}`, audit: entry => events.push(entry), telemetry: provider.getTracer('synthetic-test') });
  const port = await listen(proxy, context);
  const result = await call(port, { headers: { 'x-team-id': 'forged', 'x-litellm-api-key': 'forged', cookie: 'forged', traceparent: 'forged' }, body: { model: 'model-a', input: 'synthetic', user: 'forged' } });
  assert.equal(result.code, 200);
  assert.equal(received.headers.authorization, 'Bearer synthetic-api-key');
  assert.equal(received.headers.cookie, undefined);
  assert.equal(received.headers['x-team-id'], undefined);
  assert.equal(received.headers['x-litellm-api-key'], undefined);
  assert.equal(received.body.user, subjectTag(tenantId, oid));
  assert.match(received.headers.traceparent, /^00-[a-f0-9]{32}-[a-f0-9]{16}-01$/);
  assert.equal(received.headers.traceparent.split('-')[1], result.headers['x-trace-id']);
  assert.equal(received.headers['x-request-id'], result.headers['x-request-id']);
  await provider.forceFlush();
  const span = exporter.getFinishedSpans()[0];
  assert.equal(span.spanContext().traceId, result.headers['x-trace-id']);
  assert.deepEqual(Object.keys(span.attributes).sort(), ['gateway.outcome', 'gateway.plane', 'gateway.request_id', 'http.response.status_code']);
  assert.ok(!JSON.stringify(span.attributes).includes(token));
  assert.equal(result.headers['set-cookie'], undefined);
  assert.equal(result.headers.location, undefined);
  assert.ok(!JSON.stringify(events).includes(token));
  for (const update of [{ host: config.adminHost }, { path: '/key/generate' }, { body: { model: 'other' } }, { body: { model: 'model-a', previous_response_id: 'someone-elses-response' } }, { headers: { authorization: 'Bearer bad' } }]) assert.ok((await call(port, update)).code >= 400);
});

test('SSE first event reaches client before upstream finishes', { timeout: 5000 }, async context => {
  let finish;
  const upstream = createServer((request, response) => {
    request.resume();
    response.writeHead(200, { 'content-type': 'text/event-stream' });
    response.write('data: first\n\n');
    finish = () => response.end('data: [DONE]\n\n');
  });
  const upstreamPort = await listen(upstream, context);
  const port = await listen(createGateway({ plane: 'api', getConfig: () => config, verifyToken, keyFor: () => 'synthetic-key', target: `http://127.0.0.1:${upstreamPort}`, audit: () => {} }), context);
  await new Promise((resolve, reject) => {
    const request = httpRequest({ hostname: '127.0.0.1', port, path: '/v1/responses', method: 'POST', headers: { host: config.apiHost, authorization: `Bearer ${token}`, 'content-type': 'application/json' } }, response => {
      response.once('data', chunk => { assert.match(chunk.toString(), /data: first/); finish(); });
      response.on('end', resolve);
    });
    request.on('error', reject);
    request.end(JSON.stringify({ model: 'model-a', input: 'synthetic', stream: true }));
  });
});

test('admin OIDC login uses PKCE and state; session and CSRF protect writes', { timeout: 5000 }, async context => {
  const sessions = createSessions(new Uint8Array(32).fill(2), config.adminHost);
  const cookie = await sessions.seal({ tid: tenantId, oid, roles: ['proxy_admin'], csrf: 'test-csrf' }, 'session', Math.floor(Date.now() / 1000) + 300);
  const upstream = createServer((request, response) => { request.resume(); response.end('{"ok":true}'); });
  const upstreamPort = await listen(upstream, context);
  const oidcConfig = new oidc.Configuration({ issuer: `https://login.microsoftonline.com/${tenantId}/v2.0`, authorization_endpoint: `https://login.microsoftonline.com/${tenantId}/oauth2/v2.0/authorize` }, clientId, 'synthetic-secret');
  const port = await listen(createGateway({ plane: 'admin', getConfig: () => config, sessions, oidcConfig, keyFor: () => 'synthetic-admin-key', target: `http://127.0.0.1:${upstreamPort}`, audit: () => {} }), context);
  const admin = { host: config.adminHost, method: 'GET', path: '/auth/login', headers: { authorization: '' } };
  assert.equal((await call(port, { ...admin, path: '/' })).headers.location, '/auth/login');
  const login = await call(port, admin);
  assert.equal(login.code, 302);
  const location = new URL(login.headers.location);
  assert.equal(location.searchParams.get('code_challenge_method'), 'S256');
  assert.ok(location.searchParams.get('state'));
  assert.ok(location.searchParams.get('nonce'));
  assert.equal(location.searchParams.get('redirect_uri'), `https://${config.adminHost}/auth/callback`);
  const headers = { authorization: '', cookie: `__Host-llm-admin=${cookie}` };
  assert.equal((await call(port, { ...admin, path: '/auth/session', headers })).code, 200);
  assert.equal((await call(port, { ...admin, path: '/model/info' })).code, 401);
  assert.equal((await call(port, { ...admin, path: '/model/info', headers: { authorization: `Bearer ${token}` } })).code, 401);
  const write = { ...admin, method: 'POST', path: '/key/block', body: { key: 'synthetic-target-key' }, headers };
  assert.equal((await call(port, write)).code, 403);
  assert.equal((await call(port, { ...write, headers: { ...headers, origin: `https://${config.adminHost}`, 'x-csrf-token': 'test-csrf' } })).code, 200);
  assert.equal((await call(port, { ...write, headers: { ...headers, origin: 'https://evil.invalid', 'x-csrf-token': 'test-csrf' } })).code, 403);
  assert.equal((await call(port, { ...admin, path: '/auth/callback?code=bad&state=bad', headers })).code, 401);
});

test('WebSocket upgrades, broken identity dependencies and missing keys fail closed', { timeout: 5000 }, async context => {
  let broken = false;
  const port = await listen(createGateway({ plane: 'api', getConfig: () => { if (broken) throw new Error('sensitive dependency error'); return config; }, verifyToken, keyFor: () => { throw new Error('sensitive key error'); }, target: 'http://127.0.0.1:1', audit: () => {} }), context);
  const missingKey = await call(port);
  assert.equal(missingKey.code, 503);
  assert.ok(!missingKey.text.includes('sensitive'));
  broken = true;
  assert.equal((await call(port)).code, 503);
  assert.equal((await call(port, { method: 'GET', path: '/readyz' })).code, 503);
  await new Promise((resolve, reject) => {
    const socket = connect(port, '127.0.0.1', () => socket.write(`GET /v1/responses HTTP/1.1\r\nHost: ${config.apiHost}\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n\r\n`));
    socket.on('data', chunk => assert.match(chunk.toString(), /403 Forbidden/));
    socket.on('end', resolve);
    socket.on('error', reject);
  });
});

test('unavailable backend returns a generic gateway error, never credentials', { timeout: 5000 }, async context => {
  const port = await listen(createGateway({ plane: 'api', getConfig: () => config, verifyToken, keyFor: () => 'synthetic-private-key', target: 'http://127.0.0.1:1', audit: () => {} }), context);
  const result = await call(port);
  assert.equal(result.code, 502);
  assert.ok(!result.text.includes('synthetic-private-key'));
});

test('Stage 9 edge check rejects bypass before token verification, but does not replace authentication', { timeout: 5000 }, async context => {
  const frontDoorId = '99999999-9999-4999-8999-999999999999';
  let authCalls = 0;
  const port = await listen(createGateway({ plane: 'api', getConfig: () => config, frontDoorId, verifyToken: async value => { authCalls += 1; return verifyToken(value); }, keyFor: () => 'synthetic-key', target: 'http://127.0.0.1:1', audit: () => {} }), context);
  assert.equal((await call(port)).code, 403);
  assert.equal(authCalls, 0);
  assert.equal((await call(port, { headers: { 'x-azure-fdid': frontDoorId, authorization: 'Bearer bad' } })).code, 401);
  assert.equal(authCalls, 1);
  assert.equal((await call(port, { headers: { 'x-azure-fdid': frontDoorId }, host: config.adminHost })).code, 421);
  assert.equal((await call(port, { headers: { 'x-azure-fdid': frontDoorId }, path: '/audit' })).code, 403);
  assert.equal((await call(port, { method: 'GET', path: '/readyz' })).code, 200);
});

test('authorized HTTP/SSE calls produce L3 records, while missing required audit prevents upstream calls', { timeout: 5000 }, async context => {
  const store = new MemoryAuditStore();
  const writer = new AuditWriter(store);
  const auditedConfig = structuredClone(config);
  auditedConfig.bindings[0].audit = { capture: true, teamId: 'team-a' };
  let upstreamCalls = 0;
  const upstreamPort = await listen(createServer((request, response) => {
    upstreamCalls += 1;
    request.resume();
    response.writeHead(200, { 'content-type': 'text/event-stream' });
    response.end('data: {"delta":"synthetic response"}\n\ndata: [DONE]\n\n');
  }), context);
  const options = { plane: 'api', getConfig: () => auditedConfig, verifyToken, keyFor: () => 'synthetic-key', target: `http://127.0.0.1:${upstreamPort}`, audit: () => {} };
  const port = await listen(createGateway({ ...options, l3: writer }), context);
  const result = await call(port);
  await writer.drain();
  assert.equal(result.code, 200);
  const record = await store.get('content', objectName(tenantId, result.headers['x-request-id']));
  assert.equal(record.traceId, result.headers['x-trace-id']);
  assert.equal(record.complete, true);
  assert.equal(record.content.originalRequest.input, 'synthetic');
  assert.ok(record.content.response.includes('synthetic response'));
  assert.ok(!JSON.stringify(record).includes(token));
  const disabledPort = await listen(createGateway(options), context);
  assert.equal((await call(disabledPort)).code, 503);
  assert.equal(upstreamCalls, 1);
  store.fail = 'pending';
  assert.equal((await call(port)).code, 503);
  assert.equal(upstreamCalls, 1);
});

test('audit viewer is unavailable to ordinary administrators and uses no-store CSP for auditors', { timeout: 5000 }, async context => {
  const sessions = createSessions(new Uint8Array(32).fill(3), config.adminHost);
  const audited = structuredClone(config);
  audited.bindings[1] = { oid, plane: 'admin', role: 'audit_reader', models: [] };
  const cookie = await sessions.seal({ tid: tenantId, oid, roles: ['audit_reader'], csrf: 'synthetic' }, 'session', Math.floor(Date.now() / 1000) + 300);
  const port = await listen(createGateway({ plane: 'admin', getConfig: () => audited, sessions, auditReader: {}, target: 'http://127.0.0.1:1', audit: () => {} }), context);
  const options = { method: 'GET', host: config.adminHost, path: '/audit', headers: { authorization: '', cookie: `__Host-llm-admin=${cookie}` } };
  const response = await call(port, options);
  assert.equal(response.code, 200);
  assert.match(response.headers['content-security-policy'], /frame-ancestors 'none'/);
  assert.equal(response.headers['cache-control'], 'no-store');
  audited.bindings[1].role = 'proxy_admin';
  assert.equal((await call(port, options)).code, 403);
});