import unittest
from datetime import datetime, timezone
from unittest.mock import patch
import json
import copy
from pathlib import Path
import tempfile

from scripts.customer_migration import ROOT, main, stage_fingerprint
from scripts.migration_evidence import checklist_summary, confirm_report, draft_report, main as evidence_main, record_evidence
from tests.test_customer_migration import customer_config


class MigrationEvidenceTests(unittest.TestCase):
    def test_confirm_cli_uses_draft_artifact_without_report_or_approver_secrets(self):
        self.config["governance"]["githubLogin"] = "migration-operator"
        draft = draft_report(self.config, 0, self.revision)
        environment = {"CUSTOMER_CONFIG_JSON": json.dumps(self.config), "GITHUB_SHA": self.revision, "MIGRATION_AUTO_EVIDENCE": "true",
                       "MIGRATION_CONFIRM_ENVIRONMENT": "test", "MIGRATION_REVIEWED_RUN_ID": "123", "MIGRATION_CHECKED_ITEMS": ",".join(draft["checks"]),
                       "MIGRATION_EVIDENCE_NOTES": "Synthetic manual acceptance contract, not a real customer result", "GITHUB_ACTOR": "migration-operator", "GITHUB_TRIGGERING_ACTOR": "migration-operator",
                       "GITHUB_ACTOR_ID": "456", "GITHUB_RUN_ID": "789", "GITHUB_RUN_ATTEMPT": "1", "GITHUB_REPOSITORY": "synthetic/gateway"}
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as directory, patch.dict("os.environ", environment, clear=True), patch("sys.argv", ["evidence", "--operation", "confirm", "--stage", "0", "--environment", "test", "--output-dir", directory]), patch("scripts.workflow_artifacts.load_evidence", return_value=[]), patch("scripts.workflow_artifacts.read_artifact", return_value={"acceptance-report.json": draft}) as load:
            evidence_main()
            load.assert_called_once_with(self.revision, "123", "customer-acceptance.yml", "acceptance-draft-test-0-123", ("acceptance-report.json",))
            record = json.loads((Path(directory) / "migration-evidence.json").read_text())[0]
            self.assertEqual(record["reportUrl"], "https://github.com/synthetic/gateway/actions/runs/789")
            self.assertEqual(record["confirmation"]["reviewedRunId"], "123")
            self.assertTrue((Path(directory) / "checklist.md").exists())
        self.assertIn("inventory,backup_restore", checklist_summary(self.config, 0, self.revision))

    def test_manual_confirmation_records_actual_operator_not_automatic_verification(self):
        self.config["governance"]["githubLogin"] = "migration-operator"
        draft = draft_report(self.config, 0, self.revision)
        draft["generatedAt"] = self.now.isoformat()
        actor = {"login": "migration-operator", "triggeringLogin": "migration-operator", "id": "123", "runId": "456", "runAttempt": "1"}
        selected = ",".join(draft["checks"])
        notes = "Synthetic manual confirmation test only; no customer checks executed"
        report = confirm_report(self.config, 0, self.revision, draft, selected, notes, actor, self.now)
        ledger = self.record(report)
        self.assertEqual(ledger[0]["approvedBy"], [self.operator])
        self.assertEqual(ledger[0]["confirmation"]["method"], "manual-workflow")
        self.assertFalse(ledger[0]["confirmation"]["independentlyVerified"])
        self.assertTrue(all(item["status"] == "pending" for item in draft["checks"].values()))
        for changes in ({"login": "other"}, {"triggeringLogin": "other"}, {"runId": ""}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                confirm_report(self.config, 0, self.revision, draft, selected, notes, {**actor, **changes}, self.now)
        for selection in ("", selected + ",inventory", "inventory", selected + ",unknown"):
            with self.subTest(selection=selection), self.assertRaises(ValueError):
                confirm_report(self.config, 0, self.revision, draft, selection, notes, actor, self.now)
        for changes in ({"revision": "b" * 40}, {"generatedAt": "2000-01-01T00:00:00Z"}, {"environment": "prod"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                confirm_report(self.config, 0, self.revision, {**draft, **changes}, selected, notes, actor, self.now)
        invalid = copy.deepcopy(self.config)
        invalid["governance"].pop("githubLogin")
        with self.assertRaisesRegex(ValueError, "githubLogin"):
            confirm_report(invalid, 0, self.revision, draft, selected, notes, actor, self.now)

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