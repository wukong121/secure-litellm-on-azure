import { readFile } from 'node:fs/promises';
import https from 'node:https';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { randomUUID } from 'node:crypto';
import { WorkloadIdentityCredential } from '@azure/identity';
import { AzureAuditStore } from './audit-store.mjs';
import { digest, objectName } from './audit-capture.mjs';
import { recoveryBatch } from './audit-recovery-job.mjs';

function canonical(value) {
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  if (value && typeof value === 'object') return '{' + Object.keys(value).sort().map(key => canonical(key) + ':' + canonical(value[key])).join(',') + '}';
  return JSON.stringify(value).replace(/[\u007f-\uffff]/g, character => '\\u' + character.charCodeAt(0).toString(16).padStart(4, '0'));
}

export const stateHash = value => digest(canonical(value));

export async function verifyWindow(get, expected) {
  const checkpoint = await get('/api/v1/namespaces/litellm/configmaps/llmgw-audit-recovery-window');
  const state = JSON.parse(checkpoint.data['window.json']);
  if (checkpoint.metadata.uid !== expected.uid || stateHash(state) !== expected.stateSha256 || state.id !== expected.windowId || state.phase !== 'paused') throw new Error('Audit maintenance checkpoint changed');
  const writer = await get('/apis/apps/v1/namespaces/litellm/deployments/llm-api-proxy');
  const retention = await get('/apis/batch/v1/namespaces/litellm/cronjobs/l3-retention');
  for (const [kind, resource] of [['deployment', writer], ['cronjob', retention]]) {
    if (resource.metadata.uid !== state.resources[kind].uid || stateHash(resource.spec) !== state.resources[kind].pausedSha256) throw new Error('Audit workload changed during recovery');
  }
  if (writer.spec.replicas !== 0 || retention.spec.suspend !== true) throw new Error('Audit workload is not paused');
  const pods = await get('/api/v1/namespaces/litellm/pods');
  if (pods.items.some(pod => ['llm-api-proxy', 'l3-retention'].includes(pod.spec.serviceAccountName) && !['Succeeded', 'Failed'].includes(pod.status?.phase))) throw new Error('Audit writer or retention Pod is still active');
  const jobs = await get('/apis/batch/v1/namespaces/litellm/jobs');
  if (jobs.items.some(job => job.spec.template.spec.serviceAccountName === 'l3-retention' && !job.status?.conditions?.some(condition => ['Complete', 'Failed'].includes(condition.type) && condition.status === 'True'))) throw new Error('Audit retention Job is still active');
}

export async function runRecoveryWorker(store, input, get) {
  const verify = () => verifyWindow(get, input.window);
  await verify();
  const access = outcome => store.put('access', objectName(input.scope.tenantId, randomUUID()), {
    event: 'audit_recovery', outcome, operation: input.operation, windowId: input.window.windowId,
    revision: input.scope.revision, time: new Date().toISOString(),
  });
  await access('attempt');
  try {
    const result = await recoveryBatch(store, input.scope, {
      operation: input.operation, approved: input.approved, cursor: input.cursor,
      verifyQuiesced: verify, runQuiesced: async action => { await verify(); await action(); await verify(); },
    });
    await verify();
    await access('completed');
    return result;
  } catch {
    await access('failed');
    throw new Error('Audit recovery failed; keep the maintenance window paused');
  }
}

async function clusterGet(path) {
  const token = (await readFile('/var/run/recovery-kube/token', 'utf8')).trim();
  const ca = await readFile('/var/run/recovery-kube/ca.crt');
  return new Promise((resolveResult, reject) => {
    const request = https.get({ hostname: 'kubernetes.default.svc', port: 443, path, ca, headers: { authorization: `Bearer ${token}` }, timeout: 15000 }, response => {
      const chunks = [];
      let size = 0;
      response.on('data', chunk => {
        size += chunk.length;
        if (size > 8 * 1024 * 1024) request.destroy(new Error('Kubernetes response exceeded bound'));
        else chunks.push(chunk);
      });
      response.on('error', reject);
      response.on('end', () => {
        try {
          if (response.statusCode !== 200) throw new Error('Kubernetes check failed');
          resolveResult(JSON.parse(Buffer.concat(chunks).toString()));
        } catch { reject(new Error('Invalid Kubernetes verification response')); }
      });
    });
    request.on('timeout', () => request.destroy(new Error('Kubernetes verification timed out')));
    request.on('error', reject);
  });
}

async function main() {
  const input = JSON.parse(await readFile('/etc/recovery/input.json', 'utf8'));
  const store = new AzureAuditStore(input.scope.storageUrl, new WorkloadIdentityCredential());
  console.log(JSON.stringify(await runRecoveryWorker(store, input, clusterGet)));
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  main().catch(() => { console.error('Audit recovery failed. Keep the maintenance window paused and replan; no acceptance issued.'); process.exitCode = 1; });
}