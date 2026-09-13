import unittest
import json
from pathlib import Path
import subprocess
import tempfile
from unittest.mock import patch

from scripts.customer_migration import ROOT
from scripts.installation_readiness import INSTALLATION_CHECKS, inspect_runner, installation_readiness
from tests.test_customer_migration import customer_config


class InstallationReadinessTests(unittest.TestCase):
    def test_live_probes_are_read_only_and_do_not_require_future_cluster(self):
        config = customer_config()
        commands = []
        def run(arguments, **kwargs):
            commands.append(arguments)
            output = "Python 3.13.6" if arguments[-1] == "--version" and arguments[0] != "node" else "v24.1.0" if arguments[0] == "node" else "deployment/litellm"
            if arguments[:3] == ["az", "account", "show"]:
                output = json.dumps({"tenantId": config["azure"]["tenantId"], "id": config["azure"]["subscriptionId"]})
            return subprocess.CompletedProcess(arguments, 0, output, "private diagnostic not for output")
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as directory, patch("scripts.installation_readiness.shutil.which", return_value="installed"), patch("scripts.migration_runtime.connect_cluster", return_value=["kubectl"]) as cluster:
            report = inspect_runner(config, Path(directory), run=run)
            self.assertEqual(report["status"], "passed")
            self.assertFalse(report["readyForDeployment"])
            self.assertFalse(report["stageAccepted"])
            self.assertEqual(cluster.call_count, 1)
            self.assertTrue(cluster.call_args.args[2])
            self.assertNotIn("private diagnostic", json.dumps(report))
            self.assertTrue(any(command[:3] == ["kubectl", "get", "deployments"] for command in commands))
            self.assertFalse(any("apply" in command or "create" in command for command in commands))
            inspect_runner(config, Path(directory), check_target=True, run=run)
            self.assertFalse(cluster.call_args.args[2])

    def test_wrong_azure_scope_never_connects_to_cluster(self):
        def run(arguments, **kwargs):
            return subprocess.CompletedProcess(arguments, 0, "{}", "")
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as directory, patch("scripts.installation_readiness.shutil.which", return_value="installed"), patch("scripts.migration_runtime.connect_cluster") as cluster:
            report = inspect_runner(customer_config(), Path(directory), run=run)
            self.assertEqual(report["status"], "failed")
            cluster.assert_not_called()

    def test_partial_or_claimed_approval_does_not_make_installation_ready(self):
        self.assertFalse(installation_readiness({})["readyForDeployment"])
        observation = {"passed": True, "source": "synthetic test fixture only", "scope": "synthetic target"}
        observations = {name: observation for name in INSTALLATION_CHECKS}
        self.assertTrue(installation_readiness(observations)["readyForDeployment"])
        for name in INSTALLATION_CHECKS:
            incomplete = dict(observations)
            incomplete.pop(name)
            self.assertFalse(installation_readiness(incomplete)["readyForDeployment"])
            incomplete[name] = {**observation, "passed": False}
            self.assertFalse(installation_readiness(incomplete)["readyForDeployment"])
        self.assertFalse(installation_readiness(observations)["stageAccepted"])