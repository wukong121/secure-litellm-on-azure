import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { once } from 'node:events';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { createServer } from 'node:https';
import { chromium } from 'playwright';
import { createSessions } from '../auth.mjs';
import { createGateway } from '../proxy.mjs';
import { nativeUiClaims } from '../native-ui.mjs';

const [backend, certificate, privateKey, output] = process.argv.slice(2);
assert.match(backend, /^http:\/\/(?:10|172|192)\.[0-9.]+:4000$/);
const tenant = '11111111-1111-4111-8111-111111111111';
const actor = '12121212-1212-4212-8212-121212121212';
const keyFile = 'key-' + createHash('sha256').update(`${tenant}:admin:${actor}`).digest('hex').slice(0, 48);
const binding = { plane: 'admin', oid: actor, role: 'proxy_admin_viewer', keyFile, nativeAuditRead: true };
const config = { nativeUi: true, tenantId: tenant, adminHost: '', bindings: [binding] };
let sessions;
const backendKey = 'sk-' + createHash('sha256').update('synthetic-native-reader').digest('hex');
const failures = [];
const gateway = createGateway({ plane: 'admin', getConfig: () => config, sessions: { open: (...args) => sessions.open(...args) }, target: backend, keyFor: () => backendKey, audit: () => {} });
const handler = gateway.listeners('request')[0];
const server = createServer({ cert: await readFile(certificate), key: await readFile(privateKey) }, handler);
server.listen(0, '127.0.0.1');
await once(server, 'listening');
config.adminHost = `localhost:${server.address().port}`;
sessions = createSessions(Buffer.alloc(32, 9), config.adminHost);
await mkdir(output, { recursive: true });
let browser;
try {
  browser = await chromium.launch({ executablePath: process.env.BROWSER_EXECUTABLE_PATH || '/usr/bin/google-chrome', headless: true, args: ['--no-sandbox'] });
  const context = await browser.newContext({ ignoreHTTPSErrors: true, viewport: { width: 1440, height: 1000 } });
  const page = await context.newPage();
  page.on('response', response => { if (response.status() >= 400) failures.push({ path: new URL(response.url()).pathname, status: response.status() }); });
  const origin = 'https://' + config.adminHost;
  const claims = { tid: tenant, oid: actor, roles: [binding.role], csrf: 'b'.repeat(64), exp: Math.floor(Date.now() / 1000) + 300 };
  const cookie = await sessions.seal(claims, 'session', claims.exp);
  await context.addCookies([
    { name: '__Host-llm-admin', value: cookie, url: origin, secure: true, httpOnly: true, sameSite: 'Lax' },
    { name: 'token', value: await sessions.uiToken(nativeUiClaims(binding, claims, 'synthetic-subject')), url: origin, secure: true, sameSite: 'Lax' },
  ]);
  await page.goto(origin + '/ui/', { waitUntil: 'domcontentloaded' });
  await page.getByText('Virtual Keys', { exact: true }).first().waitFor({ timeout: 30000 });
  const listing = page.waitForResponse(response => new URL(response.url()).pathname === '/spend/logs/ui' && response.status() === 200, { timeout: 30000 });
  await page.getByText('Logs', { exact: true }).first().click();
  const logs = await (await listing).json();
  assert.ok(logs.data?.length >= 2, 'Native UI did not list the synthetic JSON/SSE calls');
  await page.screenshot({ path: output + '/desktop.png', fullPage: true });
  const detail = page.waitForResponse(response => /^\/spend\/logs\/ui\/[^/]+$/.test(new URL(response.url()).pathname) && response.status() === 200, { timeout: 15000 });
  await page.locator('tbody tr').first().click();
  const content = await (await detail).json();
  assert.ok(JSON.stringify(content).includes('SYNTHETIC_NATIVE_PROMPT'));
  assert.ok(JSON.stringify(content).includes('SYNTHETIC_NATIVE_RESPONSE'));
  assert.ok(!JSON.stringify(await context.cookies()).includes(backendKey));
  await page.screenshot({ path: output + '/detail.png', fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: output + '/mobile.png', fullPage: true });
  const denied = await page.evaluate(async () => {
    const response = await fetch('/key/generate', { method: 'POST', headers: { 'content-type': 'application/json', authorization: 'Bearer ' + 'b'.repeat(64) }, body: JSON.stringify({ models: ['coding'] }) });
    return response.status;
  });
  assert.equal(denied, 403);
  const report = { scope: 'native-read-core-only', nativeUiRendered: true, nativeLogsListed: true, nativeContentRead: true, unauthorizedWritesDenied: true, fullManagementAccepted: false, mobileAccepted: false, failures };
  await writeFile(output + '/report.json', JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report));
} catch (error) {
  await writeFile(output + '/failure.json', JSON.stringify({ error: error.message, failures }, null, 2));
  throw error;
} finally {
  await browser?.close();
  server.closeAllConnections();
  await new Promise(resolve => server.close(resolve));
  gateway.close();
}