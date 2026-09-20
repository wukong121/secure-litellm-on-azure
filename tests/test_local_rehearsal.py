import json
import subprocess
import unittest
from unittest.mock import patch

from scripts.local_rehearsal import checks, run_check, summary_report


class LocalRehearsalTests(unittest.TestCase):
    def test_passed_local_checks_never_certify_customer_migration(self):
        state = {"revision": "a" * 40, "sourceTreeSha256": "b" * 64, "dirty": True}
        results = [{"id": item["id"], "status": "passed"} for item in checks("full", state["revision"])]
        report = summary_report("full", state, results, state)
        self.assertEqual(report["status"], "passed")
        self.assertTrue(report["runComplete"])
        self.assertEqual(summary_report("full", state, results[:1], state)["status"], "incomplete")
        self.assertFalse(report["readyForCustomerMigration"])
        self.assertFalse(report["stageAccepted"])
        self.assertTrue(report["deliveryBlockers"])
        changed = {**state, "sourceTreeSha256": "c" * 64}
        self.assertEqual(summary_report("full", state, [], changed)["status"], "failed")

    def test_missing_tool_and_skipped_required_test_are_blocked(self):
        spec = {"id": "synthetic", "command": ["synthetic"], "tools": ["synthetic"], "structured": True, "noSkips": True}
        with patch("scripts.local_rehearsal.shutil.which", return_value=None):
            self.assertEqual(run_check(spec, {})["status"], "blocked")
        summary = {"testsRun": 1, "failedTests": [], "skippedTests": [{"test": "synthetic", "reason": "missing prerequisite"}]}
        def run(*args, **kwargs):
            return subprocess.CompletedProcess(args[0], 0, json.dumps(summary), "SYNTHETIC_PRIVATE_DIAGNOSTIC")
        with patch("scripts.local_rehearsal.shutil.which", return_value="synthetic"):
            result = run_check(spec, {}, run)
        self.assertEqual(result["status"], "blocked")
        self.assertNotIn("SYNTHETIC_PRIVATE_DIAGNOSTIC", json.dumps(result))

    def test_full_profile_includes_real_isolated_and_source_checks(self):
        selected = {item["id"]: item for item in checks("full", "a" * 40)}
        self.assertTrue({"isolated-runtime", "locked-prisma", "source-supply-chain", "oss-callbacks", "stage3-to-stage9"}.issubset(selected))
        self.assertIn("none", selected["locked-prisma"]["command"])
        self.assertIn("docker", selected["source-supply-chain"]["tools"])
        for item in selected.values():
            self.assertNotIn("deploy", item["command"])
            self.assertNotIn("apply", item["command"])