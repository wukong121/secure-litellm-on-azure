import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer, request as httpRequest } from 'node:http';
import { once } from 'node:events';
import { Readable, Writable } from 'node:stream';
import { deliverAuditedResponse } from '../audit-delivery.mjs';
import { metadata } from './audit-fixture.mjs';
import { objectName } from '../audit-capture.mjs';
import { createGateway } from '../proxy.mjs';
import { JournalAuditWriter } from '../audit-journal.mjs';
import { MemoryAuditStore } from './audit-fixture.mjs';

const tenant = '11111111-1111-4111-8111-111111111111';
const subject = '22222222-2222-4222-8222-222222222222';
const client = '33333333-3333-4333-8333-333333333333';
const config = { tenantId: tenant, apiHost: 'llm-api.synthetic.invalid', apiClientIds: [client], bindings: [{ oid: subject, plane: 'api', role: 'internal_user', models: ['model'], keyFile: 'synthetic', audit: { capture: true, teamId: 'team' } }] };

async function serve(context, server) {
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  context.after(() => { server.closeAllConnections(); server.close(); });
  return server.address().port;
}

function request(port, onResponse) {
  const request = httpRequest({ hostname: '127.0.0.1', port, method: 'POST', path: '/v1/responses', headers: { host: config.apiHost, authorization: 'Bearer synthetic', 'content-type': 'application/json' } }, onResponse);
  request.end(JSON.stringify({ model: 'model', input: 'synthetic', stream: true }));
  return request;
}

async function gateway(context, upstream, store) {
  const upstreamPort = await serve(context, upstream);
  const writer = new JournalAuditWriter(store);
  const port = await serve(context, createGateway({ plane: 'api', getConfig: () => config,
    verifyToken: async () => ({ tid: tenant, oid: subject, azp: client, ver: '2.0', scp: 'llm.invoke' }),
    keyFor: () => 'synthetic-key', l3: writer, target: `http://127.0.0.1:${upstreamPort}`, audit: () => {},
  }));
  return { port, writer };
}

test('SSE is delivered only after its durable frame exists and still streams before upstream end', { timeout: 5000 }, async context => {
  const store = new MemoryAuditStore();
  let finish;
  const upstream = createServer((request, response) => {
    request.resume();
    response.writeHead(200, { 'content-type': 'text/event-stream' });
    response.write('data: {"delta":"first"}\n\n');
    finish = () => response.end('data: [DONE]\n\n');
  });
  const { port, writer } = await gateway(context, upstream, store);
  await new Promise((resolve, reject) => {
    request(port, response => {
      response.once('data', chunk => {
        try {
          assert.match(chunk.toString(), /first/);
          assert.ok([...store.blobs.entries()].some(([name, data]) => name.endsWith('/000000.json') && data.includes('first')));
          assert.equal([...store.blobs.keys()].filter(name => name.startsWith('index/')).length, 0);
          finish();
        } catch (error) { reject(error); }
      });
      response.on('end', resolve);
      response.on('error', reject);
    }).on('error', reject);
  });
  await writer.drain();
  assert.equal([...store.blobs.keys()].filter(name => name.startsWith('index/')).length, 1);
});

test('blocked frame commit emits no response before acknowledgement', { timeout: 5000 }, async context => {
  const store = new MemoryAuditStore();
  let release;
  let entered;
  const pending = new Promise(resolve => { entered = resolve; });
  const original = store.put.bind(store);
  store.put = async (kind, name, value) => {
    if (name.endsWith('/000000.json')) {
      entered();
      await new Promise(resolve => { release = resolve; });
    }
    await original(kind, name, value);
  };
  const { port } = await gateway(context, createServer((request, response) => { request.resume(); response.setHeader('content-type', 'application/json'); response.end('{"output":"synthetic"}'); }), store);
  let received = false;
  const done = new Promise((resolve, reject) => {
    request(port, response => { received = true; response.resume(); response.on('end', resolve); }).on('error', reject);
  });
  await pending;
  assert.equal(received, false);
  release();
  await done;
});

test('frame storage failure returns generic error without releasing uncommitted content', { timeout: 5000 }, async context => {
  const store = new MemoryAuditStore();
  const original = store.put.bind(store);
  store.put = async (kind, name, value) => {
    if (name.endsWith('/000000.json')) throw new Error('synthetic persistence failure');
    return original(kind, name, value);
  };
  const { port } = await gateway(context, createServer((request, response) => { request.resume(); response.setHeader('content-type', 'text/event-stream'); response.end('data: {"delta":"must-not-escape"}\n\n'); }), store);
  const result = await new Promise((resolve, reject) => {
    request(port, response => {
      let text = '';
      response.on('data', chunk => { text += chunk; });
      response.on('end', () => resolve({ status: response.statusCode, text }));
    }).on('error', reject);
  });
  assert.equal(result.status, 503);
  assert.ok(!result.text.includes('must-not-escape'));
});

test('client disconnect during storage acknowledgement releases the active audit slot', { timeout: 5000 }, async context => {
  const store = new MemoryAuditStore();
  const original = store.put.bind(store);
  let committed;
  const entered = new Promise(resolve => { committed = resolve; });
  let release;
  store.put = async (kind, name, value) => {
    if (name.endsWith('/000000.json')) {
      committed();
      await new Promise(resolve => { release = resolve; });
    }
    return original(kind, name, value);
  };
  let closed;
  const completed = new Promise(resolve => { closed = resolve; });
  const { port, writer } = await gateway(context, createServer((incoming, response) => { incoming.resume(); response.setHeader('content-type', 'text/event-stream'); response.end('data: {"delta":"synthetic"}\n\n'); }), store);
  writer.signal = event => { if (['l3_partial', 'l3_gap'].includes(event.event)) closed(); };
  const clientRequest = request(port, response => response.resume());
  clientRequest.on('error', () => {});
  await entered;
  clientRequest.destroy();
  release();
  await completed;
  await writer.drain();
  assert.equal(writer.active, 0);
});

test('disconnect before upstream response headers releases audit capacity and closes upstream', { timeout: 5000 }, async context => {
  let accepted;
  let closed;
  let finished;
  const incoming = new Promise(resolve => { accepted = resolve; });
  const backendClosed = new Promise(resolve => { closed = resolve; });
  const audited = new Promise(resolve => { finished = resolve; });
  const { port, writer } = await gateway(context, createServer((request, response) => {
    request.resume();
    response.on('close', closed);
    accepted();
  }), new MemoryAuditStore());
  writer.signal = event => { if (event.event === 'l3_partial') finished(); };
  const clientRequest = request(port, response => response.resume());
  clientRequest.on('error', () => {});
  await incoming;
  clientRequest.destroy();
  await Promise.all([backendClosed, audited]);
  await writer.drain();
  assert.equal(writer.active, 0);
});

async function deliverChunks(chunks, contentType = 'text/event-stream') {
  const store = new MemoryAuditStore();
  const capture = await new JournalAuditWriter(store).begin(metadata, {}, {});
  const upstream = Readable.from(chunks);
  upstream.headers = { 'content-type': contentType };
  upstream.statusCode = 200;
  const delivered = [];
  const response = new Writable({ write(chunk, encoding, callback) { delivered.push(chunk); callback(); } });
  response.writeHead = () => { response.headersSent = true; };
  await deliverAuditedResponse(upstream, response, capture);
  return { record: await store.get('content', objectName(metadata.tenantId, metadata.id)), delivered: Buffer.concat(delivered).toString() };
}

test('SSE UTF-8 and CRLF boundaries survive byte-by-byte delivery', async () => {
  const text = 'data: {"delta":"' + String.fromCodePoint(0x4e2d, 0x6587) + '"}\r\n\r\ndata: [DONE]\r\n\r\n';
  const result = await deliverChunks([...Buffer.from(text)].map(byte => Buffer.from([byte])));
  assert.equal(result.record.complete, true);
  assert.equal(result.delivered, text.replace(/\r\n/g, '\n'));
});

test('terminal marker followed by an unterminated event is never marked complete', async () => {
  for (const tail of ['data: {"delta":"unfinished"}', 'data: {"delta":"unfinished"}\n', 'data: {"delta":"unfinished"}\r\n']) {
    const result = await deliverChunks([Buffer.from('data: [DONE]\n\n'), Buffer.from(tail)]);
    assert.equal(result.record.complete, false);
    assert.equal(result.record.outcome, 'invalid_response');
    assert.ok(!result.delivered.includes('unfinished'));
  }
});

test('malformed JSON and invalid UTF-8 are not forwarded', async () => {
  for (const body of [Buffer.from('{"broken":'), Buffer.from([0xff])]) {
    const result = await deliverChunks([body], 'application/json');
    assert.equal(result.record.complete, false);
    assert.match(result.delivered, /Required audit delivery unavailable/);
  }
});