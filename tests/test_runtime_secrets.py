import unittest
import json
from pathlib import Path
import tempfile
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scripts.runtime_secrets import BACKEND_SECRETS, apply_backend_secrets, backend_secret_values, ensure_key_creation_is_safe, initialize_backend_secrets, inspect_backend_secrets, read_legacy_keys
import base64
from scripts.customer_migration import ROOT, parameters_for
from scripts.migration_deploy import group_id
from tests.test_customer_migration import customer_config


class RuntimeSecretTests(unittest.TestCase):
    def test_missing_keys_in_initialized_database_are_not_regenerated(self):
        for existing in ({}, {"litellm-master-key": "present"}):
            with self.assertRaisesRegex(ValueError, "recover original keys"):
                ensure_key_creation_is_safe("greenfield", existing, True)
        ensure_key_creation_is_safe("greenfield", {}, False)
        ensure_key_creation_is_safe("greenfield", {name: "present" for name in BACKEND_SECRETS}, True)
        ensure_key_creation_is_safe("migration", {}, True)

    def test_plan_execute_controller_outputs_only_metadata(self):
        config = customer_config()
        config["deploymentMode"] = "greenfield"
        config.pop("legacy")
        config["parameters"].pop("monitoring")
        config["databaseAccess"] = {"migrationPrincipalId": "33333333-3333-4333-8333-333333333333"}
        config["parameters"]["platform"]["stage5Data"] = {"postgresqlDatabaseName": "litellm"}
        azure = Mock()
        azure.run.return_value = {"tenantId": config["azure"]["tenantId"], "id": config["azure"]["subscriptionId"]}

        def cloud(arguments):
            if arguments[:3] == ["deployment", "group", "show"]:
                return {"state": "Succeeded", "platform": {"stage5Deployed": True, "keyVaultName": "synthetic-backend", "postgresqlServerName": "target", "workloadIdentityClientId": "44444444-4444-4444-8444-444444444444", "workloadIdentityPrincipalId": "55555555-5555-4555-8555-555555555555"}}
            if arguments[0] == "keyvault":
                return {"id": group_id(config) + "/providers/Microsoft.KeyVault/vaults/synthetic-backend", "uri": "https://synthetic-backend.vault.azure.net/", "rbac": True, "public": "Disabled", "purge": True}
            if arguments[0] == "postgres":
                return {"host": "synthetic.postgres.database.azure.com", "auth": {"activeDirectoryAuth": "Enabled", "passwordAuth": "Disabled"}, "network": {"publicNetworkAccess": "Disabled"}}
            return {"properties": {"provisioningState": "Succeeded"}}

        azure.scoped.side_effect = cloud
        versions = {name: {"id": "https://synthetic-backend.vault.azure.net/secrets/" + name + "/" + "a" * 32, "version": "a" * 32} for name in BACKEND_SECRETS}
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as directory, ExitStack() as stack:
            stack.enter_context(patch("scripts.runtime_secrets.AzureCommands", return_value=azure))
            stack.enter_context(patch("azure.identity.AzureCliCredential"))
            stack.enter_context(patch("azure.keyvault.secrets.SecretClient"))
            connection = stack.enter_context(patch("psycopg.connect"))
            connection.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value.fetchone.return_value = (False,)
            stack.enter_context(patch("scripts.runtime_secrets.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout="synthetic-db-token")))
            stack.enter_context(patch("scripts.runtime_secrets.inspect_backend_secrets", return_value=({}, {name: None for name in BACKEND_SECRETS})))
            apply = stack.enter_context(patch("scripts.runtime_secrets.apply_backend_secrets", return_value=versions))
            random = stack.enter_context(patch("scripts.runtime_secrets.secrets.token_urlsafe"))
            legacy = stack.enter_context(patch("scripts.runtime_secrets.read_legacy_keys"))
            path = Path(directory)
            plan = initialize_backend_secrets(config, "plan", "a" * 40, path, "")
            apply.assert_not_called()
            random.assert_not_called()
            legacy.assert_not_called()
            with self.assertRaisesRegex(ValueError, "plan changed"):
                initialize_backend_secrets(config, "execute", "a" * 40, path, "f" * 64)
            apply.assert_not_called()
            result = initialize_backend_secrets(config, "execute", "a" * 40, path, plan["planSha256"])
            apply.assert_called_once()
            self.assertTrue(result["initialized"])
            self.assertFalse(result["stageAccepted"])
            self.assertTrue((path / "backend-access.json").exists())
            for artifact in path.glob("*.json"):
                self.assertNotIn("synthetic-db-token", artifact.read_text())

    def test_bootstrap_permission_is_only_emitted_for_stage5(self):
        config = customer_config()
        config["databaseAccess"] = {"migrationPrincipalId": "33333333-3333-4333-8333-333333333333"}
        config["parameters"]["platform"]["stage5Data"] = {"postgresqlDatabaseName": "litellm"}
        for stage in (3, 4):
            _template, parameters = parameters_for(config, stage, "platform")
            self.assertNotIn("bootstrapPrincipalId", parameters["parameters"])
        _template, parameters = parameters_for(config, 5, "platform")
        self.assertEqual(parameters["parameters"]["bootstrapPrincipalId"]["value"], config["databaseAccess"]["migrationPrincipalId"])

    def test_sdk_initialization_reuses_existing_values_after_partial_failure(self):
        from azure.core.exceptions import ResourceNotFoundError

        stored = {}
        writes = []

        class Client:
            def get_secret(self, name):
                if name not in stored:
                    raise ResourceNotFoundError()
                return stored[name]

            def set_secret(self, name, value, **kwargs):
                writes.append(name)
                item = SimpleNamespace(value=value, properties=SimpleNamespace(id="https://synthetic.vault.azure.net/secrets/" + name + "/version", version="version", enabled=True, expires_on=None, tags=kwargs["tags"]))
                stored[name] = item
                return item

        client = Client()
        existing, metadata = inspect_backend_secrets(client, "greenfield")
        versions = apply_backend_secrets(client, "greenfield", existing, metadata, None)
        self.assertEqual(set(versions), set(BACKEND_SECRETS))
        original = {name: item.value for name, item in stored.items()}
        existing, metadata = inspect_backend_secrets(client, "greenfield")
        apply_backend_secrets(client, "greenfield", existing, metadata, None)
        self.assertEqual(len(writes), 2)
        self.assertEqual(original, {name: item.value for name, item in stored.items()})
        stored["litellm-master-key"].properties.tags = {}
        with self.assertRaisesRegex(ValueError, "unmanaged"):
            inspect_backend_secrets(client, "greenfield")

    def test_partial_write_failure_keeps_the_first_key_on_retry(self):
        from azure.core.exceptions import ResourceNotFoundError

        store = {}
        failed = False

        class Client:
            def get_secret(self, name):
                if name not in store:
                    raise ResourceNotFoundError()
                return store[name]

            def set_secret(self, name, value, **kwargs):
                nonlocal failed
                if name == "litellm-salt-key" and not failed:
                    failed = True
                    raise RuntimeError("synthetic interruption")
                item = SimpleNamespace(value=value, properties=SimpleNamespace(id=name, version="v1", enabled=True, expires_on=None, tags=kwargs["tags"]))
                store[name] = item
                return item

        client = Client()
        existing, metadata = inspect_backend_secrets(client, "greenfield")
        with self.assertRaises(RuntimeError):
            apply_backend_secrets(client, "greenfield", existing, metadata, None)
        first = store["litellm-master-key"].value
        existing, metadata = inspect_backend_secrets(client, "greenfield")
        apply_backend_secrets(client, "greenfield", existing, metadata, None)
        self.assertEqual(store["litellm-master-key"].value, first)
        self.assertEqual(set(store), set(BACKEND_SECRETS))

    def test_legacy_secret_is_read_only_and_values_never_written_to_diagnostics(self):
        config = customer_config()
        source = {"metadata": {"name": "litellm-env", "namespace": "litellm", "uid": "source", "resourceVersion": "42"}, "data": {name: base64.b64encode(value.encode()).decode() for name, value in {"LITELLM_MASTER_KEY": "old-master", "LITELLM_SALT_KEY": "old-salt"}.items()}}
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as directory, patch("scripts.migration_runtime.connect_cluster", return_value=["kubectl"]), patch("scripts.runtime_secrets.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=json.dumps(source))) as command:
            values, metadata = read_legacy_keys(config, Path(directory))
            self.assertEqual(values["LITELLM_SALT_KEY"], "old-salt")
            self.assertEqual(metadata["resourceVersion"], "42")
            self.assertEqual(command.call_args.args[0][1:4], ["get", "secret", "litellm-env"])
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_new_keys_are_distinct_and_existing_values_are_never_rotated(self):
        values = backend_secret_values("greenfield", {})
        self.assertEqual(set(values), set(BACKEND_SECRETS))
        self.assertTrue(values["litellm-master-key"].startswith("sk-"))
        self.assertNotEqual(values["litellm-master-key"], values["litellm-salt-key"])
        with patch("scripts.runtime_secrets.secrets.token_urlsafe") as random:
            self.assertEqual(backend_secret_values("greenfield", values), {})
            random.assert_not_called()

    def test_partial_initialization_only_creates_missing_secret(self):
        values = backend_secret_values("greenfield", {"litellm-master-key": "existing-master"})
        self.assertEqual(set(values), {"litellm-salt-key"})

    def test_migration_preserves_bytes_and_rejects_conflicting_target(self):
        legacy = {"LITELLM_MASTER_KEY": "existing-master", "LITELLM_SALT_KEY": "existing-salt"}
        with patch("scripts.runtime_secrets.secrets.token_urlsafe") as random:
            values = backend_secret_values("migration", {}, legacy)
            self.assertEqual(values, {"litellm-master-key": "existing-master", "litellm-salt-key": "existing-salt"})
            self.assertEqual(backend_secret_values("migration", values, legacy), {})
            with self.assertRaisesRegex(ValueError, "overwrite is forbidden"):
                backend_secret_values("migration", {"litellm-master-key": "different"}, legacy)
            random.assert_not_called()

    def test_missing_legacy_salt_or_empty_keys_never_generate_replacements(self):
        for source in (None, {"LITELLM_MASTER_KEY": "existing"}, {"LITELLM_MASTER_KEY": "existing", "LITELLM_SALT_KEY": ""}):
            with patch("scripts.runtime_secrets.secrets.token_urlsafe") as random, self.assertRaises(ValueError):
                backend_secret_values("migration", {}, source)
            random.assert_not_called()