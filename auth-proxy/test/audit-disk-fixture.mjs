import { mkdir, open, readFile, readdir, rm } from 'node:fs/promises';
import { dirname, join } from 'node:path';

export class DiskAuditStore {
  constructor(root) { this.root = root; }
  path(kind, name) {
    if (!['pending', 'content', 'index', 'access'].includes(kind) || !/^[a-zA-Z0-9./-]+$/.test(name) || name.includes('..') || name.startsWith('/')) throw new Error('Invalid synthetic object');
    return join(this.root, kind, name);
  }
  async put(kind, name, value) {
    const path = this.path(kind, name);
    const data = JSON.stringify(value);
    await mkdir(dirname(path), { recursive: true });
    let file;
    try { file = await open(path, 'wx', 0o600); }
    catch (error) {
      if (error.code === 'EEXIST' && await readFile(path, 'utf8') === data) return;
      throw error;
    }
    try { await file.writeFile(data); await file.sync(); }
    finally { await file.close(); }
  }
  async get(kind, name) {
    try { return JSON.parse(await readFile(this.path(kind, name), 'utf8')); }
    catch (error) { if (error.code === 'ENOENT') return null; throw error; }
  }
  async list(kind, prefix, limit = 1000, cursor) {
    let entries;
    try { entries = await readdir(join(this.root, kind), { recursive: true }); }
    catch (error) { if (error.code !== 'ENOENT') throw error; entries = []; }
    const matches = entries.filter(name => name.endsWith('.json') && name.startsWith(prefix) && (!cursor || name > cursor)).sort();
    const names = matches.slice(0, limit);
    return { names, truncated: matches.length > limit, nextCursor: matches.length > limit ? names.at(-1) : null };
  }
  async remove(kind, name) { await rm(this.path(kind, name), { force: true }); }
}