import unittest
from datetime import datetime, timezone
from unittest.mock import patch
import json

from scripts.customer_migration import main, stage_fingerprint
from scripts.migration_evidence import draft_report, record_evidence
from tests.test_customer_migration import customer_config


class MigrationEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.config = customer_config()
        self.operator = "11111111-1111-4111-8111-111111111111"
        self.config["governance"] = {"approvalMode": "single-operator", "approverObjectIds": [self.operator], "singleOperatorRiskAccepted": True}
        self.revision = "a" * 40
        self.now = datetime(2026, 9, 8, tzinfo=timezone.utc)
        self.report = draft_report(self.config, 0, self.revision)
        self.report["observedAt"] = self.now.isoformat()

    def record(self, report):
        return record_evidence(self.config, 0, self.revision, [], report, "https://evidence.customer.invalid/stage0", [self.operator], self.now)

    def test_pending_report_never_creates_passed_evidence(self):
        self.assertEqual(self.report["configSha256"], stage_fingerprint(self.config, 0))
        with self.assertRaisesRegex(ValueError, "has not passed"):
            self.record(self.report)

    def test_report_requires_results_even_when_status_says_passed(self):
        for result in self.report["checks"].values():
            result["status"] = "passed"
        with self.assertRaisesRegex(ValueError, "Actual test results"):
            self.record(self.report)

    def test_complete_reviewed_report_generates_stage_bound_record(self):
        for result in self.report["checks"].values():
            result.update(status="passed", evidence="Synthetic unit-test result; not customer acceptance")
        ledger = self.record(self.report)
        self.assertEqual(ledger[0]["approvedBy"], [self.operator])
        self.assertEqual(ledger[0]["binding"], "stage-config")
        self.assertEqual(ledger[0]["approvalMode"], "single-operator")
        self.report["revision"] = "b" * 40
        with self.assertRaises(ValueError):
            self.record(self.report)

    def test_config_check_does_not_require_evidence_or_call_azure(self):
        environment = {"CUSTOMER_CONFIG_JSON": json.dumps(self.config), "AZURE_TENANT_ID": self.config["azure"]["tenantId"], "AZURE_SUBSCRIPTION_ID": self.config["azure"]["subscriptionId"]}
        with patch.dict("os.environ", environment, clear=True), patch("sys.argv", ["migration", "--stage", "1", "--mode", "config-check", "--environment", "test", "--component", "monitoring"]), patch("scripts.customer_migration.subprocess.run") as cloud:
            main()
            cloud.assert_not_called()

    def test_preflight_reads_the_same_automatic_ledger_without_cloud_operations(self):
        environment = {"CUSTOMER_CONFIG_JSON": json.dumps(self.config), "AZURE_TENANT_ID": self.config["azure"]["tenantId"], "AZURE_SUBSCRIPTION_ID": self.config["azure"]["subscriptionId"], "GITHUB_SHA": self.revision, "MIGRATION_AUTO_EVIDENCE": "true"}
        with patch.dict("os.environ", environment, clear=True), patch("sys.argv", ["migration", "--stage", "0", "--mode", "preflight", "--environment", "test"]), patch("scripts.workflow_artifacts.load_evidence", return_value=[]) as ledger, patch("scripts.customer_migration.subprocess.run") as cloud:
            main()
            ledger.assert_called_once_with(self.config, 0, self.revision)
            cloud.assert_not_called()

    def test_rerecording_invalidates_current_and_later_acceptance(self):
        for result in self.report["checks"].values():
            result.update(status="passed", evidence="Synthetic revalidation result, not a real customer report")
        old = [{"stage": 0, "status": "expired"}, {"stage": 1, "status": "expired"}]
        records = record_evidence(self.config, 0, self.revision, old, self.report, "https://evidence.customer.invalid/stage0", [self.operator], self.now)
        self.assertEqual([record["stage"] for record in records], [0])

    def test_greenfield_records_stage_zero_then_two_without_legacy_evidence(self):
        self.config["deploymentMode"] = "greenfield"
        self.config.pop("legacy")
        self.config["parameters"].pop("monitoring")
        records = []
        for stage in (0, 2):
            report = draft_report(self.config, stage, self.revision)
            report["observedAt"] = self.now.isoformat()
            for result in report["checks"].values():
                result.update(status="passed", evidence="Synthetic greenfield unit-test result only")
            records = record_evidence(self.config, stage, self.revision, records, report, "https://evidence.customer.invalid/greenfield", [self.operator], self.now)
        self.assertEqual([record["stage"] for record in records], [0, 2])
        with self.assertRaisesRegex(ValueError, "does not apply"):
            draft_report(self.config, 1, self.revision)

    def test_switching_deployment_mode_cannot_reuse_migration_evidence(self):
        for result in self.report["checks"].values():
            result.update(status="passed", evidence="Synthetic migration report, not customer evidence")
        records = self.record(self.report)
        self.config["deploymentMode"] = "greenfield"
        self.config.pop("legacy")
        self.config["parameters"].pop("monitoring")
        report = draft_report(self.config, 2, self.revision)
        with self.assertRaisesRegex(ValueError, "configuration or revision mismatch"):
            record_evidence(self.config, 2, self.revision, records, report, "https://evidence.customer.invalid/greenfield", [self.operator], self.now)