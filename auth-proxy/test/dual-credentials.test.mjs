import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer, request as httpRequest } from 'node:http';
import { once } from 'node:events';
import { createHash } from 'node:crypto';
import { createGateway } from '../proxy.mjs';
import { apiCredentials, validateConfig } from '../policy.mjs';

const tenant = '11111111-1111-1111-1111-111111111111';
const subject = '22222222-2222-2222-2222-222222222222';
const client = '33333333-3333-3333-3333-333333333333';
const config = { tenantId: tenant, adminClientId: client, apiHost: 'llm-api.test.invalid', adminHost: 'llm-admin.test.invalid', apiAudience: 'test-api', apiClientIds: [client], bindings: [
  { plane: 'api', oid: subject, principalType: 'User', role: 'internal_user' },
] };

async function listen(server, context) {
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  context.after(() => { server.closeAllConnections(); server.close(); });
  return server.address().port;
}

test('API admission config has no backend credential mapping or model ACL', () => {
  assert.equal(validateConfig(config), config);
  for (const update of [{ keyFile: 'old-key' }, { models: ['old-model'] }, { principalType: undefined }]) {
    assert.throws(() => validateConfig({ ...config, bindings: [{ ...config.bindings[0], ...update }] }));
  }
});

test('duplicate, missing and ambiguous credential headers are rejected', () => {
  const headers = { authorization: 'Bearer synthetic-token', 'x-litellm-api-key': 'synthetic-vkey' };
  const rawHeaders = Object.entries(headers).flat();
  assert.deepEqual(apiCredentials({ headers, rawHeaders }), { token: 'synthetic-token', key: 'synthetic-vkey' });
  for (const name of Object.keys(headers)) {
    assert.throws(() => apiCredentials({ headers, rawHeaders: [...rawHeaders, name.toUpperCase(), headers[name]] }));
    assert.throws(() => apiCredentials({ headers, rawHeaders: rawHeaders.filter((value, index) => Math.floor(index / 2) !== Object.keys(headers).indexOf(name)) }));
  }
  for (const key of ['', 'first,second', 'Bearer key', 'key\nvalue', 'key'.repeat(400)]) {
    assert.throws(() => apiCredentials({ headers: { ...headers, 'x-litellm-api-key': key }, rawHeaders }));
  }
});

test('both credentials required; LiteLLM alone decides key and model permissions', { timeout: 5000 }, async context => {
  const received = [];
  const events = [];
  const upstream = await listen(createServer(async (request, response) => {
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    received.push({ headers: request.headers, body: JSON.parse(Buffer.concat(chunks).toString()) });
    const code = request.headers.authorization === 'Bearer invalid-vkey' ? 401 : request.headers.authorization === 'Bearer allowed-vkey' ? 200 : 403;
    response.writeHead(code, { 'content-type': 'application/json' });
    response.end('{"error":"backend decision"}');
  }), context);
  const port = await listen(createGateway({ plane: 'api', getConfig: () => config,
    verifyToken: async token => {
      assert.equal(token, 'synthetic-token');
      return { tid: tenant, oid: subject, azp: client, ver: '2.0', scp: 'llm.invoke' };
    },
    keyFor: () => assert.fail('API must not read internal credentials'),
    target: `http://127.0.0.1:${upstream}`, audit: entry => events.push(entry),
  }), context);
  const call = headers => new Promise((resolve, reject) => {
    const request = httpRequest({ host: '127.0.0.1', port, path: '/v1/responses', method: 'POST', headers: { host: config.apiHost, 'content-type': 'application/json', ...headers } }, response => {
      response.resume();
      response.on('end', () => resolve(response.statusCode));
    });
    request.on('error', reject);
    request.end(JSON.stringify({ model: 'backend-only-model', input: 'synthetic' }));
  });
  assert.equal(await call({ authorization: 'Bearer synthetic-token' }), 401);
  assert.equal(await call({ 'x-litellm-api-key': 'synthetic-vkey' }), 401);
  assert.equal(received.length, 0);
  assert.equal(await call({ authorization: 'Bearer synthetic-token', 'x-litellm-api-key': 'synthetic-vkey' }), 403);
  assert.equal(await call({ authorization: 'Bearer synthetic-token', 'x-litellm-api-key': 'invalid-vkey' }), 401);
  assert.equal(received.length, 2);
  assert.equal(received[0].headers.authorization, 'Bearer synthetic-vkey');
  assert.equal(received[0].headers['x-litellm-api-key'], undefined);
  assert.equal(received[0].body.model, 'backend-only-model');
  assert.deepEqual(await Promise.all(['allowed-vkey', 'invalid-vkey'].map(key => call({ authorization: 'Bearer synthetic-token', 'x-litellm-api-key': key }))), [200, 401]);
  assert.deepEqual(received.slice(2).map(item => item.headers.authorization).sort(), ['Bearer allowed-vkey', 'Bearer invalid-vkey']);
  assert.ok(!JSON.stringify(received).includes('synthetic-token'));
  assert.ok(!JSON.stringify(events).includes('synthetic-token'));
  assert.ok(!JSON.stringify(events).includes('synthetic-vkey'));
  assert.ok(events.some(entry => entry.keyFingerprint === createHash('sha256').update('synthetic-vkey').digest('hex') && entry.subject));
});