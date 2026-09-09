import io
import json
import unittest
import zipfile
from unittest.mock import patch

from scripts.audit_plan import approved_audit_plan
from scripts.customer_migration import fingerprint
from tests.test_customer_migration import customer_config


class AuditPlanTests(unittest.TestCase):
    def fixture(self, changes=None):
        config = customer_config()
        plan = {"scope": {"configSha256": "b" * 64}, "windowPlan": {}}
        summary = {"stage": 8, "action": "audit-pause", "revision": "a" * 40, "applied": False, "planSha256": fingerprint(plan)}
        summary.update(changes or {})
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("runtime-summary.json", json.dumps(summary))
            bundle.writestr("runtime-review.json", json.dumps(plan))
        run = {"event": "workflow_dispatch", "conclusion": "success", "head_sha": "a" * 40, "head_branch": "main", "path": ".github/workflows/customer-runtime.yml", "repository": {"full_name": "synthetic/gateway"}}
        def api(path):
            if path.endswith("/zip"):
                return archive.getvalue()
            if "artifacts?" in path:
                return json.dumps({"total_count": 1, "artifacts": [{"name": f"runtime-{config['environment']}-8-123", "id": 456, "expired": False, "size_in_bytes": 1024}]}).encode()
            return json.dumps(run).encode()
        return config, plan, run, api

    @patch.dict("os.environ", {"GITHUB_REPOSITORY": "synthetic/gateway", "GITHUB_REF": "refs/heads/main"})
    def test_approved_run_supplies_hash_and_plan_without_customer_hash_input(self):
        config, plan, _, api = self.fixture()
        selected, digest = approved_audit_plan(config, "audit-pause", "a" * 40, "123", api)
        self.assertEqual(selected, plan)
        self.assertEqual(digest, fingerprint(plan))

    @patch.dict("os.environ", {"GITHUB_REPOSITORY": "synthetic/gateway", "GITHUB_REF": "refs/heads/main"})
    def test_wrong_revision_workflow_execute_or_corrupt_hash_is_rejected(self):
        for changes in ({"applied": True}, {"planSha256": "f" * 64}, {"action": "audit-resume"}):
            config, _, _, api = self.fixture(changes)
            with self.assertRaises(ValueError):
                approved_audit_plan(config, "audit-pause", "a" * 40, "123", api)
        for changes in ({"head_sha": "f" * 40}, {"event": "pull_request"}, {"path": ".github/workflows/untrusted.yml"}, {"conclusion": "failure"}):
            config, _, run, api = self.fixture()
            run.update(changes)
            with self.assertRaises(ValueError):
                approved_audit_plan(config, "audit-pause", "a" * 40, "123", api)