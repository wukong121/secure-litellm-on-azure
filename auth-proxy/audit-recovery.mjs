import { auditIndex, digest, objectName, requestObjectName } from './audit-capture.mjs';
import { frameName, journalEndName, journalHeaderName, journalPrefix, journalRecord } from './audit-journal.mjs';

const fields = ['id', 'tenantId', 'traceId', 'subject', 'teamId', 'model', 'createdAt', 'expiresAt'];
const uuid = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;

function assertContext(value, context) {
  if (!value || fields.some(field => value[field] !== context[field])) throw new Error('Audit journal context mismatch');
}

export async function recoverRequest(store, name, { clock = () => Date.now(), execute = false, expectedState } = {}) {
  const pending = await store.get('pending', name);
  if (!pending) return { status: 'missing' };
  if (!uuid.test(pending.tenantId) || !uuid.test(pending.id) || name !== objectName(pending.tenantId, pending.id)) throw new Error('Audit intent name mismatch');
  if (!Number.isFinite(Date.parse(pending.createdAt)) || !Number.isFinite(Date.parse(pending.expiresAt))) throw new Error('Invalid audit lifetime');
  if (Date.parse(pending.expiresAt) <= clock()) return { status: 'expired' };
  if (clock() - Date.parse(pending.createdAt) < 15 * 60000) return { status: 'active' };
  const context = Object.fromEntries(fields.map(field => [field, pending[field]]));
  const content = await store.get('content', name);
  const index = await store.get('index', name);
  if (index) {
    assertContext(index, context);
    assertContext(content, context);
    if (digest(JSON.stringify(content)) !== index.contentHash) throw new Error('Audit content/index hash mismatch');
    return { status: 'indexed', stateSha256: digest(JSON.stringify({ pending, content, index })) };
  }
  if (pending.deliveryMode !== 'persist-before-forward') {
    if (!content) return { status: 'gap', reason: 'no_durable_completion' };
    assertContext(content, context);
    if (content.schemaVersion !== 1 || !content.responseInfo || typeof content.complete !== 'boolean') throw new Error('Invalid buffered completion');
    const stateSha256 = digest(JSON.stringify({ pending, content }));
    if (execute) {
      if (expectedState && stateSha256 !== expectedState) throw new Error('Audit state changed after approval');
      if (Date.parse(pending.expiresAt) <= clock()) throw new Error('Audit record expired during recovery');
      await store.put('index', name, auditIndex(content));
    }
    return { status: 'index_repair', applied: execute, stateSha256 };
  }
  const request = await store.get('content', requestObjectName(pending.tenantId, pending.id));
  if (!request) return { status: 'gap', reason: 'no_durable_request' };
  assertContext(request, context);
  const header = await store.get('content', journalHeaderName(pending.tenantId, pending.id));
  const end = await store.get('content', journalEndName(pending.tenantId, pending.id));
  if (header) assertContext(header, context);
  if (end) assertContext(end, context);
  const prefix = journalPrefix(pending.tenantId, pending.id);
  const names = [];
  const cursors = new Set();
  let cursor;
  for (let page = 0; page < 20; page += 1) {
    const listed = await store.list('content', prefix, 100, cursor);
    for (const entry of listed.names) {
      if (!entry.startsWith(prefix)) throw new Error('Journal listing left the request scope');
      if (entry !== journalHeaderName(pending.tenantId, pending.id) && entry !== journalEndName(pending.tenantId, pending.id)) names.push(entry);
    }
    if (!listed.truncated) break;
    if (!listed.nextCursor || cursors.has(listed.nextCursor) || page === 19) throw new Error('Journal pagination exceeded recovery bound');
    cursors.add(listed.nextCursor);
    cursor = listed.nextCursor;
  }
  names.sort();
  if (names.length > 1024 || new Set(names).size !== names.length || (names.length && !header)) throw new Error('Invalid journal frame count or header');
  const frames = [];
  let bytes = 0;
  for (const [sequence, entry] of names.entries()) {
    if (entry !== frameName(pending.tenantId, pending.id, sequence)) throw new Error('Missing or unexpected journal sequence');
    const frame = await store.get('content', entry);
    assertContext(frame, context);
    if (frame.schemaVersion !== 2 || frame.sequence !== sequence || frame.previousHash !== (frames.at(-1)?.hash ?? null) || typeof frame.text !== 'string' || !Number.isInteger(frame.redactionCount) || frame.redactionCount < 0) throw new Error('Audit journal chain is invalid');
    if (!Number.isInteger(frame.observedBytes) || frame.observedBytes < 0 || frame.observedBytes > 4 * 1024 * 1024) throw new Error('Invalid observed frame size');
    bytes += Buffer.byteLength(frame.text);
    if (bytes > 16 * 1024 * 1024) throw new Error('Journal recovery byte limit exceeded');
    frames.push({ ...frame, hash: digest(JSON.stringify(frame)) });
  }
  const responseInfo = header?.responseInfo ?? {};
  if (end && (end.frameCount !== frames.length || end.lastHash !== (frames.at(-1)?.hash ?? null) || JSON.stringify(end.responseInfo) !== JSON.stringify(responseInfo))) throw new Error('Terminal journal does not match persisted frames');
  const outcome = end?.outcome ?? 'process_interrupted';
  const options = { truncated: end?.truncated ?? false };
  const record = content ?? journalRecord(context, request, responseInfo, frames, outcome, { ...options, recovered: true });
  if (content) {
    assertContext(content, context);
    const expected = journalRecord(context, request, responseInfo, frames, outcome, { ...options, recovered: content.recovered === true });
    if (JSON.stringify(content) !== JSON.stringify(expected)) throw new Error('Completion does not match the durable journal');
  }
  const stateSha256 = digest(JSON.stringify({ pending, request, header, end, frames, content }));
  if (execute) {
    if (expectedState && stateSha256 !== expectedState) throw new Error('Audit state changed after approval');
    if (Date.parse(pending.expiresAt) <= clock()) throw new Error('Audit record expired during recovery');
    if (!content) await store.put('content', name, record);
    await store.put('index', name, auditIndex(record));
  }
  return { status: content ? 'index_repair' : end ? 'completion_repair' : 'partial_recovery', applied: execute, complete: record.complete, frameCount: frames.length, stateSha256 };
}

export async function recoverAudit(store, { clock = () => Date.now(), execute = false, limit = 50, cursor } = {}) {
  if (!Number.isInteger(limit) || limit < 1 || limit > 100) throw new Error('Invalid recovery page size');
  const listed = await store.list('pending', '', limit, cursor);
  const counts = {};
  for (const name of listed.names) {
    try {
      const result = await recoverRequest(store, name, { clock, execute });
      counts[result.status] = (counts[result.status] ?? 0) + 1;
    } catch {
      counts.integrity_or_storage_failure = (counts.integrity_or_storage_failure ?? 0) + 1;
    }
  }
  return { event: 'l3_recovery', execute, counts, scanTruncated: listed.truncated, nextCursor: listed.nextCursor };
}