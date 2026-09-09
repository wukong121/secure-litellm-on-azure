# Security-Enhanced LiteLLM on Azure

[中文](README_ZH.md) | [Customer migration guide (Chinese)](docs/customer-migration-guide-zh.md)

A customer deployment and migration project for a security-enhanced [LiteLLM](https://github.com/BerriAI/litellm) gateway on Azure. It combines Azure infrastructure as code, Kubernetes deployment components, customer-owned Microsoft Entra authentication, audit controls and staged delivery workflows.

The delivery target covers both staged migration of existing gateways and greenfield deployment. Customers supply identities, resource IDs, domains and necessary decisions; workflows should perform deployment and verification without requiring customers to author manifests or test code. Model routing uses authorized Azure OpenAI resources within their quotas; the project does not bypass service limits.

> **Delivery status:** migration/greenfield routing, guidance, configuration checks, infrastructure deployment and bounded private runtime operations are available. Automatic application rendering, runtime integration and fully automated customer verification are not complete. This is not a one-click in-place upgrade; unresolved items remain release blockers.

## Target Architecture

```text
API clients -> llm-api.<customer-domain> -> Front Door / WAF -> private API ingress
                                                               -> Entra API proxy
Administrators -> private llm-admin.<customer-domain> -> Entra admin proxy
                                                                  |
                                                       LiteLLM on Private AKS
                                                                  |
                                                      Azure OpenAI / Foundry

Supporting services: Key Vault, PostgreSQL Flexible Server, Managed Redis,
private ACR, Azure Monitor and independent L3 audit storage.
```

| Area | Design and implementation scope |
| --- | --- |
| Network | Private AKS, private endpoints/DNS, controlled egress and default-deny network policies |
| Identity | Entra authentication, separate API/admin policies, workload identities and customer-owned authorization |
| Data and secrets | Key Vault/CSI, managed PostgreSQL and Redis templates, backup and restore controls |
| Runtime | Digest-pinned containers, non-root/read-only baseline, HA and routing components |
| Audit and observation | Trace correlation, L3 capture/query/retention code, OSS callbacks, input Content Safety and detection templates |
| Delivery | Customer Environment configuration, OIDC, staged evidence checks, IaC previews and offline validation |

These are target capabilities, not claims that every component is deployed or production-ready. The solution uses Azure services, OSS and customer-owned code; it has no LiteLLM Enterprise dependency. The former APIM implementation has been removed.

## Start Here

1. Choose migration or greenfield in the [deployment guide](docs/customer-deployment-workflows-zh.md). Existing gateway migration details are in the [migration guide](docs/customer-migration-guide-zh.md).
2. Create protected customer GitHub Environments (`dev`, `test`, `prod`) and environment-scoped Azure OIDC identities.
3. Use the [migration template](config/customer.example.json) or [greenfield template](config/customer.greenfield.example.json) for the `CUSTOMER_CONFIG_JSON` Environment secret. Greenfield uses `deploymentMode=greenfield` and omits `legacy`. Configure deployment/runtime OIDC identities as documented; initialize `MIGRATION_EVIDENCE_JSON` to `[]`.
4. Open **Customer staged migration**, select stage `0`, mode `guide`, component `none`, then proceed to `preflight` and approved `what-if` stages.
5. Migration builds an isolated environment alongside the old gateway. Greenfield starts with Stage 0 `bootstrap` then `network` and skips the inapplicable Stage 1. Deployments and releases require approval; successful infrastructure deployment is not application acceptance.

The original migration workflow remains read-only. New [deployment and acceptance workflows](docs/customer-deployment-workflows-zh.md) provide plan-bound ARM deployment, private-runner backup/restore and manifest publishing, and explicit single-operator or dual approval. They do not automatically change DNS or retire the old environment, and outstanding runtime integration blockers still apply. Runtime credentials belong in customer Key Vault, never in the configuration JSON.

## Migration Stages

| Stage | Customer milestone |
| --- | --- |
| 0-2 | Inventory, recoverable backup, minimum legacy hardening and approved architecture decisions |
| 3-5 | Supply chain, isolated target infrastructure, private networking and data migration rehearsal |
| 6-8 | HA/routing, Entra and split domains, protocol authorization, L3 audit and observation |
| 9 | Approved pilot, cutover and verified rollback window; retirement is a separate change |

Keep the old database, gateway, identity and Master/Salt path until rollback criteria are met. Do not connect the candidate version to the old production database for an automatic schema migration.

## Repository Map

| Path | Purpose |
| --- | --- |
| [config](config/customer.example.json) | Public customer configuration and evidence examples; no real customer values |
| [.github](.github/README_ZH.md) | Validation, staged migration and image promotion workflows |
| [infra](infra/README_ZH.md) | Bicep platform, backup, monitoring, audit and edge templates |
| [deploy](deploy/README_ZH.md) | Kustomize base, staged components and validation overlays |
| [auth-proxy](auth-proxy/README_ZH.md) | Customer-owned Entra API/admin proxy and audit governance code |
| [LiteLLM](LiteLLM/README.md) | Legacy gateway reference, operational runbooks and OSS callback adapter |
| [tests](tests/README.md) | Offline checks, isolated runtime probes and explicitly invoked live tests |
| [scripts](scripts/customer_migration.py) | Customer preflight, parameter rendering and validation tools |

## Local Validation

Prerequisites: Python 3.10+, Node.js 24, Azure CLI with Bicep, kubectl and make. The OSS callback probes additionally require Docker. From the repository root:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
npm ci --prefix auth-proxy --ignore-scripts
make validate-stage9
make validate-oss-callbacks
.venv/bin/python -m unittest tests.test_customer_migration tests.test_customer_templates tests.test_public_config
```

These checks do not deploy resources or call customer gateways. Dependency installation and uncached container images need download access. Live protocol, private-network, Entra and Azure data-plane tests require a separately approved environment; see the [test guide](tests/README.md).

## Readiness and Cost

- Supported routes in the secure front door are deliberately restricted. Legacy image, video, WebSocket and Codex test results do not establish compatibility with the new authorization layer.
- L3 is a required release capability, but the current RAM-buffered capture can lose content on a crash. The OSS adapter is not yet wired to a trusted durable receiver; callback success is not proof of stream completeness or audit delivery.
- Private ingress/controller integration, identity/Vault wiring, PostgreSQL authentication/HA and monitoring integration still require work and customer acceptance. Do not apply placeholder validation overlays to production.

Use the [completion backlog](docs/litellm-code-completion-backlog-2026-09-07.md), [architecture](docs/litellm-azure-security-hardening-zh.md) and [implementation roadmap](docs/litellm-security-hardening-implementation-roadmap-zh.md) for the current boundaries. Historical stage records are reference evidence, not customer acceptance reports.

Estimate costs using the [security-enhanced BOM](docs/litellm-bom-cost-comparison-zh.md) and [Azure Pricing Calculator](https://azure.microsoft.com/en-us/pricing/calculator/). Customer region, HA, Firewall, private endpoints, edge traffic, database size and audit retention determine the bill; legacy single-node estimates are not representative of this architecture.
