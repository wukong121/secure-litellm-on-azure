import { BlobServiceClient } from '@azure/storage-blob';

export const containers = { content: 'l3-content', index: 'l3-index', pending: 'l3-pending', access: 'l3-access' };

export class AzureAuditStore {
  constructor(url, credential, { resourceId, fetcher = fetch } = {}) {
    if (!/^https:\/\/[a-z0-9]{3,24}\.blob\.core\.windows\.net\/?$/.test(url)) throw new Error('Invalid audit storage endpoint');
    Object.assign(this, { url, credential, resourceId, fetcher });
    this.client = new BlobServiceClient(url, credential, { retryOptions: { maxTries: 3, tryTimeoutInMs: 10000, maxRetryDelayInMs: 2000 } });
  }
  container(kind) {
    if (!containers[kind]) throw new Error('Invalid container');
    return this.client.getContainerClient(containers[kind]);
  }
  async put(kind, name, value) {
    await this.container(kind).getBlockBlobClient(name).uploadData(Buffer.from(JSON.stringify(value)), {
      conditions: { ifNoneMatch: '*' }, abortSignal: AbortSignal.timeout(15000),
      blobHTTPHeaders: { blobContentType: 'application/json', blobCacheControl: 'no-store' },
    });
  }
  async get(kind, name) {
    const blob = this.container(kind).getBlockBlobClient(name);
    try {
      const properties = await blob.getProperties({ abortSignal: AbortSignal.timeout(15000) });
      if (properties.contentLength > 16 * 1024 * 1024) throw new Error('Audit object too large');
      const data = await blob.downloadToBuffer(0, undefined, { abortSignal: AbortSignal.timeout(15000), conditions: { ifMatch: properties.etag } });
      return JSON.parse(data.toString('utf8'));
    } catch (error) {
      if (error.statusCode === 404) return null;
      throw error;
    }
  }
  async list(kind, prefix, limit = 1000, cursor) {
    const page = await this.container(kind).listBlobsFlat({ prefix, abortSignal: AbortSignal.timeout(15000) }).byPage({ maxPageSize: limit, continuationToken: cursor }).next();
    return { names: page.value?.segment.blobItems.map(blob => blob.name) ?? [], truncated: Boolean(page.value?.continuationToken), nextCursor: page.value?.continuationToken ?? null };
  }
  async remove(kind, name) {
    await this.container(kind).getBlobClient(name).deleteIfExists({ deleteSnapshots: 'include', abortSignal: AbortSignal.timeout(15000) });
  }
  async assertPurgePolicy() {
    if (!/^\/subscriptions\/[a-f0-9-]{36}\/resourceGroups\/[a-zA-Z0-9_.-]+\/providers\/Microsoft.Storage\/storageAccounts\/[a-z0-9]{3,24}$/.test(this.resourceId ?? '') || this.resourceId.split('/').at(-1) !== new URL(this.url).hostname.split('.')[0]) throw new Error('Retention requires the matching Storage Account ARM resource ID');
    const token = await this.credential.getToken('https://management.azure.com/.default');
    const result = await this.fetcher(`https://management.azure.com${this.resourceId}/blobServices/default?api-version=2025-06-01`, {
      headers: { authorization: `Bearer ${token.token}` }, redirect: 'error', signal: AbortSignal.timeout(15000),
    });
    if (!result.ok) throw new Error('Cannot verify storage retention policy');
    const { properties } = await result.json();
    if (properties?.deleteRetentionPolicy?.enabled !== false || properties?.containerDeleteRetentionPolicy?.enabled !== false || properties?.isVersioningEnabled !== false) throw new Error('Purge requires explicit disabled retention of versions and soft-deleted copies');
  }
}