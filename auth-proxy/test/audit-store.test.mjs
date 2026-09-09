import test from 'node:test';
import assert from 'node:assert/strict';
import { AzureAuditStore } from '../audit-store.mjs';

function fixture(initial, uploadError) {
  let persisted = initial;
  const calls = [];
  const blob = {
    async uploadData(data, options) {
      calls.push(['upload', options]);
      if (persisted !== undefined) throw Object.assign(new Error('exists'), { statusCode: 412, code: 'ConditionNotMet' });
      persisted = Buffer.from(data);
      if (uploadError) throw uploadError;
    },
    async getProperties() { return { contentLength: persisted.length, etag: 'version-1' }; },
    async downloadToBuffer(start, length, options) {
      calls.push(['read', options]);
      assert.equal(start, 0);
      assert.equal(length, persisted.length);
      return persisted;
    },
  };
  const store = new AzureAuditStore('https://synthetic.blob.core.windows.net', { getToken() {} });
  store.container = () => ({ getBlockBlobClient: () => blob });
  return { store, calls };
}

test('immutable retry accepts identical bytes and still uses conditional create/read', async () => {
  const { store, calls } = fixture();
  const value = { id: 'synthetic', content: 'test' };
  await store.put('content', 'request.json', value);
  await store.put('content', 'request.json', value);
  assert.ok(calls.filter(([kind]) => kind === 'upload').every(([, options]) => options.conditions.ifNoneMatch === '*'));
  assert.equal(calls.find(([kind]) => kind === 'read')[1].conditions.ifMatch, 'version-1');
});

test('retry after an uncertain committed write is safe but does not suppress first transport failure', async () => {
  const { store } = fixture(undefined, new Error('synthetic lost acknowledgement'));
  await assert.rejects(store.put('content', 'request.json', { value: 'same' }), /lost acknowledgement/);
  await store.put('content', 'request.json', { value: 'same' });
});

test('different bytes, including same-length changes, never replace immutable content', async () => {
  const { store } = fixture(Buffer.from(JSON.stringify({ value: 'first' })));
  await assert.rejects(store.put('content', 'request.json', { value: 'other' }), /Conflicting/);
  await assert.rejects(store.put('content', 'request.json', { value: 'longer-content' }), /Conflicting/);
});

const url = 'https://syntheticaudit.blob.core.windows.net';
const resourceId = '/subscriptions/11111111-1111-4111-8111-111111111111/resourceGroups/synthetic/providers/Microsoft.Storage/storageAccounts/syntheticaudit';
const credential = { getToken: async scope => { assert.equal(scope, 'https://management.azure.com/.default'); return { token: 'synthetic-only-token' }; } };

test('physical deletion gate uses ARM, not incomplete Blob data-plane properties', async () => {
  const safe = { deleteRetentionPolicy: { enabled: false }, containerDeleteRetentionPolicy: { enabled: false }, isVersioningEnabled: false };
  const store = properties => new AzureAuditStore(url, credential, { resourceId, fetcher: async endpoint => {
    assert.equal(endpoint, `https://management.azure.com${resourceId}/blobServices/default?api-version=2025-06-01`);
    return { ok: true, json: async () => ({ properties }) };
  } });
  await store(safe).assertPurgePolicy();
  for (const properties of [{}, { ...safe, isVersioningEnabled: true }, { ...safe, deleteRetentionPolicy: { enabled: true } }, { ...safe, containerDeleteRetentionPolicy: { enabled: true } }]) await assert.rejects(() => store(properties).assertPurgePolicy());
  await assert.rejects(() => new AzureAuditStore(url, credential).assertPurgePolicy());
});