import unittest
import os
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scripts.database_roles import create_roles, database_access, grant_roles, isolated_postgres_environment, provision_database_roles, role_contract
from scripts.customer_migration import ROOT, stage_fingerprint, validate_config
from scripts.migration_deploy import group_id
from tests.test_customer_migration import customer_config


class DatabaseRoleTests(unittest.TestCase):
    def test_inherited_postgres_destinations_are_temporarily_removed(self):
        with patch.dict(os.environ, {"PGHOSTADDR": "unexpected", "PGSERVICE": "other", "PGPASSWORD": "synthetic", "UNRELATED": "retained"}, clear=True):
            with isolated_postgres_environment():
                self.assertFalse(any(key.startswith("PG") for key in os.environ))
                self.assertEqual(os.environ["UNRELATED"], "retained")
            self.assertEqual(os.environ["PGHOSTADDR"], "unexpected")

    def setUp(self):
        self.config = customer_config()
        self.config["databaseAccess"] = {"migrationPrincipalId": "33333333-3333-4333-8333-333333333333"}
        self.application = {"principalId": "44444444-4444-4444-8444-444444444444"}

    def test_roles_are_distinct_and_never_accept_admin_flags(self):
        roles = role_contract(self.config, self.application)
        self.assertEqual([role["name"] for role in roles], ["llmgw_migrator", "llmgw_app"])
        self.assertTrue(all(role["kind"] == "service" for role in roles))
        with self.assertRaises(ValueError):
            role_contract(self.config, {"principalId": self.config["databaseAccess"]["migrationPrincipalId"]})
        self.config["databaseAccess"]["admin"] = True
        with self.assertRaises(ValueError):
            database_access(self.config)

    def test_creation_uses_bound_parameters_and_separate_ddl_dml_grants(self):
        roles = role_contract(self.config, self.application)
        connection = Mock()
        cursor = Mock()
        cursor.fetchone.return_value = {"migration_table": None}
        connection.cursor.return_value.__enter__ = Mock(return_value=cursor)
        connection.cursor.return_value.__exit__ = Mock(return_value=False)
        create_roles(connection, roles, [{"existing": None}, {"existing": None}])
        grant_roles(connection, roles, "litellm")
        statements = [(call.args[0] if isinstance(call.args[0], str) else call.args[0].as_string()) for call in cursor.execute.call_args_list]
        self.assertIn("false, false", statements[0])
        self.assertNotIn(roles[0]["objectId"], statements[0])
        self.assertIn('GRANT CONNECT, CREATE ON DATABASE "litellm" TO "llmgw_migrator"', statements)
        self.assertIn('GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO "llmgw_app"', statements)
        self.assertFalse(any("PASSWORD" in statement or "SUPERUSER" in statement or "DROP" in statement for statement in statements))

    def test_database_identity_config_only_invalidates_stage5_onwards(self):
        config = customer_config()
        before = {stage: stage_fingerprint(config, stage) for stage in (4, 5)}
        config["databaseAccess"] = self.config["databaseAccess"]
        validate_config(config, "test")
        self.assertEqual(before[4], stage_fingerprint(config, 4))
        self.assertNotEqual(before[5], stage_fingerprint(config, 5))

    def test_plan_and_execution_use_management_and_target_databases(self):
        self.config["parameters"]["platform"]["stage5Data"] = {"postgresqlDatabaseName": "litellm", "postgresqlEntraAdministratorPrincipalName": "approved-admin", "postgresqlEntraAdministratorObjectId": "55555555-5555-4555-8555-555555555555"}
        azure = Mock()
        azure.run.return_value = {"tenantId": self.config["azure"]["tenantId"], "id": self.config["azure"]["subscriptionId"]}

        def cloud(arguments):
            if arguments[:3] == ["deployment", "group", "show"]:
                return {"state": "Succeeded", "platform": {"stage5Deployed": True, "postgresqlServerName": "target-server", "workloadIdentityName": "app-identity"}}
            if arguments[0] == "postgres":
                return {"id": group_id(self.config) + "/providers/Microsoft.DBforPostgreSQL/flexibleServers/target-server", "host": "target.postgres.database.azure.com", "auth": {"activeDirectoryAuth": "Enabled", "passwordAuth": "Disabled"}, "network": {"publicNetworkAccess": "Disabled"}}
            return {**self.application, "id": group_id(self.config) + "/providers/Microsoft.ManagedIdentity/userAssignedIdentities/app-identity", "clientId": "synthetic"}

        azure.scoped.side_effect = cloud
        from contextlib import ExitStack
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as directory, ExitStack() as stack:
            path = Path(directory)
            stack.enter_context(patch("scripts.database_roles.AzureCommands", return_value=azure))
            stack.enter_context(patch("scripts.database_roles.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout="synthetic-token")))
            connect = stack.enter_context(patch("scripts.database_roles.psycopg.connect"))
            stack.enter_context(patch("scripts.database_roles.inspect_roles", return_value=[{"existing": None}, {"existing": None}]))
            stack.enter_context(patch("scripts.database_roles.inspect_schema", return_value={"schema": {"owner": "pg_database_owner"}, "owners": []}))
            create = stack.enter_context(patch("scripts.database_roles.create_roles"))
            grant = stack.enter_context(patch("scripts.database_roles.grant_roles"))
            verify = stack.enter_context(patch("scripts.database_roles.verify_grants"))
            plan = provision_database_roles(self.config, "plan", "a" * 40, path, "")
            self.assertEqual([call.kwargs["dbname"] for call in connect.call_args_list], ["postgres", "litellm"])
            self.assertTrue(all(call.kwargs["sslmode"] == "verify-full" for call in connect.call_args_list))
            create.assert_not_called()
            grant.assert_not_called()
            self.assertNotIn("synthetic-token", (path / "runtime-review.json").read_text())
            with self.assertRaisesRegex(ValueError, "plan changed"):
                provision_database_roles(self.config, "execute", "a" * 40, path, "f" * 64)
            create.assert_not_called()
            result = provision_database_roles(self.config, "execute", "a" * 40, path, plan["planSha256"])
            create.assert_called_once()
            grant.assert_called_once()
            verify.assert_called_once()
            self.assertTrue(result["applied"])
            self.assertFalse(result["stageAccepted"])
            self.assertFalse(json.loads((path / "runtime-summary.json").read_text())["stageAccepted"])