import { createParser } from 'eventsource-parser';
import { auditIndex, digest, objectName, redactSecrets, requestObjectName } from './audit-capture.mjs';

export const journalPrefix = (tenantId, id) => `${tenantId}/${id}.journal/`;
export const frameName = (tenantId, id, sequence) => `${journalPrefix(tenantId, id)}${String(sequence).padStart(6, '0')}.json`;
const uuid = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;
export const journalHeaderName = (tenantId, id) => `${journalPrefix(tenantId, id)}header.json`;
export const journalEndName = (tenantId, id) => `${journalPrefix(tenantId, id)}end.json`;

function redactFrame(text, format) {
  if (format === 'json') {
    const redacted = redactSecrets(JSON.parse(text));
    return { value: JSON.stringify(redacted.value), count: redacted.count };
  }
  const events = [];
  let count = 0;
  createParser({
    onEvent(event) {
      const redacted = redactSecrets({ ...event, data: event.data === '[DONE]' ? '[DONE]' : JSON.parse(event.data) });
      count += redacted.count;
      const value = redacted.value;
      events.push((value.id !== undefined ? `id: ${value.id}\n` : '') + (value.event ? `event: ${value.event}\n` : '') +
        `data: ${event.data === '[DONE]' ? '[DONE]' : JSON.stringify(value.data)}\n\n`);
    },
    onError() { throw new Error('Invalid audit frame'); },
  }).feed(text);
  if (!events.length) throw new Error('Empty audit frame');
  return { value: events.join(''), count };
}

export function responseState(text, info) {
  let terminal = false;
  let formatValid = true;
  let modelOutcome = 'unknown';
  let content = text;
  if (info.format === 'sse') {
    const parser = createParser({
      onEvent(event) {
        if (event.data === '[DONE]') { terminal = true; return; }
        try {
          const value = JSON.parse(event.data);
          if (['response.completed', 'response.failed', 'response.incomplete'].includes(value.type)) {
            terminal = true;
            modelOutcome = value.type.slice('response.'.length);
          }
        } catch { formatValid = false; }
      },
      onError() { formatValid = false; },
    });
    parser.feed(text);
  } else if (info.format === 'json') {
    try {
      content = JSON.parse(text);
      terminal = true;
      modelOutcome = content?.error || info.status >= 400 ? 'failed' : 'unknown';
    } catch { formatValid = false; }
  } else { formatValid = false; content = null; }
  return { content, terminal, formatValid, modelOutcome };
}

export function journalRecord(context, request, responseInfo, frames, outcome, { recovered = false, truncated = false } = {}) {
  const text = frames.map(frame => frame.text).join('');
  const state = responseState(text, responseInfo);
  const storedBytes = Buffer.byteLength(text);
  return {
    schemaVersion: 2, ...context, outcome, responseInfo,
    complete: outcome === 'upstream_end' && state.terminal && state.formatValid && !truncated,
    truncated, observedBytes: frames.reduce((total, frame) => total + frame.observedBytes, 0), storedBytes, chunkCount: frames.length, terminal: state.terminal,
    modelOutcome: state.modelOutcome, recovered, deliveryMode: 'persist-before-forward',
    clientDelivery: 'unconfirmed', fidelity: 'credential-redacted-normalized-events',
    redactionCount: request.redactionCount + frames.reduce((total, frame) => total + frame.redactionCount, 0),
    journalHash: frames.at(-1)?.hash ?? null,
    content: { ...request.content, response: state.content },
  };
}

export class JournalAuditWriter {
  constructor(store, { retentionDays = 7, maxBytes = 2 * 1024 * 1024, maxFrames = 1024, maxActive = 16, clock = () => Date.now(), signal = () => {} } = {}) {
    if (!Number.isInteger(retentionDays) || retentionDays < 1 || retentionDays > 30 ||
        !Number.isInteger(maxBytes) || maxBytes < 1 || maxBytes > 4 * 1024 * 1024 ||
        !Number.isInteger(maxFrames) || maxFrames < 1 || maxFrames > 1024 ||
        !Number.isInteger(maxActive) || maxActive < 1 || maxActive > 32) throw new Error('Invalid durable audit limits');
    Object.assign(this, { store, retentionDays, maxBytes, maxFrames, maxActive, clock, signal });
    this.durable = true;
    this.active = 0;
    this.pending = new Set();
    this.failedAt = -Infinity;
  }

  async begin(metadata, original, forwarded) {
    if (!uuid.test(metadata.id) || !uuid.test(metadata.tenantId) || !/^[a-f0-9]{32}$/.test(metadata.traceId)) throw new Error('Invalid trusted audit context');
    if (this.active >= this.maxActive || this.clock() - this.failedAt < 30000) throw new Error('Audit capacity unavailable');
    this.active += 1;
    const context = {
      id: metadata.id, tenantId: metadata.tenantId, traceId: metadata.traceId,
      subject: metadata.subject, teamId: metadata.teamId, model: metadata.model,
      createdAt: new Date(this.clock()).toISOString(), expiresAt: new Date(this.clock() + this.retentionDays * 86400000).toISOString(),
    };
    const redacted = redactSecrets({ originalRequest: original, forwardedRequest: forwarded });
    const request = { schemaVersion: 2, ...context, recordType: 'request', content: redacted.value, redactionCount: redacted.count };
    try {
      await this.store.put('pending', objectName(context.tenantId, context.id), { ...context, schemaVersion: 2, deliveryMode: 'persist-before-forward' });
      await this.store.put('content', requestObjectName(context.tenantId, context.id), request);
    } catch {
      this.active -= 1;
      this.failedAt = this.clock();
      this.signal({ event: 'l3_gap', traceId: context.traceId, reason: 'intent_write_failed' });
      throw new Error('Audit unavailable');
    }
    const frames = [];
    let responseInfo = {};
    let observedBytes = 0;
    let truncated = false;
    let uncertainWrite = false;
    let finished;
    let chain = Promise.resolve();
    const enqueue = task => {
      const operation = chain.then(task);
      chain = operation.catch(() => {});
      return operation;
    };
    const write = async (name, value) => {
      try { await this.store.put('content', name, value); }
      catch {
        uncertainWrite = true;
        this.failedAt = this.clock();
        this.signal({ event: 'l3_gap', traceId: context.traceId, reason: 'journal_write_failed' });
        throw new Error('Durable audit write failed');
      }
    };
    return {
      durable: true, maxBytes: this.maxBytes,
      response: info => {
        if (finished) return Promise.reject(new Error('Audit response already finished'));
        return enqueue(async () => {
          if (responseInfo.format || uncertainWrite) throw new Error('Audit response already initialized');
          if (!['json', 'sse'].includes(info.format) || !Number.isInteger(info.status) || info.status < 100 || info.status > 599) throw new Error('Unsupported audited response');
          responseInfo = { format: info.format, status: info.status };
          await write(journalHeaderName(context.tenantId, context.id), { schemaVersion: 2, ...context, responseInfo });
        });
      },
      frame: text => {
        if (finished) return Promise.reject(new Error('Audit response already finished'));
        return enqueue(async () => {
          if (!responseInfo.format || uncertainWrite) throw new Error('Audit response is not accepting frames');
          const frameBytes = Buffer.byteLength(text);
          observedBytes += frameBytes;
          if (observedBytes > this.maxBytes || frames.length >= this.maxFrames) { truncated = true; throw new Error('Durable audit capacity exceeded'); }
          const cleaned = redactFrame(text, responseInfo.format);
          const frame = { schemaVersion: 2, ...context, sequence: frames.length, previousHash: frames.at(-1)?.hash ?? null, text: cleaned.value, observedBytes: frameBytes, redactionCount: cleaned.count };
          const hash = digest(JSON.stringify(frame));
          await write(frameName(context.tenantId, context.id, frame.sequence), frame);
          frames.push({ ...frame, hash });
        });
      },
      finish: outcome => {
        if (finished) return finished;
        finished = enqueue(async () => {
          try {
            if (uncertainWrite) throw new Error('Journal acknowledgement is uncertain');
            const end = { schemaVersion: 2, ...context, outcome, responseInfo, frameCount: frames.length, lastHash: frames.at(-1)?.hash ?? null, truncated };
            await write(journalEndName(context.tenantId, context.id), end);
            const record = journalRecord(context, request, responseInfo, frames, outcome, { truncated });
            await write(objectName(context.tenantId, context.id), record);
            await this.store.put('index', objectName(context.tenantId, context.id), auditIndex(record));
            this.signal({ event: record.complete ? 'l3_committed' : 'l3_partial', traceId: context.traceId, id: context.id });
            return true;
          } catch {
            this.failedAt = this.clock();
            this.signal({ event: 'l3_gap', traceId: context.traceId, reason: 'completion_write_failed' });
            return false;
          } finally { this.active -= 1; frames.length = 0; }
        });
        this.pending.add(finished);
        finished.finally(() => this.pending.delete(finished));
        return finished;
      },
    };
  }

  async drain() { await Promise.all([...this.pending]); }
}