import { readFile } from 'node:fs/promises';
import { WorkloadIdentityCredential } from '@azure/identity';
import { BasicTracerProvider, BatchSpanProcessor } from '@opentelemetry/sdk-trace-base';
import { OTLPTraceExporter } from '@opentelemetry/exporter-trace-otlp-http';
import { resourceFromAttributes } from '@opentelemetry/resources';
import { AuditWriter } from './audit-capture.mjs';
import { AzureAuditStore } from './audit-store.mjs';
import { AuditReader } from './audit-reader.mjs';
import { ContentSafetyGuardrail } from './guardrail.mjs';

export async function stage8Services(plane) {
  if (!process.env.STAGE8_CONFIG) return {};
  const config = JSON.parse(await readFile(process.env.STAGE8_CONFIG, 'utf8'));
  const signal = event => console.log(JSON.stringify(event));
  const services = {};
  if (plane === 'api' && config.guardrail?.enabled === true) services.guardrail = new ContentSafetyGuardrail(config.guardrail, new WorkloadIdentityCredential(), { signal });
  if (config.l3?.enabled === true) {
    const store = new AzureAuditStore(config.l3.storageUrl, new WorkloadIdentityCredential());
    if (plane === 'api') services.l3 = new AuditWriter(store, { retentionDays: config.l3.retentionDays, signal });
    if (plane === 'admin') services.auditReader = new AuditReader(store, async () => JSON.parse(await readFile('/etc/audit-approvals/approvals.json', 'utf8')), { signal });
  }
  if (config.telemetry?.enabled === true) {
    if (config.telemetry.endpoint !== 'http://otel-collector.litellm.svc.cluster.local:4318/v1/traces') throw new Error('Unapproved telemetry endpoint');
    const provider = new BasicTracerProvider({
      resource: resourceFromAttributes({ 'service.name': `llm-${plane}-proxy` }),
      spanProcessors: [new BatchSpanProcessor(new OTLPTraceExporter({ url: config.telemetry.endpoint }))],
    });
    services.telemetry = provider.getTracer('llm-gateway');
    services.shutdownTelemetry = () => provider.shutdown();
  }
  return services;
}