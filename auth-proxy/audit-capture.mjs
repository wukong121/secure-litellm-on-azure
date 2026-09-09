import { createHash } from 'node:crypto';
import { createParser } from 'eventsource-parser';

export const digest = value => createHash('sha256').update(value).digest('hex');
export const objectName = (tenantId, id) => `${tenantId}/${id}.json`;
export const requestObjectName = (tenantId, id) => `${tenantId}/${id}.request.json`;

export function auditIndex(record) {
  const { id, traceId, tenantId, subject, teamId, model, createdAt, expiresAt, outcome, complete, truncated, redactionCount } = record;
  return { id, traceId, tenantId, subject, teamId, model, createdAt, expiresAt, outcome, complete, truncated,
    status: record.responseInfo.status ?? 0, contentHash: digest(JSON.stringify(record)), redactionCount };
}

export function redactSecrets(value) {
  let count = 0;
  const mask = () => { count += 1; return '[REDACTED]'; };
  const visit = current => {
    if (typeof current === 'string') return current
      .replace(/("(?:authorization|cookie|api_key|access_token|refresh_token|password|client_secret|connection_string)"\s*:\s*)"(?:\\.|[^"\\])*"/gi, (_match, prefix) => `${prefix}"${mask()}"`)
      .replace(/Bearer\s+[A-Za-z0-9_.~+\/-]+/gi, mask)
      .replace(/\bsk-[A-Za-z0-9_-]{16,}\b/g, mask)
      .replace(/\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b/g, mask)
      .replace(/(?:postgres(?:ql)?|rediss?):\/\/[^\s"\\]+/gi, mask)
      .replace(/(?:AccountKey|SharedAccessSignature)=[^;\s"\\]+/gi, mask);
    if (Array.isArray(current)) return current.map(visit);
    if (current && typeof current === 'object') return Object.fromEntries(Object.entries(current).map(([key, child]) => [key,
      /^(authorization|cookie|set-cookie|api[_-]?key|access[_-]?token|refresh[_-]?token|password|client[_-]?secret|connection[_-]?string)$/i.test(key) ? mask() : visit(child),
    ]));
    return current;
  };
  return { value: visit(value), count };
}

export class AuditWriter {
  constructor(store, { retentionDays = 7, maxBytes = 2 * 1024 * 1024, maxActive = 16, clock = () => Date.now(), signal = () => {} } = {}) {
    if (!Number.isInteger(retentionDays) || retentionDays < 1 || retentionDays > 30 || maxBytes < 1 || maxBytes > 4 * 1024 * 1024 || maxActive < 1 || maxActive > 32) throw new Error('Invalid audit limits');
    Object.assign(this, { store, retentionDays, maxBytes, maxActive, clock, signal });
    this.active = 0;
    this.pending = new Set();
    this.failedAt = -Infinity;
  }

  async begin(metadata, original, forwarded) {
    if (this.active >= this.maxActive || this.clock() - this.failedAt < 30000) throw new Error('Audit capacity unavailable');
    this.active += 1;
    const createdAt = new Date(this.clock()).toISOString();
    const context = { ...metadata, createdAt, expiresAt: new Date(this.clock() + this.retentionDays * 86400000).toISOString() };
    const name = objectName(context.tenantId, context.id);
    try {
      await this.store.put('pending', name, context);
      const request = redactSecrets({ originalRequest: original, forwardedRequest: forwarded });
      await this.store.put('content', requestObjectName(context.tenantId, context.id), {
        schemaVersion: 1, ...context, recordType: 'request', content: request.value, redactionCount: request.count,
      });
    } catch {
      this.active -= 1;
      this.failedAt = this.clock();
      this.signal({ event: 'l3_gap', traceId: context.traceId, reason: 'intent_write_failed' });
      throw new Error('Audit unavailable');
    }
    const chunks = [];
    let observedBytes = 0;
    let storedBytes = 0;
    let chunkCount = 0;
    let responseInfo = {};
    let finished;
    return {
      response: info => { responseInfo = info; },
      chunk: chunk => {
        if (finished) return;
        chunkCount += 1;
        observedBytes += chunk.length;
        const size = Math.max(0, Math.min(chunk.length, this.maxBytes - storedBytes));
        if (size) chunks.push(Buffer.from(chunk.subarray(0, size)));
        storedBytes += size;
      },
      finish: outcome => {
        if (finished) return finished;
        finished = (async () => {
          try {
            const text = Buffer.concat(chunks).toString('utf8');
            const truncated = observedBytes > storedBytes;
            let terminal = false;
            let formatValid = true;
            let content = text;
            if (responseInfo.format === 'sse') {
              const parser = createParser({
                onEvent: event => {
                  if (event.data === '[DONE]') terminal = true;
                  else {
                    try {
                      const parsed = JSON.parse(event.data);
                      if (['response.completed', 'response.failed', 'response.incomplete'].includes(parsed.type)) terminal = true;
                    } catch { formatValid = false; }
                  }
                },
                onError: () => { formatValid = false; },
              });
              parser.feed(text);
            } else if (responseInfo.format === 'json') {
              try { content = JSON.parse(text); terminal = true; }
              catch { formatValid = false; }
            } else { formatValid = false; content = null; }
            const redacted = redactSecrets({ originalRequest: original, forwardedRequest: forwarded, response: content });
            const complete = outcome === 'upstream_end' && terminal && formatValid && !truncated;
            const record = {
              schemaVersion: 1, ...context, outcome, responseInfo, complete,
              truncated, observedBytes, storedBytes, chunkCount, terminal,
              redactionCount: redacted.count, fidelity: redacted.count ? 'credential-redacted' : 'captured-content',
              content: redacted.value,
            };
            await this.store.put('content', name, record);
            await this.store.put('index', name, auditIndex(record));
            this.signal({ event: complete ? 'l3_committed' : 'l3_partial', traceId: context.traceId, id: context.id });
            return true;
          } catch {
            this.failedAt = this.clock();
            this.signal({ event: 'l3_gap', traceId: context.traceId, reason: 'completion_write_failed' });
            return false;
          } finally {
            this.active -= 1;
            chunks.length = 0;
          }
        })();
        this.pending.add(finished);
        finished.finally(() => this.pending.delete(finished));
        return finished;
      },
    };
  }

  async drain() { await Promise.all([...this.pending]); }
}