import { readFile } from 'node:fs/promises';
import { join } from 'node:path';
import * as oidc from 'openid-client';
import { createGateway } from './proxy.mjs';
import { createSessions, createTokenVerifier } from './auth.mjs';
import { validateConfig } from './policy.mjs';
import { stage8Services } from './stage8-runtime.mjs';

async function main() {
  const plane = process.env.PROXY_PLANE;
  const configPath = process.env.PROXY_CONFIG ?? '/etc/auth-proxy/policy.json';
  const secretsPath = '/mnt/auth-secrets';
  const readConfig = async () => validateConfig(JSON.parse(await readFile(configPath, 'utf8')));
  const config = await readConfig();
  const getConfig = async () => {
    const current = await readConfig();
    for (const field of ['tenantId', 'apiAudience', 'apiHost', 'adminHost', 'adminClientId']) {
      if (current[field] !== config[field]) throw new Error('Trust configuration changed; restart required');
    }
    return current;
  };
  const readSecret = async name => (await readFile(join(secretsPath, name), 'utf8')).trim();
  const sessions = plane === 'admin' ? createSessions(Buffer.from(await readSecret('session-key'), 'base64'), config.adminHost) : undefined;
  const oidcConfig = plane === 'admin' ? await oidc.discovery(
    new URL(`https://login.microsoftonline.com/${config.tenantId}/v2.0`),
    config.adminClientId, await readSecret('oidc-client-secret'),
  ) : undefined;
  const services = await stage8Services(plane);
  const server = createGateway({
    ...services,
    plane, getConfig, verifyToken: createTokenVerifier(config), sessions, oidcConfig,
    frontDoorId: process.env.FRONT_DOOR_ID,
    keyFor: binding => readSecret(binding.keyFile),
    target: 'http://litellm.litellm.svc.cluster.local:4000',
  });
  server.listen(8080, '0.0.0.0');
  process.on('SIGTERM', () => {
    server.close(async () => {
      await services.l3?.drain();
      await services.shutdownTelemetry?.();
    });
    setTimeout(() => process.exit(1), 570000).unref();
  });
}

main().catch(() => {
  console.error('Auth proxy startup failed; check protected configuration and identity dependencies');
  process.exitCode = 1;
});