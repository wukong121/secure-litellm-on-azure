"""Offline execution contracts for shared migration and greenfield workflow paths."""

import copy
from datetime import datetime, timezone
import io
import json
import unittest
from unittest.mock import patch
import zipfile

from scripts.customer_migration import active_stages, validate_config, validate_evidence
from scripts.migration_evidence import draft_report, record_evidence
from scripts.migration_runtime import validate_action
from scripts.workflow_artifacts import load_evidence
from tests.test_customer_migration import customer_config


class DeliveryPathTests(unittest.TestCase):
    def configuration(self, mode):
        config = customer_config()
        config["deploymentMode"] = mode
        if mode == "greenfield":
            config.pop("legacy")
            config["parameters"].pop("monitoring")
        config["governance"] = {"approvalMode": "single-operator", "approverObjectIds": ["99999999-9999-4999-8999-999999999999"], "singleOperatorRiskAccepted": True}
        return validate_config(config, "test")

    @patch.dict("os.environ", {"GITHUB_REPOSITORY": "synthetic/gateway", "GITHUB_REF": "refs/heads/main"})
    def test_recorded_artifacts_flow_between_every_applicable_stage_without_secret_replacement(self):
        revision = "a" * 40
        now = datetime.now(timezone.utc)
        for mode in ("migration", "greenfield"):
            with self.subTest(mode=mode):
                config = self.configuration(mode)
                ledger, artifacts = [], {}
                def api(route):
                    if "/workflows/" in route:
                        return json.dumps({"workflow_runs": [{"id": run} for run in sorted(artifacts, reverse=True)]}).encode()
                    if route.endswith("/zip"):
                        run = int(route.split("/artifacts/")[1].split("/")[0])
                        return artifacts[run][1]
                    if "artifacts?" in route:
                        run = int(route.split("/runs/")[1].split("/")[0])
                        return json.dumps({"total_count": 1, "artifacts": [{"id": run, "name": artifacts[run][0], "expired": False, "size_in_bytes": len(artifacts[run][1])}]}).encode()
                    return json.dumps({"event": "workflow_dispatch", "conclusion": "success", "head_sha": revision, "head_branch": "main", "path": ".github/workflows/customer-acceptance.yml", "repository": {"full_name": "synthetic/gateway"}}).encode()
                for stage in active_stages(config):
                    self.assertEqual(load_evidence(config, stage, revision, api=api), ledger)
                    report = draft_report(config, stage, revision)
                    report["observedAt"] = now.isoformat()
                    with self.assertRaisesRegex(ValueError, "not passed"):
                        record_evidence(config, stage, revision, ledger, report, "https://synthetic.invalid/reports", config["governance"]["approverObjectIds"], now)
                    for result in report["checks"].values():
                        result.update(status="passed", evidence="Synthetic offline contract result only; never customer acceptance")
                    ledger = record_evidence(config, stage, revision, ledger, report, "https://synthetic.invalid/reports", config["governance"]["approverObjectIds"], now)
                    archive = io.BytesIO()
                    with zipfile.ZipFile(archive, "w") as bundle:
                        bundle.writestr("migration-evidence.json", json.dumps(ledger))
                    run = 100 + stage
                    artifacts[run] = (f"acceptance-record-test-{stage}-{run}", archive.getvalue())
                validate_evidence(ledger, 10, config, revision, now)
                self.assertEqual([entry["stage"] for entry in ledger], list(active_stages(config)))
                changed = copy.deepcopy(config)
                changed["baseDomain"] = "different.synthetic.invalid"
                with self.assertRaises(ValueError):
                    load_evidence(changed, 10, revision, api=api)

    def test_mode_and_stage_boundaries_for_all_new_runtime_actions(self):
        actions = {"certificate-renew": 4, "entra-revoke": 7, "admin-credentials-session-rotate": 7, "admin-credentials-retire-expired": 7, "audit-pause": 8, "audit-recover": 8, "audit-resume": 8, "dns-publish": 9, "dns-rollback": 9}
        for mode in ("migration", "greenfield"):
            config = self.configuration(mode)
            for action, owner in actions.items():
                validate_action(config, owner, action)
                with self.assertRaises(ValueError):
                    validate_action(config, 3, action)
            if mode == "greenfield":
                for action, stage in (("backup-restore", 0), ("legacy-hardening", 1), ("restore-target", 5)):
                    with self.assertRaises(ValueError):
                        validate_action(config, stage, action)