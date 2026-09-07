import test from 'node:test';
import assert from 'node:assert/strict';
import { createLocalJWKSet, exportJWK, generateKeyPair, SignJWT } from 'jose';
import { createSessions, createTokenVerifier, readCookie, sessionCookie } from '../auth.mjs';

const tenantId = '11111111-1111-1111-1111-111111111111';
const issuer = `https://login.microsoftonline.com/${tenantId}/v2.0`;
const { privateKey, publicKey } = await generateKeyPair('RS256');
const verify = createTokenVerifier({ tenantId, apiAudience: 'api-audience' }, createLocalJWKSet({ keys: [{ ...await exportJWK(publicKey), kid: 'test' }] }));
const timestamp = Math.floor(Date.now() / 1000);
const payload = { iss: issuer, aud: 'api-audience', tid: tenantId, oid: 'user', azp: 'client', ver: '2.0', iat: timestamp, nbf: timestamp, exp: timestamp + 300 };
const sign = (overrides = {}, key = privateKey) => new SignJWT({ ...payload, ...overrides }).setProtectedHeader({ alg: 'RS256', kid: 'test' }).sign(key);

test('real RS256 JWT requires trusted signature, issuer, audience, expiry and activation', async () => {
  assert.equal((await verify(await sign())).oid, 'user');
  for (const update of [{ iss: 'other' }, { aud: 'admin-audience' }, { exp: timestamp - 60 }, { nbf: timestamp + 300 }, { exp: undefined }, { oid: undefined }]) await assert.rejects(() => sign(update).then(verify));
  const other = await generateKeyPair('RS256');
  await assert.rejects(() => sign({}, other.privateKey).then(verify));
  await assert.rejects(() => verify('not-a-jwt'));
});

test('encrypted cookies are short-lived, purpose-bound, host-bound and shared across replicas', async () => {
  const key = new Uint8Array(32).fill(1);
  const sessions = createSessions(key, 'llm-admin.test.invalid');
  const token = await sessions.seal({ oid: 'user' }, 'session', timestamp + 300);
  assert.equal((await createSessions(key, 'llm-admin.test.invalid').open(token, 'session')).oid, 'user');
  await assert.rejects(() => sessions.open(token, 'login'));
  await assert.rejects(() => createSessions(key, 'llm-api.test.invalid').open(token, 'session'));
  await assert.rejects(() => sessions.open(`${token}bad`, 'session'));
  await assert.rejects(() => sessions.seal({}, 'session', timestamp - 60).then(value => sessions.open(value, 'session')));
  assert.match(sessionCookie('__Host-test', token), /Secure; HttpOnly; SameSite=Lax; Max-Age=300/);
  assert.throws(() => readCookie('__Host-test=a; __Host-test=b', '__Host-test'));
});