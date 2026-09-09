"""Render backend Workload Identity and CSI resources from verified deployment outputs."""

import re
from uuid import UUID

import yaml

from scripts.customer_migration import require
from scripts.runtime_secrets import BACKEND_SECRETS


def render_backend_access(config, platform, versions):
    client_id = platform["workloadIdentityClientId"]
    require(UUID(client_id).int != 0 and UUID(platform["workloadIdentityPrincipalId"]).int != 0, "Backend Workload Identity client and principal IDs are required")
    vault = platform["keyVaultName"]
    require(re.fullmatch(r"[a-zA-Z][a-zA-Z0-9-]{1,22}[a-zA-Z0-9]", vault) is not None, "Invalid backend Vault name")
    require(set(versions) == set(BACKEND_SECRETS), "Both initialized backend secret versions are required")
    objects = []
    for name, alias in BACKEND_SECRETS.items():
        version = versions[name]["version"]
        require(re.fullmatch(r"[0-9a-f]{32}", version) is not None, "Backend CSI must bind a concrete verified secret version")
        require(versions[name]["id"].lower() == f"https://{vault}.vault.azure.net/secrets/{name}/{version}".lower(), "Secret version belongs to a different Vault or name")
        objects.append(yaml.safe_dump({"objectName": name, "objectAlias": alias, "objectType": "secret", "objectVersion": version, "filePermission": "0440"}, sort_keys=False))
    account = {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": "litellm", "namespace": "litellm", "annotations": {"azure.workload.identity/client-id": client_id}}, "automountServiceAccountToken": False}
    provider = {"apiVersion": "secrets-store.csi.x-k8s.io/v1", "kind": "SecretProviderClass", "metadata": {"name": "litellm-key-vault", "namespace": "litellm"}, "spec": {"provider": "azure", "parameters": {"usePodIdentity": "false", "clientID": client_id, "keyvaultName": vault, "tenantId": config["azure"]["tenantId"], "objects": yaml.safe_dump({"array": objects}, sort_keys=False)}}}
    patch = {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "litellm", "namespace": "litellm"}, "spec": {"template": {"metadata": {"labels": {"azure.workload.identity/use": "true"}}, "spec": {"serviceAccountName": "litellm", "automountServiceAccountToken": False, "containers": [{"name": "litellm", "env": [{"name": name, "$patch": "delete"} for name in ("LITELLM_MASTER_KEY", "LITELLM_SALT_KEY", "DATABASE_URL", "AZURE_CLIENT_ID")] + [{"name": "LLMGW_BACKEND_SECRETS_DIR", "value": "/mnt/backend-secrets"}], "volumeMounts": [{"name": "key-vault-secrets", "mountPath": "/mnt/backend-secrets", "readOnly": True}]}], "volumes": [{"name": "key-vault-secrets", "csi": {"driver": "secrets-store.csi.k8s.io", "readOnly": True, "volumeAttributes": {"secretProviderClass": "litellm-key-vault"}}}]}}}}
    return {"resources": [account, provider], "deploymentPatch": patch}