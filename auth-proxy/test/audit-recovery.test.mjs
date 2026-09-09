import test from 'node:test';
import assert from 'node:assert/strict';
import { JournalAuditWriter, frameName, journalEndName } from '../audit-journal.mjs';
import { recoverRequest } from '../audit-recovery.mjs';
import { MemoryAuditStore, metadata } from './audit-fixture.mjs';
import { objectName } from '../audit-capture.mjs';

const timestamp = Date.parse('2026-09-09T00:00:00Z');
const later = () => timestamp + 16 * 60000;
const name = objectName(metadata.tenantId, metadata.id);

async function started() {
  const store = new MemoryAuditStore();
  const capture = await new JournalAuditWriter(store, { clock: () => timestamp }).begin(metadata, { input: 'synthetic' }, {});
  await capture.response({ format: 'sse', status: 200 });
  await capture.frame('data: {"delta":"persisted"}\n\n');
  return { store, capture };
}

test('restart recovers only persisted frames and never invents successful completion', async () => {
  const { store } = await started();
  const preview = await recoverRequest(store, name, { clock: later });
  assert.equal(preview.status, 'partial_recovery');
  assert.equal(preview.complete, false);
  assert.equal(await store.get('content', name), null);
  await recoverRequest(store, name, { clock: later, execute: true });
  const record = await store.get('content', name);
  assert.equal(record.outcome, 'process_interrupted');
  assert.equal(record.complete, false);
  assert.equal(record.clientDelivery, 'unconfirmed');
  assert.match(record.content.response, /persisted/);
  assert.equal((await recoverRequest(store, name, { clock: later, execute: true })).status, 'indexed');
});

test('index-only write failure can be repaired without changing existing content', async () => {
  const { store, capture } = await started();
  await capture.frame('data: [DONE]\n\n');
  store.fail = 'index';
  assert.equal(await capture.finish('upstream_end'), false);
  const before = await store.get('content', name);
  store.fail = null;
  assert.equal((await recoverRequest(store, name, { clock: later, execute: true })).status, 'index_repair');
  assert.deepEqual(await store.get('content', name), before);
  assert.equal((await store.get('index', name)).complete, true);
});

test('durable terminal allows rebuilding missing completion and rejects a tampered frame', async () => {
  const { store, capture } = await started();
  await capture.frame('data: [DONE]\n\n');
  const original = store.put.bind(store);
  store.put = async (kind, path, value) => {
    if (kind === 'content' && path === name) throw new Error('synthetic process loss');
    return original(kind, path, value);
  };
  assert.equal(await capture.finish('upstream_end'), false);
  store.put = original;
  const firstName = frameName(metadata.tenantId, metadata.id, 0);
  const first = await store.get('content', firstName);
  store.blobs.set('content/' + firstName, JSON.stringify({ ...first, text: 'tampered' }));
  await assert.rejects(recoverRequest(store, name, { clock: later, execute: true }), /chain/);
  assert.equal(await store.get('index', name), null);
  store.blobs.set('content/' + firstName, JSON.stringify(first));
  const recovered = await recoverRequest(store, name, { clock: later, execute: true });
  assert.equal(recovered.status, 'completion_repair');
  assert.equal(recovered.complete, true);
});

test('recovery never finalizes active requests or resurrects expired content', async () => {
  const { store } = await started();
  assert.equal((await recoverRequest(store, name, { clock: () => timestamp + 1000, execute: true })).status, 'active');
  assert.equal((await recoverRequest(store, name, { clock: () => timestamp + 8 * 86400000, execute: true })).status, 'expired');
  assert.equal(await store.get('index', name), null);
});

test('lost write acknowledgement leaves no contradictory terminal and recovers persisted data', async () => {
  const { store, capture } = await started();
  const original = store.put.bind(store);
  store.put = async (kind, path, value) => {
    await original(kind, path, value);
    if (path.endsWith('/000001.json')) throw new Error('Synthetic acknowledgement loss');
  };
  await assert.rejects(capture.frame('data: [DONE]\n\n'), /write failed/);
  assert.equal(await capture.finish('upstream_error'), false);
  assert.equal(await store.get('content', journalEndName(metadata.tenantId, metadata.id)), null);
  store.put = original;
  const recovered = await recoverRequest(store, name, { clock: later, execute: true });
  assert.equal(recovered.frameCount, 2);
  assert.equal(recovered.complete, false);
});