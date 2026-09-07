export class MemoryAuditStore {
  constructor() { this.blobs = new Map(); this.fail = null; }
  async put(container, name, value) {
    if (this.fail === container) throw new Error('synthetic storage failure');
    const key = `${container}/${name}`;
    if (this.blobs.has(key)) throw new Error('Immutable object already exists');
    this.blobs.set(key, JSON.stringify(value));
  }
  async get(container, name) {
    const value = this.blobs.get(`${container}/${name}`);
    return value === undefined ? null : JSON.parse(value);
  }
  async list(container, prefix, limit = 1000, cursor) {
    const matches = [...this.blobs.keys()].filter(key => key.startsWith(`${container}/${prefix}`) && (!cursor || key > `${container}/${cursor}`)).sort();
    const names = matches.slice(0, limit).map(key => key.slice(container.length + 1));
    return { names, truncated: matches.length > limit, nextCursor: matches.length > limit ? names.at(-1) : null };
  }
  async remove(container, name) { this.blobs.delete(`${container}/${name}`); }
  async assertPurgePolicy() {}
}

export const metadata = {
  id: '11111111-1111-4111-8111-111111111111',
  tenantId: '22222222-2222-4222-8222-222222222222',
  teamId: 'team-a', subject: 'synthetic-subject', traceId: 'a'.repeat(32), model: 'model-a',
};