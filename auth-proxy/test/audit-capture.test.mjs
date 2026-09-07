import test from 'node:test';
import assert from 'node:assert/strict';
import { AuditWriter, digest, objectName, redactSecrets } from '../audit-capture.mjs';
import { MemoryAuditStore, metadata } from './audit-fixture.mjs';

test('capture stores original/forwarded payloads, immutable completion and metadata-only index', async () => {
  const store = new MemoryAuditStore();
  const writer = new AuditWriter(store);
  const capture = await writer.begin(metadata, { input: 'synthetic original' }, { input: 'synthetic forwarded' });
  capture.response({ format: 'json', status: 200 });
  capture.chunk(Buffer.from('{"output":"synthetic answer"}'));
  assert.equal(await capture.finish('upstream_end'), true);
  assert.equal(await capture.finish('disconnected'), true);
  const name = objectName(metadata.tenantId, metadata.id);
  const index = await store.get('index', name);
  const content = await store.get('content', name);
  assert.equal(index.complete, true);
  assert.equal(index.contentHash, digest(JSON.stringify(content)));
  assert.ok(!JSON.stringify(index).includes('synthetic original'));
  assert.equal(content.content.originalRequest.input, 'synthetic original');
  assert.equal(writer.active, 0);
});

test('SSE records preserve order, detect missing terminal, truncation and client disconnect', async () => {
  for (const [outcome, maxBytes, terminal, complete] of [['upstream_end', 1024, true, true], ['upstream_end', 5, true, false], ['client_disconnect', 1024, false, false], ['upstream_end', 1024, false, false]]) {
    const store = new MemoryAuditStore();
    const writer = new AuditWriter(store, { maxBytes });
    const capture = await writer.begin(metadata, { input: 'synthetic' }, {});
    capture.response({ format: 'sse', status: 200 });
    capture.chunk(Buffer.from('data: {"delta":"one"}\n\n'));
    capture.chunk(Buffer.from('data: {"delta":"two"}\n\n'));
    if (terminal) capture.chunk(Buffer.from('data: [DONE]\n\n'));
    await capture.finish(outcome);
    const result = await store.get('content', objectName(metadata.tenantId, metadata.id));
    assert.equal(result.complete, complete);
    assert.equal(result.truncated, maxBytes === 5);
    assert.equal(result.chunkCount, terminal ? 3 : 2);
  }
});

test('credential fields and embedded credential patterns are removed but ordinary approved content is retained', () => {
  const result = redactSecrets({ input: 'synthetic source code and personal content', authorization: 'Bearer synthetic-token', tools: [{ api_key: 'synthetic-key' }], response: 'Bearer synthetic-secret' });
  assert.equal(result.value.input, 'synthetic source code and personal content');
  assert.ok(!JSON.stringify(result.value).includes('synthetic-secret'));
  assert.equal(result.count, 3);
  const stream = redactSecrets('data: {"api_key":"synthetic-secret-value"}\n\ndata: [DONE]\n\n');
  assert.ok(!stream.value.includes('synthetic-secret-value'));
  assert.equal(stream.count, 1);
});

test('capacity and intent failure deny capture; final storage failure leaves a durable pending record', async () => {
  const store = new MemoryAuditStore();
  const events = [];
  const writer = new AuditWriter(store, { maxActive: 1, signal: event => events.push(event) });
  const capture = await writer.begin(metadata, { input: 'sensitive synthetic text' }, {});
  await assert.rejects(() => writer.begin(metadata, {}, {}));
  store.fail = 'content';
  assert.equal(await capture.finish('upstream_error'), false);
  assert.ok(await store.get('pending', objectName(metadata.tenantId, metadata.id)));
  assert.equal(await store.get('index', objectName(metadata.tenantId, metadata.id)), null);
  assert.ok(!JSON.stringify(events).includes('sensitive synthetic text'));
  await assert.rejects(() => writer.begin(metadata, {}, {}));
  const failing = new MemoryAuditStore();
  failing.fail = 'pending';
  await assert.rejects(() => new AuditWriter(failing).begin(metadata, {}, {}));
});