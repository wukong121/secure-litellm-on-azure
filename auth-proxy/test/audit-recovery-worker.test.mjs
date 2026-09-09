import test from 'node:test';
import assert from 'node:assert/strict';
import { stateHash, verifyWindow, runRecoveryWorker } from '../audit-recovery-worker.mjs';
import { MemoryAuditStore, metadata } from './audit-fixture.mjs';
import { JournalAuditWriter } from '../audit-journal.mjs';

function fixture() {
  const writer = { metadata: { uid: 'writer' }, spec: { replicas: 0 } };
  const retention = { metadata: { uid: 'retention' }, spec: { suspend: true } };
  const state = { phase: 'paused', id: 'window', resources: {
    deployment: { uid: 'writer', pausedSha256: stateHash(writer.spec) }, cronjob: { uid: 'retention', pausedSha256: stateHash(retention.spec) },
  } };
  const checkpoint = { metadata: { uid: 'checkpoint' }, data: { 'window.json': JSON.stringify(state) } };
  const expected = { uid: 'checkpoint', windowId: 'window', stateSha256: stateHash(state) };
  const pods = { items: [] };
  const jobs = { items: [] };
  const get = async path => path.includes('/configmaps/') ? checkpoint : path.includes('/deployments/') ? writer : path.includes('/cronjobs/') ? retention : path.endsWith('/pods') ? pods : jobs;
  return { get, expected, writer, retention, pods, jobs, checkpoint };
}

test('worker independently rejects checkpoint drift and active writers or retention jobs', async () => {
  const current = fixture();
  await verifyWindow(current.get, current.expected);
  current.writer.spec.replicas = 1;
  await assert.rejects(verifyWindow(current.get, current.expected), /changed/);
  current.writer.spec.replicas = 0;
  current.pods.items.push({ spec: { serviceAccountName: 'llm-api-proxy' }, status: { phase: 'Terminating' } });
  await assert.rejects(verifyWindow(current.get, current.expected), /still active/);
  current.pods.items = [];
  current.jobs.items.push({ spec: { template: { spec: { serviceAccountName: 'l3-retention' } } } });
  await assert.rejects(verifyWindow(current.get, current.expected), /still active/);
  current.jobs.items = [];
  current.checkpoint.metadata.uid = 'replacement';
  await assert.rejects(verifyWindow(current.get, current.expected), /checkpoint/);
});

test('worker emits metadata-only plans, durably logs recovery access and repairs inside verified pause', async () => {
  const current = fixture();
  const store = new MemoryAuditStore();
  const capture = await new JournalAuditWriter(store, { clock: () => Date.now() - 3600000 }).begin(metadata, { input: 'private synthetic body' }, {});
  await capture.response({ format: 'sse', status: 200 });
  await capture.frame('data: {"delta":"private synthetic delta"}\n\n');
  const input = { scope: { tenantId: metadata.tenantId, revision: 'a'.repeat(40), configSha256: 'b'.repeat(64), storageUrl: 'https://synthetic.blob.core.windows.net' }, window: current.expected, operation: 'plan' };
  const plan = await runRecoveryWorker(store, input, current.get);
  assert.ok(!JSON.stringify(plan).includes('private synthetic'));
  assert.equal((await store.list('index', '')).names.length, 0);
  const result = await runRecoveryWorker(store, { ...input, operation: 'execute', approved: plan.summary.planSha256 }, current.get);
  assert.equal(result.summary.applied, true);
  assert.equal(result.summary.stageAccepted, false);
  assert.equal((await store.list('access', '')).names.length, 4);
  assert.equal((await store.list('index', '')).names.length, 1);
});

test('failed access logging prevents recovery writes', async () => {
  const current = fixture();
  const store = new MemoryAuditStore();
  store.fail = 'access';
  await assert.rejects(runRecoveryWorker(store, { scope: { tenantId: metadata.tenantId }, window: current.expected, operation: 'execute' }, current.get));
  assert.equal((await store.list('index', '')).names.length, 0);
});