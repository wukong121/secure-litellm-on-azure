import assert from 'node:assert/strict';
import { randomUUID, randomBytes } from 'node:crypto';
import { createServer, request } from 'node:http';
import { once } from 'node:events';
import { createGateway } from '../proxy.mjs';
import { createSessions } from '../auth.mjs';
import { AuditWriter } from '../audit-capture.mjs';
import { AuditReader, maintainAudit } from '../audit-reader.mjs';
import { MemoryAuditStore } from './audit-fixture.mjs';

const servers = [];
const timestamp = Date.now();
const tenantId = randomUUID();
const callerId = randomUUID();
const auditorId = randomUUID();
const clientId = randomUUID();
const store = new MemoryAuditStore();
const writer = new AuditWriter(store, { clock: () => timestamp });
const approval = { id: randomUUID(), actorOid: auditorId, tenantId, teamIds: ['synthetic-team'], ticketId: 'SYNTHETIC-DEMO', reason: 'Synthetic demonstration only', approvedBy: [randomUUID(), randomUUID()], validFrom: new Date(timestamp - 60000).toISOString(), validUntil: new Date(timestamp + 3600000).toISOString(), from: new Date(timestamp - 60000).toISOString(), to: new Date(timestamp + 60000).toISOString() };
const config = { tenantId, apiHost: 'llm-api.demo.invalid', adminHost: 'llm-admin.demo.invalid', apiClientIds: [clientId], bindings: [
  { oid: callerId, plane: 'api', role: 'internal_user', models: ['synthetic-model'], keyFile: 'synthetic-key', audit: { capture: true, teamId: 'synthetic-team' } },
  { oid: auditorId, plane: 'admin', role: 'audit_reader', models: [] },
] };
const sessions = createSessions(randomBytes(32), config.adminHost);

async function listen(server) {
  servers.push(server);
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  return server.address().port;
}
function call(port, host, path, body, headers) {
  return new Promise((resolve, reject) => {
    const data = JSON.stringify(body);
    const incoming = request({ host: '127.0.0.1', port, path, method: 'POST', headers: { host, 'content-type': 'application/json', 'content-length': Buffer.byteLength(data), ...headers } }, response => {
      const chunks = [];
      response.on('data', chunk => chunks.push(chunk));
      response.on('end', () => resolve({ status: response.statusCode, data: JSON.parse(Buffer.concat(chunks).toString()) }));
    });
    incoming.on('error', reject);
    incoming.end(data);
  });
}

try {
  const upstreamPort = await listen(createServer((incoming, response) => {
    incoming.resume();
    response.writeHead(200, { 'content-type': 'application/json' });
    response.end(JSON.stringify({ output: [{ type: 'message', content: [{ type: 'output_text', text: 'Synthetic answer for an audit demonstration.' }] }] }));
  }));
  const common = { getConfig: () => config, target: `http://127.0.0.1:${upstreamPort}`, audit: () => {} };
  const apiPort = await listen(createGateway({ ...common, plane: 'api', l3: writer, keyFor: () => 'synthetic-only-key', verifyToken: async () => ({ tid: tenantId, oid: callerId, ver: '2.0', azp: clientId, scp: 'llm.invoke' }) }));
  const adminPort = await listen(createGateway({ ...common, plane: 'admin', sessions, auditReader: new AuditReader(store, () => [approval]) }));
  const cookie = await sessions.seal({ tid: tenantId, oid: auditorId, roles: ['audit_reader'], csrf: 'synthetic-csrf' }, 'session', Math.floor(timestamp / 1000) + 300);
  const adminHeaders = { cookie: `__Host-llm-admin=${cookie}`, origin: `https://${config.adminHost}`, 'x-csrf-token': 'synthetic-csrf' };
  const inference = await call(apiPort, config.apiHost, '/v1/responses', { model: 'synthetic-model', input: 'Synthetic source context for the L3 audit feature.' }, { authorization: 'Bearer synthetic' });
  assert.equal(inference.status, 200);
  await writer.drain();
  const search = await call(adminPort, config.adminHost, '/audit/search', { approvalId: approval.id }, adminHeaders);
  assert.equal(search.status, 200);
  assert.equal(search.data.records.length, 1);
  assert.ok(!JSON.stringify(search.data).includes('Synthetic source context'));
  const view = await call(adminPort, config.adminHost, '/audit/view', { approvalId: approval.id, id: search.data.records[0].id }, adminHeaders);
  assert.equal(view.status, 200);
  assert.ok(view.data.content.originalRequest.input.includes('Synthetic source context'));
  const denied = await call(adminPort, config.adminHost, '/audit/view', { approvalId: randomUUID(), id: search.data.records[0].id }, adminHeaders);
  assert.equal(denied.status, 403);
  const cleanup = await maintainAudit(store, { clock: () => timestamp + 8 * 86400000 });
  assert.equal(cleanup.deleted, 1);
  assert.equal((await store.list('content', '')).names.length, 0);
  console.log(JSON.stringify({ syntheticDemo: 'passed', inference: 200, indexed: 1, authorizedView: 200, unauthorizedView: 403, expiredRecordsDeleted: cleanup.deleted, backend: 'in-memory; no Azure or real Entra calls' }));
} finally {
  for (const server of servers) { server.closeAllConnections(); server.close(); }
}