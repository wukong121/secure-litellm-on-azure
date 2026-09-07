import { Denied } from './policy.mjs';

export class ContentSafetyGuardrail {
  constructor(config, credential, { fetcher = fetch, signal = () => {} } = {}) {
    if (!/^https:\/\/[a-z0-9-]+\.cognitiveservices\.azure\.com\/?$/.test(config.endpoint) || !['observe', 'block'].includes(config.mode)) throw new Error('Invalid Content Safety configuration');
    if (!Number.isInteger(config.threshold) || config.threshold < 0 || config.threshold > 6) throw new Error('Invalid content severity threshold');
    Object.assign(this, { config, credential, fetcher, signal });
  }
  async inspect(body, { traceId }) {
    try {
      const text = JSON.stringify({ messages: body.messages, input: body.input, instructions: body.instructions, tools: body.tools });
      if ([...text].length > 10000) throw new Error('Content Safety input too large');
      const token = await this.credential.getToken('https://cognitiveservices.azure.com/.default');
      const response = await this.fetcher(`${this.config.endpoint.replace(/\/$/, '')}/contentsafety/text:analyze?api-version=2024-09-01`, {
        method: 'POST', redirect: 'error', signal: AbortSignal.timeout(5000),
        headers: { authorization: `Bearer ${token.token}`, 'content-type': 'application/json' },
        body: JSON.stringify({ text, outputType: 'FourSeverityLevels' }),
      });
      if (!response.ok) throw new Error('Content Safety unavailable');
      const result = await response.json();
      const categories = ['Hate', 'SelfHarm', 'Sexual', 'Violence'];
      if (!Array.isArray(result.categoriesAnalysis) || !categories.every(category => result.categoriesAnalysis.some(item => item.category === category && [0, 2, 4, 6].includes(item.severity)))) throw new Error('Incomplete Content Safety result');
      const flagged = result.categoriesAnalysis.some(item => item.severity >= this.config.threshold);
      this.signal({ event: flagged ? 'guardrail_flagged' : 'guardrail_passed', traceId, mode: this.config.mode });
      if (flagged && this.config.mode === 'block') throw new Denied(422);
    } catch (error) {
      if (error instanceof Denied) throw error;
      this.signal({ event: 'guardrail_unavailable', traceId, mode: this.config.mode });
      if (this.config.mode === 'block') throw new Denied(503);
    }
  }
}