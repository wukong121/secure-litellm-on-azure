import test from 'node:test';
import assert from 'node:assert/strict';
import { JournalAuditWriter, frameName, journalEndName } from '../audit-journal.mjs';
import { MemoryAuditStore, metadata } from './audit-fixture.mjs';
import { digest, objectName } from '../audit-capture.mjs';

test('journal commits linked redacted frames before acknowledging and distinguishes model failure', async () => {
  const store = new MemoryAuditStore();
  const writer = new JournalAuditWriter(store);
  const capture = await writer.begin(metadata, { input: 'synthetic', api_key: 'private-value' }, {});
  await capture.response({ format: 'sse', status: 200 });
  await capture.frame('data: {"type":"response.output_text.delta","delta":"synthetic"}\n\n');
  const first = await store.get('content', frameName(metadata.tenantId, metadata.id, 0));
  assert.equal(first.sequence, 0);
  await capture.frame('data: {"type":"response.failed","api_key":"private-value"}\n\n');
  const second = await store.get('content', frameName(metadata.tenantId, metadata.id, 1));
  assert.equal(second.previousHash, digest(JSON.stringify(first)));
  assert.ok(!JSON.stringify(second).includes('private-value'));
  assert.equal(await capture.finish('upstream_end'), true);
  const record = await store.get('content', objectName(metadata.tenantId, metadata.id));
  assert.equal(record.modelOutcome, 'failed');
  assert.equal(record.clientDelivery, 'unconfirmed');
  assert.equal(record.complete, true);
  assert.equal(writer.active, 0);
  assert.equal((await store.get('content', journalEndName(metadata.tenantId, metadata.id))).frameCount, 2);
});

test('journal capacity and write failures cannot be acknowledged as stored frames', async () => {
  const store = new MemoryAuditStore();
  const writer = new JournalAuditWriter(store, { maxFrames: 1 });
  const capture = await writer.begin(metadata, {}, {});
  await capture.response({ format: 'sse', status: 200 });
  await capture.frame('data: {"delta":"first"}\n\n');
  await assert.rejects(capture.frame('data: [DONE]\n\n'), /capacity/);
  await capture.finish('audit_limit');
  const result = await store.get('content', objectName(metadata.tenantId, metadata.id));
  assert.equal(result.complete, false);
  assert.equal(result.truncated, true);
  const failing = new MemoryAuditStore();
  const other = new JournalAuditWriter(failing);
  const request = await other.begin(metadata, {}, {});
  await request.response({ format: 'json', status: 200 });
  failing.fail = 'content';
  await assert.rejects(request.frame('{"output":"synthetic"}'), /write failed/);
  assert.equal(await request.finish('upstream_error'), false);
  assert.equal(await failing.get('index', objectName(metadata.tenantId, metadata.id)), null);
});

test('finish drains already accepted writes but rejects new frames', async () => {
  const store = new MemoryAuditStore();
  const capture = await new JournalAuditWriter(store).begin(metadata, {}, {});
  const header = capture.response({ format: 'sse', status: 200 });
  const frame = capture.frame('data: [DONE]\n\n');
  const finished = capture.finish('upstream_end');
  await Promise.all([header, frame]);
  assert.equal(await finished, true);
  await assert.rejects(capture.frame('data: [DONE]\n\n'), /finished/);
  assert.equal((await store.get('content', journalEndName(metadata.tenantId, metadata.id))).frameCount, 1);
});

test('structured redaction handles escaped credential field names and preserves byte counts', async () => {
  for (const format of ['json', 'sse']) {
    const store = new MemoryAuditStore();
    const capture = await new JournalAuditWriter(store).begin(metadata, {}, {});
    await capture.response({ format, status: 200 });
    const json = '{"api\\u005fkey":"synthetic-private-value","output":"answer"}';
    const text = format === 'json' ? json : `data: ${json}\n\n`;
    await capture.frame(text);
    await capture.finish('upstream_end');
    const record = await store.get('content', objectName(metadata.tenantId, metadata.id));
    assert.ok(!JSON.stringify(record).includes('synthetic-private-value'));
    assert.equal(record.redactionCount, 1);
    assert.equal(record.observedBytes, Buffer.byteLength(text));
    assert.notEqual(record.observedBytes, record.storedBytes);
  }
});