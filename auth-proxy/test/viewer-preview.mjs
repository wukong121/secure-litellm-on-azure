import { createServer } from 'node:http';
import { auditPage, auditStyle, auditClient } from '../audit-page.mjs';

const record = { id: '11111111-1111-4111-8111-111111111111', createdAt: '2026-09-07T12:00:00Z', teamId: 'synthetic-team', model: 'synthetic-model', complete: true };
const server = createServer(async (request, response) => {
  response.setHeader('cache-control', 'no-store');
  const assets = { '/audit': ['text/html', auditPage], '/audit/style.css': ['text/css', auditStyle], '/audit/client.js': ['text/javascript', auditClient] };
  if (assets[request.url] && request.method === 'GET') {
    response.setHeader('content-type', `${assets[request.url][0]}; charset=utf-8`);
    return response.end(assets[request.url][1]);
  }
  response.setHeader('content-type', 'application/json');
  if (request.url === '/auth/session') return response.end(JSON.stringify({ role: 'audit_reader (synthetic preview)', csrf: 'synthetic' }));
  if (request.method === 'POST' && ['/audit/search', '/audit/view'].includes(request.url)) {
    let data = '';
    for await (const chunk of request) { data += chunk; if (data.length > 4096) { response.writeHead(413); return response.end('{}'); } }
    try {
      if (JSON.parse(data).approvalId !== record.id) { response.writeHead(403); return response.end('{}'); }
      return response.end(JSON.stringify(request.url === '/audit/search' ? { records: [record], nextCursor: null } : { ...record, content: { originalRequest: { input: 'Synthetic source text <script>alert(1)</script>' }, response: { output: 'Synthetic model response.' } } }));
    } catch { response.writeHead(400); return response.end('{}'); }
  }
  response.writeHead(404);
  response.end('{}');
});
server.listen(Number(process.env.PORT ?? 4188), '127.0.0.1', () => console.log('Synthetic viewer only: http://127.0.0.1:4188/audit (approval ID 11111111-1111-4111-8111-111111111111). No Entra, Azure or real data.'));