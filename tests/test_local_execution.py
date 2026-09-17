import copy
import inspect
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from local_execution.runner import (
    STEPS,
    check_configuration,
    execute_infrastructure_plan,
    load_config,
    main,
    remove_sensitive_runtime_files,
    run_backup_restore,
    run_connectivity_check,
    run_infrastructure,
    run_runtime,
    require_legacy_cluster_running,
)
from scripts.customer_migration import ROOT, validate_config
from scripts.migration_deploy import deploy_component
from tests.test_customer_migration import customer_config


class LocalExecutionTests(unittest.TestCase):
    def configuration(self):
        config = customer_config()
        config["localExecution"] = {
            "postgresRestoreImage": "postgres@sha256:" + "a" * 64,
            "executionHost": {
                "virtualNetworkId": f"/subscriptions/{config['azure']['subscriptionId']}/resourceGroups/rg-execution/providers/Microsoft.Network/virtualNetworks/execution-vnet",
                "managePeering": True,
                "manageBlobDnsLink": True,
            },
        }
        config["parameters"].update({
            "backup": {
                "logAnalyticsWorkspaceName": "target-logs",
                "backupOwnerPrincipalId": "33333333-3333-4333-8333-333333333333",
                "virtualNetworkName": "target-vnet",
                "virtualNetworkAddressPrefix": "10.30.0.0/16",
                "privateEndpointSubnetName": "snet-private-endpoints",
                "privateEndpointSubnetPrefix": "10.30.8.0/24",
            },
            "legacy-logging": {"workspaceMode": "create"},
        })
        return config

    def prepared(self):
        source = self.configuration()
        settings = source.pop("localExecution")
        host = settings["executionHost"]
        source["parameters"]["runner-connectivity"] = {
            "runnerVirtualNetworkId": host["virtualNetworkId"],
            "managePeering": host["managePeering"],
            "manageBlobDnsLink": host["manageBlobDnsLink"],
        }
        return validate_config(source, "test"), settings

    def test_standalone_configuration_is_translated_before_shared_validation(self):
        source = self.configuration()
        with self.assertRaisesRegex(ValueError, "Unexpected or missing"):
            validate_config(source, "test")
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as folder:
            path = Path(folder) / "customer.json"
            path.write_text(json.dumps(source))
            config, settings = load_config(path)
        self.assertNotIn("localExecution", config)
        self.assertEqual(settings, source["localExecution"])
        self.assertEqual(config["parameters"]["runner-connectivity"]["runnerVirtualNetworkId"], settings["executionHost"]["virtualNetworkId"])

    def test_local_settings_require_pinned_image_and_execution_host(self):
        for update, message in (
            ({"postgresRestoreImage": "postgres:16"}, "full image digest"),
            ({"executionHost": {}}, "unexpected or missing"),
            ({"unexpected": True}, "requires postgresRestoreImage"),
        ):
            source = self.configuration()
            source["localExecution"].update(update)
            if "unexpected" in update:
                source["localExecution"].pop("executionHost")
            with tempfile.TemporaryDirectory(dir=ROOT / "temp") as folder:
                path = Path(folder) / "customer.json"
                path.write_text(json.dumps(source))
                with self.subTest(update=update), self.assertRaisesRegex(ValueError, message):
                    load_config(path)

    def test_shared_deployment_controller_has_no_local_bypass(self):
        self.assertNotIn("enforce_evidence", inspect.signature(deploy_component).parameters)

    def test_infrastructure_uses_shared_plan_and_local_execution(self):
        config, _settings = self.prepared()
        plan = {"planSha256": "b" * 64, "revision": "c" * 40, "configSha256": "d" * 64}
        receipt = {"provisioningState": "Succeeded"}
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as folder, patch("local_execution.runner.deploy_component", return_value=plan) as preview, patch("local_execution.runner.execute_infrastructure_plan", return_value=receipt) as execute:
            result = run_infrastructure(config, "c" * 40, 1, "monitoring", Path(folder))
        self.assertEqual(result, {"planSha256": plan["planSha256"], "receipt": receipt})
        self.assertEqual(preview.call_args.args[4], "plan")
        execute.assert_called_once()

    def test_direct_infrastructure_execution_uses_generated_template(self):
        config, _settings = self.prepared()
        plan = {"planSha256": "b" * 64, "revision": "c" * 40, "configSha256": "d" * 64}
        calls = []

        class FakeAzure:
            def run(self, arguments):
                calls.append(arguments)
                return {"tenantId": config["azure"]["tenantId"], "id": config["azure"]["subscriptionId"]}

            def scoped(self, arguments):
                calls.append(arguments)
                return {"id": "/synthetic/deployment", "properties": {"provisioningState": "Succeeded", "outputs": {}}}

        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as folder, patch("local_execution.runner.AzureCommands", return_value=FakeAzure()):
            base = Path(folder)
            plan_directory = base / "plan"
            execute_directory = base / "execute"
            plan_directory.mkdir()
            (plan_directory / "template.json").write_text("{}")
            (plan_directory / "parameters.json").write_text("{}")
            receipt = execute_infrastructure_plan(config, 0, "bootstrap", plan, plan_directory, execute_directory)
        self.assertEqual(receipt["provisioningState"], "Succeeded")
        self.assertEqual(calls[1][:3], ["deployment", "sub", "create"])
        self.assertIn(str(plan_directory / "template.json"), calls[1])

    def test_config_check_cli_reads_only_standalone_customer_file(self):
        source = self.configuration()
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as folder, patch("local_execution.runner.reviewed_revision", return_value="a" * 40), patch("sys.stdout", new_callable=io.StringIO) as output:
            base = Path(folder)
            config_path = base / "customer.json"
            config_path.write_text(json.dumps(source))
            with patch("sys.argv", ["local-execution", "--config", str(config_path), "--step", "config-check", "--output-root", str(base / "runs")]):
                main()
            summary = json.loads(output.getvalue().splitlines()[-1])
        self.assertEqual(summary["result"], {"status": "valid"})

    def test_failed_step_records_status_and_removes_sensitive_files(self):
        source = self.configuration()
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as folder, patch("local_execution.runner.reviewed_revision", return_value="a" * 40):
            base = Path(folder)
            config_path = base / "customer.json"
            config_path.write_text(json.dumps(source))

            def fail(_config, _settings, _revision, _step, destination):
                (destination / "database.dump").write_text("sensitive")
                raise RuntimeError("synthetic failure")

            with patch("local_execution.runner.execute_step", side_effect=fail), patch("sys.argv", ["local-execution", "--config", str(config_path), "--step", "config-check", "--output-root", str(base / "runs")]), self.assertRaisesRegex(RuntimeError, "synthetic failure"):
                main()
            outputs = list((base / "runs").glob("*/local-execution.json"))
            status = json.loads(outputs[0].read_text())
        self.assertEqual(status["result"], {"status": "failed", "errorType": "RuntimeError"})
        self.assertFalse((outputs[0].parent / "database.dump").exists())

    def test_sensitive_cleanup_preserves_reports(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as folder:
            directory = Path(folder)
            for name in ("kubeconfig", "database.dump", "downloaded.dump", "runtime-summary.json"):
                (directory / name).write_text("synthetic")
            remove_sensitive_runtime_files(directory)
            self.assertFalse(any((directory / name).exists() for name in ("kubeconfig", "database.dump", "downloaded.dump")))
            self.assertTrue((directory / "runtime-summary.json").exists())

    def test_connectivity_check_is_local_and_does_not_use_actions_readiness(self):
        config, _settings = self.prepared()
        target = {"blobHost": "backup.blob.core.windows.net", "privateEndpointIps": ["10.30.8.4"], "storageAccountName": "backup", "containerName": "litellm-postgresql"}

        def run(arguments, **_kwargs):
            if arguments[0] == "docker":
                output = "27.0.0"
            elif arguments[:3] == ["az", "account", "show"]:
                output = json.dumps({"tenantId": config["azure"]["tenantId"], "id": config["azure"]["subscriptionId"]})
            elif arguments[0] == "getent":
                output = "10.30.8.4 STREAM backup"
            elif arguments[0] == "curl":
                output = "10.30.8.4 403"
            elif arguments[:3] == ["az", "storage", "blob"]:
                output = "0"
            else:
                output = "deployment/postgres"
            return subprocess.CompletedProcess(arguments, 0, output, "")

        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as folder, patch("local_execution.runner.shutil.which", return_value="installed"), patch("scripts.migration_runtime.connect_cluster", return_value=["kubectl"]), patch("scripts.runner_connectivity.backup_target", return_value=target), patch("local_execution.runner.require_legacy_cluster_running", return_value={"powerState": "Running"}), patch("local_execution.runner.AzureCommands"):
            directory = Path(folder)
            result = run_connectivity_check(config, "a" * 40, directory, run=run)
            report = json.loads((directory / "local-readiness.json").read_text())
        self.assertEqual(result, {"status": "passed"})
        self.assertEqual(report["status"], "passed")
        self.assertIn("local-tools", {check["name"] for check in report["checks"]})

    def test_stopped_legacy_cluster_is_rejected_before_kubernetes_access(self):
        config, _settings = self.prepared()

        class FakeAzure:
            def scoped(self, _arguments):
                return {"provisioningState": "Succeeded", "powerState": "Stopped", "fqdn": "legacy.invalid", "privateFqdn": None}

        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as folder, self.assertRaisesRegex(ValueError, "not Running"):
            require_legacy_cluster_running(config, Path(folder), FakeAzure())

    def test_runtime_preview_hash_is_used_immediately(self):
        config, _settings = self.prepared()
        plan_sha = "d" * 64
        calls = []

        def monitoring(_config, stage, operation, revision, directory, approved):
            calls.append((stage, operation, revision, approved))
            directory.mkdir(parents=True, exist_ok=True)
            if operation == "plan":
                (directory / "runtime-summary.json").write_text(json.dumps({"planSha256": plan_sha}))

        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as folder, patch("scripts.migration_runtime.monitoring_onboard", side_effect=monitoring):
            result = run_runtime(config, "e" * 40, 1, "monitoring-onboard", Path(folder))
        self.assertEqual(result, {"planSha256": plan_sha})
        self.assertEqual(calls[-1][-1], plan_sha)

    def test_backup_restore_reads_image_from_local_settings(self):
        config, settings = self.prepared()
        observed = []

        def restore(_config, revision, _directory):
            observed.append((revision, os.environ.get("POSTGRES_RESTORE_IMAGE")))

        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as folder, patch.dict(os.environ, {"POSTGRES_RESTORE_IMAGE": "previous"}), patch("local_execution.runner.require_legacy_cluster_running"), patch("scripts.migration_runtime.backup_restore", side_effect=restore):
            result = run_backup_restore(config, settings, "f" * 40, Path(folder))
            self.assertEqual(os.environ["POSTGRES_RESTORE_IMAGE"], "previous")
        self.assertEqual(result, {"status": "completed"})
        self.assertEqual(observed, [("f" * 40, settings["postgresRestoreImage"])])

    def test_guide_and_example_match_steps(self):
        guide = (ROOT / "local_execution/README_ZH.md").read_text()
        example = json.loads((ROOT / "local_execution/customer.example.json").read_text())
        self.assertEqual(set(example["localExecution"]), {"postgresRestoreImage", "executionHost"})
        self.assertNotIn("runner-connectivity", example["parameters"])
        self.assertIn("在线Runner VM绝不能挂载高权限UAMI", guide)
        self.assertIn("可以复用客户现有的deploy/runtime UAMI", guide)
        self.assertIn("不要使用数据库管理员UAMI", guide)
        self.assertIn("Client ID用于VM内`az login --identity`", guide)
        for value in ("llmgw-manual-stage01-test", "S0-L02之前", "S0-L03之前", "17d1049b-9a84-46fb-8f53-869881c3d3ab", "b7e6dc6d-f1e8-4753-8033-0f276bb0955b", "ba92f5b4-2d11-453d-a403-e96b0029c9fe", "不是创建UAMI时预先手工分配的角色", "不属于Stage0–1手工执行UAMI的最低要求"):
            self.assertIn(value, guide)
        for value in ("旧AKS必须处于`Running`", "power=Stopped", "az aks start", "getent ahostsv4 \"$AKS_FQDN\"", "不是Runner DNS配置或数据库错误"):
            self.assertIn(value, guide)
        for value in ("database system is shutting down", "容器PID 1已经切换为`postgres`", "restore-container-state.json", "restore-container.log", "本次失败不会生成正式`backupBlob`"):
            self.assertIn(value, guide)
        for value in ("没有`legacy-monitoring`字段", "workspaceMode", "只能是`create`或`existing`", "REPLACE_EXISTING_LEGACY_WORKSPACE_NAME", "REPLACE_NEW_LEGACY_WORKSPACE_NAME", "不能用`create`试探资源是否存在", "az monitor log-analytics workspace list"):
            self.assertIn(value, guide)
        for step in STEPS:
            self.assertIn(f"--step {step}", guide)
        ordered = ["config-check", "bootstrap", "backup", "execution-host-connectivity", "connectivity-check", "backup-restore", "legacy-logging", "monitoring-onboard", "monitoring", "legacy-hardening", "legacy-access-restrict"]
        self.assertEqual([guide.index(f"--step {step}") for step in ordered], sorted(guide.index(f"--step {step}") for step in ordered))


if __name__ == "__main__":
    unittest.main()