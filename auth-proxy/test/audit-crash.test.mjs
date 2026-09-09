import test from 'node:test';
import assert from 'node:assert/strict';
import { fork } from 'node:child_process';
import { once } from 'node:events';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { DiskAuditStore } from './audit-disk-fixture.mjs';
import { metadata } from './audit-fixture.mjs';
import { objectName } from '../audit-capture.mjs';
import { recoverRequest } from '../audit-recovery.mjs';

for (const checkpoint of ['frame', 'terminal']) {
  test(`SIGKILL after durable ${checkpoint} permits recovery from a new store instance`, { timeout: 10000 }, async context => {
    const directory = await mkdtemp(join(tmpdir(), 'llmgw-audit-crash-'));
    const worker = fork(new URL('./audit-crash-worker.mjs', import.meta.url), [directory, checkpoint], { stdio: ['ignore', 'ignore', 'ignore', 'ipc'] });
    const exited = once(worker, 'exit');
    context.after(async () => {
      if (worker.exitCode === null && worker.signalCode === null) worker.kill('SIGKILL');
      await exited;
      await rm(directory, { recursive: true, force: true });
    });
    const ready = await Promise.race([once(worker, 'message').then(([message]) => message), exited.then(() => { throw new Error('Synthetic writer exited before checkpoint'); })]);
    assert.equal(ready.checkpoint, checkpoint);
    worker.kill('SIGKILL');
    assert.equal((await exited)[1], 'SIGKILL');
    const store = new DiskAuditStore(directory);
    const name = objectName(metadata.tenantId, metadata.id);
    assert.equal(await store.get('index', name), null);
    const result = await recoverRequest(store, name, { execute: true, clock: () => Date.parse('2026-09-09T00:16:00Z') });
    assert.equal(result.status, checkpoint === 'frame' ? 'partial_recovery' : 'completion_repair');
    const record = await store.get('content', name);
    assert.equal(record.complete, checkpoint === 'terminal');
    assert.equal(record.clientDelivery, 'unconfirmed');
    assert.match(record.content.response, /synthetic disk response/);
    assert.equal((await store.get('index', name)).complete, record.complete);
  });
}