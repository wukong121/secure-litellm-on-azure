import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

from scripts.customer_migration import ROOT, STAGES, MigrationError, fingerprint, main, parameters_for, prepare, validate_config, validate_evidence, preview


def customer_config():
    return {
        "schemaVersion": 1, "environment": "test", "location": "westus",
        "azure": {"tenantId": "11111111-1111-4111-8111-111111111111", "subscriptionId": "22222222-2222-4222-8222-222222222222"},
        "baseDomain": "customer.invalid", "ownerEmail": "owner@customer.invalid",
        "legacy": {"resourceGroup": "rg-legacy", "aksClusterName": "old-aks", "namespace": "litellm", "postgresPvc": "pg-data"},
        "target": {"resourceGroup": "rg-secure"},
        "parameters": {"platform": {"containerRegistryName": "customerregistry", "logAnalyticsWorkspaceName": "customer-logs", "stage4Network": {"virtualNetworkName": "target-vnet"}, "stage4Aks": {"name": "new-aks"}}, "monitoring": {"logAnalyticsWorkspaceName": "legacy-logs"}, "edge": {"privateOrigin": {"privateLinkServiceId": "/synthetic/pls", "privateLinkLocation": "westus"}, "logAnalyticsWorkspaceName": "target-logs"}},
    }


class CustomerMigrationTests(unittest.TestCase):
    def setUp(self):
        self.config = customer_config()
        self.revision = "a" * 40
        self.now = datetime(2026, 9, 7, tzinfo=timezone.utc)
        self.record = {"stage": 0, "environment": "test", "configSha256": fingerprint(self.config), "revision": self.revision, "status": "passed", "checks": list(STAGES[0][1]), "approvedBy": ["11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"], "observedAt": "2026-09-07T00:00:00Z", "reportUrl": "https://evidence.customer.invalid/report", "reportSha256": "b" * 64}

    def test_environment_and_target_isolation(self):
        validate_config(self.config, "test")
        with self.assertRaises(ValueError):
            validate_config(self.config, "prod")
        self.config["target"]["resourceGroup"] = "RG-LEGACY"
        with self.assertRaises(ValueError):
            validate_config(self.config, "test")

    def test_placeholders_and_reserved_overrides_rejected(self):
        for mutate in (lambda config: config.update(ownerEmail="owner@example.com"), lambda config: config["parameters"]["platform"].update(deployStage5=True), lambda config: config["parameters"]["platform"].update(databasePassword="synthetic")):
            config = copy.deepcopy(self.config)
            mutate(config)
            with self.assertRaises(ValueError):
                validate_config(config, "test")

    def test_platform_flags_are_selected_by_stage(self):
        self.config["parameters"]["platform"]["stage5Data"] = {"postgresqlEntraAdministratorObjectId": "REPLACE_LATER"}
        for stage in (3, 4):
            _template, document = parameters_for(self.config, stage, "platform")
            values = {key: item["value"] for key, item in document["parameters"].items()}
            self.assertEqual(values["deployStage4"], stage >= 4)
            self.assertFalse(values["deployStage5"])
            self.assertNotIn("stage5Data", values)
            self.assertEqual(values["containerRegistryPublicNetworkAccess"], "Disabled")
        with self.assertRaises(ValueError):
            parameters_for(self.config, 5, "platform")

    def test_tenant_or_subscription_change_invalidates_evidence(self):
        self.config["azure"]["subscriptionId"] = "33333333-3333-4333-8333-333333333333"
        with self.assertRaises(ValueError):
            validate_evidence([self.record], 1, self.config, self.revision, self.now)

    def test_cli_preflight_uses_environment_config_without_cloud_calls(self):
        environment = {"CUSTOMER_CONFIG_JSON": json.dumps(self.config), "MIGRATION_EVIDENCE_JSON": "[]", "MIGRATION_REVISION": self.revision, "AZURE_TENANT_ID": self.config["azure"]["tenantId"], "AZURE_SUBSCRIPTION_ID": self.config["azure"]["subscriptionId"]}
        with patch.dict("os.environ", environment, clear=True), patch("sys.argv", ["migration", "--stage", "0", "--mode", "preflight", "--environment", "test"]), patch("scripts.customer_migration.subprocess.run") as cloud:
            main()
            cloud.assert_not_called()
            with patch.dict("os.environ", {"AZURE_SUBSCRIPTION_ID": "different-scope"}):
                with self.assertRaisesRegex(MigrationError, "OIDC scope"):
                    main()

    def test_edge_preview_never_enables_traffic(self):
        _template, document = parameters_for(self.config, 9, "edge")
        self.assertFalse(document["parameters"]["enableApiTraffic"]["value"])
        self.assertEqual(document["parameters"]["baseDomain"]["value"], self.config["baseDomain"])
        with self.assertRaises(ValueError):
            parameters_for(self.config, 8, "edge")

    def test_evidence_cannot_skip_stages(self):
        validate_evidence([self.record], 1, self.config, self.revision, self.now)
        with self.assertRaises(ValueError):
            validate_evidence([self.record], 2, self.config, self.revision, self.now)

    def test_evidence_bound_to_config_revision_approvals_checks_and_date(self):
        for updates in ({"configSha256": "c" * 64}, {"revision": "d" * 40}, {"checks": []}, {"observedAt": "2026-08-01T00:00:00Z"}, {"observedAt": "2026-09-08T00:00:00Z"}, {"approvedBy": [self.record["approvedBy"][0]] * 2}, {"reportUrl": "https://evidence.customer.invalid/report?sig=private"}, {"status": "pending"}):
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                validate_evidence([{**self.record, **updates}], 1, self.config, self.revision, self.now)

    def test_private_generated_parameters_and_no_workspace_escape(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as directory:
            _template, path = prepare(self.config, 1, "monitoring", Path(directory))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(path.read_text())["parameters"]["aksClusterName"]["value"], "old-aks")
        with self.assertRaises(ValueError):
            prepare(self.config, 1, "monitoring", ROOT / "deploy")

    def test_what_if_routes_monitoring_to_legacy_and_refuses_destructive_changes(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as directory:
            template, path = prepare(self.config, 1, "monitoring", Path(directory))
            for kind in ("Delete", "Modify", "Unsupported", "Unknown", "Ignore"):
                with patch("scripts.customer_migration.subprocess.run") as command:
                    command.return_value.returncode = 0
                    command.return_value.stdout = json.dumps({"status": "Succeeded", "changes": [{"changeType": kind}]})
                    command.return_value.stderr = ""
                    with self.assertRaises(ValueError):
                        preview(self.config, template, path, "monitoring")
                    self.assertIn("rg-legacy", command.call_args.args[0])
                    self.assertNotIn("create", command.call_args.args[0])

    def test_workflow_requires_environment_and_never_applies_or_uploads_customer_data(self):
        workflow = yaml.load((ROOT / ".github/workflows/customer-migration.yml").read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        self.assertEqual(workflow["on"]["workflow_dispatch"]["inputs"]["stage"]["options"], [str(stage) for stage in range(10)])
        for job in workflow["jobs"].values():
            self.assertEqual(job["environment"], "${{ inputs.environment }}")
            for step in job["steps"]:
                if "uses" in step:
                    self.assertRegex(step["uses"], r"@[0-9a-f]{40}$")
                    self.assertNotIn("upload-artifact", step["uses"])
                command = step.get("run", "")
                self.assertNotIn("${{", command)
                self.assertNotIn("deployment group create", command)
                self.assertNotIn("kubectl apply", command)
                self.assertNotIn("az network dns", command)
        self.assertEqual(workflow["jobs"]["preview"]["needs"], "prepare")
        self.assertEqual(workflow["jobs"]["preview"]["if"], "inputs.mode == 'what-if'")