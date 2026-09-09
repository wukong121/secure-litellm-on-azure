import unittest
import yaml

from scripts.customer_migration import COMPONENTS, ROOT


class MigrationWorkflowTests(unittest.TestCase):
    def test_workflow_components_match_controller_including_network(self):
        for name, extra in (("customer-deploy.yml", set()), ("customer-migration.yml", {"none"})):
            workflow = yaml.load((ROOT / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader)
            options = workflow["on"]["workflow_dispatch"]["inputs"]["component"]["options"]
            self.assertEqual(set(options), set(COMPONENTS) | extra)

    def test_new_workflows_are_private_protected_and_pinned(self):
        for name in ("customer-deploy.yml", "customer-runtime.yml", "customer-acceptance.yml"):
            with self.subTest(name=name):
                content = (ROOT / ".github/workflows" / name).read_text()
                workflow = yaml.load(content, Loader=yaml.BaseLoader)
                self.assertEqual(workflow["permissions"], {"contents": "read"})
                self.assertEqual(workflow["concurrency"]["group"], "customer-change-${{ inputs.environment }}")
                self.assertIn("github.event.repository.private", content)
                for job in workflow["jobs"].values():
                    for step in job["steps"]:
                        if "uses" in step:
                            self.assertRegex(step["uses"], r"@[0-9a-f]{40}$")
                        self.assertNotIn("${{", step.get("run", ""))
                        if "CUSTOMER_CONFIG_JSON" in step.get("env", {}):
                            self.assertEqual(job["environment"], "${{ inputs.environment }}")
                        if "upload-artifact" in step.get("uses", ""):
                            self.assertNotIn("database.dump", step["with"]["path"])
                            self.assertNotIn("parameters.json", step["with"]["path"])
                            self.assertNotIn("command-", step["with"]["path"])
                self.assertNotIn("secrets: write", content)

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