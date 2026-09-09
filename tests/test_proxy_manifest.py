import copy
import json
import unittest
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import yaml

from scripts.admin_credentials import OIDC_SECRET, SESSION_SECRET
from scripts.migration_deploy import group_id
from scripts.proxy_credentials import binding_contract
from scripts.proxy_manifest import prepare_proxy_documents, render_proxy_documents
from scripts.backend_manifest import render_backend_manifest
from scripts.migration_runtime import check_application, publish
from scripts.customer_migration import ROOT, stage_fingerprint
from scripts.runtime_secrets import BACKEND_SECRETS
from tests.test_proxy_config import APPS, proxy_customer


class ProxyManifestTests(unittest.TestCase):
    def test_preparation_requires_access_receipt_for_same_applications(self):
        config = proxy_customer()
        config["privateIngress"] = {}
        config["proxy"]["image"] = "customerregistry.azurecr.io/auth-proxy@sha256:" + "a" * 64
        revision = "a" * 40
        common = {"revision": revision, "configSha256": stage_fingerprint(config, 7)}
        receipts = {
            "entra-apps": {**common, "applications": APPS},
            "entra-access": {**common, "applications": APPS, "directoryVerified": True, "loginVerified": False},
            "admin-credentials": common,
            "proxy-credentials": common,
        }

        def cloud(arguments):
            name = arguments[arguments.index("--name") + 1]
            if name.endswith("proxy-foundation"):
                return {"state": "Succeeded", "foundation": {}}
            key = next(key for key in receipts if name.endswith(key))
            return {"state": "Succeeded", "receipt": receipts[key]}

        azure = Mock()
        azure.scoped.side_effect = cloud
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as directory, patch("scripts.proxy_manifest.verify_runtime_image") as image, patch("scripts.proxy_manifest.render_proxy_documents", return_value=[]) as render:
            path = Path(directory)
            prepare_proxy_documents(config, revision, path, azure)
            image.assert_called_once()
            render.assert_called_once()
            receipts["entra-access"] = {**receipts["entra-access"], "applications": {}}
            with self.assertRaisesRegex(ValueError, "Access grant receipt"):
                prepare_proxy_documents(config, revision, path, azure)
            receipts["entra-access"] = {**common, "applications": APPS, "directoryVerified": False}
            with self.assertRaisesRegex(ValueError, "Access grant receipt"):
                prepare_proxy_documents(config, revision, path, azure)
            self.assertEqual(image.call_count, 1)

    def test_manifests_isolate_plane_secrets_and_have_no_placeholder(self):
        config = proxy_customer()
        config["proxy"]["image"] = "customerregistry.azurecr.io/auth-proxy@sha256:" + "a" * 64
        foundation = {plane: {"identity": {"clientId": APPS[plane]["appId"], "serviceAccountName": f"llm-{plane}-proxy", "kubernetesNamespace": "litellm"}, "vault": {"name": "synthetic-" + plane, "id": group_id(config) + "/providers/Microsoft.KeyVault/vaults/synthetic-" + plane}} for plane in ("api", "admin")}

        def secret(plane, name):
            return {"id": f"https://synthetic-{plane}.vault.azure.net/secrets/{name}/" + "a" * 32, "version": "a" * 32, "expiresAt": "2099-01-01T00:00:00+00:00" if name == OIDC_SECRET else None}

        applications = {plane: {"id": value["appId"], **value} for plane, value in APPS.items()}
        admin = {"application": applications["admin"], "vaultId": foundation["admin"]["vault"]["id"], "secrets": {name: secret("admin", name) for name in (OIDC_SECRET, SESSION_SECRET)}}
        credentials = {"bindings": []}
        for binding in config["proxy"]["bindings"]:
            contract = binding_contract(config, binding)
            credentials["bindings"].append({"contract": contract, "secret": {**secret(binding["plane"], contract["secretName"]), "state": "ready"}, "userExists": True, "keyExists": True})
        documents = render_proxy_documents(config, foundation, applications, admin, credentials)
        rotated = copy.deepcopy(admin)
        rotated["secrets"][OIDC_SECRET].update(version="b" * 32, id=f"https://synthetic-admin.vault.azure.net/secrets/{OIDC_SECRET}/" + "b" * 32)
        replacement = render_proxy_documents(config, foundation, applications, rotated, credentials)
        before_pods = {item["metadata"]["name"]: item["spec"]["template"] for item in documents if item["kind"] == "Deployment"}
        after_pods = {item["metadata"]["name"]: item["spec"]["template"] for item in replacement if item["kind"] == "Deployment"}
        self.assertEqual(before_pods["llm-api-proxy"], after_pods["llm-api-proxy"])
        self.assertNotEqual(before_pods["llm-admin-proxy"]["metadata"]["annotations"], after_pods["llm-admin-proxy"]["metadata"]["annotations"])
        rotated_session = copy.deepcopy(admin)
        rotated_session["secrets"][SESSION_SECRET].update(version="c" * 32, id=f"https://synthetic-admin.vault.azure.net/secrets/{SESSION_SECRET}/" + "c" * 32)
        session_documents = render_proxy_documents(config, foundation, applications, rotated_session, credentials)
        session_pods = {item["metadata"]["name"]: item["spec"]["template"] for item in session_documents if item["kind"] == "Deployment"}
        self.assertEqual(before_pods["llm-api-proxy"], session_pods["llm-api-proxy"])
        self.assertNotEqual(before_pods["llm-admin-proxy"], session_pods["llm-admin-proxy"])
        backend = {"keyVaultName": "synthetic-backend", "workloadIdentityClientId": "55555555-5555-4555-8555-555555555555", "workloadIdentityPrincipalId": "66666666-6666-4666-8666-666666666666", "managedRedisHostName": "synthetic.westus.redis.azure.net"}
        backend_versions = {name: {"id": f"https://synthetic-backend.vault.azure.net/secrets/{name}/" + "a" * 32, "version": "a" * 32} for name in BACKEND_SECRETS}
        backend_documents = render_backend_manifest(config, backend, backend_versions, "synthetic.postgres.database.azure.com", "10.30.8.0/24")
        combined = backend_documents + documents
        check_application(combined, 7, config)
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as directory, patch.dict("os.environ", {}, clear=True), patch("scripts.migration_runtime.connect_cluster", return_value=["kubectl"]), patch("scripts.backend_manifest.prepare_backend_documents", return_value=backend_documents), patch("scripts.proxy_manifest.prepare_proxy_documents", return_value=documents) as generated, patch("scripts.migration_runtime.run_command", side_effect=lambda arguments, path, label: "{}" if label == "server-dry-run" else "") as command:
            path = Path(directory)
            publish(config, 7, "application", "plan", "a" * 40, path, "")
            generated.assert_called_once()
            self.assertTrue(all("--dry-run=server" in call.args[0] for call in command.call_args_list if "apply" in call.args[0]))
            digest = json.loads((path / "runtime-summary.json").read_text())["planSha256"]
            publish(config, 7, "application", "execute", "a" * 40, path, digest)
            self.assertTrue(json.loads((path / "runtime-summary.json").read_text())["applied"])
            self.assertEqual(len([call for call in command.call_args_list if "rollout" in call.args[0]]), 3)
            with self.assertRaisesRegex(ValueError, "Stage8 remains blocked"):
                publish(config, 8, "application", "plan", "a" * 40, path, "")
            from scripts.audit_manifest import render_audit_documents
            from tests.test_audit_manifest import fixture
            _, _, audit_foundation, storage = fixture()
            config["auditRuntime"] = {"retentionDays": 7, "captureEnabled": True, "retentionEnabled": False, "deliveryPolicyAccepted": True}
            audited = render_audit_documents(config, combined, audit_foundation, storage, "10.30.8.0/24")
            check_application(audited, 8, config)
            providers_before = [item for item in combined if item["kind"] == "SecretProviderClass"]
            providers_after = [item for item in audited if item["kind"] == "SecretProviderClass"]
            self.assertEqual(providers_before, providers_after)
            with patch("scripts.audit_manifest.prepare_audit_documents", return_value=audited) as audit_render:
                command.reset_mock()
                publish(config, 8, "application", "plan", "a" * 40, path, "")
                self.assertTrue(all("--dry-run=server" in call.args[0] for call in command.call_args_list if "apply" in call.args[0]))
                digest = json.loads((path / "runtime-summary.json").read_text())["planSha256"]
                publish(config, 8, "application", "execute", "a" * 40, path, digest)
                self.assertEqual(audit_render.call_count, 2)
                summary = json.loads((path / "runtime-summary.json").read_text())
                self.assertTrue(summary["applied"])
                self.assertFalse(summary["stageAccepted"])
                self.assertEqual(len([call for call in command.call_args_list if "rollout" in call.args[0]]), 3)
        self.assertNotIn("REPLACE", json.dumps(documents))
        self.assertNotIn("litellm-master-key", json.dumps(documents))
        self.assertNotIn("secretObjects", json.dumps(documents))
        self.assertNotIn("Ingress", [item["kind"] for item in documents])
        providers = [item for item in documents if item["kind"] == "SecretProviderClass"]
        api_objects = providers[0]["spec"]["parameters"]["objects"]
        self.assertNotIn("oidc-client-secret", api_objects)
        self.assertNotIn("session-key", api_objects)
        for provider in providers:
            for entry in yaml.safe_load(provider["spec"]["parameters"]["objects"])["array"]:
                self.assertEqual(yaml.safe_load(entry)["objectVersion"], "a" * 32)
        invalid = copy.deepcopy(credentials)
        invalid["bindings"][0]["secret"]["id"] = secret("admin", invalid["bindings"][0]["contract"]["secretName"])["id"]
        with self.assertRaisesRegex(ValueError, "crosses its approved plane"):
            render_proxy_documents(config, foundation, applications, admin, invalid)
        expired = copy.deepcopy(admin)
        expired["secrets"][OIDC_SECRET]["expiresAt"] = "2000-01-01T00:00:00+00:00"
        with self.assertRaisesRegex(ValueError, "Rotate the admin"):
            render_proxy_documents(config, foundation, applications, expired, credentials)