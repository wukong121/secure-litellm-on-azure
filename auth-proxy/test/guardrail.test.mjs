import test from 'node:test';
import assert from 'node:assert/strict';
import { ContentSafetyGuardrail } from '../guardrail.mjs';

const config = { endpoint: 'https://synthetic.cognitiveservices.azure.com', threshold: 4, mode: 'block' };
const credential = { getToken: async () => ({ token: 'synthetic-only-token' }) };
const result = severity => ({ ok: true, json: async () => ({ categoriesAnalysis: ['Hate', 'SelfHarm', 'Sexual', 'Violence'].map(category => ({ category, severity })) }) });

test('Content Safety supports block/observe without logging source text or credentials', async () => {
  const events = [];
  for (const text of ['synthetic English business input', '\u5408\u6210\u4e2d\u6587\u4e1a\u52a1\u8f93\u5165']) {
    const safe = new ContentSafetyGuardrail(config, credential, { fetcher: async () => result(0), signal: event => events.push(event) });
    await safe.inspect({ input: text }, { traceId: 'test-trace' });
  }
  const flagged = new ContentSafetyGuardrail(config, credential, { fetcher: async () => result(6), signal: event => events.push(event) });
  await assert.rejects(() => flagged.inspect({ input: 'synthetic' }, { traceId: 'test-trace' }));
  const observer = new ContentSafetyGuardrail({ ...config, mode: 'observe' }, credential, { fetcher: async () => result(6) });
  await observer.inspect({ input: 'synthetic' }, { traceId: 'test-trace' });
  assert.ok(!JSON.stringify(events).includes('business input'));
  assert.ok(!JSON.stringify(events).includes('synthetic-only-token'));
});

test('blocking mode refuses provider failure, incomplete results and oversized input; observe signals gaps', async () => {
  for (const fetcher of [async () => { throw new Error('synthetic unavailable'); }, async () => ({ ok: true, json: async () => ({}) })]) {
    await assert.rejects(() => new ContentSafetyGuardrail(config, credential, { fetcher }).inspect({ input: 'synthetic' }, { traceId: 'test' }));
  }
  await assert.rejects(() => new ContentSafetyGuardrail(config, credential).inspect({ input: 'a'.repeat(10001) }, { traceId: 'test' }));
  const events = [];
  await new ContentSafetyGuardrail({ ...config, mode: 'observe' }, credential, { fetcher: async () => { throw new Error(); }, signal: event => events.push(event) }).inspect({ input: 'synthetic' }, { traceId: 'test' });
  assert.equal(events[0].event, 'guardrail_unavailable');
});