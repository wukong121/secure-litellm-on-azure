import test from 'node:test';
import assert from 'node:assert/strict';
import { auditCredentialOptions } from '../stage8-runtime.mjs';

const proxyId = '11111111-1111-4111-8111-111111111111';
const auditId = '22222222-2222-4222-8222-222222222222';

test('durable audit explicitly selects a separate identity on the same federated service account', () => {
  const config = { deliveryMode: 'persist-before-forward', clientId: auditId };
  assert.deepEqual(auditCredentialOptions(config, { AZURE_CLIENT_ID: proxyId }), { clientId: auditId });
  for (const clientId of [undefined, '', proxyId, 'REPLACE_ID', '00000000-0000-0000-0000-000000000000']) {
    assert.throws(() => auditCredentialOptions({ ...config, clientId }, { AZURE_CLIENT_ID: proxyId }));
  }
});

test('legacy buffered configuration keeps its previous implicit identity contract', () => {
  assert.deepEqual(auditCredentialOptions({}), {});
  assert.deepEqual(auditCredentialOptions({ deliveryMode: 'buffered' }), {});
});