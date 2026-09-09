import copy
import json
import unittest
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import yaml

from scripts.audit_manifest import audit_settings, prepare_audit_documents, render_audit_documents
from scripts.customer_migration import ROOT, stage_fingerprint
from scripts.migration_deploy import group_id
from tests.test_proxy_config import proxy_customer


def fixture():
    config = proxy_customer()
    config["auditRuntime"] = {"retentionDays": 7, "captureEnabled": True, "retentionEnabled": False, "deliveryPolicyAccepted": True}
    config["proxy"]["image"] = "customerregistry.azurecr.io/proxy@sha256:" + "a" * 64
    foundation = {role: {"clientId": f"{index:08d}-1111-4111-8111-111111111111", "principalId": f"{index:08d}-2222-4222-8222-222222222222", "serviceAccountName": account, "kubernetesNamespace": "litellm"} for index, (role, account) in enumerate((("writer", "llm-api-proxy"), ("reader", "llm-admin-proxy"), ("retention", "l3-retention"), ("recovery", "l3-recovery")), 1)}
    documents = []
    for file in ("workloads.yaml", "admin-workload.yaml"):
        documents.extend(yaml.safe_load_all((ROOT / "deploy/components/stage7-identity" / file).read_text()))
    for document in documents:
        document["metadata"]["namespace"] = "litellm"
        if document["kind"] == "ServiceAccount":
            document["metadata"]["annotations"]["azure.workload.identity/client-id"] = "99999999-9999-4999-8999-999999999999"
    documents.append({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "policy", "namespace": "litellm"}, "data": {"policy.json": json.dumps({"bindings": [{"plane": "api", "audit": {"capture": True, "teamId": "synthetic"}}]})}})
    storage = {"storageUrl": "https://synthetic.blob.core.windows.net", "storageResourceId": "/synthetic/storage"}
    return config, documents, foundation, storage


class AuditManifestTests(unittest.TestCase):
    def test_prepare_checks_actual_identities_storage_policy_and_preserves_registry_ownership(self):
        config, source, foundation, _ = fixture()
        config["parameters"]["audit"] = {"virtualNetworkName": "synthetic-vnet", "privateEndpointSubnetName": "snet-pe"}
        base = group_id(config)
        for role, identity in foundation.items():
            identity.update(name="audit-" + role, id=base + "/providers/Microsoft.ManagedIdentity/userAssignedIdentities/audit-" + role)
        storage_id = base + "/providers/Microsoft.Storage/storageAccounts/synthetic"
        storage_url = "https://synthetic.blob.core.windows.net"
        policy = {"isVersioningEnabled": False, "deleteRetentionPolicy": {"enabled": False}, "containerDeleteRetentionPolicy": {"enabled": False}}
        issuer = "https://synthetic.invalid/issuer"
        def cloud(command):
            if command[:2] == ["deployment", "group"]:
                if command[command.index("--name") + 1].endswith("audit-foundation"):
                    return {"state": "Succeeded", "foundation": foundation}
                return {"state": "Succeeded", "storageId": storage_id, "storageUrl": storage_url, "parameters": {role + "PrincipalId": {"value": identity["principalId"]} for role, identity in foundation.items()}}
            if command[0] == "aks":
                return {"apiServerAccessProfile": {"enablePrivateCluster": True}, "oidcIssuerProfile": {"issuerUrl": issuer}}
            if command[:2] == ["identity", "show"]:
                return next(identity for identity in foundation.values() if identity["id"] == command[command.index("--ids") + 1])
            if command[0] == "identity":
                identity = next(identity for identity in foundation.values() if identity["name"] == command[command.index("--identity-name") + 1])
                return [{"issuer": issuer, "subject": "system:serviceaccount:litellm:" + identity["serviceAccountName"], "audiences": ["api://AzureADTokenExchange"]}]
            if command[0] == "storage":
                return {"publicNetworkAccess": "Disabled", "allowSharedKeyAccess": False, "allowBlobPublicAccess": False, "primaryEndpoints": {"blob": storage_url}}
            if command[0] == "rest":
                return {"properties": policy}
            if command[:2] == ["network", "private-endpoint"]:
                return [{"subnet": {"id": base + "/providers/Microsoft.Network/virtualNetworks/synthetic-vnet/subnets/snet-pe"}, "privateLinkServiceConnections": [{"privateLinkServiceId": storage_id, "groupIds": ["blob"], "privateLinkServiceConnectionState": {"status": "Approved"}}]}]
            return {"addressPrefix": "10.30.8.0/24"}
        azure = Mock()
        azure.scoped.side_effect = cloud
        with tempfile.TemporaryDirectory() as directory, patch("scripts.audit_runtime.AuditCluster") as client:
            registry = {"data": {"approvals.json": "[]", "holds.json": "[]"}}
            client.return_value.get.side_effect = lambda kind, name, **kwargs: registry if name == "audit-approvals" else None
            result = prepare_audit_documents(config, "a" * 40, Path(directory), azure, source, ["kubectl"])
            self.assertNotIn("audit-approvals", [item["metadata"]["name"] for item in result])
            registry = None
            result = prepare_audit_documents(config, "a" * 40, Path(directory), azure, source, ["kubectl"])
            self.assertIn("audit-approvals", [item["metadata"]["name"] for item in result])
            client.return_value.get.side_effect = lambda kind, name, **kwargs: {"spec": {"template": {"spec": {"volumes": [{"name": "stage8", "configMap": {"name": "existing-stage8"}}]}}}} if kind == "deployment" else {"data": {"config.json": json.dumps({"telemetry": {"enabled": True}})}}
            with self.assertRaisesRegex(ValueError, "cannot disable"):
                prepare_audit_documents(config, "a" * 40, Path(directory), azure, source, ["kubectl"])
            policy["isVersioningEnabled"] = True
            with self.assertRaisesRegex(ValueError, "hidden retained"):
                prepare_audit_documents(config, "a" * 40, Path(directory), azure, source, ["kubectl"])

    def test_render_preserves_proxy_identities_and_existing_governance(self):
        config, source, foundation, storage = fixture()
        original = copy.deepcopy(source)
        registry = {"approvals.json": '[{"id":"synthetic-approved"}]', "holds.json": '[{"id":"synthetic-held"}]'}
        result = render_audit_documents(config, source, foundation, storage, "10.30.8.0/24", registry)
        self.assertEqual(source, original)
        self.assertEqual(next(item for item in result if item["metadata"]["name"] == "audit-approvals")["data"], registry)
        for plane, role in (("api", "writer"), ("admin", "reader")):
            account = next(item for item in result if item["kind"] == "ServiceAccount" and item["metadata"]["name"] == f"llm-{plane}-proxy")
            self.assertEqual(account["metadata"]["annotations"]["azure.workload.identity/client-id"], "99999999-9999-4999-8999-999999999999")
            settings = next(json.loads(item["data"]["config.json"]) for item in result if item["kind"] == "ConfigMap" and item["metadata"]["name"].startswith("stage8-" + plane))
            self.assertEqual(settings["l3"]["clientId"], foundation[role]["clientId"])
            self.assertFalse(settings["telemetry"]["enabled"])
        self.assertTrue(next(item for item in result if item["kind"] == "CronJob")["spec"]["suspend"])

    def test_policy_decisions_and_identity_mismatch_fail_closed(self):
        config, source, foundation, storage = fixture()
        config["auditRuntime"]["deliveryPolicyAccepted"] = False
        with self.assertRaisesRegex(ValueError, "Approve"):
            audit_settings(config)
        config["auditRuntime"]["deliveryPolicyAccepted"] = True
        foundation["writer"]["serviceAccountName"] = "llm-admin-proxy"
        with self.assertRaisesRegex(ValueError, "another plane"):
            render_audit_documents(config, source, foundation, storage, "10.30.8.0/24")

    def test_capture_toggle_rolls_api_only_and_retention_uses_own_identity(self):
        config, source, foundation, storage = fixture()
        before = render_audit_documents(config, source, foundation, storage, "10.30.8.0/24")
        config["auditRuntime"]["captureEnabled"] = False
        after = render_audit_documents(config, source, foundation, storage, "10.30.8.0/24")
        mappings = lambda documents: {item["metadata"]["name"]: item["spec"]["template"] for item in documents if item["kind"] == "Deployment"}
        self.assertNotEqual(mappings(before)["llm-api-proxy"], mappings(after)["llm-api-proxy"])
        self.assertEqual(mappings(before)["llm-admin-proxy"], mappings(after)["llm-admin-proxy"])
        cron = next(item for item in after if item["kind"] == "CronJob")
        pod = cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        self.assertEqual(pod["serviceAccountName"], "l3-retention")
        name = next(volume["configMap"]["name"] for volume in pod["volumes"] if volume["name"] == "stage8")
        settings = json.loads(next(item for item in after if item["metadata"]["name"] == name)["data"]["config.json"])
        self.assertNotIn("clientId", settings["l3"])

    def test_audit_decisions_do_not_invalidate_prior_stage_evidence(self):
        config, _, _, _ = fixture()
        baseline = copy.deepcopy(config)
        baseline.pop("auditRuntime")
        for stage in range(8):
            self.assertEqual(stage_fingerprint(config, stage), stage_fingerprint(baseline, stage))
        self.assertNotEqual(stage_fingerprint(config, 8), stage_fingerprint(baseline, 8))