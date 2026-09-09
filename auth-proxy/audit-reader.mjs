import { randomUUID } from 'node:crypto';
import { Denied } from './policy.mjs';
import { digest, objectName, requestObjectName } from './audit-capture.mjs';
import { journalPrefix } from './audit-journal.mjs';

const uuid = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;

export class AuditReader {
  constructor(store, getApprovals, { clock = () => Date.now(), signal = () => {} } = {}) {
    Object.assign(this, { store, getApprovals, clock, signal });
  }
  async access(claims, event) {
    try {
      await this.store.put('access', objectName(claims.tid, randomUUID()), {
        time: new Date(this.clock()).toISOString(), actor: claims.oid, tenantId: claims.tid, ...event,
      });
    } catch {
      this.signal({ event: 'l3_access_log_failed' });
      throw new Error('Audit access logging unavailable');
    }
    this.signal({ event: 'l3_access', action: event.action, outcome: event.outcome });
  }
  async approval(claims, binding, approvalId) {
    if (binding.role !== 'audit_reader' || !uuid.test(approvalId ?? '')) throw new Denied();
    const approval = (await this.getApprovals()).find(item => item.id === approvalId);
    if (!approval || approval.actorOid !== claims.oid || approval.tenantId !== claims.tid || approval.disabled === true ||
        !approval.ticketId || !approval.reason || !Array.isArray(approval.teamIds) || !approval.teamIds.length || approval.teamIds.includes('*') ||
        !Array.isArray(approval.approvedBy) || approval.approvedBy.length !== 2 || new Set(approval.approvedBy).size !== 2 ||
        approval.approvedBy.some(actor => !uuid.test(actor) || actor === claims.oid) ||
        !(Date.parse(approval.validUntil) > this.clock()) || !(Date.parse(approval.validFrom) <= this.clock()) ||
        !(Date.parse(approval.to) > Date.parse(approval.from)) || Date.parse(approval.to) - Date.parse(approval.from) > 7 * 86400000) throw new Denied();
    return approval;
  }
  allowed(record, approval) {
    return record && record.tenantId === approval.tenantId && approval.teamIds.includes(record.teamId) &&
      Date.parse(record.createdAt) >= Date.parse(approval.from) && Date.parse(record.createdAt) <= Date.parse(approval.to) &&
      Date.parse(record.expiresAt) > this.clock();
  }
  async execute(action, input, claims, binding) {
    try {
      if (!input || Array.isArray(input) || Object.keys(input).some(key => !['approvalId', 'id', 'subject', 'traceId', 'cursor'].includes(key))) throw new Denied(400);
      if (input.cursor !== undefined && (typeof input.cursor !== 'string' || input.cursor.length > 4096)) throw new Denied(400);
      const approval = await this.approval(claims, binding, input.approvalId);
      await this.access(claims, { action, outcome: 'authorized_attempt', approvalId: approval.id, ticketId: approval.ticketId });
      if (action === 'view') {
        if (!uuid.test(input.id ?? '')) throw new Denied(400);
        const name = objectName(claims.tid, input.id);
        const index = await this.store.get('index', name);
        if (!this.allowed(index, approval)) throw new Denied();
        const content = await this.store.get('content', name);
        if (!content || digest(JSON.stringify(content)) !== index.contentHash) throw new Error('Audit integrity failure');
        await this.access(claims, { action, outcome: 'released', approvalId: approval.id, recordId: input.id });
        return content;
      }
      if (action !== 'search') throw new Denied();
      const listed = await this.store.list('index', `${claims.tid}/`, 50, input.cursor);
      const records = [];
      for (const name of listed.names) {
        const record = await this.store.get('index', name);
        if (this.allowed(record, approval) && (!input.subject || record.subject === input.subject) && (!input.traceId || record.traceId === input.traceId)) records.push(record);
      }
      await this.access(claims, { action, outcome: 'released', approvalId: approval.id, count: records.length });
      return { records, boundedScan: true, scanTruncated: listed.truncated, nextCursor: listed.nextCursor };
    } catch (error) {
      await this.access(claims, { action, outcome: 'denied_or_failed' });
      throw error;
    }
  }
}

export async function maintainAudit(store, { clock = () => Date.now(), holds = [], limit = 1000, cursor } = {}) {
  if (!Array.isArray(holds) || holds.some(hold => !uuid.test(hold.id) || !uuid.test(hold.tenantId) || !Number.isFinite(Date.parse(hold.until)) || !hold.caseId)) throw new Error('Invalid hold registry');
  await store.assertPurgePolicy();
  const listed = await store.list('pending', '', limit, cursor);
  let deleted = 0;
  let held = 0;
  let gaps = 0;
  for (const name of listed.names) {
    const record = await store.get('pending', name);
    if (!record) continue;
    if (!uuid.test(record.tenantId) || !uuid.test(record.id) || name !== objectName(record.tenantId, record.id)) throw new Error('Invalid audit retention scope');
    const index = await store.get('index', name);
    if (!index && clock() - Date.parse(record.createdAt) > 15 * 60000) gaps += 1;
    if (!(Date.parse(record.expiresAt) <= clock())) continue;
    if (holds.some(hold => hold.tenantId === record.tenantId && hold.id === record.id && Date.parse(hold.until) > clock())) { held += 1; continue; }
    await store.put('access', objectName(record.tenantId, randomUUID()), { action: 'retention_delete', outcome: 'attempt', recordId: record.id, time: new Date(clock()).toISOString() });
    await store.remove('content', name);
    await store.remove('content', requestObjectName(record.tenantId, record.id));
    if (record.deliveryMode === 'persist-before-forward') {
      const prefix = journalPrefix(record.tenantId, record.id);
      let journalCursor;
      const seen = new Set();
      for (let page = 0; page < 20; page += 1) {
        const journal = await store.list('content', prefix, 100, journalCursor);
        for (const entry of journal.names) {
          if (!entry.startsWith(prefix) || !/^(?:[0-9]{6}|header|end)\.json$/.test(entry.slice(prefix.length))) throw new Error('Unexpected journal object during retention');
          await store.remove('content', entry);
        }
        if (!journal.truncated) break;
        if (!journal.nextCursor || seen.has(journal.nextCursor) || page === 19) throw new Error('Journal retention exceeded bounded scan');
        seen.add(journal.nextCursor);
        journalCursor = journal.nextCursor;
      }
    }
    await store.remove('index', name);
    await store.remove('pending', name);
    await store.put('access', objectName(record.tenantId, randomUUID()), { action: 'retention_delete', outcome: 'completed', recordId: record.id, time: new Date(clock()).toISOString() });
    deleted += 1;
  }
  return { event: 'l3_maintenance', deleted, held, gaps, scanTruncated: listed.truncated, nextCursor: listed.nextCursor };
}