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
    def test_backup_probes_use_exact_private_endpoint_tls_and_oidc_read_only(self):
        from scripts.runner_connectivity import check_backup_access
        from tests.test_migration_deploy import MigrationDeploymentTests
        fixture = MigrationDeploymentTests()
        config = fixture.connectivity_config()
        target = {"blobHost": "syntheticbackup.blob.core.windows.net", "privateEndpointIps": ["10.30.8.4"], "storageAccountName": "syntheticbackup", "containerName": "litellm-postgresql"}
        for dns, tls, listing, failed in (
            ("10.30.8.4 STREAM host", "10.30.8.4 403", "0", None),
            ("20.10.10.10 STREAM host", "10.30.8.4 403", "0", "backup-private-dns"),
            ("10.30.8.4 STREAM host\n20.10.10.10 STREAM host", "10.30.8.4 403", "0", "backup-private-dns"),
            ("10.30.8.4 STREAM host", "20.10.10.10 403", "0", "backup-private-tls"),
            ("10.30.8.4 STREAM host", "10.30.8.4 000", "0", "backup-private-tls"),
            ("10.30.8.4 STREAM host", "10.30.8.4 403", "PRIVATE_ERROR", "backup-blob-read"),
        ):
            commands = []
            def execute(command):
                commands.append(command)
                return dns if command[0] == "getent" else tls if command[0] == "curl" else listing
            with self.subTest(failed=failed), patch("scripts.runner_connectivity.backup_target", return_value=target):
                report = check_backup_access(config, None, execute)
                self.assertEqual([item["name"] for item in report if item["status"] == "failed"], [failed] if failed else [])
                self.assertNotIn("PRIVATE_ERROR", json.dumps(report))
                if failed in {"backup-private-dns", "backup-private-tls"}:
                    self.assertFalse(any(command[0] == "az" for command in commands))
                if failed is None:
                    self.assertIn("--noproxy", commands[1])
                    self.assertNotIn("-k", commands[1])
                    self.assertEqual(commands[2][commands[2].index("--auth-mode") + 1], "login")
                    self.assertNotIn("upload", commands[2])

    def test_backup_checks_are_opt_in_and_never_use_wrong_azure_scope(self):
        def run(command, **kwargs):
            if command[:3] == ["az", "account", "show"]:
                output = json.dumps({"tenantId": config["azure"]["tenantId"], "id": config["azure"]["subscriptionId"]})
            else:
                output = "Python 3.13.6" if command[0] != "node" else "v24.1.0"
            return subprocess.CompletedProcess(command, 0, output, "")
        config = customer_config()
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as folder, patch("scripts.installation_readiness.shutil.which", return_value="installed"), patch("scripts.migration_runtime.connect_cluster", return_value=["kubectl"]), patch("scripts.runner_connectivity.check_backup_access", return_value=[{"name": "backup-private-dns", "status": "failed", "details": "safe"}]) as backup:
            inspect_runner(config, Path(folder), run=run)
            backup.assert_not_called()
            report = inspect_runner(config, Path(folder), run=run, check_backup=True)
            self.assertEqual(report["status"], "failed")
            self.assertFalse(report["stageAccepted"])
            self.assertIn("write permissions, including Blob upload", report["notCovered"])
            backup.assert_called_once()
            backup.reset_mock()
            invalid_scope = lambda command, **kwargs: subprocess.CompletedProcess(command, 0, "{}", "")
            inspect_runner(config, Path(folder), run=invalid_scope, check_backup=True)
            backup.assert_not_called()

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