import test from 'node:test';
import assert from 'node:assert/strict';
import { AzureAuditStore } from '../audit-store.mjs';

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