import { JournalAuditWriter } from '../audit-journal.mjs';
import { objectName } from '../audit-capture.mjs';
import { metadata } from './audit-fixture.mjs';
import { DiskAuditStore } from './audit-disk-fixture.mjs';

const [root, checkpoint] = process.argv.slice(2);
const store = new DiskAuditStore(root);
const writer = new JournalAuditWriter(store, { clock: () => Date.parse('2026-09-09T00:00:00Z') });
const capture = await writer.begin(metadata, { input: 'synthetic disk request' }, {});
await capture.response({ format: 'sse', status: 200 });
await capture.frame('data: {"delta":"synthetic disk response"}\n\n');
process.on('message', () => {});
if (checkpoint === 'terminal') {
  await capture.frame('data: [DONE]\n\n');
  const original = store.put.bind(store);
  store.put = async (kind, name, value) => {
    if (kind === 'content' && name === objectName(metadata.tenantId, metadata.id)) {
      process.send({ checkpoint });
      await new Promise(() => {});
    }
    return original(kind, name, value);
  };
  await capture.finish('upstream_end');
} else { process.send({ checkpoint }); }