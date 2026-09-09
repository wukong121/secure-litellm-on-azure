import copy
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from scripts.customer_migration import ROOT
from scripts.migration_runtime import backup_restore, check_application, hardened_spec, monitoring_onboard, publish, target_database_restore
from tests.test_customer_migration import customer_config


class RuntimeSafetyTests(unittest.TestCase):
    def test_application_publish_cannot_override_active_audit_window(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as directory, patch("scripts.migration_runtime.connect_cluster", return_value=["kubectl"]), patch("scripts.migration_runtime.run_command", return_value="configmap/llmgw-audit-recovery-window") as command:
            with self.assertRaisesRegex(ValueError, "audit-resume"):
                publish(customer_config(), 7, "application", "execute", "a" * 40, Path(directory), "approved")
            self.assertEqual(command.call_count, 1)
            self.assertNotIn("apply", command.call_args.args[0])

    def test_legacy_patch_keeps_credentials_image_volumes_and_unknown_settings(self):
        source = {"metadata": {"name": "postgres"}, "spec": {"replicas": 1, "strategy": {"type": "RollingUpdate"}, "template": {"spec": {"containers": [{"name": "postgres", "image": "postgres:16-alpine", "env": [{"name": "POSTGRES_PASSWORD", "value": "synthetic"}], "resources": {"requests": {"cpu": "200m"}}}], "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "pg-data"}}], "nodeSelector": {"existing": "selection"}}}}}
        original = copy.deepcopy(source)
        result = hardened_spec(source)
        self.assertEqual(source, original)
        self.assertEqual(result["strategy"], {"type": "Recreate"})
        container = result["template"]["spec"]["containers"][0]
        for key in ("image", "env", "resources"):
            self.assertEqual(container[key], original["spec"]["template"]["spec"]["containers"][0][key])
        self.assertEqual(result["template"]["spec"]["volumes"], original["spec"]["template"]["spec"]["volumes"])
        self.assertIn("startupProbe", container)

    def test_runtime_does_not_apply_generic_validation_placeholders_or_secrets(self):
        config = customer_config()
        for document in (
            {"kind": "Secret", "metadata": {"name": "credential", "namespace": "litellm"}},
            {"kind": "PersistentVolumeClaim", "metadata": {"name": "pg-data", "namespace": "litellm"}},
            {"kind": "ConfigMap", "metadata": {"name": "config", "namespace": "litellm"}, "data": {"host": "REPLACE_DATABASE"}},
            {"kind": "ConfigMap", "metadata": {"name": "config", "namespace": "other"}},
            {"kind": "Service", "metadata": {"name": "public", "namespace": "litellm"}, "spec": {"type": "LoadBalancer"}},
        ):
            with self.subTest(kind=document["kind"]), self.assertRaises(ValueError):
                check_application([document], 7, config)

    def target_setup(self):
        config = customer_config()
        config["parameters"]["platform"]["stage5Data"] = {"postgresqlDatabaseName": "litellm"}
        azure = Mock()
        azure.run.return_value = {"tenantId": config["azure"]["tenantId"], "id": config["azure"]["subscriptionId"]}

        def cloud(command):
            if command[:3] == ["deployment", "group", "show"]:
                if "llmgw-test-s5-platform" in command:
                    return {"state": "Succeeded", "platform": {"stage5Deployed": True, "postgresqlServerName": "new-db"}}
                return {"storageAccountName": "syntheticbackup", "containerName": "backups"}
            if command[:3] == ["postgres", "flexible-server", "show"]:
                return {"id": "/synthetic/new-database", "host": "new-db.postgres.database.azure.com"}
            if command[:3] == ["storage", "blob", "download"]:
                Path(command[command.index("--file") + 1]).write_bytes(b"synthetic-backup")
                return {}
            raise AssertionError(command)

        azure.scoped.side_effect = cloud
        return config, azure

    def test_restore_plan_never_gets_token_downloads_or_writes_database(self):
        config, azure = self.target_setup()
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as directory, patch("scripts.migration_runtime.AzureCommands", return_value=azure), patch.dict("os.environ", {"POSTGRES_MIGRATION_USER": "migration-role", "MIGRATION_RESTORE_BLOB": "pre-change/" + "a" * 32 + ".dump", "MIGRATION_BACKUP_SHA256": hashlib.sha256(b"synthetic-backup").hexdigest()}, clear=True), patch("scripts.migration_runtime.subprocess.run") as command:
            target_database_restore(config, "plan", "a" * 40, Path(directory), "")
            from scripts.workflow_artifacts import write_operation_receipt
            receipt = write_operation_receipt(Path(directory), config, "a" * 40, 5, "restore-target", "plan", "runtime")
            self.assertEqual(receipt["operation"], "plan")
            command.assert_not_called()
            self.assertFalse(any(call.args[0][:3] == ["storage", "blob", "download"] for call in azure.scoped.call_args_list))

    def test_restore_refuses_nonempty_target_and_does_not_log_token(self):
        config, azure = self.target_setup()
        for count in ("3", "0"):
            with self.subTest(tables=count), tempfile.TemporaryDirectory(dir=ROOT / "temp") as directory, patch("scripts.migration_runtime.AzureCommands", return_value=azure), patch.dict("os.environ", {"POSTGRES_MIGRATION_USER": "migration-role", "MIGRATION_RESTORE_BLOB": "pre-change/" + "a" * 32 + ".dump", "MIGRATION_BACKUP_SHA256": hashlib.sha256(b"synthetic-backup").hexdigest(), "PGSERVICE": "legacy-connection", "PGHOST": "old-db.invalid"}, clear=True), patch("scripts.migration_runtime.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout="synthetic-token", stderr="")), patch("scripts.migration_runtime.run_command") as command:
                command.return_value = count
                destination = Path(directory)
                target_database_restore(config, "plan", "a" * 40, destination, "")
                plan = json.loads((destination / "runtime-summary.json").read_text())["planSha256"]
                if count != "0":
                    with self.assertRaisesRegex(ValueError, "not empty"):
                        target_database_restore(config, "execute", "a" * 40, destination, plan)
                    self.assertFalse(any(call.args[0][0] == "pg_restore" for call in command.call_args_list))
                else:
                    target_database_restore(config, "execute", "a" * 40, destination, plan)
                    restore = next(call for call in command.call_args_list if call.args[0][0] == "pg_restore")
                    self.assertEqual(restore.kwargs["environment"]["PGHOST"], "new-db.postgres.database.azure.com")
                    self.assertEqual(restore.kwargs["environment"]["PGSSLMODE"], "verify-full")
                    self.assertNotIn("PGSERVICE", restore.kwargs["environment"])
                    self.assertNotIn("--clean", restore.args[0])
                    self.assertNotIn("synthetic-token", json.dumps(restore.args, default=str))
                self.assertNotIn("synthetic-token", (destination / "runtime-summary.json").read_text())

    def test_failed_backup_never_outputs_passed_acceptance(self):
        config = customer_config()
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as directory, patch.dict("os.environ", {"POSTGRES_RESTORE_IMAGE": "postgres@sha256:" + "a" * 64}, clear=True), patch("scripts.migration_runtime.connect_cluster", return_value=["kubectl"]), patch("scripts.migration_runtime.subprocess.run"), patch("scripts.migration_runtime.run_command") as command:
            inventory = {"items": [{"metadata": {"name": "postgres"}, "spec": {"template": {"spec": {"containers": [{"image": "postgres:16"}], "volumes": [{"persistentVolumeClaim": {"claimName": "pg-data"}}]}}}}]}
            command.side_effect = [json.dumps(inventory), json.dumps({"items": [{"metadata": {"name": "postgres-pod"}, "status": {"phase": "Running"}}]}), ValueError("synthetic dump failed")]
            with self.assertRaises(ValueError):
                backup_restore(config, "a" * 40, Path(directory))
            self.assertFalse((Path(directory) / "acceptance-report.json").exists())

    def test_successful_backup_restores_uploads_verifies_and_stays_pending(self):
        config = customer_config()
        azure = Mock()
        command_calls = []

        def commands(arguments, directory, label, **kwargs):
            command_calls.append(arguments)
            if label == "legacy-inventory":
                return json.dumps({"items": [{"metadata": {"name": "postgres"}, "spec": {"template": {"spec": {"containers": [{"image": "postgres:16"}], "volumes": [{"persistentVolumeClaim": {"claimName": "pg-data"}}]}}}}]})
            if label == "postgres-pods":
                return json.dumps({"items": [{"metadata": {"name": "postgres-pod"}, "status": {"phase": "Running"}}]})
            if label == "copy-backup":
                (directory / "database.dump").write_bytes(b"synthetic-backup")
            return "2" if label == "restored-tables" else ""

        def cloud(arguments):
            if arguments[:3] == ["deployment", "group", "show"]:
                return {"storageAccountName": "backupaccount", "containerName": "backups"}
            if arguments[:3] == ["storage", "blob", "download"]:
                Path(arguments[arguments.index("--file") + 1]).write_bytes(b"synthetic-backup")
            return {}

        azure.scoped.side_effect = cloud
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as directory, patch.dict("os.environ", {"POSTGRES_RESTORE_IMAGE": "postgres@sha256:" + "a" * 64}, clear=True), patch("scripts.migration_runtime.connect_cluster", return_value=["kubectl"]), patch("scripts.migration_runtime.AzureCommands", return_value=azure), patch("scripts.migration_runtime.run_command", side_effect=commands), patch("scripts.migration_runtime.subprocess.run") as cleanup:
            backup_restore(config, "a" * 40, Path(directory))
            report = json.loads((Path(directory) / "acceptance-report.json").read_text())
            self.assertTrue(report["observations"]["fullRestoreSucceeded"])
            self.assertEqual(report["observations"]["backupSha256"], hashlib.sha256(b"synthetic-backup").hexdigest())
            self.assertTrue(all(item["status"] == "pending" for item in report["checks"].values()))
            docker_run = next(command for command in command_calls if command[:2] == ["docker", "run"])
            self.assertEqual(docker_run[docker_run.index("--network") + 1], "none")
            self.assertNotIn("--publish", docker_run)
            self.assertTrue(any(command[:2] == ["docker", "exec"] and "pg_restore" in command for command in command_calls))
            self.assertTrue(any(call.args[0][:2] == ["docker", "rm"] for call in cleanup.call_args_list))

    def test_application_manifest_pass_and_security_rejections(self):
        container = {
            "name": "litellm", "image": "registry.invalid/litellm@sha256:" + "a" * 64,
            "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}},
            "resources": {"requests": {"cpu": "250m", "memory": "1Gi"}, "limits": {"cpu": "1", "memory": "2Gi"}},
            **{name: {"httpGet": {"path": "/health/liveliness", "port": 4000}} for name in ("startupProbe", "readinessProbe", "livenessProbe")},
        }
        deployment = {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "litellm", "namespace": "litellm"}, "spec": {"replicas": 2, "template": {"spec": {"automountServiceAccountToken": False, "securityContext": {"runAsNonRoot": True}, "containers": [container]}}}}
        documents = [deployment] + [{"apiVersion": "v1", "kind": kind, "metadata": {"name": "synthetic", "namespace": "litellm"}} for kind in ("NetworkPolicy", "PodDisruptionBudget", "SecretProviderClass")]
        check_application(documents, 6, customer_config())
        for updates in ({"image": "registry.invalid/litellm:latest"}, {"env": [{"name": "DATABASE_URL", "value": "secret"}]}, {"envFrom": [{"secretRef": {"name": "all"}}]}, {"securityContext": {"privileged": True}}):
            modified = copy.deepcopy(documents)
            modified[0]["spec"]["template"]["spec"]["containers"][0].update(updates)
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                check_application(modified, 6, customer_config())

    def test_legacy_publish_plans_without_patch_and_executes_sequentially(self):
        config = customer_config()
        calls = []

        def commands(arguments, directory, label, **kwargs):
            calls.append(arguments)
            if label.startswith("before-"):
                name = label.removeprefix("before-")
                return json.dumps({"metadata": {"name": name, "uid": "synthetic-" + name, "resourceVersion": "123"}, "spec": {"replicas": 1, "template": {"spec": {"containers": [{"name": "postgres" if name == "postgres" else "litellm", "image": "original:image"}], "volumes": []}}}})
            return ""

        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as directory, patch("scripts.migration_runtime.connect_cluster", return_value=["kubectl"]), patch("scripts.migration_runtime.run_command", side_effect=commands):
            path = Path(directory)
            publish(config, 1, "legacy-hardening", "plan", "a" * 40, path, "")
            plan = json.loads((path / "runtime-summary.json").read_text())["planSha256"]
            self.assertFalse(any("patch" in command for command in calls))
            publish(config, 1, "legacy-hardening", "execute", "a" * 40, path, plan)
            mutations = [command for command in calls if "patch" in command or "rollout" in command]
            self.assertEqual([command[1] for command in mutations], ["patch", "rollout", "patch", "rollout"])
            final_patch = json.loads((path / "workload-patch.json").read_text())
            self.assertEqual(final_patch[0], {"op": "test", "path": "/metadata/resourceVersion", "value": "123"})

    def test_monitoring_cannot_redirect_existing_workspace(self):
        config = customer_config()
        azure = Mock()
        azure.run.return_value = {"tenantId": config["azure"]["tenantId"], "id": config["azure"]["subscriptionId"]}
        azure.scoped.side_effect = ["/approved/workspace", {"enabled": True, "config": {"logAnalyticsWorkspaceResourceID": "/other/workspace"}}]
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as directory, patch("scripts.migration_runtime.AzureCommands", return_value=azure):
            with self.assertRaisesRegex(ValueError, "differs"):
                monitoring_onboard(config, 1, "execute", "a" * 40, Path(directory), "approved")
            self.assertFalse(any("enable-addons" in call.args[0] for call in azure.scoped.call_args_list))