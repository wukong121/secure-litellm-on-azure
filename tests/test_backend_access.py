import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

from LiteLLM.runtime.application import load_backend_keys
from scripts.backend_access import render_backend_access
from scripts.customer_migration import ROOT, parameters_for
from scripts.runtime_secrets import BACKEND_SECRETS
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