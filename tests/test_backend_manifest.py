import copy
import json
import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import yaml

from scripts.backend_manifest import application_settings, prepare_backend_documents, render_backend_manifest
from scripts.customer_migration import ROOT, stage_fingerprint, validate_config
from scripts.migration_deploy import group_id
from scripts.runtime_secrets import BACKEND_SECRETS
from LiteLLM.runtime.application import application_config
from scripts.migration_runtime import check_application, publish
from tests.test_customer_migration import customer_config


def backend_customer():
    config = customer_config()
    config["parameters"]["platform"]["azureOpenAIConnections"] = [{"alias": "primary", "accountName": "synthetic-model"}]
    config["parameters"]["platform"]["stage5Data"] = {"postgresqlDatabaseName": "litellm"}
    config["application"] = {"backendImage": "customerregistry.azurecr.io/litellm-azure@sha256:" + "a" * 64, "models": [{"modelGroup": "coding", "connectionAlias": "primary", "deploymentName": "gpt-deployment", "id": "primary-coding", "apiVersion": "v1"}]}
    return config


class BackendManifestTests(unittest.TestCase):
    def test_application_config_only_changes_stage6_fingerprint(self):
        config = backend_customer()
        plain = copy.deepcopy(config)
        plain.pop("application")
        validate_config(config, "test")
        self.assertEqual(stage_fingerprint(config, 5), stage_fingerprint(plain, 5))
        self.assertNotEqual(stage_fingerprint(config, 6), stage_fingerprint(plain, 6))

    def test_publisher_uses_generated_documents_and_rejects_manual_override(self):
        config = backend_customer()
        platform = {"keyVaultName": "synthetic-backend", "workloadIdentityClientId": "33333333-3333-4333-8333-333333333333", "workloadIdentityPrincipalId": "44444444-4444-4444-8444-444444444444", "managedRedisHostName": "synthetic.westus.redis.azure.net"}
        versions = {name: {"id": f"https://synthetic-backend.vault.azure.net/secrets/{name}/" + "a" * 32, "version": "a" * 32} for name in BACKEND_SECRETS}
        documents = render_backend_manifest(config, platform, versions, "synthetic.postgres.database.azure.com", "10.30.8.0/24")

        def command(arguments, directory, label, **kwargs):
            return "{}" if label == "server-dry-run" else ""

        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as directory, patch.dict("os.environ", {}, clear=True), patch("scripts.migration_runtime.connect_cluster", return_value=["kubectl"]), patch("scripts.backend_manifest.prepare_backend_documents", return_value=documents) as render, patch("scripts.migration_runtime.run_command", side_effect=command) as execute:
            path = Path(directory)
            publish(config, 6, "application", "plan", "a" * 40, path, "")
            render.assert_called_once()
            digest = json.loads((path / "runtime-summary.json").read_text())["planSha256"]
            self.assertTrue(all("--dry-run=server" in call.args[0] for call in execute.call_args_list if "apply" in call.args[0]))
            publish(config, 6, "application", "execute", "a" * 40, path, digest)
            self.assertTrue(json.loads((path / "runtime-summary.json").read_text())["applied"])
            with patch.dict("os.environ", {"MIGRATION_MANIFEST_YAML": "manual"}), self.assertRaisesRegex(ValueError, "cannot be mixed"):
                publish(config, 6, "application", "plan", "a" * 40, path, "")
            with self.assertRaisesRegex(ValueError, "requires proxy settings for Stage7"):
                publish(config, 7, "application", "plan", "a" * 40, path, "")
    def test_receipts_and_image_signature_are_bound_before_rendering(self):
        config = backend_customer()
        config["parameters"]["platform"]["stage4Network"]["privateEndpointSubnetName"] = "snet-private-endpoints"
        platform = {"stage5Deployed": True, "postgresqlServerName": "target", "keyVaultName": "synthetic-backend", "workloadIdentityClientId": "33333333-3333-4333-8333-333333333333", "workloadIdentityPrincipalId": "44444444-4444-4444-8444-444444444444", "managedRedisHostName": "synthetic.westus.redis.azure.net"}
        revision = "a" * 40
        common = {"revision": revision, "configSha256": stage_fingerprint(config, 5)}
        schema = {**common, "serverId": group_id(config) + "/providers/Microsoft.DBforPostgreSQL/flexibleServers/target", "database": "litellm"}
        versions = {name: {"id": f"https://synthetic-backend.vault.azure.net/secrets/{name}/" + "a" * 32, "version": "a" * 32} for name in BACKEND_SECRETS}
        secrets_receipt = {**common, "vaultId": group_id(config) + "/providers/Microsoft.KeyVault/vaults/synthetic-backend", "secrets": versions}

        def cloud(arguments):
            if arguments[0] == "deployment":
                name = arguments[arguments.index("--name") + 1]
                if name.endswith("platform"):
                    return {"state": "Succeeded", "platform": platform}
                return {"state": "Succeeded", "receipt": schema if name.endswith("schema-migrate") else secrets_receipt}
            if arguments[0] == "postgres":
                return {"host": "synthetic.postgres.database.azure.com", "auth": {"activeDirectoryAuth": "Enabled", "passwordAuth": "Disabled"}, "network": {"publicNetworkAccess": "Disabled"}}
            return "10.30.8.0/24"

        azure = Mock()
        azure.scoped.side_effect = cloud
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as directory, patch.dict("os.environ", {"GITHUB_REPOSITORY": "customer/gateway", "GITHUB_REF": "refs/heads/main"}), patch("scripts.backend_manifest.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=json.dumps({"loginServer": "customerregistry.azurecr.io", "accessToken": "synthetic-acr-token"}))), patch("scripts.migration_runtime.run_command") as command:
            path = Path(directory)
            result = prepare_backend_documents(config, revision, path, azure)
            check_application(result, 6, config)
            arguments = command.call_args.args[0]
            self.assertIn("llmgw.runtime=azure", arguments)
            self.assertIn("llmgw.revision=" + revision, arguments)
            self.assertIn("llmgw.environment=test", arguments)
            self.assertIn("https://github.com/customer/gateway/.github/workflows/promote-litellm-image.yml@refs/heads/main", arguments)
            self.assertNotIn("synthetic-acr-token", str(command.call_args))
            self.assertFalse((path / "backend-acr-auth/config.json").exists())
            schema["revision"] = "b" * 40
            with self.assertRaisesRegex(ValueError, "stale"):
                prepare_backend_documents(config, revision, path, azure)

    def test_rendered_backend_uses_real_outputs_without_secret_sync_or_legacy_refs(self):
        config = backend_customer()
        platform = {"keyVaultName": "synthetic-backend", "workloadIdentityClientId": "33333333-3333-4333-8333-333333333333", "workloadIdentityPrincipalId": "44444444-4444-4444-8444-444444444444", "managedRedisHostName": "synthetic.westus.redis.azure.net"}
        versions = {name: {"id": f"https://synthetic-backend.vault.azure.net/secrets/{name}/" + "a" * 32, "version": "a" * 32} for name in BACKEND_SECRETS}
        documents = render_backend_manifest(config, platform, versions, "synthetic.postgres.database.azure.com", "10.30.8.0/24")
        check_application(documents, 6, config)
        unsafe = copy.deepcopy(documents)
        bad_deployment = next(item for item in unsafe if item["kind"] == "Deployment")
        bad_container = bad_deployment["spec"]["template"]["spec"]["containers"][0]
        next(item for item in bad_container["env"] if item["name"] == "AZURE_DATABASE_URL_TEMPLATE")["value"] = "postgresql://stored-secret"
        with self.assertRaises(ValueError):
            check_application(unsafe, 6, config)
        serialized = json.dumps(documents)
        self.assertNotIn("REPLACE", serialized)
        self.assertNotIn("litellm-runtime-secrets", serialized)
        self.assertNotIn("secretObjects", serialized)
        deployment = next(item for item in documents if item["kind"] == "Deployment")
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        environment = {item["name"]: item["value"] for item in container["env"]}
        self.assertNotIn("DATABASE_URL", environment)
        self.assertEqual(environment["LLMGW_BACKEND_SECRETS_DIR"], "/mnt/backend-secrets")
        self.assertEqual(container["image"], config["application"]["backendImage"])
        runtime = yaml.safe_load(next(item for item in documents if item["kind"] == "ConfigMap")["data"]["config.yaml"])
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as directory:
            config_file = Path(directory) / "generated.yaml"
            config_file.write_text(yaml.safe_dump(runtime))
            application_config(config_file)
        self.assertTrue(runtime["general_settings"]["disable_prisma_schema_update"])
        self.assertTrue(runtime["router_settings"]["cache_kwargs"]["azure_redis_ad_token"])
        self.assertTrue(runtime["router_settings"]["cache_kwargs"]["ssl_check_hostname"])
        self.assertEqual(runtime["model_list"][0]["litellm_params"]["api_base"], "https://synthetic-model.openai.azure.com")
        traffic = next(item for item in documents if item["kind"] == "NetworkPolicy" and item["metadata"]["name"] == "allow-litellm-required-traffic")
        self.assertNotIn("namespaceSelector", json.dumps(traffic["spec"]["ingress"]))
        self.assertEqual(traffic["spec"]["egress"][1]["to"][0]["ipBlock"]["cidr"], "10.30.8.0/24")

    def test_unapproved_images_accounts_and_duplicate_ids_are_rejected(self):
        config = backend_customer()
        for change in (lambda value: value["application"].update(backendImage="other.azurecr.io/litellm:latest"), lambda value: value["application"]["models"][0].update(connectionAlias="other"), lambda value: value["application"]["models"].append(copy.deepcopy(value["application"]["models"][0])), lambda value: value["parameters"]["platform"]["azureOpenAIConnections"].append(copy.deepcopy(value["parameters"]["platform"]["azureOpenAIConnections"][0]))):
            modified = copy.deepcopy(config)
            change(modified)
            with self.assertRaises(ValueError):
                application_settings(modified)