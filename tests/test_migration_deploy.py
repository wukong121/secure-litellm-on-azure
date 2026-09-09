import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts.customer_migration import ROOT, parameters_for
from scripts.migration_deploy import assert_change_scope, build_plan, deploy_component, group_id, resolve_origin
from tests.test_customer_migration import customer_config


class FakeAzure:
    def __init__(self, config, changes):
        self.config = config
        self.changes = changes
        self.calls = []

    def run(self, command):
        self.calls.append(command)
        return {"tenantId": self.config["azure"]["tenantId"], "id": self.config["azure"]["subscriptionId"]}

    def scoped(self, command):
        self.calls.append(command)
        if "what-if" in command:
            return {"status": "Succeeded", "changes": self.changes}
        return {"id": "/synthetic/deployment", "properties": {"provisioningState": "Succeeded"}}


class MigrationDeploymentTests(unittest.TestCase):
    def test_recovery_identity_is_explicit_separate_and_automatically_resolved(self):
        from unittest.mock import Mock
        config = customer_config()
        config["parameters"]["audit"] = {
            "storageAccountName": "stauditsynthetic", "virtualNetworkName": "synthetic-vnet",
            "privateEndpointSubnetName": "snet-private-endpoints", "logAnalyticsWorkspaceName": "synthetic-logs",
            "cmkVaultName": "synthetic-audit-kv", "cmkKeyName": "audit-key", "recoveryPrincipalId": "auto",
        }
        roles = ("writer", "reader", "retention", "recovery")
        foundation = {role: {"principalId": f"{index:08d}-1111-4111-8111-111111111111"} for index, role in enumerate(roles, 1)}
        foundation.update({key: config["parameters"]["audit"][key] for key in ("cmkVaultName", "cmkKeyName")})
        for role in roles:
            config["parameters"]["audit"][role + "PrincipalId"] = "auto"
        azure = Mock()
        azure.scoped.return_value = {"state": "Succeeded", "foundation": foundation}
        resolved = resolve_origin(config, "audit", azure)
        _, parameters = parameters_for(resolved, 8, "audit")
        self.assertEqual(parameters["parameters"]["recoveryPrincipalId"]["value"], foundation["recovery"]["principalId"])
        self.assertEqual(config["parameters"]["audit"]["recoveryPrincipalId"], "auto")
        foundation["recovery"] = foundation["writer"]
        with self.assertRaisesRegex(ValueError, "distinct"):
            resolve_origin(config, "audit", azure)
        foundation.pop("recovery")
        with self.assertRaisesRegex(ValueError, "Redeploy audit-foundation"):
            resolve_origin(config, "audit", azure)

    def setUp(self):
        self.config = customer_config()
        self.revision = "a" * 40
        self.change = {"changeType": "Create", "resourceId": group_id(self.config), "after": {"location": "westus"}}

    def test_bootstrap_parameters_work_before_target_group_exists(self):
        template, parameters = parameters_for(self.config, 0, "bootstrap")
        self.assertEqual(template.parent.name, "bootstrap")
        self.assertEqual(parameters["parameters"]["resourceGroupName"]["value"], "rg-secure")

    def test_delete_unsupported_and_out_of_scope_are_rejected(self):
        for update in ({"changeType": "Delete"}, {"changeType": "Unsupported"}, {"resourceId": group_id(self.config, True)}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                assert_change_scope(self.config, "bootstrap", [{**self.change, **update}])

    def test_only_declared_external_model_role_assignments_are_allowed(self):
        account = "/subscriptions/external/resourceGroups/models/providers/Microsoft.CognitiveServices/accounts/model"
        self.config["parameters"]["platform"]["azureOpenAIConnections"] = [{"accountResourceId": account}]
        assert_change_scope(self.config, "platform", [{"changeType": "Create", "resourceId": account + "/providers/Microsoft.Authorization/roleAssignments/role"}])
        with self.assertRaises(ValueError):
            assert_change_scope(self.config, "platform", [{"changeType": "Modify", "resourceId": account}])

    def test_plan_hash_binds_changes_config_code_and_parameters(self):
        plan = build_plan(self.config, 0, "bootstrap", self.revision, "b" * 64, {}, [self.change])
        changed = copy.deepcopy(self.change)
        changed["after"]["location"] = "eastus"
        self.assertNotEqual(plan["planSha256"], build_plan(self.config, 0, "bootstrap", self.revision, "b" * 64, {}, [changed])["planSha256"])
        self.assertNotEqual(plan["planSha256"], build_plan(self.config, 0, "bootstrap", "c" * 40, "b" * 64, {}, [self.change])["planSha256"])
        self.assertNotIn("after", plan["changes"][0])

    def compile(self, command, **kwargs):
        Path(command[command.index("--outfile") + 1]).write_text('{"resources": []}')
        return SimpleNamespace(returncode=0, stderr="")

    def test_plan_never_creates_and_deploy_requires_same_approved_plan(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as directory, patch("scripts.migration_deploy.subprocess.run", side_effect=self.compile):
            azure = FakeAzure(self.config, [self.change])
            plan = deploy_component(self.config, 0, "bootstrap", self.revision, "plan", [], directory, azure=azure)
            self.assertFalse(any("create" in call for call in azure.calls))
            with self.assertRaisesRegex(ValueError, "Plan changed"):
                deploy_component(self.config, 0, "bootstrap", self.revision, "deploy", [], directory, "f" * 64, azure)
            self.assertFalse(any("create" in call for call in azure.calls))
            receipt = deploy_component(self.config, 0, "bootstrap", self.revision, "deploy", [], directory, plan["planSha256"], azure)
            self.assertTrue(any(call[:3] == ["deployment", "sub", "create"] for call in azure.calls))
            self.assertFalse(receipt["stageAccepted"])
            self.assertFalse((Path(directory) / "migration-evidence.json").exists())

    def test_stage_one_deploy_still_needs_stage_zero_evidence(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as directory, patch("scripts.migration_deploy.subprocess.run") as command:
            with self.assertRaisesRegex(ValueError, "Prior stage"):
                deploy_component(self.config, 1, "monitoring", self.revision, "deploy", [], directory, "f" * 64)
            command.assert_not_called()

    def test_greenfield_network_uses_target_group_without_old_inventory(self):
        self.config["deploymentMode"] = "greenfield"
        self.config.pop("legacy")
        self.config["parameters"].pop("monitoring")
        self.config["parameters"]["network"] = {"virtualNetworkName": "target-vnet", "virtualNetworkAddressPrefix": "10.30.0.0/16", "privateEndpointSubnetName": "snet-private-endpoints", "privateEndpointSubnetPrefix": "10.30.8.0/24"}
        change = {"changeType": "Create", "resourceId": group_id(self.config) + "/providers/Microsoft.Network/virtualNetworks/target-vnet"}
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as directory, patch("scripts.migration_deploy.subprocess.run", side_effect=self.compile):
            azure = FakeAzure(self.config, [change])
            plan = deploy_component(self.config, 0, "network", self.revision, "plan", [], directory, azure=azure)
            self.assertFalse(any("create" in call for call in azure.calls))
            receipt = deploy_component(self.config, 0, "network", self.revision, "deploy", [], directory, plan["planSha256"], azure)
            creates = [call for call in azure.calls if call[:3] == ["deployment", "group", "create"]]
            self.assertEqual(len(creates), 1)
            self.assertEqual(creates[0][creates[0].index("--resource-group") + 1], "rg-secure")
            self.assertFalse(receipt["stageAccepted"])
            with self.assertRaisesRegex(ValueError, "does not apply"):
                deploy_component(self.config, 1, "monitoring", self.revision, "plan", [], directory, azure=azure)

    def test_release_cannot_target_earlier_stage(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as directory, patch("scripts.migration_deploy.subprocess.run", side_effect=self.compile):
            azure = FakeAzure(self.config, [self.change])
            with self.assertRaisesRegex(ValueError, "Stage 9 edge"):
                deploy_component(self.config, 0, "bootstrap", self.revision, "plan", [], directory, azure=azure, release={})
            self.assertFalse(any("create" in call for call in azure.calls))

    def test_edge_resolves_successful_origin_output_only(self):
        self.config["parameters"]["edge"]["privateOrigin"]["privateLinkServiceId"] = "auto"
        from unittest.mock import Mock
        azure = Mock()
        expected = group_id(self.config) + "/providers/Microsoft.Network/privateLinkServices/api"
        azure.scoped.return_value = {"state": "Succeeded", "origin": {"privateLinkServiceId": expected, "privateLinkLocation": "westus"}}
        resolved = resolve_origin(self.config, "edge", azure)
        self.assertEqual(resolved["parameters"]["edge"]["privateOrigin"]["privateLinkServiceId"], expected)
        self.assertEqual(self.config["parameters"]["edge"]["privateOrigin"]["privateLinkServiceId"], "auto")
        azure.scoped.return_value["state"] = "Failed"
        with self.assertRaises(ValueError):
            resolve_origin(self.config, "edge", azure)