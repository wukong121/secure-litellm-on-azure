import json
import subprocess
import tempfile
from pathlib import Path
import unittest
import yaml

from scripts.customer_migration import COMPONENTS, ROOT
from scripts.source_supply_chain import check_source, source_image


class MigrationWorkflowTests(unittest.TestCase):
    def test_manual_confirmation_and_read_only_runner_entrypoints(self):
        acceptance = yaml.load((ROOT / ".github/workflows/customer-acceptance.yml").read_text(), Loader=yaml.BaseLoader)
        inputs = acceptance["on"]["workflow_dispatch"]["inputs"]
        self.assertIn("confirm", inputs["operation"]["options"])
        self.assertTrue({"reviewed_run_id", "checked_items", "evidence_notes", "confirm_environment"}.issubset(inputs))
        acceptance_steps = {step.get("name"): step for step in acceptance["jobs"]["acceptance"]["steps"]}
        self.assertEqual(acceptance_steps["Encrypt draft or generated ledger"]["if"], "success() || failure()")
        self.assertIn("operation-status.json", acceptance_steps["Encrypt draft or generated ledger"]["run"])
        self.assertEqual(acceptance_steps["Save encrypted acceptance evidence"]["if"], "success() || failure()")
        runner_text = (ROOT / ".github/workflows/customer-runner-checks.yml").read_text()
        runner = yaml.load(runner_text, Loader=yaml.BaseLoader)
        self.assertEqual(runner["jobs"]["check"]["runs-on"], "${{ fromJSON(vars.MIGRATION_PRIVATE_RUNNER_LABELS) }}")
        self.assertIn("vars.AZURE_RUNTIME_CLIENT_ID", runner_text)
        self.assertNotIn("scripts.migration_runtime --", runner_text)
        for name in ("customer-deploy.yml", "customer-migration.yml", "customer-acceptance.yml", "customer-runner-checks.yml"):
            workflow = yaml.load((ROOT / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader)
            for job in workflow["jobs"].values():
                for step in job["steps"]:
                    if "uses" in step:
                        self.assertRegex(step["uses"], r"@[0-9a-f]{40}$")
                    command = step.get("run", "")
                    self.assertNotIn("${{", command)
                    if "pip install" in command:
                        self.assertIn("cryptography==50.0.1", command)
                        self.assertIn("service-identity==24.2.0", command)

    def test_source_check_needs_no_private_runner_or_azure_credentials(self):
        workflow = yaml.load((ROOT / ".github/workflows/source-image-checks.yml").read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        job = workflow["jobs"]["source"]
        self.assertEqual(job["runs-on"], "ubuntu-24.04")
        self.assertNotIn("environment", job)
        for step in job["steps"]:
            self.assertNotIn("${{", step.get("run", ""))
            if "uses" in step:
                self.assertRegex(step["uses"], r"@[0-9a-f]{40}$")
                self.assertNotIn("azure/login", step["uses"])
        self.assertEqual(job["steps"][-1]["if"], "always()")

    def test_source_report_retains_failures_without_certifying_customer_stage(self):
        for scan_code in (0, 1):
            commands = []
            def run(command, **kwargs):
                commands.append(command)
                if command[0] == "docker":
                    output = "sha256:" + "b" * 64 if command[1:3] == ["image", "inspect"] else ""
                    return subprocess.CompletedProcess(command, 0, output, "not-for-artifact")
                document = {"spdxVersion": "SPDX-2.3", "packages": [{"name": "synthetic"}]} if command[0] == "syft" else {"ArtifactName": "sha256:" + "b" * 64, "Results": [{"Target": "synthetic"}]}
                return subprocess.CompletedProcess(command, 0 if command[0] == "syft" else scan_code, json.dumps(document), "not-for-artifact")
            with self.subTest(scan_code=scan_code), tempfile.TemporaryDirectory(dir=ROOT / "temp") as folder:
                report = check_source(Path(folder), "a" * 40, run)
                self.assertEqual(report["status"], "passed" if scan_code == 0 else "failed")
                self.assertFalse(report["stageAccepted"])
                self.assertEqual([command[0] for command in commands], ["docker", "docker", "syft", "trivy"])
                self.assertEqual(len(report["results"]), 3)
                self.assertNotIn("not-for-artifact", (Path(folder) / "source-summary.json").read_text())
                self.assertTrue(all("sha256" in item for item in report["results"]))
                if scan_code:
                    self.assertEqual(report["results"][2]["reason"], "Fixable CRITICAL vulnerabilities matched policy")

    def test_missing_source_tool_is_a_failed_check(self):
        def unavailable(*args, **kwargs):
            raise FileNotFoundError("private environment details")
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as folder:
            report = check_source(Path(folder), "a" * 40, unavailable)
            self.assertEqual(report["status"], "failed")
            self.assertNotIn("private environment details", json.dumps(report))
            self.assertTrue(all(item["diagnostic"]["code"] == "validation-failed" for item in report["results"]))

    def test_workflow_components_match_controller_including_network(self):
        for name, extra in (("customer-deploy.yml", set()), ("customer-migration.yml", {"none"})):
            workflow = yaml.load((ROOT / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader)
            options = workflow["on"]["workflow_dispatch"]["inputs"]["component"]["options"]
            self.assertEqual(set(options), set(COMPONENTS) | extra)

    def test_new_workflows_support_public_forks_with_protected_encrypted_execution(self):
        for name in ("customer-deploy.yml", "customer-runtime.yml", "customer-acceptance.yml"):
            with self.subTest(name=name):
                content = (ROOT / ".github/workflows" / name).read_text()
                workflow = yaml.load(content, Loader=yaml.BaseLoader)
                self.assertEqual(workflow["permissions"], {"contents": "read"})
                self.assertEqual(workflow["concurrency"]["group"], "customer-change-${{ inputs.environment }}")
                self.assertIn("GITHUB_REF_PROTECTED", content)
                self.assertNotIn('[[ "$PRIVATE_REPOSITORY" == true ]]', content)
                self.assertIn("secrets.WORKFLOW_ARTIFACT_KEY", content)
                self.assertNotIn("vars.CUSTOMER_CONFIG_JSON", content)
                for job in workflow["jobs"].values():
                    for step in job["steps"]:
                        if "uses" in step:
                            self.assertRegex(step["uses"], r"@[0-9a-f]{40}$")
                        self.assertNotIn("${{", step.get("run", ""))
                        if "CUSTOMER_CONFIG_JSON" in step.get("env", {}):
                            self.assertEqual(job["environment"], "${{ inputs.environment }}")
                        if "upload-artifact" in step.get("uses", ""):
                            self.assertTrue(step["with"]["path"].endswith("/sealed-artifact.json"))
                            self.assertNotIn("database.dump", step["with"]["path"])
                            self.assertNotIn("parameters.json", step["with"]["path"])
                            self.assertNotIn("command-", step["with"]["path"])
                self.assertNotIn("secrets: write", content)

    def test_all_primary_public_fork_jobs_are_manual_protected_and_use_secret_config(self):
        for name in ("customer-deploy.yml", "customer-runtime.yml", "customer-acceptance.yml", "customer-migration.yml", "customer-runner-checks.yml", "customer-gateway-checks.yml", "promote-litellm-image.yml"):
            content = (ROOT / ".github/workflows" / name).read_text()
            workflow = yaml.load(content, Loader=yaml.BaseLoader)
            with self.subTest(workflow=name):
                self.assertEqual(set(workflow["on"]), {"workflow_dispatch"})
                self.assertNotIn("PRIVATE_REPOSITORY", content)
                self.assertIn("GITHUB_REF_PROTECTED", content)
                self.assertNotIn("vars.CUSTOMER_CONFIG_JSON", content)
                for job in workflow["jobs"].values():
                    if "fromJSON" in job.get("runs-on", ""):
                        self.assertEqual(job["needs"], "authorize")
                    for step in job["steps"]:
                        if "upload-artifact" in step.get("uses", ""):
                            self.assertTrue(step["with"]["path"].endswith("/sealed-artifact.json"))

    def test_runtime_requires_private_runner_and_separate_identity(self):
        content = (ROOT / ".github/workflows/customer-runtime.yml").read_text()
        self.assertIn("fromJSON(vars.MIGRATION_PRIVATE_RUNNER_LABELS)", content)
        self.assertIn("vars.AZURE_RUNTIME_CLIENT_ID", content)
        self.assertIn("needs: authorize", content)
        self.assertIn("vars.AZURE_ENTRA_CLIENT_ID", content)
        self.assertIn("vars.AZURE_ENTRA_ACCESS_CLIENT_ID", content)
        self.assertIn("entra-access", content)
        self.assertIn("admin-credentials-rotate", content)
        self.assertIn("admin-credentials-recover", content)
        self.assertIn("entra-apps", content)
        self.assertIn("admin-credentials", content)
        self.assertIn("proxy-credentials", content)
        for action in ("audit-pause", "audit-recover", "audit-resume"):
            self.assertIn(action, content)
        self.assertIn("inputs.approved_run_id", content)
        self.assertIn("inputs.audit_continue_run_id", content)
        self.assertIn("actions: read", content)
        self.assertNotIn("AZURE_AUDIT_STORAGE_KEY", content)

    def test_legacy_preview_workflow_still_never_calls_deployer(self):
        content = (ROOT / ".github/workflows/customer-migration.yml").read_text()
        self.assertIn("config-check", content)
        self.assertNotIn("scripts.migration_deploy", content)

    def test_execution_workflows_load_evidence_and_plan_artifacts_without_secret_writes(self):
        for name in ("customer-runtime.yml", "customer-deploy.yml", "customer-acceptance.yml"):
            content = (ROOT / ".github/workflows" / name).read_text()
            self.assertIn("MIGRATION_AUTO_EVIDENCE: 'true'", content)
            self.assertIn("actions: read", content)
            self.assertNotIn("secrets: write", content)
        for name in ("customer-runtime.yml", "customer-deploy.yml"):
            content = (ROOT / ".github/workflows" / name).read_text()
            self.assertIn("inputs.approved_run_id", content)
            self.assertIn("operation-receipt.json", content)

    def test_image_promotion_uses_private_runner_and_approved_registry(self):
        content = (ROOT / ".github/workflows/promote-litellm-image.yml").read_text()
        self.assertIn("fromJSON(vars.MIGRATION_PRIVATE_RUNNER_LABELS)", content)
        self.assertIn("needs: authorize", content)
        self.assertIn("Target ACR differs from approved customer configuration", content)
        workflow = yaml.load(content, Loader=yaml.BaseLoader)
        self.assertEqual(workflow["jobs"]["promote"]["environment"], "${{ inputs.environment }}")
        self.assertEqual(workflow["on"]["workflow_dispatch"]["inputs"]["environment"]["options"], ["dev", "test", "prod"])
        self.assertEqual(workflow["concurrency"]["group"], "customer-change-${{ inputs.environment }}")
        self.assertIn("llmgw.environment=$CUSTOMER_ENVIRONMENT", content)
        self.assertIn("llmgw.runtime=auth-proxy", content)
        self.assertIn("Choose only one runtime build", content)
        self.assertIn("Verify promoted digest signature", content)
        self.assertIn("cosign verify", content)
        self.assertIn("target-image-verification.json", content)
        self.assertIn("target-image-summary.json", content)
        self.assertIn('"signatureVerified": True', content)