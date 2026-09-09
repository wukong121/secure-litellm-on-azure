import test from 'node:test';
import assert from 'node:assert/strict';
import { randomBytes } from 'node:crypto';
import { ResponseContext } from '../response-context.mjs';

const identity = { tenantId: 'tenant-a', subject: 'subject-a', model: 'model-a' };

test('responses and encrypted contexts are bound to tenant, subject, model and expiry', async () => {
  let now = 1788912000000;
  const context = new ResponseContext(randomBytes(32), { clock: () => now, ttlSeconds: 60 });
  for (const kind of ['response', 'item', 'encrypted']) {
    const token = await context.seal(kind, 'synthetic-original', identity);
    assert.equal(await context.open(kind, token, identity), 'synthetic-original');
    assert.ok(!token.includes('synthetic-original'));
    for (const key of ['tenantId', 'subject', 'model']) await assert.rejects(context.open(kind, token, { ...identity, [key]: 'other' }));
    await assert.rejects(context.open(kind, token.slice(0, -2) + '!!', identity));
    await assert.rejects(context.open(kind, 'synthetic-original', identity));
    now += 61000;
    await assert.rejects(context.open(kind, token, identity));
  }
});

test('stream encoder keeps identifiers stable and round-trips on a separate replica', async () => {
  const key = randomBytes(32);
  const writer = new ResponseContext(key);
  const reader = new ResponseContext(key);
  const encode = writer.outputEncoder(identity);
  const created = await encode({ type: 'response.created', response: { object: 'response', id: 'resp_synthetic' } });
  const completed = await encode({ type: 'response.completed', response: { object: 'response', id: 'resp_synthetic', output: [{ type: 'reasoning', id: 'rs_synthetic', encrypted_content: 'vendor-ciphertext' }] } });
  assert.equal(created.response.id, completed.response.id);
  const input = await reader.decodeInput({ previous_response_id: completed.response.id, input: completed.response.output }, identity);
  assert.deepEqual(JSON.parse(JSON.stringify(input)), { previous_response_id: 'resp_synthetic', input: [{ type: 'reasoning', id: 'rs_synthetic', encrypted_content: 'vendor-ciphertext' }] });
  await assert.rejects(reader.decodeInput({ input: completed.response.output }, { ...identity, subject: 'other' }));
});

test('unsealed objects, files and excessive nesting fail closed', async () => {
  const context = new ResponseContext(randomBytes(32));
  for (const input of [{ previous_response_id: 'resp_unsealed' }, { input: [{ type: 'item_reference', id: 'item_unsealed' }] }, { input: [{ type: 'input_file', file_id: 'file_a' }] }, { input: [{ type: 'reasoning', encrypted_content: 'foreign' }] }]) {
    await assert.rejects(context.decodeInput(input, identity));
  }
  let nested = {};
  for (let count = 0; count < 60; count += 1) nested = { nested };
  await assert.rejects(context.decodeInput(nested, identity));
});

test('parallel duplicate item IDs stay stable and inherited properties cannot be injected', async () => {
  const context = new ResponseContext(randomBytes(32));
  const encode = context.outputEncoder(identity);
  const output = await encode({ output: [{ type: 'message', id: 'msg_duplicate' }, { type: 'message', id: 'msg_duplicate' }] });
  assert.equal(output.output[0].id, output.output[1].id);
  const body = JSON.parse('{"__proto__":{"injected":true}}');
  const clean = await context.decodeInput(body, identity);
  assert.equal(Object.getPrototypeOf(clean), null);
  assert.equal(clean.injected, undefined);
  await assert.rejects(encode({ text: 'a'.repeat(4 * 1024 * 1024 + 1) }));
});

test('encoding rejects concurrent messages and bounds retained encrypted values', async () => {
  const context = new ResponseContext(randomBytes(32));
  const encode = context.outputEncoder(identity);
  const pending = encode({ response: { object: 'response', id: 'resp_pending' } });
  await assert.rejects(encode({ response: { object: 'response', id: 'resp_other' } }));
  await pending;
  const large = 'a'.repeat(512 * 1024 - 1);
  for (let index = 0; index < 7; index += 1) await encode({ encrypted_content: large + index });
  await assert.rejects(encode({ encrypted_content: large + '8' }));
});