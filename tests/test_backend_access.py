import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

from LiteLLM.runtime.application import configure_gateway_authentication, load_backend_keys
from scripts.backend_access import render_backend_access
from scripts.customer_migration import ROOT, parameters_for
from scripts.runtime_secrets import BACKEND_SECRETS, NATIVE_UI_SECRET, backend_secrets
from tests.test_customer_migration import customer_config


class BackendAccessTests(unittest.TestCase):
    def test_proxy_foundation_uses_bounded_names_and_approved_identity(self):
        template = (ROOT / "infra/proxy-foundation/main.bicep").read_text()
        self.assertIn("name: 'kv-p-${plane}-${uniqueString(resourceGroup().id, environmentName)}'", template)
        for plane in ("api", "admin"):
            self.assertLessEqual(len("kv-p-" + plane + "-" + "a" * 13), 24)
        config = customer_config()
        config["databaseAccess"] = {"migrationPrincipalId": "33333333-3333-4333-8333-333333333333"}
        config["parameters"]["platform"]["stage4Network"]["privateEndpointSubnetName"] = "snet-private-endpoints"
        _path, parameters = parameters_for(config, 7, "proxy-foundation")
        self.assertEqual(parameters["parameters"]["bootstrapPrincipalId"]["value"], config["databaseAccess"]["migrationPrincipalId"])
        self.assertNotIn("clientSecret", parameters["parameters"])
        with self.assertRaises(ValueError):
            parameters_for(config, 6, "proxy-foundation")

    def test_csi_uses_output_identity_and_versions_without_kubernetes_secret_sync(self):
        platform = {"workloadIdentityClientId": "33333333-3333-4333-8333-333333333333", "workloadIdentityPrincipalId": "44444444-4444-4444-8444-444444444444", "keyVaultName": "synthetic-backend"}
        versions = {name: {"version": "a" * 32, "id": f"https://synthetic-backend.vault.azure.net/secrets/{name}/" + "a" * 32} for name in BACKEND_SECRETS}
        result = render_backend_access(customer_config(), platform, versions)
        account, provider = result["resources"]
        self.assertEqual(account["metadata"]["annotations"]["azure.workload.identity/client-id"], platform["workloadIdentityClientId"])
        self.assertNotIn("secretObjects", provider["spec"])
        objects = [yaml.safe_load(item) for item in yaml.safe_load(provider["spec"]["parameters"]["objects"])["array"]]
        self.assertEqual({item["objectAlias"] for item in objects}, set(BACKEND_SECRETS.values()))
        self.assertTrue(all(item["objectVersion"] == "a" * 32 for item in objects))
        self.assertTrue(all(item["filePermission"] == "0444" for item in objects))
        invalid = copy.deepcopy(versions)
        invalid["litellm-master-key"]["id"] = "https://other.vault.azure.net/secrets/key/version"
        with self.assertRaises(ValueError):
            render_backend_access(customer_config(), platform, invalid)

    def test_application_reads_mounts_atomically_and_rejects_environment_conflicts(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            root = Path(directory)
            (root / "LITELLM_MASTER_KEY").write_bytes(b"synthetic-master\r\n")
            (root / "LITELLM_SALT_KEY").write_text("synthetic-salt")
            load_backend_keys(root)
            self.assertEqual(os.environ["LITELLM_MASTER_KEY"], "synthetic-master\r\n")
            os.environ["LITELLM_MASTER_KEY"] = "conflicting-value"
            with self.assertRaises(ValueError):
                load_backend_keys(root)
            os.environ.clear()
            (root / "LITELLM_SALT_KEY").unlink()
            with self.assertRaises(OSError):
                load_backend_keys(root)
            self.assertNotIn("LITELLM_MASTER_KEY", os.environ)

    def test_native_login_uses_mounted_master_key_without_a_manifest_password(self):
        values = {"LITELLM_MASTER_KEY": "synthetic-master", "LITELLM_SALT_KEY": "synthetic-salt", "UI_PASSWORD": "synthetic-ui-password"}
        with patch.dict(os.environ, {"LLMGW_GATEWAY_AUTH_MODE": "native", "LLMGW_NATIVE_ADMIN_USERNAME": "gateway-admin"}, clear=True):
            configure_gateway_authentication(values)
            self.assertEqual(os.environ["UI_USERNAME"], "gateway-admin")
            self.assertEqual(os.environ["UI_PASSWORD"], "synthetic-ui-password")
        for environment in (
            {"LLMGW_GATEWAY_AUTH_MODE": "native", "LLMGW_NATIVE_ADMIN_USERNAME": "bad name"},
            {"LLMGW_GATEWAY_AUTH_MODE": "native", "LLMGW_NATIVE_ADMIN_USERNAME": "gateway-admin", "UI_PASSWORD": "other"},
            {"LLMGW_GATEWAY_AUTH_MODE": "entra", "LLMGW_NATIVE_ADMIN_USERNAME": "gateway-admin"},
            {"LLMGW_GATEWAY_AUTH_MODE": "entra", "UI_PASSWORD": "hidden-password"},
        ):
            with self.subTest(environment=environment), patch.dict(os.environ, environment, clear=True), self.assertRaises(ValueError):
                configure_gateway_authentication(values)

    def test_native_csi_adds_dedicated_ui_password_without_changing_entra(self):
        platform = {"workloadIdentityClientId": "33333333-3333-4333-8333-333333333333", "workloadIdentityPrincipalId": "44444444-4444-4444-8444-444444444444", "keyVaultName": "synthetic-backend"}
        native = customer_config()
        native["application"] = {"authentication": {"mode": "native", "adminUsername": "gateway-admin"}}
        required = backend_secrets(native)
        versions = {name: {"version": "a" * 32, "id": f"https://synthetic-backend.vault.azure.net/secrets/{name}/" + "a" * 32} for name in required}
        result = render_backend_access(native, platform, versions)
        provider = result["resources"][1]
        objects = [yaml.safe_load(item) for item in yaml.safe_load(provider["spec"]["parameters"]["objects"])["array"]]
        self.assertEqual({item["objectAlias"] for item in objects}, set(BACKEND_SECRETS.values()) | set(NATIVE_UI_SECRET.values()))