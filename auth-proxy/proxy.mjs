import { createServer } from 'node:http';
import { createHash, randomUUID, randomBytes } from 'node:crypto';
import { createProxyMiddleware } from 'http-proxy-middleware';
import { pipeline } from 'node:stream';
import * as oidc from 'openid-client';
import { apiCredentials, authorizeRoute, bindingFor, cleanHeaders, Denied, sanitizeBody, subjectTag } from './policy.mjs';
import { readCookie, sessionCookie } from './auth.mjs';
import { auditPage, auditStyle, auditClient } from './audit-page.mjs';
import { enforceFrontDoor, validateFrontDoorId } from './edge-policy.mjs';
import { deliverAuditedResponse } from './audit-delivery.mjs';
import { authorizeNativeUiRead, nativeUiAsset, nativeUiClaims, nativeUiCookie, nativeUiEnabled, nativeUiKeyLookup, verifyNativeUiRequest } from './native-ui.mjs';

const sessionName = '__Host-llm-admin';
const transactionName = '__Host-llm-login';
const now = () => Math.floor(Date.now() / 1000);

function reply(response, code, body) {
  response.writeHead(code, { 'content-type': 'application/json', 'cache-control': 'no-store', 'x-content-type-options': 'nosniff' });
  response.end(JSON.stringify(body));
}

async function readBody(request) {
  if (request.headers['content-type']?.split(';')[0] !== 'application/json' || request.headers['content-encoding']) throw new Denied(415);
  if (Number(request.headers['content-length']) > 1024 * 1024) throw new Denied(413);
  const chunks = [];
  let length = 0;
  for await (const chunk of request) {
    length += chunk.length;
    if (length > 1024 * 1024) throw new Denied(413);
    chunks.push(chunk);
  }
  try { return JSON.parse(Buffer.concat(chunks).toString('utf8')); }
  catch { throw new Denied(400); }
}

export function createGateway({ plane, getConfig, verifyToken, keyFor, sessions, oidcConfig, target, l3, auditReader, guardrail, telemetry, frontDoorId, audit = entry => console.log(JSON.stringify(entry)) }) {
  if (!['api', 'admin'].includes(plane)) throw new Error('Invalid proxy plane');
  validateFrontDoorId(plane, frontDoorId);
  const proxy = createProxyMiddleware({
    target, changeOrigin: true, xfwd: false, ws: false, proxyTimeout: 570000,
    selfHandleResponse: l3?.durable === true,
    on: {
      proxyReq(upstream, request, response) {
        if (l3?.durable) response.once('close', () => { if (!response.writableFinished) upstream.destroy(); });
        if (request.forwardBody) {
          upstream.setHeader('content-length', request.forwardBody.length);
          upstream.write(request.forwardBody);
        }
      },
      proxyRes(upstream, request, response) {
        delete upstream.headers['set-cookie'];
        const location = upstream.headers.location;
        if (request.nativeUiAsset && (location === '/ui/' || /^\/ui\/[A-Za-z0-9_./-]*$/.test(location ?? ''))) upstream.headers.location = location;
        else delete upstream.headers.location;
        upstream.headers['cache-control'] = 'no-store';
        if (request.nativeUiAsset) {
          upstream.headers['content-security-policy'] = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; font-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'";
          upstream.headers['referrer-policy'] = 'no-referrer';
          upstream.headers['x-content-type-options'] = 'nosniff';
        }
        if (l3?.durable === true) {
          if (request.l3?.durable) {
            request.auditDeliveryStarted = true;
            void deliverAuditedResponse(upstream, response, request.l3);
          } else {
            response.writeHead(upstream.statusCode, upstream.headers);
            pipeline(upstream, response, () => {});
          }
          return;
        }
        if (request.l3) {
          const contentType = upstream.headers['content-type'] ?? '';
          const format = upstream.headers['content-encoding'] && upstream.headers['content-encoding'] !== 'identity' ? 'unsupported' : contentType.includes('text/event-stream') ? 'sse' : contentType.includes('application/json') ? 'json' : 'unsupported';
          request.l3.response({ format, status: upstream.statusCode });
          upstream.on('data', chunk => request.l3.chunk(chunk));
          upstream.on('end', () => request.l3.finish('upstream_end'));
          upstream.on('error', () => request.l3.finish('upstream_error'));
          upstream.on('aborted', () => request.l3.finish('upstream_aborted'));
          response.on('close', () => upstream.destroy());
        }
      },
      error(_error, request, response) {
        request.l3?.finish('upstream_error');
        if (!response.headersSent) reply(response, 502, { error: 'Upstream unavailable' });
        else response.destroy();
      },
    },
  });
  const server = createServer(async (request, response) => {
    const requestId = randomUUID();
    const span = telemetry?.startSpan('gateway.request');
    const traceId = span?.spanContext().traceId ?? randomBytes(16).toString('hex');
    const traceparent = `00-${traceId}-${span?.spanContext().spanId ?? randomBytes(8).toString('hex')}-01`;
    const started = performance.now();
    let subject;
    let keyFingerprint;
    let logged = false;
    const logCompletion = outcome => {
      if (logged) return;
      logged = true;
      audit({ requestId, traceId, plane, subject, keyFingerprint, status: response.statusCode, outcome, durationMs: Math.round(performance.now() - started) });
      span?.setAttributes({ 'gateway.plane': plane, 'gateway.request_id': requestId, 'gateway.outcome': outcome, 'http.response.status_code': response.statusCode });
      span?.end();
    };
    const deadline = setTimeout(() => response.destroy(), 570000);
    response.on('close', () => clearTimeout(deadline));
    response.on('close', () => { if (!request.auditDeliveryStarted) request.l3?.finish('client_disconnect'); });
    response.on('finish', () => logCompletion('completed'));
    response.on('close', () => logCompletion('disconnected'));
    response.setHeader('x-request-id', requestId);
    response.setHeader('x-trace-id', traceId);
    try {
      if (request.url === '/healthz' && request.method === 'GET') return reply(response, 200, { ready: true });
      const config = await getConfig();
      if (request.url === '/readyz' && request.method === 'GET') return reply(response, 200, { ready: true });
      if (request.headers.host !== (plane === 'api' ? config.apiHost : config.adminHost)) throw new Denied(421);
      enforceFrontDoor(request, frontDoorId);
      if ((request.url?.length ?? 0) > 8192) throw new Denied(414);
      let claims;
      let clientKey;
      if (plane === 'api') {
        const credentials = apiCredentials(request);
        claims = await verifyToken(credentials.token);
        clientKey = credentials.key;
      } else {
        if (request.headers.authorization && config.nativeUi !== true) throw new Denied(401);
        const origin = `https://${config.adminHost}`;
        if (request.url === '/' && request.method === 'GET') {
          response.writeHead(302, { location: '/auth/login', 'cache-control': 'no-store' });
          return response.end();
        }
        if (request.url === '/auth/login' && request.method === 'GET') {
          const transaction = { state: oidc.randomState(), nonce: oidc.randomNonce(), verifier: oidc.randomPKCECodeVerifier() };
          const url = oidc.buildAuthorizationUrl(oidcConfig, {
            redirect_uri: `${origin}/auth/callback`, scope: 'openid profile', response_type: 'code',
            state: transaction.state, nonce: transaction.nonce, code_challenge_method: 'S256',
            code_challenge: await oidc.calculatePKCECodeChallenge(transaction.verifier),
          });
          response.setHeader('set-cookie', sessionCookie(transactionName, await sessions.seal(transaction, 'login', now() + 300)));
          response.writeHead(302, { location: url.href, 'cache-control': 'no-store' });
          return response.end();
        }
        if (request.url.startsWith('/auth/callback?') && request.method === 'GET') {
          const transaction = await sessions.open(readCookie(request.headers.cookie, transactionName), 'login');
          const tokens = await oidc.authorizationCodeGrant(oidcConfig, new URL(request.url, origin), {
            expectedState: transaction.state, expectedNonce: transaction.nonce, pkceCodeVerifier: transaction.verifier, idTokenExpected: true,
          });
          claims = tokens.claims();
          const loginBinding = bindingFor(config, 'admin', claims);
          const expiresAt = Math.min(claims.exp, now() + 300);
          const session = { tid: claims.tid, oid: claims.oid, roles: claims.roles, csrf: randomBytes(32).toString('hex'), exp: expiresAt };
          const cookie = await sessions.seal(session, 'session', expiresAt);
          const cookies = [sessionCookie(sessionName, cookie, expiresAt - now()), sessionCookie(transactionName, '', 0)];
          if (nativeUiEnabled(config, loginBinding)) cookies.push(nativeUiCookie(await sessions.uiToken(nativeUiClaims(loginBinding, session, subjectTag(config.tenantId, claims.oid))), expiresAt - now()));
          response.setHeader('set-cookie', cookies);
          response.writeHead(302, { location: nativeUiEnabled(config, loginBinding) ? '/ui/' : loginBinding.role === 'audit_reader' ? '/audit' : '/auth/session', 'cache-control': 'no-store' });
          return response.end();
        }
        try {
          claims = await sessions.open(readCookie(request.headers.cookie, sessionName), 'session');
        } catch (error) {
          if (config.nativeUi === true && request.method === 'GET' && ['/ui', '/ui/'].includes(request.url)) {
            response.writeHead(302, { location: '/auth/login', 'cache-control': 'no-store' });
            return response.end();
          }
          throw error;
        }
        if (request.headers.authorization) verifyNativeUiRequest(request, claims, origin);
        else if (!['GET', 'HEAD'].includes(request.method) &&
            (request.headers.origin !== origin || !claims.csrf || request.headers['x-csrf-token'] !== claims.csrf)) throw new Denied();
      }
      const binding = bindingFor(config, plane, claims);
      subject = subjectTag(config.tenantId, claims.oid);
      if (plane === 'api') keyFingerprint = createHash('sha256').update(clientKey).digest('hex');
      if (plane === 'admin' && request.method === 'GET' && ['/audit', '/audit/style.css', '/audit/client.js'].includes(request.url)) {
        if (!auditReader || binding.role !== 'audit_reader') throw new Denied();
        const asset = request.url === '/audit' ? ['text/html', auditPage] : request.url.endsWith('.css') ? ['text/css', auditStyle] : ['text/javascript', auditClient];
        response.writeHead(200, {
          'content-type': `${asset[0]}; charset=utf-8`, 'cache-control': 'no-store',
          'content-security-policy': "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
          'x-content-type-options': 'nosniff', 'referrer-policy': 'no-referrer',
        });
        return response.end(asset[1]);
      }
      if (plane === 'admin' && request.url === '/auth/session' && request.method === 'GET') return reply(response, 200, { subject, role: binding.role, csrf: claims.csrf });
      if (plane === 'admin' && request.url === '/auth/logout' && request.method === 'POST') {
        response.setHeader('set-cookie', [sessionCookie(sessionName, '', 0), 'token=; Path=/; Secure; SameSite=Lax; Max-Age=0']);
        return reply(response, 200, { signedOut: true });
      }
      if (plane === 'admin' && nativeUiAsset(request.method, request.url)) {
        if (!nativeUiEnabled(config, binding)) throw new Denied();
        request.nativeUiAsset = true;
        request.headers = Object.fromEntries(Object.entries(request.headers).filter(([name]) => ['accept', 'accept-encoding'].includes(name.toLowerCase())));
        return await proxy(request, response);
      }
      if (plane === 'admin' && request.method === 'POST' && ['/audit/search', '/audit/view'].includes(request.url)) {
        if (!auditReader) throw new Denied();
        const input = await readBody(request);
        return reply(response, 200, await auditReader.execute(request.url.slice('/audit/'.length), input, claims, binding));
      }
      const nativeRead = plane === 'admin' && nativeUiEnabled(config, binding) && authorizeNativeUiRead(request.method, request.url, binding);
      if (!nativeRead) authorizeRoute(plane, request.method, request.url, binding);
      const key = plane === 'api' ? clientKey : await keyFor(binding);
      if (!key || /[\r\n\s]/.test(key)) throw new Error('Missing backend credential');
      let originalBody;
      let forwardedBody;
      if (request.method === 'POST') {
        let body = await readBody(request);
        originalBody = body;
        if (plane === 'api') body = sanitizeBody(body, binding, subject);
        else if (nativeRead && request.url === '/v2/key/info') body = nativeUiKeyLookup(body, claims, key);
        else if (Object.keys(body).length !== 1 || typeof body.key !== 'string') throw new Denied(400);
        request.forwardBody = Buffer.from(JSON.stringify(body));
        forwardedBody = body;
      }
      request.headers = cleanHeaders(request.headers, key);
      request.headers.traceparent = traceparent;
      request.headers['x-request-id'] = requestId;
      if (plane === 'api' && binding.audit?.capture) {
        if (!l3) throw new Error('Required audit service is disabled');
        request.l3 = await l3.begin({ id: requestId, traceId, tenantId: claims.tid, subject, teamId: binding.audit.teamId, model: forwardedBody.model }, originalBody, forwardedBody);
        request.headers['accept-encoding'] = 'identity';
        if (response.destroyed) { request.l3.finish('client_disconnect'); return; }
      }
      if (plane === 'api' && guardrail) await guardrail.inspect(forwardedBody, { traceId });
      if (request.forwardBody) request.headers['content-length'] = String(request.forwardBody.length);
      await proxy(request, response);
    } catch (error) {
      request.l3?.finish(error instanceof Denied ? 'policy_denied' : 'gateway_error');
      const code = error instanceof Denied ? error.code : 503;
      request.resume();
      if (code === 401 && plane === 'api') response.setHeader('www-authenticate', 'Bearer');
      if (!response.headersSent) reply(response, code, { error: code === 503 ? 'Identity service unavailable' : 'Request denied', requestId });
      else response.destroy();
    }
  });
  server.on('upgrade', (_request, socket) => {
    socket.end('HTTP/1.1 403 Forbidden\r\nConnection: close\r\nContent-Length: 0\r\n\r\n');
  });
  server.requestTimeout = 30000;
  server.headersTimeout = 15000;
  return server;
}