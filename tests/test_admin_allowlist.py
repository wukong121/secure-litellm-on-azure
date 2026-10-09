import copy
import json
from pathlib import Path
import tempfile
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from local_execution.admin_allowlist import desired_configuration, refresh_binding, update
from scripts.customer_migration import ROOT, MigrationError, stage_fingerprint
from scripts.migration_deploy import assert_allowlist_changes, deploy_component, prepare_allowlist_template
from tests.test_customer_migration import customer_config


class AdminAllowlistTests(unittest.TestCase):
    def setUp(self):
        self.config = customer_config()
        self.config["parameters"]["edge"]["adminAllowedCidrs"] = ["167.220.232.6/32"]
        self.additions = ["111.193.185.231/32"]

    def test_adds_exact_ip_without_mutating_other_configuration(self):
        before = copy.deepcopy(self.config)
        desired = desired_configuration(self.config, self.additions)
        self.assertEqual(self.config, before)
        self.assertEqual(desired["parameters"]["edge"]["adminAllowedCidrs"],
                         ["111.193.185.231/32", "167.220.232.6/32"])
        desired["parameters"]["edge"]["adminAllowedCidrs"] = before["parameters"]["edge"]["adminAllowedCidrs"]
        self.assertEqual(desired, before)

    def test_rejects_private_broad_duplicate_and_invalid_addresses(self):
        for value in ("10.0.0.1/32", "0.0.0.0/0", "111.193.185.0/24",
                      "167.220.232.6/32", "invalid", "111.193.185.231/24"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                desired_configuration(self.config, [value])
        with self.assertRaises(MigrationError):
            desired_configuration(self.config, self.additions * 2)

    def test_only_admin_waf_custom_rules_can_change(self):
        edge = {"adminWafId": "/synthetic/admin-waf", "apiTrafficEnabled": True, "adminTrafficEnabled": True}
        change = {"changeType": "Modify", "resourceId": edge["adminWafId"],
                  "delta": [{"path": "properties.customRules.rules[0].matchConditions[0].matchValue"}]}
        assert_allowlist_changes([change, {"changeType": "NoChange"}], edge)
        for value in (
            {**change, "changeType": "Delete"},
            {**change, "changeType": "Create"},
            {**change, "resourceId": "/synthetic/api-route"},
            {**change, "delta": []},
            {**change, "delta": [{"path": "properties.policySettings.mode"}]},
            {**change, "delta": [{"path": "properties.managedRules"}]},
            {**change, "delta": [{"path": "properties.customRules.rules[0].action"}]},
            {**change, "delta": [{"path": "properties.customRules.rules[1].matchConditions[0].matchValue"}]},
            {**change, "delta": [{"path": "properties.customRules.rules"}]},
        ):
            with self.subTest(value=value), self.assertRaises(MigrationError):
                assert_allowlist_changes([value], edge)
        with self.assertRaises(MigrationError):
            assert_allowlist_changes([change], {**edge, "adminTrafficEnabled": False})

    def test_nested_azure_array_deltas_are_checked_at_leaf_level(self):
        edge = {"adminWafId": "/synthetic/admin-waf", "apiTrafficEnabled": True, "adminTrafficEnabled": True}
        delta = {"path": "properties.customRules.rules", "children": [
            {"path": "0", "children": [
                {"path": "matchConditions", "children": [
                    {"path": "0", "children": [
                        {"path": "matchValue", "children": [
                            {"path": "0", "propertyChangeType": "Create", "after": "111.193.185.231/32"}
                        ]}
                    ]}
                ]}
            ]}
        ]}
        change = {"changeType": "Modify", "resourceId": edge["adminWafId"], "delta": [delta]}
        assert_allowlist_changes([change], edge)
        delta["children"].append({"path": "2", "children": [{"path": "groupBy", "propertyChangeType": "Delete"}]})
        with self.assertRaisesRegex(MigrationError, "granular"):
            assert_allowlist_changes([change], edge)

    def test_focused_template_preserves_live_defaults_and_only_deploys_admin_waf(self):
        desired = desired_configuration(self.config, self.additions)
        waf_id = (f"/subscriptions/{self.config['azure']['subscriptionId']}"
                  "/resourceGroups/rg-secure/providers/Microsoft.Network/frontDoorWebApplicationFirewallPolicies/admin")
        live = {
            "id": waf_id, "location": "Global", "sku": {"name": "Premium_AzureFrontDoor", "tier": None},
            "tags": {"owner": "test"},
            "properties": {
                "provisioningState": "Succeeded", "resourceState": "Enabled",
                "frontendEndpointLinks": [], "securityPolicyLinks": [{"id": "/synthetic/association"}],
                "policySettings": {"mode": "Prevention", "javascriptChallengeExpirationInMinutes": 30},
                "managedRules": {"managedRuleSets": [{"ruleSetVersion": "2.1"}]},
                "customRules": {"rules": [
                    {"name": "BlockUnapprovedAdminSources", "matchConditions": [
                        {"matchVariable": "SocketAddr", "matchValue": ["167.220.232.6/32"]}]},
                    {"name": "BlockUnsafeMethods"},
                    {"name": "RateLimitAdmin", "groupBy": [{"variableName": "SocketAddr"}]},
                ]},
            },
        }
        before = copy.deepcopy(live)
        edge = {"adminWafId": waf_id, "adminAllowedCidrs": ["167.220.232.6/32"],
                "apiTrafficEnabled": True, "adminTrafficEnabled": True, "endpointHost": "api.azurefd.net"}
        azure = Mock()
        azure.scoped.return_value = live
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as name:
            compiled = prepare_allowlist_template(desired, edge, azure, Path(name),
                                                  {"parameters": {"enableApiTraffic": {"value": True}}})
            template = json.loads(compiled.read_text())
        self.assertEqual(live, before)
        self.assertEqual(len(template["resources"]), 1)
        resource = template["resources"][0]
        self.assertEqual(resource["name"], "admin")
        self.assertEqual(resource["properties"]["policySettings"], live["properties"]["policySettings"])
        self.assertEqual(resource["properties"]["customRules"]["rules"][2],
                         live["properties"]["customRules"]["rules"][2])
        self.assertEqual(resource["properties"]["customRules"]["rules"][0]["matchConditions"][0]["matchValue"],
                         desired["parameters"]["edge"]["adminAllowedCidrs"])
        self.assertNotIn("securityPolicyLinks", resource["properties"])
        self.assertEqual(template["parameters"], {"enableApiTraffic": {"type": "bool"}})
        output = template["outputs"]["edge"]["value"]
        self.assertTrue(output["apiTrafficEnabled"])
        self.assertTrue(output["adminTrafficEnabled"])
        self.assertEqual(output["endpointHost"], edge["endpointHost"])
        self.assertEqual(output["adminAllowedCidrs"], desired["parameters"]["edge"]["adminAllowedCidrs"])

    def test_baseline_cannot_hide_unrelated_configuration_changes(self):
        desired = desired_configuration(self.config, self.additions)
        desired["baseDomain"] = "another.invalid"
        with self.assertRaisesRegex(MigrationError, "only add"):
            deploy_component(desired, 9, "edge", "a" * 40, "plan", [], ROOT / "temp/unused",
                             release={"phase": "canary"}, allowlist_baseline=self.config)

    def test_baseline_cannot_remove_cidrs_or_use_production(self):
        desired = copy.deepcopy(self.config)
        desired["parameters"]["edge"]["adminAllowedCidrs"] = self.additions
        with self.assertRaisesRegex(MigrationError, "only add"):
            deploy_component(desired, 9, "edge", "a" * 40, "plan", [], ROOT / "temp/unused",
                             release={}, allowlist_baseline=self.config)
        desired = desired_configuration(self.config, self.additions)
        with self.assertRaisesRegex(MigrationError, "native dev/test"):
            deploy_component(desired, 9, "edge", "a" * 40, "plan", [], ROOT / "temp/unused",
                             release={"phase": "production"}, allowlist_baseline=self.config)

    def test_receipt_refresh_cannot_modify_ingress(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as name:
            root = Path(name)
            state = {"currentRouteConfigSha256": "a", "desiredRouteConfigSha256": "a",
                     "currentPodTemplateSha256": "b", "desiredPodTemplateSha256": "b"}
            review = {"bindingMode": "native-private-ingress", "planes": {"api": state, "admin": state}}

            def plan(*args, **kwargs):
                (args[3] / "runtime-review.json").write_text(json.dumps(review))
                return {"planSha256": "c" * 64}

            with patch("local_execution.admin_allowlist.bind_edge", side_effect=plan) as bind:
                refresh_binding(self.config, "a" * 40, root / "binding", Mock())
                self.assertEqual(bind.call_count, 2)
                self.assertEqual(bind.call_args.args[1], "execute")
                self.assertEqual(bind.call_args.args[4], "c" * 64)
                state["desiredRouteConfigSha256"] = "different"
                bind.reset_mock()
                with self.assertRaisesRegex(MigrationError, "must not change"):
                    refresh_binding(self.config, "a" * 40, root / "other-binding", Mock())
                self.assertEqual(bind.call_count, 1)

    def run_update(self, operation, approved="", failure=None):
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as name:
            root = Path(name)
            config_path = root / "customer.json"
            config_path.write_text(json.dumps(self.config))
            settings = {"releaseReportPath": str(root / "release.json")}
            directory = root / "run"
            directory.mkdir()
            summary = {"planSha256": "a" * 64}
            report = {"adminAllowedCidrs": self.config["parameters"]["edge"]["adminAllowedCidrs"]}
            events = []
            def binding(*args):
                persisted = json.loads((directory / "allowlist-summary.json").read_text())
                self.assertTrue(persisted["deploymentPerformed"])
                self.assertFalse(persisted["bindingRefreshed"])
                self.assertEqual(persisted["status"], "executing")
                if failure:
                    raise failure
                events.append("bind")
            def deploy(*args, **kwargs):
                (directory / "plan").mkdir()
                self.assertEqual(kwargs["allowlist_baseline"], self.config)
                return summary
            with patch("local_execution.admin_allowlist.load_config", return_value=(self.config, settings)), \
                    patch("local_execution.admin_allowlist.reviewed_revision", return_value="b" * 40), \
                    patch("local_execution.admin_allowlist.subprocess.run", return_value=Mock(returncode=0)), \
                    patch("local_execution.admin_allowlist.operation_root", return_value=directory), \
                    patch("local_execution.admin_allowlist.authenticate_azure"), \
                    patch("local_execution.admin_allowlist.AzureCommands"), \
                    patch("local_execution.admin_allowlist.generate_report", return_value=report), \
                    patch("local_execution.admin_allowlist.deploy_component", side_effect=deploy), \
                    patch("local_execution.admin_allowlist.execute_infrastructure_plan",
                          side_effect=lambda *args: events.append("deploy")), \
                    patch("local_execution.admin_allowlist.install_report",
                          side_effect=lambda *args, **kwargs: events.append("persist")), \
                    patch("local_execution.admin_allowlist.refresh_binding",
                          side_effect=binding), \
                    patch("builtins.print") as output:
                if failure:
                    with self.assertRaisesRegex(MigrationError, "refresh failed"):
                        update(config_path, self.additions, "approved-change", ["owner"], operation, approved)
                    outcome = json.loads((directory / "allowlist-summary.json").read_text())
                elif operation == "execute" and approved != "a" * 64:
                    with self.assertRaisesRegex(MigrationError, "not approved"):
                        update(config_path, self.additions, "approved-change", ["owner"], operation, approved)
                    outcome = {}
                else:
                    outcome = update(config_path, self.additions, "approved-change", ["owner"], operation, approved)
                messages = [call.args[0] for call in output.call_args_list
                            if call.kwargs.get("file") is sys.stderr]
                self.assertTrue(any("What-if" in message for message in messages))
                self.assertTrue(all(call.kwargs.get("flush") is True for call in output.call_args_list))
                if operation == "execute" and approved == "a" * 64:
                    self.assertTrue(any("No terminal confirmation" in message for message in messages))
                    self.assertTrue(any("overwrite is automatic" in message for message in messages))
            return events, outcome

    def test_plan_never_deploys_or_persists_customer_configuration(self):
        events, result = self.run_update("plan")
        self.assertEqual(events, [])
        self.assertFalse(result["deploymentPerformed"])

    def test_execute_requires_reviewed_hash(self):
        events, _ = self.run_update("execute", "wrong")
        self.assertEqual(events, [])

    def test_execute_persists_config_report_and_refreshes_binding_after_deploy(self):
        events, result = self.run_update("execute", "a" * 64)
        self.assertEqual(events, ["deploy", "persist", "persist", "bind"])
        self.assertTrue(result["bindingRefreshed"])
        self.assertEqual(result["status"], "completed")

    def test_partial_failure_is_explicit_and_records_applied_state(self):
        events, result = self.run_update("execute", "a" * 64, MigrationError("refresh failed"))
        self.assertTrue(result["deploymentPerformed"])
        self.assertFalse(result["bindingRefreshed"])
        self.assertNotEqual(result["status"], "completed")
        self.assertEqual(events, ["deploy", "persist", "persist"])

    def test_real_deployment_plan_uses_old_binding_and_keeps_both_traffic_gates(self):
        self.config["application"] = {"authentication": {"mode": "native", "adminUsername": "gateway-admin"}}
        self.config["contentAudit"] = {}
        desired = desired_configuration(self.config, self.additions)
        revision = "b" * 40
        identifier = "11111111-1111-4111-8111-111111111111"
        waf = (f"/subscriptions/{self.config['azure']['subscriptionId']}"
               "/resourceGroups/rg-secure/providers/Microsoft.Network/frontDoorWebApplicationFirewallPolicies/admin")
        report = {
            **desired["parameters"]["edge"], "phase": "canary", "environmentName": "test",
            "baseDomain": self.config["baseDomain"], "authenticationMode": "native",
            "auditMode": "native", "telemetryEnabled": False, "frontDoorId": identifier,
            "changeTicket": "approved-change", "approvedBy": [
                identifier, "22222222-2222-4222-8222-222222222222"],
            "rateLimitPerMinute": 600, "adminRateLimitPerMinute": 120, "wafMode": "Detection",
            "revision": revision, "configSha256": stage_fingerprint(desired, 9),
        }
        change = {"changeType": "Modify", "resourceId": waf,
                  "delta": [{"path": "properties.customRules.rules[0].matchConditions[0].matchValue"}]}
        azure = Mock()
        azure.run.return_value = {"tenantId": self.config["azure"]["tenantId"],
                                 "id": self.config["azure"]["subscriptionId"]}
        azure.scoped.side_effect = [
            {"state": "Succeeded", "edge": {"profileId": identifier, "adminWafId": waf,
                                          "apiTrafficEnabled": True, "adminTrafficEnabled": True,
                                          "adminAllowedCidrs": ["167.220.232.6/32"]}},
            {"id": waf, "location": "Global", "sku": {"name": "Premium_AzureFrontDoor"},
             "properties": {"provisioningState": "Succeeded", "policySettings": {"mode": "Prevention"},
                            "managedRules": {}, "customRules": {"rules": [
                                {"name": "BlockUnapprovedAdminSources",
                                 "matchConditions": [{"matchValue": ["167.220.232.6/32"]}]}]}}},
            {"status": "Succeeded", "changes": [change]},
        ]

        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as name:
            directory = Path(name)
            parameters = directory / "parameters.json"
            parameters.write_text(json.dumps({"parameters": {}}))
            compiled = directory / "template.json"

            def compile_template(*args, **kwargs):
                compiled.write_text("{}")
                return SimpleNamespace(returncode=0, stderr="")

            with patch("scripts.migration_deploy.validate_config"), \
                    patch("scripts.migration_deploy.resolve_origin", return_value=desired), \
                    patch("scripts.migration_deploy.prepare", return_value=(directory / "edge.bicep", parameters)), \
                    patch("scripts.migration_deploy.subprocess.run", side_effect=compile_template), \
                    patch("scripts.migration_runtime.connect_cluster", return_value=["kubectl", "--namespace", "litellm"]), \
                    patch("scripts.audit_runtime.AuditCluster"), \
                    patch("scripts.edge_binding.require_edge_binding") as binding, \
                    patch("scripts.private_ingress_runtime.require_private_ingress_backends") as backend, \
                    patch("builtins.print"):
                plan = deploy_component(desired, 9, "edge", revision, "plan", [], directory,
                                        azure=azure, release=report, allowlist_baseline=self.config)
            self.assertEqual(binding.call_args.args[0], self.config)
            self.assertEqual(backend.call_args.args[0], self.config)
            self.assertEqual(backend.call_args.kwargs["front_door_id"], identifier)
            self.assertEqual(len(plan["planSha256"]), 64)
            actual = json.loads(parameters.read_text())["parameters"]
            self.assertTrue(actual["enableApiTraffic"]["value"])
            self.assertTrue(actual["enableAdminTraffic"]["value"])
            self.assertEqual(actual["wafMode"]["value"], "Detection")
            self.assertFalse(any("create" in call.args[0] for call in azure.scoped.call_args_list))


if __name__ == "__main__":
    unittest.main()
