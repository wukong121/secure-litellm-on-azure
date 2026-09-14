import json
import re
import unittest
from pathlib import Path
from urllib.parse import unquote, urlsplit

import yaml


ROOT = Path(__file__).resolve().parents[1]
READMES = (
    "README.md", "README_ZH.md", "LiteLLM/README.md", "LiteLLM/README_ZH.md",
    "tests/README.md", "tests/README_ZH.md", "infra/README_ZH.md",
    "deploy/README_ZH.md", "auth-proxy/README_ZH.md", ".github/README_ZH.md",
    "docs/customer-deployment-workflows-zh.md",
    "docs/customer-private-runner-preparation-zh.md",
    "docs/litellm-content-audit-phase1-customer-brief-zh.md",
    "docs/litellm-ingress-tls-certificate-design-zh.md",
    "docs/litellm-bom-cost-comparison-zh.md",
    "docs/litellm-azure-security-hardening-zh.md",
    "docs/litellm-stage7-entra-proxy-domains-2026-09-07.md",
    "docs/customer-migration-guide-zh.md",
    "docs/litellm-security-hardening-implementation-roadmap-zh.md",
    "docs/litellm-security-hardening-change-list-zh.md",
    "docs/litellm-code-completion-backlog-2026-09-07.md",
    "docs/litellm-stage8-l3-audit-observability-2026-09-07.md",
    "docs/litellm-stage9-edge-cutover-preparation-2026-09-07.md",
    "infra/edge/README_ZH.md",
)


class ProjectDocumentationTests(unittest.TestCase):
    def test_runner_connectivity_workflow_example_and_runbook_stay_aligned(self):
        from scripts.customer_migration import COMPONENTS
        from scripts.runner_connectivity import BACKUP_PROBES
        example = json.loads((ROOT / "config/customer.example.json").read_text())
        settings = example["parameters"]["runner-connectivity"]
        self.assertEqual(set(settings), COMPONENTS["runner-connectivity"][2])
        self.assertTrue(settings["managePeering"])
        self.assertTrue(settings["manageBlobDnsLink"])
        self.assertTrue(settings["runnerVirtualNetworkId"].startswith("REPLACE_"))
        for filename in ("customer-deploy.yml", "customer-migration.yml"):
            workflow = yaml.load((ROOT / ".github/workflows" / filename).read_text(), Loader=yaml.BaseLoader)
            self.assertIn("runner-connectivity", workflow["on"]["workflow_dispatch"]["inputs"]["component"]["options"])
        source = (ROOT / ".github/workflows/customer-runner-checks.yml").read_text()
        workflow = yaml.load(source, Loader=yaml.BaseLoader)
        self.assertEqual(workflow["on"]["workflow_dispatch"]["inputs"]["check_backup"]["default"], "false")
        self.assertIn("${{ inputs.check_backup }}", source)
        self.assertIn("--check-backup", source)
        guide = (ROOT / "docs/customer-migration-guide-zh.md").read_text()
        for value in (*settings, *BACKUP_PROBES, "check_backup=true", "同订阅", "S0-08"):
            self.assertIn(value, guide)
        self.assertNotIn("人工网络/权限准备，不是workflow按钮", guide)

    def test_runbook_stage_tables_match_workflows_and_controllers(self):
        from scripts.customer_migration import COMPONENTS
        from scripts.migration_runtime import validate_action
        from tests.test_customer_migration import customer_config

        workflows = {}
        for filename in ("customer-deploy.yml", "customer-runtime.yml"):
            workflow = yaml.load((ROOT / ".github/workflows" / filename).read_text(), Loader=yaml.BaseLoader)
            workflows[workflow["name"]] = workflow["on"]["workflow_dispatch"]["inputs"]
        guide = (ROOT / "docs/customer-migration-guide-zh.md").read_text()
        rows = re.findall(r"^\| (S\d+-\d+) \| (Customer infrastructure deployment|Customer private runtime operations) \| (\d) \| (component|action)=([a-z-]+) \| (plan|deploy|execute) \| ([^|]+) \| ([^|]+) \|$", guide, re.M)
        self.assertGreaterEqual(len(rows), 50)
        self.assertEqual({int(row[2]) for row in rows}, {0, 1, 3, 4, 5, 6, 7, 8, 9})
        self.assertEqual(len({row[0] for row in rows}), len(rows))
        plans = {}
        for step, workflow_name, stage, selector, action, operation, approval, confirmation in rows:
            with self.subTest(step=step):
                inputs = workflows[workflow_name]
                self.assertIn(stage, inputs["stage"]["options"])
                self.assertIn(action, inputs[selector]["options"])
                self.assertIn(operation, inputs["operation"]["options"])
                if selector == "component":
                    self.assertIn(int(stage), {3, 4, 5} if action == "platform" else {COMPONENTS[action][0]})
                else:
                    validate_action(customer_config(), int(stage), action)
                key = (workflow_name, stage, action)
                if operation == "plan":
                    self.assertIn("留空", approval)
                    self.assertEqual(confirmation.strip(), "留空")
                    self.assertNotEqual(action, "backup-restore")
                    plans[key] = step
                else:
                    self.assertEqual(confirmation.strip(), "test")
                    if action == "backup-restore":
                        self.assertEqual(operation, "execute")
                        self.assertEqual(approval.strip(), "留空")
                    else:
                        self.assertIn(plans[key], approval)

    def test_runbook_places_backup_requirements_next_to_execution(self):
        guide = (ROOT / "docs/customer-migration-guide-zh.md").read_text()
        backup = guide.split("### 阶段0：", 1)[1].split("### 阶段1：", 1)[0]
        for required in ("POSTGRES_RESTORE_IMAGE", "Environment Variable", "docker pull --platform linux/amd64", ".RepoDigests", "backupAutomationPrincipalId", "Private DNS", "backupBlob", "backupSha256", "只支持execute"):
            self.assertIn(required, backup)
        for required in ("Legacy direct hash input; prefer approved_run_id", "Successful matching infrastructure plan run ID", "MIGRATION_RELEASE_JSON", "最终目标选择/接线待补齐", "当前无stop-legacy workflow按钮"):
            self.assertIn(required, guide)
        self.assertNotIn("Stage8/application仍要求auditRuntime", guide)
        self.assertNotIn("本仓库尚未自动编排该controller/LB", guide)

    def test_backup_preflight_review_separates_network_identity_and_data_proofs(self):
        guide = (ROOT / "docs/customer-migration-guide-zh.md").read_text()
        review = guide.split("#### 0-C1. S0-05逐项审查", 1)[1].split("### 阶段1：", 1)[0]
        for required in ("properties.parameters.backupAutomationPrincipalId.value", "properties.outputs.backupStorage.value", "networkInterfaces[0].id", "ipConfigurations[].privateIPAddress", "peeringState", "allowVirtualNetworkAccess", "FullyInSync", "az network private-dns link vnet list", "show-effective-route-table", "list-effective-nsg", 'getent ahostsv4 "$BLOB_HOST"', "--noproxy '*'", "remote_ip=%{remote_ip}", "--connect-timeout 5", "Storage Blob Data Contributor", "--auth-mode login", "--subresource=exec", "Blob只读探针已由check_backup覆盖", "不能证明Blob授权", "列举成功不能证明上传/下载成功"):
            with self.subTest(required=required):
                self.assertIn(required, review)
        commands = "\n".join(re.findall(r"```bash\n(.*?)\n```", review, re.S))
        self.assertEqual(commands.count("az network vnet peering list"), 2)
        self.assertNotRegex(commands, r"az\s+network\s+[^\n]*(?:\bcreate\b|\bdelete\b|\bupdate\b)")
        self.assertNotIn("role assignment create", commands)
        self.assertNotIn("curl -k", commands)
        self.assertNotIn("kubectl exec", commands)

    def test_backup_identity_example_and_value_discovery_match_runtime_contract(self):
        from scripts.customer_migration import COMPONENTS, parameters_for
        from tests.test_customer_migration import customer_config

        example = json.loads((ROOT / "config/customer.example.json").read_text())
        for name in ("backupOwnerPrincipalId", "backupAutomationPrincipalId"):
            with self.subTest(parameter=name):
                self.assertIn(name, COMPONENTS["backup"][2])
                self.assertRegex(example["parameters"]["backup"][name], r"^REPLACE_[A-Z_]+$")
        config = customer_config()
        principal = "55555555-5555-4555-8555-555555555555"
        config["parameters"]["backup"] = {
            **example["parameters"]["backup"],
            "logAnalyticsWorkspaceName": "customer-logs",
            "backupOwnerPrincipalId": "33333333-3333-4333-8333-333333333333",
            "backupAutomationPrincipalId": principal,
            "virtualNetworkName": "target-vnet",
        }
        _, document = parameters_for(config, 0, "backup")
        self.assertEqual(document["parameters"]["backupAutomationPrincipalId"]["value"], principal)
        guide = (ROOT / "docs/customer-migration-guide-zh.md").read_text()
        identity = guide.split("#### 0-A1. ", 1)[1].split("#### 0-B. ", 1)[0]
        for required in ("backupOwnerPrincipalId", "backupAutomationPrincipalId", "AZURE_RUNTIME_CLIENT_ID", "Enterprise applications", 'az ad sp show --id "$RUNTIME_CLIENT_ID"', "clientId:appId,objectId:id", "az identity show", "objectId:principalId", 'az ad user show --id "$BACKUP_OWNER_UPN"', "claims.appid", "claims/objectidentifier", "CUSTOMER_CONFIG_JSON", "component=backup", "Storage Blob Data Contributor", "--assignee-object-id", "--include-inherited"):
            with self.subTest(required=required):
                self.assertIn(required, identity)

    def test_runbook_covers_configuration_for_all_primary_workflows(self):
        guide = (ROOT / "docs/customer-migration-guide-zh.md").read_text()
        configuration = guide.split("### 2.1 Variables和Secrets", 1)[1].split("### 2.2", 1)[0]
        files = ("customer-deploy.yml", "customer-runtime.yml", "customer-migration.yml", "customer-acceptance.yml", "customer-runner-checks.yml", "customer-gateway-checks.yml", "promote-litellm-image.yml")
        for filename in files:
            workflow = (ROOT / ".github/workflows" / filename).read_text()
            for context, name in set(re.findall(r"\b(vars|secrets)\.([A-Z][A-Z0-9_]*)", workflow)):
                with self.subTest(workflow=filename, context=context, name=name):
                    self.assertIn("`" + name + "`", configuration)

    def test_migration_guide_documents_actual_runner_check_inputs(self):
        guide = (ROOT / "docs/customer-migration-guide-zh.md").read_text()
        section = guide.split("### 2.1 Variables和Secrets", 1)[1].split("### 2.2", 1)[0]
        rows = {
            columns[0].strip("`"): columns[1]
            for line in section.splitlines() if line.startswith("| `")
            for columns in ([cell.strip() for cell in line.strip("|").split("|")],)
        }
        workflow = (ROOT / ".github/workflows/customer-runner-checks.yml").read_text()
        inputs = set(re.findall(r"\$\{\{\s*(vars|secrets)\.([A-Z][A-Z0-9_]*)\s*\}\}", workflow))
        self.assertIn(("vars", "AZURE_RUNTIME_CLIENT_ID"), inputs)
        for context, name in inputs:
            expected = "Environment Secret" if context == "secrets" else "Environment Variable"
            if name == "MIGRATION_PRIVATE_RUNNER_LABELS":
                expected = "Repository Variable"
            with self.subTest(name=name):
                self.assertEqual(rows.get(name), expected)
        self.assertEqual(rows.get("AZURE_CLIENT_ID"), "Environment Variable")
        self.assertIn("Runner检查不读取它", section)
        self.assertNotIn("兼容Variable", section)

    def test_public_fork_setup_explains_secret_and_manual_confirmation_contract(self):
        for name in ("README.md", "README_ZH.md", "docs/customer-deployment-workflows-zh.md", "docs/customer-private-runner-preparation-zh.md"):
            text = (ROOT / name).read_text()
            with self.subTest(document=name):
                self.assertIn("fork", text)
                self.assertIn("WORKFLOW_ARTIFACT_KEY", text)
                self.assertIn("Customer private runner checks", text)
        guide = (ROOT / "docs/customer-deployment-workflows-zh.md").read_text()
        self.assertIn("scripts.workflow_security open", guide)
        self.assertIn("independentlyVerified=false", guide)
        self.assertIn("legacy-access-restore", guide)
        self.assertIn("edge-bind", guide)

    def test_current_guides_select_native_audit_without_claiming_rollout(self):
        for name in ("README.md", "README_ZH.md", "docs/customer-migration-guide-zh.md", "docs/customer-deployment-workflows-zh.md"):
            text = (ROOT / name).read_text()
            with self.subTest(document=name):
                self.assertIn("Spend Logs", text)
                self.assertIn("2026-09-10", text)
                self.assertNotIn("L3是首发必需", text)
                self.assertNotIn("L3 is a required release capability", text)
        guide = (ROOT / "docs/customer-deployment-workflows-zh.md").read_text()
        self.assertIn("不是原生模式已上线", guide)
        self.assertIn("不跳过Stage8进入Stage9", guide)
        self.assertIn("原生模式的observability不再依赖auditRuntime", guide)
        self.assertIn("readyForCustomerMigration", guide)
        self.assertIn("原生UI/API受控查询", guide)

    def test_audit_costs_describe_native_storage_and_optional_enhancement(self):
        text = (ROOT / "docs/litellm-bom-cost-comparison-zh.md").read_text()
        section = text.split("## 6. 员工与 Agent 上下文审计成本影响", 1)[1].split("## 7.", 1)[0]
        for required in ("原生 Spend Logs", "PostgreSQL", "WAL", "备份", "自建 L3", "本轮不生成未经询价和压测的新总价"):
            self.assertIn(required, section)
        self.assertNotIn("L3通过异步 callback输出到独立存储", section)
        self.assertNotIn("`store_prompts_in_spend_logs` 保持 `false`", section)

    def test_customer_api_example_uses_admission_only_bindings(self):
        text = (ROOT / "docs/customer-deployment-workflows-zh.md").read_text()
        block = next(block for block in re.findall(r"```json\n(.*?)\n```", text, re.S) if '"proxy":' in block)
        example = json.loads("{" + block + "}")
        api = next(item for item in example["proxy"]["bindings"] if item["plane"] == "api")
        self.assertNotIn("models", api)
        self.assertNotIn("keyFile", api)
        for name in ("auth-proxy/README_ZH.md", "docs/customer-deployment-workflows-zh.md", "docs/litellm-content-audit-phase1-customer-brief-zh.md"):
            with self.subTest(document=name):
                self.assertIn("X-LiteLLM-API-Key", (ROOT / name).read_text())

    def test_customer_readme_local_links_exist(self):
        for name in READMES:
            document = ROOT / name
            for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", document.read_text(encoding="utf-8")):
                link = urlsplit(target)
                if link.scheme or link.netloc or not link.path:
                    continue
                with self.subTest(document=name, target=target):
                    self.assertTrue((document.parent / unquote(link.path)).exists())

    def test_root_readmes_point_to_customer_solution(self):
        for name in ("README.md", "README_ZH.md"):
            text = (ROOT / name).read_text(encoding="utf-8")
            with self.subTest(document=name):
                self.assertTrue(text.startswith("# "))
                self.assertIn("LiteLLM on Azure", text.splitlines()[0])
                self.assertIn("docs/customer-migration-guide-zh.md", text)
                self.assertIn("CUSTOMER_CONFIG_JSON", text)
                self.assertIn("docs/litellm-code-completion-backlog-2026-09-07.md", text)

    def test_removed_gateway_has_no_entrypoint_or_sdk_dependency(self):
        for name in ("APIM/deploy_mi_apim.py", "APIM/azure-openai.json", "APIM/README.md", "APIM/README_ZH.md", "tests/test_apim_deploy.py"):
            with self.subTest(path=name):
                self.assertFalse((ROOT / name).exists())
        self.assertNotIn("azure-mgmt-apimanagement", (ROOT / "requirements.txt").read_text())

    def test_full_plan_covers_both_paths_and_runtime_acceptance(self):
        text = (ROOT / "docs/customer-deployment-workflows-zh.md").read_text()
        for number in range(1, 13):
            self.assertIn(f"| A{number:02d} ", text)
        for required in ("greenfield", "RTO/RPO", "Prisma", "OIDC", "Python", "Go", "MIGRATION_MANIFEST_YAML"):
            self.assertIn(required, text)