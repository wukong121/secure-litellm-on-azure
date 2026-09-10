# LiteLLM on Azure Validation

[中文](README_ZH.md) | [Project overview](../README.md) | [Customer migration guide](../docs/customer-migration-guide-zh.md)

Tests cover customer configuration, migration gates, Azure/Kubernetes template contracts, the authentication/audit proxy and LiteLLM runtime behavior. Offline validation, isolated runtime probes and live customer acceptance are separate activities.

## Offline Checks

Run from the repository root with Python dependencies from [requirements.txt](../requirements.txt), Node.js 24 dependencies installed in [auth-proxy](../auth-proxy/package.json), Azure CLI/Bicep and kubectl available:

```bash
make validate-stage9
.venv/bin/python -m unittest tests.test_customer_migration tests.test_customer_templates tests.test_public_config
```

- Stage9 includes earlier-stage checks: Bicep compilation, Kustomize rendering, security policies, Node tests and synthetic L3 lifecycle tests. It does not deploy resources.
- Customer tests verify configuration/evidence binding, target isolation, workflow constraints and generated parameters against compiled Bicep.
- Public configuration checks reject non-example identities and obvious credentials without reading ignored customer files.
- Legacy deployment tests mock cloud/Kubernetes operations and verify subscription selection, configuration compatibility, probes, PVC and Salt handling.

## Isolated OSS Callback Probes

```bash
make validate-oss-callbacks
```

Requires Docker and the pinned LiteLLM image. The probes run SDK and actual Proxy scenarios against a synthetic loopback upstream with no external container network. They do not install LiteLLM into the host environment. Image download may need network access if not cached. See the [callback evidence](../docs/litellm-oss-callback-validation-2026-09-07.md) for coverage and limitations.

## Live Legacy Gateway Tests

[test_all_deployments.py](test_all_deployments.py) sends real model requests to an explicitly selected legacy gateway. It is a standard-library CLI, not an Entra sign-in client or acceptance suite for the new security front door. Approved HTTPS access, customer model configuration and a restricted Virtual Key are required; requests may incur model charges.

Inject `API_KEY` into the process through the customer secret manager, then run from the repository root:

```bash
python tests/test_all_deployments.py \
  --config LiteLLM/azure-openai.loc.json \
  --base-url "https://<approved-legacy-gateway-host>" \
  --prompt "synthetic validation"
```

Do not pass a Master Key on the command line, use customer prompts, or upload raw results to public logs. The CLI sends both Bearer and `api-key` headers for legacy routes. It tests Chat and image generation via OpenAI-style/Azure-style paths; Sora coverage only checks the model registry, not video generation.

[test_codex_cache_affinity.py](test_codex_cache_affinity.py) uses real Codex sessions and can inspect Spend Logs through kubectl. See the [Chinese test guide](README_ZH.md) for controlled baseline/affinity comparisons, requirements and cost considerations. Retain customer results outside Git.

## Secure Gateway Acceptance

The new proxy accepts a restricted set of Entra-authorized routes. Legacy tests for Azure-style paths, image/video, WebSocket or encrypted multi-turn references must not be treated as proof of secure gateway support, nor used as a reason to bypass policy. Phase one now selects native Spend Logs: validate actual retained content, actor/Key correlation, access control, cleanup, capacity, backups and write failures. Custom L3 recovery/governance tests apply to the optional enhanced branch; they are not native-logging acceptance evidence. Runtime defaults and stage gates still need native-mode adaptation. Real tenant authorization, required Codex protocols, private DNS/identity, database migration and rollback remain required; do not skip the current [customer stage gates](../docs/customer-migration-guide-zh.md).
