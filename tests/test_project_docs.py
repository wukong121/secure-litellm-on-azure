import json
import re
import unittest
from pathlib import Path
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
READMES = (
    "README.md", "README_ZH.md", "LiteLLM/README.md", "LiteLLM/README_ZH.md",
    "tests/README.md", "tests/README_ZH.md", "infra/README_ZH.md",
    "deploy/README_ZH.md", "auth-proxy/README_ZH.md", ".github/README_ZH.md",
    "docs/customer-deployment-workflows-zh.md",
    "docs/litellm-content-audit-phase1-customer-brief-zh.md",
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
        self.assertIn("当前托管observability仍依赖auditRuntime", guide)

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