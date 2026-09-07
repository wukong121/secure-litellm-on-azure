import { createRemoteJWKSet, EncryptJWT, jwtDecrypt, jwtVerify } from 'jose';
import { Denied } from './policy.mjs';

export function createTokenVerifier(config, keySet) {
  const issuer = `https://login.microsoftonline.com/${config.tenantId}/v2.0`;
  const keys = keySet ?? createRemoteJWKSet(new URL(`https://login.microsoftonline.com/${config.tenantId}/discovery/v2.0/keys`), { timeoutDuration: 5000, cooldownDuration: 30000 });
  return async token => {
    try {
      const { payload } = await jwtVerify(token, keys, {
        issuer, audience: config.apiAudience, algorithms: ['RS256'],
        requiredClaims: ['exp', 'iat', 'nbf', 'oid', 'tid', 'azp', 'ver'],
        maxTokenAge: '90m', clockTolerance: 30,
      });
      return payload;
    } catch {
      throw new Denied(401);
    }
  };
}

export function createSessions(key, host) {
  if (key.length !== 32) throw new Error('Session encryption requires 32 bytes');
  return {
    async seal(payload, purpose, expiresAt) {
      return new EncryptJWT({ ...payload, purpose })
        .setProtectedHeader({ alg: 'dir', enc: 'A256GCM' })
        .setIssuer('llm-admin-proxy').setAudience(host).setIssuedAt()
        .setExpirationTime(expiresAt).encrypt(key);
    },
    async open(token, purpose) {
      try {
        const { payload } = await jwtDecrypt(token, key, {
          issuer: 'llm-admin-proxy', audience: host,
          keyManagementAlgorithms: ['dir'], contentEncryptionAlgorithms: ['A256GCM'],
          requiredClaims: ['exp', 'iat'],
        });
        if (payload.purpose !== purpose) throw new Denied(401);
        return payload;
      } catch {
        throw new Denied(401);
      }
    },
  };
}

export function readCookie(header, name) {
  const matches = (header ?? '').split(';').map(value => value.trim()).filter(value => value.startsWith(`${name}=`));
  if (matches.length !== 1) throw new Denied(401);
  return matches[0].slice(name.length + 1);
}

export function sessionCookie(name, value, maxAge = 300) {
  return `${name}=${value}; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age=${maxAge}`;
}