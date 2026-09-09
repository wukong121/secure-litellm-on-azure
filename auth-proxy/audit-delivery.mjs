import { TextDecoder } from 'node:util';
import { createParser } from 'eventsource-parser';

function frameText(event) {
  if (event.data !== '[DONE]') JSON.parse(event.data);
  return (event.id !== undefined ? `id: ${event.id}\n` : '') + (event.event ? `event: ${event.event}\n` : '') +
    event.data.split('\n').map(line => `data: ${line}\n`).join('') + '\n';
}

async function writeResponse(response, text) {
  if (response.destroyed) throw new Error('Client disconnected');
  if (response.write(text)) return;
  await new Promise((resolve, reject) => {
    const cleanup = () => { response.off('drain', drain); response.off('close', closed); response.off('error', failed); };
    const drain = () => { cleanup(); resolve(); };
    const closed = () => { cleanup(); reject(new Error('Client disconnected during audit backpressure')); };
    const failed = () => { cleanup(); reject(new Error('Client response failed')); };
    response.once('drain', drain);
    response.once('close', closed);
    response.once('error', failed);
    if (response.destroyed) closed();
  });
}

export async function deliverAuditedResponse(upstream, response, capture) {
  let outcome = 'upstream_error';
  const close = () => { if (!response.writableFinished) upstream.destroy(); };
  response.on('close', close);
  try {
    const contentType = upstream.headers['content-type'] ?? '';
    const format = contentType.includes('text/event-stream') ? 'sse' : contentType.includes('application/json') ? 'json' : 'unsupported';
    if (upstream.headers['content-encoding'] && upstream.headers['content-encoding'] !== 'identity') throw new Error('Compressed audit response unsupported');
    await capture.response({ format, status: upstream.statusCode });
    const headers = { ...upstream.headers, 'cache-control': 'no-store' };
    for (const name of ['content-length', 'transfer-encoding', 'connection', 'set-cookie', 'location']) delete headers[name];
    const decoder = new TextDecoder('utf-8', { fatal: true });
    let observed = 0;
    let buffered = '';
    let framingTail = '';
    let invalid = false;
    let events = [];
    const parser = format === 'sse' ? createParser({ maxBufferSize: capture.maxBytes, onEvent: event => events.push(event), onError: () => { invalid = true; } }) : null;
    const send = async text => {
      await capture.frame(text);
      if (response.destroyed) throw new Error('Client disconnected');
      if (!response.headersSent) response.writeHead(upstream.statusCode, headers);
      await writeResponse(response, text);
    };
    for await (const chunk of upstream) {
      observed += chunk.length;
      if (observed > capture.maxBytes) { outcome = 'audit_limit'; throw new Error('Audited response exceeds limit'); }
      const text = decoder.decode(chunk, { stream: true });
      if (parser) {
        framingTail = (framingTail + text).slice(-4);
        parser.feed(text);
        if (invalid) { outcome = 'invalid_response'; throw new Error('Invalid SSE framing'); }
        const complete = events;
        events = [];
        if (complete.length) await send(complete.map(frameText).join(''));
      } else { buffered += text; }
    }
    const tail = decoder.decode();
    if (parser) {
      framingTail = (framingTail + tail).slice(-4);
      parser.feed(tail);
      if (invalid || !framingTail.replace(/\r\n/g, '\n').replace(/\r/g, '\n').endsWith('\n\n')) {
        outcome = 'invalid_response';
        throw new Error('Incomplete SSE framing');
      }
      if (events.length) await send(events.map(frameText).join(''));
    } else {
      buffered += tail;
      JSON.parse(buffered);
      await capture.frame(buffered);
    }
    outcome = 'upstream_end';
    if (!await capture.finish(outcome)) throw new Error('Final audit commit failed');
    if (!response.headersSent) response.writeHead(upstream.statusCode, headers);
    response.end(parser ? undefined : buffered);
  } catch {
    if (response.destroyed) outcome = 'client_disconnect';
    await capture.finish(outcome);
    upstream.destroy();
    if (!response.headersSent && !response.destroyed) {
      response.writeHead(503, { 'content-type': 'application/json', 'cache-control': 'no-store' });
      response.end(JSON.stringify({ error: 'Required audit delivery unavailable' }));
    } else if (!response.destroyed) response.destroy();
  } finally {
    response.off('close', close);
  }
}