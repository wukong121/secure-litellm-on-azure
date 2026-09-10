# LiteLLM Legacy Gateway Reference and OSS Integration

This directory preserves the original LiteLLM on AKS deployment and operational runbooks as a migration baseline. It also contains the opt-in [OSS audit callback adapter](observability/oss_audit_callback.py), which is not yet wired into deployment or a durable audit receiver.

For the current customer solution, start with the [project overview](../README.md) and [staged migration guide](../docs/customer-migration-guide-zh.md). The target platform uses [Bicep](../infra/README_ZH.md), [Kustomize](../deploy/README_ZH.md) and customer Environment configuration. The legacy script below is not an in-place security upgrade command; retain a verified backup and rollback path. Its public ingress, single in-cluster database and direct UI instructions do not describe the new security baseline.

## Structure

- `deploy_mi_aks_litellm.py`: Deploys AKS, Managed Identity, PostgreSQL, and the LiteLLM Proxy.
- `azure-openai.json`: Commit-safe placeholder template only; never add real subscription IDs, resource names, or endpoints.
- `azure-openai.loc.json`: Ignored local deployment configuration. Copy the template here and keep all real Azure values in this file only.
- `USER_BUDGET_AND_MODEL_ACCESS_ZH.md`: Chinese guide for users, teams, virtual keys, budgets, and model access.
- FOUNDRY_MODEL_SYNC_ZH.md: Guide for syncing new Foundry/Azure OpenAI deployments into LiteLLM.
- `litellm.config.yaml`: Generated LiteLLM configuration; do not edit it manually because the deployment script regenerates it.

*Tests and dependencies are located at the project root (`../tests/` and `../requirements.txt`).*

## Legacy Deployment Usage

Use only in an approved legacy maintenance or isolated reference environment. Populate an ignored local copy of [azure-openai.json](azure-openai.json) first; the committed file contains placeholders. Preserve existing credentials before any rerun.

```powershell
# 1. Install dependencies from the project root
cd ..
python -m pip install -r .\requirements.txt

# 2. Sign in and explicitly select the subscription used for AKS
az login
$env:AZURE_SUBSCRIPTION_ID = "<AKS-subscription-id>"

# 3. Optionally override the default LiteLLM 1.95.0 image
$env:LITELLM_IMAGE = "<acr-name>.azurecr.io/litellm:1.95.0"

# 4. Use the reviewed local legacy configuration
cd .\LiteLLM
python .\deploy_mi_aks_litellm.py .\azure-openai.loc.json
```

The default image is `docker.litellm.ai/berriai/litellm:1.95.0`. It has been verified with API-key-authenticated HTTPS (HTTP 200) and a Responses WebSocket upgrade (HTTP 101). When migrating from `micl/litellm:mi-fix-image-gen`, separately regression-test Managed Identity authentication for Azure image generation because the legacy image contained a custom Bearer-token patch for that path.

The generated LiteLLM Deployment includes startup, readiness, and liveness probes plus conservative `250m/1Gi` requests and `1000m/2Gi` limits. The in-cluster PostgreSQL Deployment uses `pg_isready` for all three probes. Updating either Deployment rolls its single current replica, so use a maintenance window and validate one workload at a time.

When the deployment script updates `litellm-env`, it preserves an existing `LITELLM_SALT_KEY` but does not retain arbitrary unmanaged keys. This prevents a future permanent Salt from being silently removed without allowing stale Secret fields to accumulate. It does not make Salt migration safe by itself: existing encrypted database objects must still pass isolated compatibility and Master Key decoupling tests before production sets the Salt.

The script creates or reuses the Resource Group, Managed Identity and AKS cluster named by the configuration. New configurations use `resource_group` for the AKS/identity resource group. Existing `apim_resource_group` values remain readable for backward compatibility only; conflicting old/new values are rejected before deployment. The obsolete `apim_name` field is no longer included in the template or needed by this script.

The default AKS settings are:

```text
Region: supplied by the customer configuration
Nodes: 1
VM size: Standard_D2s_v3
```

Override the VM size when needed:

```powershell
$env:AKS_VM_SIZE = "Standard_B2ms"
```

Each `azure-openai-list[].subscription_id` is used to grant the Managed Identity access to the corresponding Azure OpenAI Resource, including cross-subscription resources.

## User budgets and model access

To add models or edit Router Settings from the Admin UI, enable database-backed configuration before deploying:

```powershell
$env:STORE_MODEL_IN_DB = "true"
python .\deploy_mi_aks_litellm.py
```

The variable must be injected into the AKS pod; setting it only in a local shell does not change an already running proxy. UI-managed models and router settings persist in PostgreSQL. Avoid managing the same deployment from both the UI database and `azure-openai*.json`, which can create duplicate routes or configuration drift.

Open:

```text
http://<AKS LoadBalancer IP>:4000/ui
```

Use `Internal Users`, `Teams`, and `Virtual Keys` to configure individual budgets, shared team budgets, model allowlists, and spend tracking. See [`USER_BUDGET_AND_MODEL_ACCESS_ZH.md`](./USER_BUDGET_AND_MODEL_ACCESS_ZH.md) for the detailed guide.

## PostgreSQL capacity and spend-log retention

This section describes the legacy script's defaults, not the current phase-one deployment procedure. The 2026-09-10 decision is approved native Spend Logs content retention in private PostgreSQL; publishing, query access and stage gates still need adaptation. Do not rerun the legacy script or enable a flag on production to bypass that work. See the [current deployment guide](../docs/customer-deployment-workflows-zh.md).

New in-cluster PostgreSQL deployments request a 20 GiB PVC and retain detailed Spend Logs for 7 days. Prompt and response body storage is disabled by default. The relevant environment variables are:

- `PG_STORAGE` (default `20Gi`)
- `EXPAND_EXISTING_PG_PVC` (default `false`)
- `MAXIMUM_SPEND_LOGS_RETENTION_PERIOD` (default `7d`)
- `MAXIMUM_SPEND_LOGS_RETENTION_INTERVAL` (default `1d`)
- `STORE_PROMPTS_IN_SPEND_LOGS` (default `false`)
- `DISABLE_SPEND_LOGS` (default `false`)

Changing `PG_STORAGE` does not resize an existing claim unless `EXPAND_EXISTING_PG_PVC=true`. Expansion requires a StorageClass with `allowVolumeExpansion` and cannot be reversed. Back up PostgreSQL first, and securely inject the deployment's existing `LITELLM_MASTER_KEY`, `PG_PASSWORD`, and other environment settings before rerunning the deployment script so the storage change does not rotate credentials. For high-volume production deployments, use Azure Database for PostgreSQL Flexible Server with HA, backups, private networking, and storage growth monitoring instead of the single in-cluster PostgreSQL deployment.

## Testing

The [legacy gateway smoke test](../tests/test_all_deployments.py) makes live model requests. Inject a restricted Virtual Key as `API_KEY` through the customer secret manager, use synthetic inputs and an approved endpoint; do not pass a Master Key on the command line:

```powershell
python ..\tests\test_all_deployments.py `
  --config .\azure-openai.loc.json `
  --base-url "https://<approved-legacy-gateway-host>" `
  --prompt ok
```

The test exercises OpenAI-style and Azure-style legacy routes, not the new Entra proxy's authorization contract. See the [test guide](../tests/README.md). On Windows consoles using `cp1252`, pass an ASCII prompt to avoid an encoding error before the first request.

## Notes

- Do not distribute the Admin Master Key; create a Virtual Key for each user.
- The default service uses a public LoadBalancer. Production deployments should add TLS, network restrictions, and a strong random key.
- PostgreSQL is currently a single in-cluster replica for lightweight deployments; production environments should use a highly available database with backups.

## Custom domain and HTTPS

To serve LiteLLM at `https://litellm.your-domain.com` instead of `http://<IP>:4000`, set `LITELLM_HOSTNAME` and the script will automatically configure ingress-nginx + cert-manager (Let's Encrypt):

```powershell
$env:LITELLM_HOSTNAME = "litellm.example.com"   # enables ingress mode
$env:LETSENCRYPT_EMAIL = "you@example.com"       # required for Let's Encrypt
python .\deploy_mi_aks_litellm.py
```

- Prerequisites: Helm and kubectl installed locally.
- The script prints the ingress public IP; create an A record for it in your DNS provider. The certificate is issued automatically after DNS propagates.
- Without `LITELLM_HOSTNAME`, the script keeps the existing `LoadBalancer:4000` behavior.

See [`CUSTOM_DOMAIN_SETUP_ZH.md`](./CUSTOM_DOMAIN_SETUP_ZH.md) for the full runbook (buy domain, DNS, certificate, verification, troubleshooting, rollback).