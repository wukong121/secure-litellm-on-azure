import { EncryptJWT, jwtDecrypt } from 'jose';
import { Denied } from './policy.mjs';

const PREFIX = { response: 'resp_llmgw.', item: 'item_llmgw.', encrypted: 'ctx_llmgw.' };
const MAX_REFERENCE_BYTES = 512 * 1024;

export class ResponseContext {
  constructor(key, { ttlSeconds = 86400, clock = () => Date.now() } = {}) {
    if (!(key instanceof Uint8Array) || key.length !== 32 || !Number.isInteger(ttlSeconds) || ttlSeconds < 60 || ttlSeconds > 86400) throw new Error('Invalid response context encryption settings');
    this.key = key;
    this.ttlSeconds = ttlSeconds;
    this.clock = clock;
  }

  scope(identity) {
    if (!identity || !['tenantId', 'subject', 'model'].every(name => typeof identity[name] === 'string' && identity[name].length > 0 && identity[name].length <= 256)) throw new Denied();
    return { tenantId: identity.tenantId, subject: identity.subject, model: identity.model };
  }

  async seal(kind, value, identity) {
    if (!PREFIX[kind] || typeof value !== 'string' || !value || Buffer.byteLength(value) > MAX_REFERENCE_BYTES) throw new Denied(400);
    const scope = this.scope(identity);
    const now = Math.floor(this.clock() / 1000);
    const encrypted = await new EncryptJWT({ kind, value, ...scope }).setProtectedHeader({ alg: 'dir', enc: 'A256GCM', typ: 'llmgw-context+jwt' })
      .setIssuer('llmgw-response-context').setAudience('llmgw-api').setIssuedAt(now).setExpirationTime(now + this.ttlSeconds).encrypt(this.key);
    return PREFIX[kind] + encrypted;
  }

  async open(kind, value, identity) {
    const scope = this.scope(identity);
    if (!PREFIX[kind] || typeof value !== 'string' || !value.startsWith(PREFIX[kind]) || Buffer.byteLength(value) > 2 * MAX_REFERENCE_BYTES) throw new Denied();
    try {
      const { payload, protectedHeader } = await jwtDecrypt(value.slice(PREFIX[kind].length), this.key, {
        issuer: 'llmgw-response-context', audience: 'llmgw-api', keyManagementAlgorithms: ['dir'], contentEncryptionAlgorithms: ['A256GCM'], currentDate: new Date(this.clock()), clockTolerance: 0,
      });
      if (protectedHeader.typ !== 'llmgw-context+jwt' || payload.kind !== kind || Object.entries(scope).some(([name, expected]) => payload[name] !== expected) ||
          typeof payload.value !== 'string' || typeof payload.iat !== 'number' || typeof payload.exp !== 'number' || payload.exp - payload.iat > this.ttlSeconds || payload.iat > Math.floor(this.clock() / 1000)) throw new Denied();
      return payload.value;
    } catch { throw new Denied(); }
  }

  outputEncoder(identity) {
    const cache = new Map();
    let count = 0;
    let bytes = 0;
    let cachedBytes = 0;
    const encode = async (kind, value) => {
      const key = kind + ':' + value;
      if (cache.has(key)) return cache.get(key);
      if (cache.size >= 2048) throw new Denied(413);
      cachedBytes += Buffer.byteLength(value);
      if (cachedBytes > 4 * 1024 * 1024) throw new Denied(413);
      const sealed = this.seal(kind, value, identity);
      cache.set(key, sealed);
      return sealed;
    };
    const visit = async (value, depth = 0) => {
      if (++count > 20000 || depth > 48) throw new Denied(413);
      if (Array.isArray(value)) return Promise.all(value.map(item => visit(item, depth + 1)));
      if (!value || typeof value !== 'object') {
        if (typeof value === 'string') bytes += Buffer.byteLength(value);
        if (bytes > 4 * 1024 * 1024) throw new Denied(413);
        return value;
      }
      const result = Object.create(null);
      for (const [key, item] of Object.entries(value)) {
        if (key === 'encrypted_content' && typeof item === 'string') result[key] = await encode('encrypted', item);
        else if (key === 'response_id' && typeof item === 'string') result[key] = await encode('response', item);
        else if (key === 'item_id' && typeof item === 'string') result[key] = await encode('item', item);
        else if (key === 'id' && typeof item === 'string' && (value.object === 'response' || item.startsWith('resp_'))) result[key] = await encode('response', item);
        else if (key === 'id' && typeof item === 'string' && value.type && ['message', 'reasoning', 'function_call', 'custom_tool_call', 'compaction'].includes(value.type)) result[key] = await encode('item', item);
        else result[key] = await visit(item, depth + 1);
      }
      return result;
    };
    let active = false;
    return async value => {
      if (active) throw new Denied(409);
      active = true;
      count = 0;
      bytes = 0;
      try { return await visit(value); }
      finally { active = false; }
    };
  }

  async decodeInput(body, identity) {
    let count = 0;
    const visit = async (value, depth = 0) => {
      if (++count > 20000 || depth > 48) throw new Denied(413);
      if (Array.isArray(value)) return Promise.all(value.map(item => visit(item, depth + 1)));
      if (!value || typeof value !== 'object') return value;
      const result = Object.create(null);
      for (const [key, item] of Object.entries(value)) {
        if (key === 'file_id' || ['input_file', 'file'].includes(value.type)) throw new Denied();
        if (['previous_response_id', 'response_id'].includes(key) && item !== null) result[key] = await this.open('response', item, identity);
        else if (key === 'encrypted_content' && item !== null) result[key] = await this.open('encrypted', item, identity);
        else if (key === 'item_id' || (key === 'id' && value.type === 'item_reference')) result[key] = await this.open('item', item, identity);
        else if (key === 'id' && typeof item === 'string' && item.startsWith(PREFIX.item)) result[key] = await this.open('item', item, identity);
        else if (key === 'id' && typeof item === 'string' && ['message', 'reasoning', 'function_call', 'custom_tool_call', 'compaction'].includes(value.type)) throw new Denied();
        else result[key] = await visit(item, depth + 1);
      }
      return result;
    };
    return visit(body);
  }
}