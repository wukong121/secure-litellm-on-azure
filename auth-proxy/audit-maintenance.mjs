import { readFile } from 'node:fs/promises';
import { WorkloadIdentityCredential } from '@azure/identity';
import { AzureAuditStore } from './audit-store.mjs';
import { maintainAudit } from './audit-reader.mjs';

async function main() {
  const config = JSON.parse(await readFile(process.env.STAGE8_CONFIG, 'utf8'));
  if (config.l3?.enabled !== true) throw new Error('Audit maintenance is not enabled');
  const holds = JSON.parse(await readFile('/etc/audit-approvals/holds.json', 'utf8'));
  if (!Array.isArray(holds)) throw new Error('Invalid hold registry');
  const store = new AzureAuditStore(config.l3.storageUrl, new WorkloadIdentityCredential(), { resourceId: config.l3.storageResourceId });
  let cursor;
  for (let page = 0; page < 100; page += 1) {
    const result = await maintainAudit(store, { holds, cursor });
    const { nextCursor, ...summary } = result;
    console.log(JSON.stringify(summary));
    if (!nextCursor) return;
    cursor = nextCursor;
  }
  throw new Error('Retention scan exceeded its bounded job budget');
}

main().catch(() => {
  console.error(JSON.stringify({ event: 'l3_maintenance_failed' }));
  process.exitCode = 1;
});