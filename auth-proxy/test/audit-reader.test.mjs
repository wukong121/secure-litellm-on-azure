import test from 'node:test';
import assert from 'node:assert/strict';
import { AuditWriter, objectName } from '../audit-capture.mjs';
import { AuditReader, maintainAudit } from '../audit-reader.mjs';
import { MemoryAuditStore, metadata } from './audit-fixture.mjs';
import { JournalAuditWriter, journalPrefix } from '../audit-journal.mjs';

const timestamp = Date.parse('2026-09-07T12:00:00Z');
const claims = { tid: metadata.tenantId, oid: '33333333-3333-4333-8333-333333333333' };
const binding = { role: 'audit_reader' };
const approval = {
  id: '44444444-4444-4444-8444-444444444444', actorOid: claims.oid, tenantId: claims.tid,
  ticketId: 'SYNTHETIC-001', reason: 'Synthetic test investigation', teamIds: ['team-a'],
  approvedBy: ['55555555-5555-4555-8555-555555555555', '66666666-6666-4666-8666-666666666666'],
  from: '2026-09-07T00:00:00Z', to: '2026-09-08T00:00:00Z', validFrom: '2026-09-07T00:00:00Z', validUntil: '2026-09-08T00:00:00Z',
};

async function fixture() {
  const store = new MemoryAuditStore();
  const capture = await new AuditWriter(store, { clock: () => timestamp }).begin(metadata, { input: 'synthetic original' }, {});
  capture.response({ format: 'json', status: 200 });
  capture.chunk(Buffer.from('{"output":"synthetic answer"}'));
  await capture.finish('upstream_end');
  return store;
}

test('independent approval gates metadata search and original view with durable access trail', async () => {
  const store = await fixture();
  const reader = new AuditReader(store, () => [approval], { clock: () => timestamp });
  const result = await reader.execute('search', { approvalId: approval.id }, claims, binding);
  assert.equal(result.records.length, 1);
  assert.ok(!JSON.stringify(result).includes('synthetic original'));
  const raw = await reader.execute('view', { approvalId: approval.id, id: metadata.id }, claims, binding);
  assert.equal(raw.content.originalRequest.input, 'synthetic original');
  assert.equal((await store.list('access', '')).names.length, 4);
});

test('ordinary admin, wrong tenant/team, self approval, expired approval and expired raw data cannot read', async () => {
  const store = await fixture();
  const input = { approvalId: approval.id, id: metadata.id };
  const reader = current => new AuditReader(store, () => [current], { clock: () => timestamp });
  await assert.rejects(() => reader(approval).execute('view', input, claims, { role: 'proxy_admin' }));
  for (const patch of [{ tenantId: 'other-tenant' }, { teamIds: ['other-team'] }, { approvedBy: [claims.oid, approval.approvedBy[1]] }, { approvedBy: [approval.approvedBy[0], approval.approvedBy[0]] }, { validUntil: '2026-09-06T00:00:00Z' }, { disabled: true }]) await assert.rejects(() => reader({ ...approval, ...patch }).execute('view', input, claims, binding));
  await assert.rejects(() => new AuditReader(store, () => [{ ...approval, validUntil: '2026-10-01T00:00:00Z' }], { clock: () => timestamp + 8 * 86400000 }).execute('view', input, claims, binding));
});

test('raw release fails closed when access logging or content integrity fails', async () => {
  const store = await fixture();
  const reader = new AuditReader(store, () => [approval], { clock: () => timestamp });
  store.fail = 'access';
  await assert.rejects(() => reader.execute('view', { approvalId: approval.id, id: metadata.id }, claims, binding));
  store.fail = null;
  store.blobs.set(`content/${objectName(metadata.tenantId, metadata.id)}`, '{}');
  await assert.rejects(() => reader.execute('view', { approvalId: approval.id, id: metadata.id }, claims, binding));
});

test('retention deletes content, index and pending; holds preserve data and missing completions are counted', async () => {
  const store = await fixture();
  const later = timestamp + 8 * 86400000;
  const hold = { tenantId: metadata.tenantId, id: metadata.id, until: new Date(later + 86400000).toISOString(), caseId: 'SYNTHETIC-HOLD' };
  assert.equal((await maintainAudit(store, { clock: () => later, holds: [hold] })).held, 1);
  const result = await maintainAudit(store, { clock: () => later });
  assert.equal(result.deleted, 1);
  for (const kind of ['content', 'index', 'pending']) assert.equal((await store.list(kind, '')).names.length, 0);
  const missing = await fixture();
  await missing.remove('index', objectName(metadata.tenantId, metadata.id));
  assert.equal((await maintainAudit(missing, { clock: () => timestamp + 3600000 })).gaps, 1);
  await assert.rejects(() => maintainAudit(missing, { holds: [{ ...hold, until: 'invalid' }] }));
});

test('retention follows stable cursors while deleting previous pages', async () => {
  const store = await fixture();
  const extra = { ...metadata, id: '77777777-7777-4777-8777-777777777777', createdAt: new Date(timestamp).toISOString(), expiresAt: new Date(timestamp + 86400000).toISOString() };
  await store.put('pending', objectName(extra.tenantId, extra.id), extra);
  const first = await maintainAudit(store, { clock: () => timestamp + 8 * 86400000, limit: 1 });
  assert.ok(first.nextCursor);
  const second = await maintainAudit(store, { clock: () => timestamp + 8 * 86400000, limit: 1, cursor: first.nextCursor });
  assert.equal(first.deleted + second.deleted, 2);
  assert.equal((await store.list('pending', '')).names.length, 0);
});

test('retention covers durable journal fragments and holds protect all copies', async () => {
  const store = new MemoryAuditStore();
  const capture = await new JournalAuditWriter(store, { clock: () => timestamp }).begin(metadata, {}, {});
  await capture.response({ format: 'sse', status: 200 });
  for (let index = 0; index < 105; index += 1) await capture.frame(`data: {"delta":"synthetic-${index}"}\n\n`);
  await capture.finish('client_disconnect');
  const prefix = journalPrefix(metadata.tenantId, metadata.id);
  const later = timestamp + 8 * 86400000;
  const hold = { tenantId: metadata.tenantId, id: metadata.id, until: new Date(later + 86400000).toISOString(), caseId: 'SYNTHETIC-HOLD' };
  assert.equal((await maintainAudit(store, { clock: () => later, holds: [hold] })).held, 1);
  assert.equal((await store.list('content', prefix)).names.length, 107);
  assert.equal((await maintainAudit(store, { clock: () => later })).deleted, 1);
  assert.equal((await store.list('content', '')).names.length, 0);
});

test('interrupted journal deletion keeps the pending marker for the next retention run', async () => {
  const store = new MemoryAuditStore();
  const capture = await new JournalAuditWriter(store, { clock: () => timestamp }).begin(metadata, {}, {});
  await capture.response({ format: 'sse', status: 200 });
  await capture.frame('data: [DONE]\n\n');
  await capture.finish('upstream_end');
  const remove = store.remove.bind(store);
  store.remove = async (kind, name) => {
    if (name.endsWith('/end.json')) throw new Error('Synthetic deletion failure');
    await remove(kind, name);
  };
  const clock = () => timestamp + 8 * 86400000;
  await assert.rejects(maintainAudit(store, { clock }), /deletion failure/);
  assert.ok(await store.get('pending', objectName(metadata.tenantId, metadata.id)));
  store.remove = remove;
  assert.equal((await maintainAudit(store, { clock })).deleted, 1);
  assert.equal((await store.list('content', '')).names.length, 0);
  assert.equal((await store.list('pending', '')).names.length, 0);
});

test('retention rejects mismatched request scope before deleting any object', async () => {
  const store = await fixture();
  const name = objectName(metadata.tenantId, metadata.id);
  const pending = await store.get('pending', name);
  store.blobs.set('pending/' + name, JSON.stringify({ ...pending, id: '77777777-7777-4777-8777-777777777777' }));
  const before = [...store.blobs.entries()];
  await assert.rejects(maintainAudit(store, { clock: () => timestamp + 8 * 86400000 }), /scope/);
  assert.deepEqual([...store.blobs.entries()], before);
});