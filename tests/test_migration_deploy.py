import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts.customer_migration import ROOT, MigrationError, parameters_for, stage_checks, stage_fingerprint, validate_config
from scripts.migration_deploy import AzureCommands, assert_change_scope, build_plan, deploy_component, group_id, resolve_origin
from tests.test_customer_migration import certificate_config, customer_config


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
    def test_azure_commands_report_operation_and_redact_failure_scope(self):
        config = customer_config()
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as directory:
            azure = AzureCommands(config, Path(directory))
            with patch("scripts.migration_deploy.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout="", stderr="")):
                with self.assertRaisesRegex(MigrationError, "az aks show returned no JSON output"):
                    azure.scoped(["aks", "show", "--resource-group", config["target"]["resourceGroup"], "--name", "target-aks"])
            failure = f"ERROR: (AuthorizationFailed) The client '{config['azure']['tenantId']}' cannot perform Microsoft.ContainerService/managedClusters/read over scope '/subscriptions/{config['azure']['subscriptionId']}/resourceGroups/{config['target']['resourceGroup']}'"
            with patch("scripts.migration_deploy.subprocess.run", return_value=SimpleNamespace(returncode=1, stdout="", stderr=failure)):
                with self.assertRaises(MigrationError) as raised:
                    azure.scoped(["aks", "show", "--resource-group", config["target"]["resourceGroup"], "--name", "target-aks"])
            message = str(raised.exception)
            self.assertIn("Azure command failed (az aks show)", message)
            self.assertIn("AuthorizationFailed", message)
            self.assertIn("Microsoft.ContainerService/managedClusters/read", message)
            self.assertNotIn(config["azure"]["tenantId"], message)
            self.assertNotIn(config["azure"]["subscriptionId"], message)

    def target_connectivity_config(self):
        config = customer_config()
        config["parameters"]["runner-target-connectivity"] = {
            "runnerVirtualNetworkId": group_id(config).replace("rg-secure", "rg-runner") + "/providers/Microsoft.Network/virtualNetworks/runner-vnet",
        }
        return config

    def test_target_connectivity_is_stage4_only_and_preserves_early_fingerprints(self):
        from scripts.runner_target_connectivity import target_connectivity_settings
        config = self.target_connectivity_config()
        validate_config(config, "test")
        template, document = parameters_for(config, 4, "runner-target-connectivity")
        self.assertEqual(template.parent.name, "runner-target-connectivity")
        self.assertTrue(document["parameters"]["manageDnsLink"]["value"])
        original = customer_config()
        for stage in range(4):
            self.assertEqual(stage_fingerprint(config, stage), stage_fingerprint(original, stage))
        self.assertNotEqual(stage_fingerprint(config, 4), stage_fingerprint(original, 4))
        for stage in (0, 3, 5):
            with self.subTest(stage=stage), self.assertRaises(ValueError):
                parameters_for(config, stage, "runner-target-connectivity")
        settings = target_connectivity_settings(config)
        config["parameters"]["runner-target-connectivity"]["runnerVirtualNetworkId"] = settings["runnerVirtualNetworkId"].upper()
        self.assertEqual(settings["linkName"], target_connectivity_settings(config)["linkName"])

    def test_target_connectivity_rejects_unapproved_scope_and_management_values(self):
        from scripts.runner_target_connectivity import target_connectivity_settings
        config = self.target_connectivity_config()
        identifier = config["parameters"]["runner-target-connectivity"]["runnerVirtualNetworkId"]
        for updates in (
            {"runnerVirtualNetworkId": identifier + "/subnets/runner"},
            {"runnerVirtualNetworkId": identifier.replace(config["azure"]["subscriptionId"], "33333333-3333-4333-8333-333333333333")},
            {"manageDnsLink": "false"},
            {"privateDnsZoneId": "/unapproved/zone"},
        ):
            invalid = copy.deepcopy(config)
            invalid["parameters"]["runner-target-connectivity"].update(updates)
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                target_connectivity_settings(invalid)
        config["parameters"]["runner-connectivity"] = {"runnerVirtualNetworkId": identifier + "-other"}
        with self.assertRaisesRegex(ValueError, "differs"):
            target_connectivity_settings(config)

    def target_connectivity_azure(self, config):
        from unittest.mock import Mock
        from scripts.runner_target_connectivity import target_connectivity_settings, target_connectivity_resource_ids
        settings = target_connectivity_settings(config)
        node_prefix = group_id(config).replace("rg-secure", "rg-nodes") + "/providers/"
        zone_name = "synthetic.privatelink.westus.azmk8s.io"
        resources = {
            "deployment": "Succeeded",
            "cluster": {"id": settings["aksResourceId"], "provisioningState": "Succeeded", "powerState": {"code": "Running"}, "nodeResourceGroup": "rg-nodes", "privateFqdn": "new-aks." + zone_name, "apiServerAccessProfile": {"enablePrivateCluster": True, "privateDnsZone": "system"}, "agentPoolProfiles": [{"vnetSubnetId": settings["targetVirtualNetworkId"] + "/subnets/system"}]},
            "zone": {"id": node_prefix + "Microsoft.Network/privateDnsZones/" + zone_name},
            "endpoints": [{"provisioningState": "Succeeded", "subnet": {"id": settings["targetVirtualNetworkId"] + "/subnets/system"}, "privateLinkServiceConnections": [{"privateLinkServiceId": settings["aksResourceId"], "privateLinkServiceConnectionState": {"status": "Approved"}}], "networkInterfaces": [{"id": node_prefix + "Microsoft.Network/networkInterfaces/api-nic"}]}],
            "ips": ["10.30.1.4"], "record": {"aRecords": [{"ipv4Address": "10.30.1.4"}]},
            "runner": {"id": settings["runnerVirtualNetworkId"]}, "links": [],
        }
        azure = Mock()
        azure.run.return_value = {"tenantId": config["azure"]["tenantId"], "id": config["azure"]["subscriptionId"]}
        def scoped(command):
            if command[:3] == ["deployment", "group", "show"]:
                return resources["deployment"]
            if command[:2] == ["aks", "show"]:
                return resources["cluster"]
            for prefix, key in ((["network", "private-dns", "zone", "show"], "zone"), (["network", "private-endpoint", "list"], "endpoints"), (["network", "nic", "show"], "ips"), (["network", "private-dns", "record-set", "a", "show"], "record"), (["network", "vnet", "show"], "runner"), (["network", "private-dns", "link", "vnet", "list"], "links")):
                if command[:len(prefix)] == prefix:
                    return resources[key]
            if "what-if" in command:
                context = {"createDnsLink": True, "privateDnsZoneId": resources["zone"]["id"], "dnsResourceGroupName": "rg-nodes", "linkName": settings["linkName"]}
                return {"status": "Succeeded", "changes": [{"resourceId": identifier, "changeType": "Create"} for identifier in sorted(target_connectivity_resource_ids(config, context))]}
            if command[:3] == ["deployment", "group", "create"]:
                resources["links"] = [{"name": settings["linkName"], "virtualNetwork": {"id": settings["runnerVirtualNetworkId"]}, "registrationEnabled": False, "provisioningState": "Succeeded", "virtualNetworkLinkState": "Completed"}]
                return {"properties": {"provisioningState": "Succeeded"}}
            raise AssertionError(command)
        azure.scoped.side_effect = scoped
        return azure, resources

    def test_target_connectivity_discovers_only_existing_aks_dns_and_scopes_changes(self):
        from scripts.runner_target_connectivity import inspect_target_connectivity, target_connectivity_resource_ids
        config = self.target_connectivity_config()
        azure, resources = self.target_connectivity_azure(config)
        context = inspect_target_connectivity(config, azure)
        self.assertEqual(context["privateEndpointIps"], ["10.30.1.4"])
        self.assertEqual(context["privateDnsZoneId"], resources["zone"]["id"])
        allowed = target_connectivity_resource_ids(config, context)
        self.assertEqual(len(allowed), 2)
        assert_change_scope(config, "runner-target-connectivity", [{"resourceId": identifier, "changeType": "Create"} for identifier in allowed], context)
        for identifier in (resources["zone"]["id"], resources["zone"]["id"] + "/A/new-aks", context["aksResourceId"], context["runnerVirtualNetworkId"], next(iter(allowed)) + "-other"):
            with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                assert_change_scope(config, "runner-target-connectivity", [{"resourceId": identifier, "changeType": "Modify"}], context)
        for change_type in ("Delete", "Unsupported"):
            with self.assertRaises(ValueError):
                assert_change_scope(config, "runner-target-connectivity", [{"resourceId": next(iter(allowed)), "changeType": change_type}], context)

    def test_target_connectivity_rejects_dns_drift_and_unapproved_resources(self):
        from scripts.runner_target_connectivity import inspect_target_connectivity
        config = self.target_connectivity_config()
        mutations = (
            lambda resources: resources.update(deployment="Failed"),
            lambda resources: resources["cluster"].update(provisioningState="Updating"),
            lambda resources: resources["cluster"]["powerState"].update(code="Stopped"),
            lambda resources: resources["cluster"]["apiServerAccessProfile"].update(enablePrivateCluster=False),
            lambda resources: resources["cluster"]["apiServerAccessProfile"].update(privateDnsZone="none"),
            lambda resources: resources["cluster"].update(privateFqdn="unapproved.invalid"),
            lambda resources: resources["cluster"]["agentPoolProfiles"][0].update(vnetSubnetId="/other/subnets/system"),
            lambda resources: resources["zone"].update(id="/other/zone"),
            lambda resources: resources["endpoints"][0]["privateLinkServiceConnections"][0]["privateLinkServiceConnectionState"].update(status="Pending"),
            lambda resources: resources["endpoints"][0]["networkInterfaces"][0].update(id="/other/nic"),
            lambda resources: resources.update(ips=["8.8.8.8"]),
            lambda resources: resources["record"].update(aRecords=[{"ipv4Address": "10.30.1.5"}]),
            lambda resources: resources["runner"].update(id="/other/vnet"),
            lambda resources: resources["runner"].update(dhcpOptions={"dnsServers": ["10.50.0.10"]}),
        )
        for mutate in mutations:
            azure, resources = self.target_connectivity_azure(config)
            mutate(resources)
            with self.subTest(mutation=mutate), self.assertRaises(ValueError):
                inspect_target_connectivity(config, azure)

    def test_target_connectivity_reuses_existing_links_and_supports_external_dns(self):
        from scripts.runner_target_connectivity import inspect_target_connectivity, target_connectivity_resource_ids, target_connectivity_settings
        config = self.target_connectivity_config()
        azure, resources = self.target_connectivity_azure(config)
        settings = target_connectivity_settings(config)
        resources["links"] = [{"name": "network-owner-link", "virtualNetwork": {"id": settings["runnerVirtualNetworkId"]}, "registrationEnabled": False, "provisioningState": "Succeeded", "virtualNetworkLinkState": "Completed"}]
        context = inspect_target_connectivity(config, azure)
        self.assertEqual(context["management"], "reused")
        self.assertEqual(target_connectivity_resource_ids(config, context), set())
        resources["links"][0]["name"] = settings["linkName"]
        self.assertTrue(inspect_target_connectivity(config, azure)["createDnsLink"])
        resources["links"][0]["registrationEnabled"] = True
        with self.assertRaisesRegex(ValueError, "conflicts"):
            inspect_target_connectivity(config, azure)
        resources["links"][0]["registrationEnabled"] = False
        resources["links"][0]["virtualNetwork"]["id"] += "-other"
        with self.assertRaisesRegex(ValueError, "another VNet"):
            inspect_target_connectivity(config, azure)
        resources["links"] = []
        config["parameters"]["runner-target-connectivity"]["manageDnsLink"] = False
        resources["runner"]["dhcpOptions"] = {"dnsServers": ["10.50.0.10"]}
        context = inspect_target_connectivity(config, azure)
        self.assertEqual(context["management"], "external")
        self.assertFalse(context["createDnsLink"])

    def test_target_connectivity_deployment_reuses_platform_but_requires_new_sha_evidence(self):
        config = self.target_connectivity_config()
        azure, resources = self.target_connectivity_azure(config)
        records = [{"stage": stage, "environment": "test", "binding": "stage-config", "configSha256": stage_fingerprint(config, stage), "revision": self.revision, "status": "passed", "checks": stage_checks(stage, config), "approvedBy": [config["azure"]["tenantId"], config["azure"]["subscriptionId"]], "observedAt": datetime.now(timezone.utc).isoformat(), "reportUrl": "https://evidence.customer.invalid/report", "reportSha256": "b" * 64} for stage in range(4)]
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as directory, patch("scripts.migration_deploy.subprocess.run", side_effect=self.compile):
            plan = deploy_component(config, 4, "runner-target-connectivity", self.revision, "plan", [], directory, azure=azure)
            self.assertFalse(any("create" in call.args[0] for call in azure.scoped.call_args_list))
            context = json.loads((Path(directory) / "connectivity-review.json").read_text())
            self.assertEqual(context["privateDnsZoneId"], resources["zone"]["id"])
            parameters = json.loads((Path(directory) / "parameters.json").read_text())["parameters"]
            self.assertEqual(parameters["dnsResourceGroupName"]["value"], "rg-nodes")
            self.assertTrue(parameters["createDnsLink"]["value"])
            with self.assertRaisesRegex(ValueError, "Prior stage evidence"):
                deploy_component(config, 4, "runner-target-connectivity", self.revision, "deploy", [], directory, plan["planSha256"], azure)
            old_records = [{**record, "revision": "c" * 40} for record in records]
            with self.assertRaisesRegex(ValueError, "revision mismatch"):
                deploy_component(config, 4, "runner-target-connectivity", self.revision, "deploy", old_records, directory, plan["planSha256"], azure)
            resources["ips"] = ["10.30.1.5"]
            resources["record"]["aRecords"][0]["ipv4Address"] = "10.30.1.5"
            with self.assertRaisesRegex(ValueError, "Plan changed"):
                deploy_component(config, 4, "runner-target-connectivity", self.revision, "deploy", records, directory, plan["planSha256"], azure)
            resources["ips"] = ["10.30.1.4"]
            resources["record"]["aRecords"][0]["ipv4Address"] = "10.30.1.4"
            receipt = deploy_component(config, 4, "runner-target-connectivity", self.revision, "deploy", records, directory, plan["planSha256"], azure)
            self.assertFalse(receipt["stageAccepted"])
            creates = [call.args[0] for call in azure.scoped.call_args_list if call.args[0][:3] == ["deployment", "group", "create"]]
            self.assertEqual(len(creates), 1)
            self.assertIn("llmgw-test-s4-runner-target-connectivity", creates[0])
            self.assertNotIn("llmgw-test-s4-platform", creates[0])
            self.assertIn("Incremental", creates[0])
            self.assertFalse(any("get-credentials" in call.args[0] for call in azure.scoped.call_args_list))

    def test_target_connectivity_postdeploy_requires_link_and_legacy_preview_is_blocked(self):
        from scripts.customer_migration import preview
        from scripts.runner_target_connectivity import inspect_target_connectivity
        config = self.target_connectivity_config()
        azure, _ = self.target_connectivity_azure(config)
        with self.assertRaisesRegex(ValueError, "missing after deployment"):
            inspect_target_connectivity(config, azure, require_link=True)
        template, _ = parameters_for(config, 4, "runner-target-connectivity")
        with patch("scripts.customer_migration.subprocess.run") as command, self.assertRaisesRegex(ValueError, "infrastructure deployment plan"):
            preview(config, template, ROOT / "temp/parameters.json", "runner-target-connectivity")
        command.assert_not_called()

    def test_target_connectivity_supports_reported_custom_zone_and_same_vnet(self):
        from scripts.runner_target_connectivity import inspect_target_connectivity, target_connectivity_settings
        config = self.target_connectivity_config()
        azure, resources = self.target_connectivity_azure(config)
        resources["zone"]["id"] = resources["zone"]["id"].replace("rg-nodes", "rg-dns")
        resources["cluster"]["apiServerAccessProfile"]["privateDnsZone"] = resources["zone"]["id"]
        self.assertEqual(inspect_target_connectivity(config, azure)["dnsResourceGroupName"], "rg-dns")
        config["parameters"]["runner-target-connectivity"]["runnerVirtualNetworkId"] = target_connectivity_settings(config)["targetVirtualNetworkId"]
        azure, resources = self.target_connectivity_azure(config)
        resources["links"] = [{"name": "aks-system-link", "virtualNetwork": {"id": resources["runner"]["id"]}, "registrationEnabled": False, "provisioningState": "Succeeded", "virtualNetworkLinkState": "Completed"}]
        self.assertEqual(inspect_target_connectivity(config, azure)["management"], "reused")

    def connectivity_config(self):
        config = customer_config()
        config["parameters"]["backup"] = {"virtualNetworkName": "backup-vnet"}
        config["parameters"]["runner-connectivity"] = {
            "runnerVirtualNetworkId": group_id(config).replace("rg-secure", "rg-runner") + "/providers/Microsoft.Network/virtualNetworks/runner-vnet",
        }
        return config

    def test_connectivity_parameters_are_stable_and_reject_arbitrary_scopes(self):
        from scripts.runner_connectivity import connectivity_settings
        config = self.connectivity_config()
        template, document = parameters_for(config, 0, "runner-connectivity")
        self.assertEqual(template.parent.name, "runner-connectivity")
        self.assertEqual(document["parameters"]["runnerResourceGroupName"]["value"], "rg-runner")
        self.assertTrue(document["parameters"]["managePeering"]["value"])
        settings = connectivity_settings(config)
        original = copy.deepcopy(config)
        config["parameters"]["runner-connectivity"]["runnerVirtualNetworkId"] = settings["runnerVirtualNetworkId"].upper()
        self.assertEqual(settings["connectionName"], connectivity_settings(config)["connectionName"])
        for identifier in (settings["runnerVirtualNetworkId"] + "/subnets/runner", settings["backupVirtualNetworkId"], settings["runnerVirtualNetworkId"].replace(config["azure"]["subscriptionId"], "33333333-3333-4333-8333-333333333333")):
            invalid = copy.deepcopy(original)
            invalid["parameters"]["runner-connectivity"]["runnerVirtualNetworkId"] = identifier
            with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                connectivity_settings(invalid)
        config = copy.deepcopy(original)
        config["parameters"]["runner-connectivity"]["managePeering"] = "false"
        with self.assertRaises(ValueError):
            connectivity_settings(config)

    def test_connectivity_scope_only_allows_exact_children_and_nested_deployment(self):
        from scripts.runner_connectivity import connectivity_resource_ids, connectivity_settings
        config = self.connectivity_config()
        context = {"privateDnsZoneName": "privatelink.blob.core.windows.net"}
        allowed = connectivity_resource_ids(config, context["privateDnsZoneName"])
        self.assertEqual(len(allowed), 4)
        changes = [{"changeType": "Create", "resourceId": identifier} for identifier in allowed]
        assert_change_scope(config, "runner-connectivity", changes, context)
        settings = connectivity_settings(config)
        for resource in (settings["runnerVirtualNetworkId"], settings["backupVirtualNetworkId"], group_id(config) + "/providers/Microsoft.Authorization/roleAssignments/extra", next(iter(allowed)) + "-other"):
            with self.subTest(resource=resource), self.assertRaises(ValueError):
                assert_change_scope(config, "runner-connectivity", [{"changeType": "Modify", "resourceId": resource}], context)
        with self.assertRaises(ValueError):
            assert_change_scope(config, "runner-connectivity", [{"changeType": "Delete", "resourceId": next(iter(allowed))}], context)
        config["parameters"]["runner-connectivity"].update(managePeering=False, manageBlobDnsLink=False)
        self.assertEqual(connectivity_resource_ids(config, context["privateDnsZoneName"]), set())

    def connectivity_azure(self, config):
        from unittest.mock import Mock
        from scripts.runner_connectivity import connectivity_settings
        settings = connectivity_settings(config)
        prefix = group_id(config) + "/providers/"
        backup = {"storageAccountName": "syntheticbackup", "containerName": "litellm-postgresql", "virtualNetworkName": "backup-vnet", "privateEndpointName": "backup-pe", "privateEndpointSubnetName": "pe-subnet", "privateDnsZoneName": "privatelink.blob.core.windows.net"}
        resources = {
            "deployment": {"state": "Succeeded", "backup": backup},
            "account": {"id": prefix + "Microsoft.Storage/storageAccounts/syntheticbackup", "publicNetworkAccess": "Disabled", "blob": "https://syntheticbackup.blob.core.windows.net/"},
            "pe": {"subnet": {"id": settings["backupVirtualNetworkId"] + "/subnets/pe-subnet"}, "networkInterfaces": [{"id": prefix + "Microsoft.Network/networkInterfaces/backup-nic"}], "privateLinkServiceConnections": [{"privateLinkServiceId": prefix + "Microsoft.Storage/storageAccounts/syntheticbackup", "groupIds": ["blob"], "privateLinkServiceConnectionState": {"status": "Approved"}}]},
            "ips": ["10.30.8.4"], "links": [],
            "runner": {"id": settings["runnerVirtualNetworkId"], "addressSpace": {"addressPrefixes": ["10.50.0.0/24"]}},
            "backup": {"id": settings["backupVirtualNetworkId"], "addressSpace": {"addressPrefixes": ["10.30.0.0/16"]}},
        }
        azure = Mock()
        azure.run.side_effect = lambda command: "core.windows.net" if command[0] == "cloud" else {"tenantId": config["azure"]["tenantId"], "id": config["azure"]["subscriptionId"]}
        def scoped(command):
            if command[:3] == ["deployment", "group", "show"]:
                return resources["deployment"]
            if command[:3] == ["storage", "account", "show"]:
                return resources["account"]
            if command[:3] == ["network", "private-endpoint", "show"]:
                return resources["pe"]
            if command[:3] == ["network", "nic", "show"]:
                return resources["ips"]
            if command[:3] == ["network", "vnet", "show"]:
                return resources["runner" if settings["runnerVirtualNetworkId"] in command else "backup"]
            if command[:4] == ["network", "private-dns", "link", "vnet"]:
                return resources["links"]
            if "what-if" in command:
                from scripts.runner_connectivity import connectivity_resource_ids
                return {"status": "Succeeded", "changes": [{"resourceId": identifier, "changeType": "Create"} for identifier in sorted(connectivity_resource_ids(config, backup["privateDnsZoneName"]))]}
            if command[:3] == ["deployment", "group", "create"]:
                return {"properties": {"provisioningState": "Succeeded"}}
            raise AssertionError(command)
        azure.scoped.side_effect = scoped
        return azure, resources

    def test_connectivity_uses_existing_backup_and_requires_plan_approval(self):
        from scripts.runner_connectivity import inspect_connectivity
        config = self.connectivity_config()
        azure, _ = self.connectivity_azure(config)
        result = inspect_connectivity(config, azure)
        self.assertEqual(result["privateEndpointIps"], ["10.30.8.4"])
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as directory, patch("scripts.migration_deploy.subprocess.run", side_effect=self.compile):
            plan = deploy_component(config, 0, "runner-connectivity", "a" * 40, "plan", [], directory, azure=azure)
            self.assertFalse(any("create" in call.args[0] for call in azure.scoped.call_args_list))
            with self.assertRaisesRegex(ValueError, "Plan changed"):
                deploy_component(config, 0, "runner-connectivity", "a" * 40, "deploy", [], directory, "f" * 64, azure)
            deploy_component(config, 0, "runner-connectivity", "a" * 40, "deploy", [], directory, plan["planSha256"], azure)
            creates = [call.args[0] for call in azure.scoped.call_args_list if call.args[0][:3] == ["deployment", "group", "create"]]
            self.assertEqual(len(creates), 1)
            self.assertIn("Incremental", creates[0])

    def test_connectivity_blocks_overlap_foreign_endpoints_and_existing_conflicts(self):
        from scripts.runner_connectivity import connectivity_settings, inspect_connectivity
        config = self.connectivity_config()
        settings = connectivity_settings(config)
        mutations = (
            lambda resources: resources["deployment"].update(state="Failed"),
            lambda resources: resources["account"].update(publicNetworkAccess="Enabled"),
            lambda resources: resources["account"].update(blob="https://other.invalid/"),
            lambda resources: resources["pe"]["networkInterfaces"][0].update(id="/unapproved/nic"),
            lambda resources: resources["runner"].update(addressSpace={"addressPrefixes": ["10.30.0.0/24"]}),
            lambda resources: resources["runner"].update(dhcpOptions={"dnsServers": ["10.50.0.10"]}),
            lambda resources: resources["runner"].update(virtualNetworkPeerings=[{"name": "existing", "remoteVirtualNetwork": {"id": settings["backupVirtualNetworkId"]}}]),
            lambda resources: resources["links"].append({"name": "existing", "virtualNetwork": {"id": settings["runnerVirtualNetworkId"]}}),
        )
        for mutate in mutations:
            azure, resources = self.connectivity_azure(config)
            mutate(resources)
            with self.subTest(mutation=mutate), self.assertRaises(ValueError):
                inspect_connectivity(config, azure)
        config["parameters"]["runner-connectivity"].update(managePeering=False, manageBlobDnsLink=False)
        azure, resources = self.connectivity_azure(config)
        resources["runner"].update(dhcpOptions={"dnsServers": ["10.50.0.10"]})
        self.assertEqual(inspect_connectivity(config, azure)["allowedResourceIds"], [])

    def test_connectivity_reuses_own_connections_but_preserves_foreign_settings(self):
        from scripts.runner_connectivity import connectivity_settings, inspect_connectivity
        config = self.connectivity_config()
        settings = connectivity_settings(config)
        azure, resources = self.connectivity_azure(config)
        for network, remote in (("runner", "backupVirtualNetworkId"), ("backup", "runnerVirtualNetworkId")):
            resources[network]["virtualNetworkPeerings"] = [{"name": settings["connectionName"], "remoteVirtualNetwork": {"id": settings[remote]}, "allowForwardedTraffic": False, "allowGatewayTransit": False, "useRemoteGateways": False}]
        resources["links"] = [{"name": settings["connectionName"], "registrationEnabled": False, "virtualNetwork": {"id": settings["runnerVirtualNetworkId"]}}]
        expected = inspect_connectivity(config, azure)
        self.assertEqual(inspect_connectivity(config, azure), expected)
        resources["runner"]["virtualNetworkPeerings"][0]["allowForwardedTraffic"] = True
        with self.assertRaisesRegex(ValueError, "conflicts"):
            inspect_connectivity(config, azure)
        resources["runner"]["virtualNetworkPeerings"][0]["allowForwardedTraffic"] = False
        resources["links"][0]["registrationEnabled"] = True
        with self.assertRaisesRegex(ValueError, "conflicts"):
            inspect_connectivity(config, azure)

    def test_managed_release_requires_matching_api_edge_binding_receipt(self):
        from unittest.mock import Mock
        from scripts.customer_migration import stage_fingerprint
        from scripts.edge_binding import require_edge_binding
        config = customer_config()
        identifier = "11111111-1111-4111-8111-111111111111"
        receipt = {"revision": "a" * 40, "configSha256": stage_fingerprint(config, 9), "frontDoorId": identifier, "apiHost": "llm-api." + config["baseDomain"], "clusterResourceId": group_id(config) + "/providers/Microsoft.ContainerService/managedClusters/new-aks", "rolloutVerified": True, "deploymentUid": "api-original", "podTemplateSha256": "b" * 64}
        azure = Mock()
        azure.scoped.return_value = {"state": "Succeeded", "binding": receipt}
        require_edge_binding(config, "a" * 40, identifier, azure)
        for updates in ({"revision": "c" * 40}, {"frontDoorId": "other"}, {"rolloutVerified": False}, {"clusterResourceId": "/other"}, {"podTemplateSha256": ""}):
            azure.scoped.return_value = {"state": "Succeeded", "binding": {**receipt, **updates}}
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                require_edge_binding(config, "a" * 40, identifier, azure)

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

    def certificate_azure(self, config):
        from unittest.mock import Mock
        from scripts.certificate_vault import certificate_resource_ids
        state = {"vaults": [], "zones": [], "links": [], "dnsServers": []}
        azure = Mock()
        azure.run.return_value = {"tenantId": config["azure"]["tenantId"], "id": config["azure"]["subscriptionId"]}
        def scoped(command):
            if command[:2] == ["keyvault", "list"]:
                return state["vaults"]
            if command[:4] == ["network", "private-dns", "zone", "list"]:
                return state["zones"]
            if command[:4] == ["network", "private-dns", "link", "vnet"]:
                return state["links"]
            if command[:3] == ["network", "vnet", "show"]:
                return {"id": command[-1], "dhcpOptions": {"dnsServers": state["dnsServers"]}}
            if "what-if" in command:
                return {"status": "Succeeded", "changes": [{"resourceId": identifier, "changeType": "Create"} for identifier in sorted(certificate_resource_ids(config))]}
            if command[:3] == ["deployment", "group", "create"]:
                return {"properties": {"provisioningState": "Succeeded"}}
            raise AssertionError(command)
        azure.scoped.side_effect = scoped
        return azure, state

    def test_certificate_vault_scope_is_exact_and_never_allows_secrets_or_business_resources(self):
        from scripts.certificate_vault import certificate_resource_ids
        config = certificate_config()
        allowed = certificate_resource_ids(config)
        assert_change_scope(config, "certificate-vault", [{"resourceId": identifier, "changeType": "Create"} for identifier in allowed])
        prefix = group_id(config) + "/providers/"
        blocked = (
            prefix + "Microsoft.Network/virtualNetworks/target-vnet",
            prefix + "Microsoft.Network/virtualNetworks/target-vnet/subnets/snet-pe",
            prefix + "Microsoft.KeyVault/vaults/kv-lt-test-backend",
            prefix + "Microsoft.KeyVault/vaults/kv-customer-cert-test/secrets/api-tls",
            prefix + "Microsoft.KeyVault/vaults/kv-customer-cert-test/providers/Microsoft.Authorization/roleAssignments/extra",
            prefix + "Microsoft.Network/privateDnsZones/privatelink.vaultcore.azure.net/virtualNetworkLinks/foreign",
        )
        for identifier in blocked:
            with self.subTest(identifier=identifier), self.assertRaisesRegex(ValueError, "unapproved"):
                assert_change_scope(config, "certificate-vault", [{"resourceId": identifier, "changeType": "Modify"}])
        for change_type in ("Delete", "Unsupported"):
            with self.assertRaises(ValueError):
                assert_change_scope(config, "certificate-vault", [{"resourceId": next(iter(allowed)), "changeType": change_type}])
        config["parameters"]["certificate-vault"].update(createPrivateDnsZone=False, manageRunnerDnsLink=False, manageTargetDnsLink=False)
        self.assertFalse(any("privateDnsZones".lower() in identifier for identifier in certificate_resource_ids(config)))

    def test_certificate_config_rejects_invalid_identities_networks_and_secret_mappings(self):
        from scripts.certificate_vault import certificate_vault_parameters
        original = certificate_config()
        settings = original["parameters"]["certificate-vault"]
        cases = (
            {"vaultName": "kv-lt-test-backend"}, {"vaultName": "not--valid"},
            {"ingressReaderPrincipalId": "00000000-0000-0000-0000-000000000000"},
            {"certificateImporterPrincipalId": settings["ingressReaderPrincipalId"]},
            {"certificateImporterPrincipalType": "ServicePrincipal"}, {"manageRunnerDnsLink": "false"},
            {"runnerVirtualNetworkId": settings["runnerVirtualNetworkId"] + "/subnets/runner"},
            {"runnerVirtualNetworkId": settings["runnerVirtualNetworkId"].replace(original["azure"]["subscriptionId"], "55555555-5555-4555-8555-555555555555")},
        )
        for updates in cases:
            config = copy.deepcopy(original)
            config["parameters"]["certificate-vault"].update(updates)
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                validate_config(config, "test")
        config = copy.deepcopy(original)
        config["privateIngress"] = {plane: {"tlsSecretId": f"https://{settings['vaultName']}.vault.azure.net/secrets/{plane}-tls", "allowedCidrs": ["10.50.0.0/26"]} for plane in ("api", "admin")}
        validate_config(config, "test")
        config["privateIngress"]["api"]["tlsSecretId"] = config["privateIngress"]["admin"]["tlsSecretId"]
        with self.assertRaises(ValueError):
            certificate_vault_parameters(config)
        with self.assertRaises(ValueError):
            validate_config(config, "test")

    def test_certificate_dns_ownership_does_not_revert_at_stage5(self):
        config = certificate_config()
        config["parameters"]["platform"]["stage5Data"] = {"postgresqlDatabaseName": "litellm"}
        _, parameters = parameters_for(config, 5, "platform")
        self.assertFalse(parameters["parameters"]["createStage5KeyVaultPrivateDnsZone"]["value"])
        self.assertFalse(parameters["parameters"]["configureStage5KeyVaultDnsLink"]["value"])
        config["parameters"]["platform"]["createStage5KeyVaultPrivateDnsZone"] = True
        with self.assertRaisesRegex(ValueError, "owns Key Vault DNS"):
            parameters_for(config, 5, "platform")
        config["parameters"].pop("certificate-vault")
        _, parameters = parameters_for(config, 5, "platform")
        self.assertTrue(parameters["parameters"]["createStage5KeyVaultPrivateDnsZone"]["value"])
        self.assertNotIn("configureStage5KeyVaultDnsLink", parameters["parameters"])

    def test_certificate_preflight_blocks_vault_adoption_custom_dns_and_link_conflicts(self):
        from scripts.certificate_vault import inspect_certificate_infrastructure, ZONE_NAME
        config = certificate_config()
        settings = config["parameters"]["certificate-vault"]
        mutations = (
            lambda state: state["vaults"].append({"name": settings["vaultName"], "tags": {"purpose": "backend"}}),
            lambda state: state["dnsServers"].append("10.50.0.10"),
            lambda state: state["links"].append({"name": "other-link", "virtualNetwork": {"id": settings["runnerVirtualNetworkId"]}}),
            lambda state: state["links"].append({"name": "certificate-runner-link", "registrationEnabled": True, "virtualNetwork": {"id": settings["runnerVirtualNetworkId"]}}),
        )
        for mutate in mutations:
            azure, state = self.certificate_azure(config)
            state["zones"] = [{"name": ZONE_NAME}]
            mutate(state)
            with self.subTest(mutation=mutate), self.assertRaises(ValueError):
                inspect_certificate_infrastructure(config, azure)
        config["parameters"]["certificate-vault"]["createPrivateDnsZone"] = False
        azure, _ = self.certificate_azure(config)
        with self.assertRaisesRegex(ValueError, "private DNS zone first"):
            inspect_certificate_infrastructure(config, azure)

    def test_certificate_preflight_reuses_owned_resources_and_same_vnet(self):
        from scripts.certificate_vault import certificate_resource_ids, inspect_certificate_infrastructure, ZONE_NAME
        config = certificate_config()
        settings = config["parameters"]["certificate-vault"]
        azure, state = self.certificate_azure(config)
        state["vaults"] = [{"name": settings["vaultName"], "tags": {"purpose": "ingress-certificates"}}]
        state["zones"] = [{"name": ZONE_NAME}]
        state["links"] = [{"name": "certificate-runner-link", "registrationEnabled": False, "virtualNetwork": {"id": settings["runnerVirtualNetworkId"]}}]
        inspect_certificate_infrastructure(config, azure)
        settings.update(manageRunnerDnsLink=False, manageTargetDnsLink=False)
        state["dnsServers"] = ["10.50.0.10"]
        inspect_certificate_infrastructure(config, azure)
        settings["runnerVirtualNetworkId"] = group_id(config) + "/providers/Microsoft.Network/virtualNetworks/target-vnet"
        settings.update(manageRunnerDnsLink=True, manageTargetDnsLink=True)
        self.assertFalse(any("certificate-runner-link" in identifier for identifier in certificate_resource_ids(config)))

    def test_certificate_deployment_requires_evidence_and_matching_plan_without_reading_secrets(self):
        config = certificate_config()
        azure, _ = self.certificate_azure(config)
        records = [{"stage": stage, "environment": "test", "binding": "stage-config", "configSha256": stage_fingerprint(config, stage), "revision": self.revision, "status": "passed", "checks": stage_checks(stage, config), "approvedBy": [config["azure"]["tenantId"], config["azure"]["subscriptionId"]], "observedAt": datetime.now(timezone.utc).isoformat(), "reportUrl": "https://evidence.customer.invalid/report", "reportSha256": "b" * 64} for stage in range(4)]
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as directory, patch("scripts.migration_deploy.subprocess.run", side_effect=self.compile):
            plan = deploy_component(config, 4, "certificate-vault", self.revision, "plan", [], directory, azure=azure)
            self.assertFalse(any("create" in call.args[0] for call in azure.scoped.call_args_list))
            with self.assertRaisesRegex(ValueError, "Prior stage evidence"):
                deploy_component(config, 4, "certificate-vault", self.revision, "deploy", [], directory, plan["planSha256"], azure)
            with self.assertRaisesRegex(ValueError, "Plan changed"):
                deploy_component(config, 4, "certificate-vault", self.revision, "deploy", records, directory, "f" * 64, azure)
            deploy_component(config, 4, "certificate-vault", self.revision, "deploy", records, directory, plan["planSha256"], azure)
            creates = [call.args[0] for call in azure.scoped.call_args_list if call.args[0][:3] == ["deployment", "group", "create"]]
            self.assertEqual(len(creates), 1)
            self.assertIn("Incremental", creates[0])
            self.assertFalse(any("secret" in call.args[0] for call in azure.scoped.call_args_list))

    def test_plan_accepts_null_what_if_delta_without_changing_reviewed_response(self):
        changes = [{**self.change, "delta": None}]
        original = copy.deepcopy(changes)
        plan = build_plan(self.config, 0, "bootstrap", self.revision, "b" * 64, {}, changes)
        self.assertEqual(plan["changes"][0]["changedProperties"], [])
        self.assertEqual(changes, original)
        changed = copy.deepcopy(changes)
        changed[0]["after"]["location"] = "eastus"
        self.assertNotEqual(plan["planSha256"], build_plan(self.config, 0, "bootstrap", self.revision, "b" * 64, {}, changed)["planSha256"])
        modified = [{**self.change, "changeType": "Modify", "delta": [{"path": "tags.owner", "propertyChangeType": "Create", "after": "synthetic-owner"}]}]
        self.assertEqual(build_plan(self.config, 0, "bootstrap", self.revision, "b" * 64, {}, modified)["changes"][0]["changedProperties"], ["tags.owner"])

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