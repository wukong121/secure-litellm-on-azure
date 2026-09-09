import { digest } from './audit-capture.mjs';
import { recoverRequest } from './audit-recovery.mjs';

export async function recoveryBatch(store, scope, { operation = 'plan', approved = '', cursor, clock = () => Date.now(), runQuiesced, verifyQuiesced = async () => {} } = {}) {
  if (!['plan', 'execute'].includes(operation)) throw new Error('Invalid recovery operation');
  if (!scope || Object.keys(scope).some(key => !['tenantId', 'revision', 'configSha256', 'storageUrl'].includes(key)) ||
      !/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i.test(scope.tenantId ?? '') ||
      !/^[a-f0-9]{40}$/.test(scope.revision ?? '') || !/^[a-f0-9]{64}$/.test(scope.configSha256 ?? '') ||
      !/^https:\/\/[a-z0-9]{3,24}\.blob\.core\.windows\.net\/?$/.test(scope.storageUrl ?? '')) throw new Error('Invalid recovery scope');
  if (cursor !== undefined && (typeof cursor !== 'string' || cursor.length > 4096)) throw new Error('Invalid recovery cursor');
  const listed = await store.list('pending', `${scope.tenantId}/`, 25, cursor);
  const entries = [];
  for (const name of listed.names) {
    if (!name.startsWith(`${scope.tenantId}/`)) throw new Error('Recovery scope left the selected tenant');
    entries.push({ name, ...await recoverRequest(store, name, { clock }) });
  }
  const plan = { stage: 8, action: 'audit-recover', ...scope, cursor: cursor ?? null, nextCursor: listed.nextCursor ?? null, entries };
  const planSha256 = digest(JSON.stringify(plan));
  if (operation === 'execute') {
    if (!/^[a-f0-9]{64}$/.test(approved) || planSha256 !== approved) throw new Error('Recovery plan changed or was not approved');
    if (typeof runQuiesced !== 'function') throw new Error('Recovery requires an orchestrator-controlled writer and retention pause');
    await runQuiesced(async () => {
      for (const entry of entries) {
        if (['index_repair', 'completion_repair', 'partial_recovery'].includes(entry.status)) {
          await verifyQuiesced();
          await recoverRequest(store, entry.name, { execute: true, clock, expectedState: entry.stateSha256 });
        }
      }
    });
  }
  return { plan, summary: { stage: 8, action: 'audit-recover', planSha256, applied: operation === 'execute', stageAccepted: false, nextCursor: plan.nextCursor, counts: entries.reduce((counts, entry) => ({ ...counts, [entry.status]: (counts[entry.status] ?? 0) + 1 }), {}) } };
}