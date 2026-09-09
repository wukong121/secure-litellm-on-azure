import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock
from uuid import uuid5, NAMESPACE_URL

from scripts.install_workflows import install_intent, installation
from tests.test_customer_migration import customer_config


class InstallationTests(unittest.TestCase):
    def test_plan_uses_exact_environment_trust_no_subscription_owner_or_passwords(self):
        config = customer_config()
        intent = install_intent(config, "synthetic/gateway")
        self.assertEqual(len(intent["identities"]), 7)
        self.assertTrue(all(item["trust"]["subject"] == "repo:synthetic/gateway:environment:test" for item in intent["identities"].values()))
        self.assertNotIn("8e3af657-a8ff-443c-a75c-2fe8c4bcb635", str(intent))
        azure = Mock()
        azure.run.return_value = {"tenantId": config["azure"]["tenantId"], "id": config["azure"]["subscriptionId"]}
        azure.scoped.return_value = []
        def read_github(arguments, payload=None):
            if "/branches/" in arguments[-1]: return {"protected": True}
            if "/environments?" in arguments[-1]: return {"total_count": 0, "environments": []}
            return {"private": True, "permissions": {"admin": True}, "default_branch": "main"}
        github = Mock(side_effect=read_github)
        with tempfile.TemporaryDirectory() as directory:
            summary = installation(config, "synthetic/gateway", "plan", Path(directory), "", azure, github)
            self.assertFalse(summary["readyForDeployment"])
            self.assertFalse(any("create" in call.args[0] for call in azure.scoped.call_args_list))
            with self.assertRaisesRegex(ValueError, "approved"):
                installation(config, "synthetic/gateway", "execute", Path(directory), "f" * 64, azure, github)
            self.assertFalse(any("create" in call.args[0] for call in azure.scoped.call_args_list))

    def test_execute_preserves_existing_environment_reviewers(self):
        config = customer_config()
        azure = Mock()
        azure.run.return_value = {"tenantId": config["azure"]["tenantId"], "id": config["azure"]["subscriptionId"]}
        def cloud(arguments):
            if arguments[:2] == ["identity", "list"] or arguments[:3] == ["identity", "federated-credential", "list"]: return []
            if arguments[:2] == ["identity", "create"]:
                name = arguments[arguments.index("--name") + 1]
                return {"clientId": str(uuid5(NAMESPACE_URL, name)), "principalId": str(uuid5(NAMESPACE_URL, name + "principal"))}
            return {}
        azure.scoped.side_effect = cloud
        preserved = {"name": "test", "deployment_branch_policy": {"protected_branches": True, "custom_branch_policies": False}, "protection_rules": [{"type": "required_reviewers", "reviewers": [{"type": "User", "reviewer": {"id": 123}}]}]}
        calls = []
        def github(arguments, payload=None):
            calls.append(arguments)
            if arguments[0] == "variable": return {}
            if "/branches/" in arguments[-1]: return {"protected": True}
            if "/environments?" in arguments[-1]: return {"total_count": 1, "environments": [preserved]}
            if "/variables?" in arguments[-1]: return {"total_count": 0, "variables": []}
            return {"private": True, "permissions": {"admin": True}, "default_branch": "main"}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            planned = installation(config, "synthetic/gateway", "plan", path, "", azure, github)
            result = installation(config, "synthetic/gateway", "execute", path, planned["planSha256"], azure, github)
            self.assertTrue(result["applied"])
            self.assertFalse(result["readyForDeployment"])
            self.assertFalse(any("PUT" in command for command in calls))
            self.assertEqual(len([command for command in calls if command[0] == "variable"]), 9)
            with self.assertRaisesRegex(ValueError, "approved"):
                installation(config, "synthetic/gateway", "execute", Path(directory), "f" * 64, azure, github)

    def test_existing_client_variable_is_not_silently_repointed(self):
        config = customer_config()
        azure = Mock()
        azure.run.return_value = {"tenantId": config["azure"]["tenantId"], "id": config["azure"]["subscriptionId"]}
        azure.scoped.return_value = []
        def github(arguments, payload=None):
            route = arguments[-1]
            if "/branches/" in route: return {"protected": True}
            if "/environments?" in route: return {"total_count": 1, "environments": [{"name": "test", "deployment_branch_policy": {"protected_branches": True, "custom_branch_policies": False}}]}
            if "/variables?" in route: return {"total_count": 1, "variables": [{"name": "AZURE_RUNTIME_CLIENT_ID", "value": "existing-customer-identity"}]}
            return {"private": True, "permissions": {"admin": True}, "default_branch": "main"}
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(ValueError, "identity differs"):
            installation(config, "synthetic/gateway", "execute", Path(directory), "approved", azure, github)
        self.assertFalse(any("create" in call.args[0] for call in azure.scoped.call_args_list))