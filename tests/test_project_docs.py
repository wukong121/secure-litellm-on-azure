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
    "docs/customer-stage0-acceptance-checklist-zh.md",
    "docs/customer-stage1-acceptance-checklist-zh.md",
    "docs/customer-stage2-acceptance-checklist-zh.md",
    "docs/customer-stage3-acceptance-checklist-zh.md",
    "docs/litellm-security-hardening-implementation-roadmap-zh.md",
    "docs/litellm-security-hardening-change-list-zh.md",
    "docs/litellm-code-completion-backlog-2026-09-07.md",
    "docs/litellm-stage8-l3-audit-observability-2026-09-07.md",
    "docs/litellm-stage9-edge-cutover-preparation-2026-09-07.md",
    "infra/edge/README_ZH.md",
)


class ProjectDocumentationTests(unittest.TestCase):
    def test_runtime_kubernetes_authorization_is_identity_and_scope_specific(self):
        import subprocess

        guide = (ROOT / "docs/customer-migration-guide-zh.md").read_text()
        section = guide.split("#### 4-B2. runtime身份的Kubernetes预授权", 1)[1].split("#### 4-C.", 1)[0]
        self.assertIn("#4-b2-runtime身份的kubernetes预授权", guide)
        for value in ("AZURE_RUNTIME_CLIENT_ID", "RUNTIME_OBJECT_ID", "不是`AZURE_CLIENT_ID`", "aadProfile.enableAzureRbac=true", "roleDefinitions/write", "roleAssignments/write", "所有Namespace", "Pod Security", "ServiceAccount", "SecretProviderClass", "不能创建Namespace", "不会收紧", "个人管理员kubectl成功不能代替", "不要求重部署platform"):
            self.assertIn(value, section)
        definition = json.loads(re.search(r"```json\n(.*?)\n```", section, re.S)[1])["properties"]
        self.assertEqual(definition["roleName"], "LLMGW AKS Namespace Bootstrapper")
        self.assertEqual(definition["assignableScopes"], ["/subscriptions/REPLACE_TARGET_SUBSCRIPTION_ID/resourceGroups/REPLACE_TARGET_RESOURCE_GROUP"])
        self.assertEqual(definition["permissions"], [{
            "actions": [], "notActions": [], "dataActions": [
                "Microsoft.ContainerService/managedClusters/namespaces/read",
                "Microsoft.ContainerService/managedClusters/namespaces/write",
            ], "notDataActions": [],
        }])
        snippets = re.findall(r"```bash\n(.*?)\n```", section, re.S)
        self.assertEqual(len(snippets), 4)
        for source in snippets:
            self.assertTrue(source.startswith("set -euo pipefail\n"))
            self.assertNotIn("|| break", source)
            result = subprocess.run(["bash", "-n"], input=source, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("--admin", source)
            self.assertNotIn("get-access-token", source)
            self.assertNotIn("role assignment delete", source)
        self.assertIn('--id "$RUNTIME_CLIENT_ID"', snippets[0])
        self.assertIn('"${AKS_ID}/namespaces/litellm"', snippets[1])
        self.assertIn('"Azure Kubernetes Service RBAC Reader"', snippets[1])
        self.assertIn("for NAMESPACE in llm-api-ingress llm-admin-ingress; do", snippets[2])
        self.assertIn('--scope "${AKS_ID}/namespaces/${NAMESPACE}"', snippets[2])
        self.assertIn('"Azure Kubernetes Service RBAC Writer"', snippets[2])
        for source in snippets[1:3]:
            self.assertIn('--assignee-object-id "$RUNTIME_OBJECT_ID" --assignee-principal-type ServicePrincipal', source)
        self.assertIn("--include-inherited", snippets[3])
        self.assertIn("--fill-principal-name false", snippets[3])

    def test_target_connectivity_workflow_and_upgrade_instructions_match(self):
        import subprocess
        from scripts.customer_migration import COMPONENTS

        guide = (ROOT / "docs/customer-migration-guide-zh.md").read_text()
        section = guide.split("#### 4-B1. Runner到新AKS的DNS连接", 1)[1].split("#### 4-B2.", 1)[0]
        example = json.loads((ROOT / "config/customer.example.json").read_text())
        self.assertEqual(set(example["parameters"]["runner-target-connectivity"]), COMPONENTS["runner-target-connectivity"][2])
        self.assertEqual(COMPONENTS["runner-target-connectivity"][0], 4)
        for filename in ("customer-deploy.yml", "customer-migration.yml"):
            workflow = yaml.load((ROOT / ".github/workflows" / filename).read_text(), Loader=yaml.BaseLoader)
            self.assertIn("runner-target-connectivity", workflow["on"]["workflow_dispatch"]["inputs"]["component"]["options"])
        self.assertIn("connectivity-review.json", (ROOT / ".github/workflows/customer-deploy.yml").read_text())
        self.assertNotIn("runner-target-connectivity", (ROOT / ".github/workflows/customer-runner-checks.yml").read_text())
        for value in ("AZURE_CLIENT_ID", "节点RG", "join/action", "manageDnsLink", "privateFqdn", "nodeResourceGroup", "privatelink.azurecr.io", "acrPrivateEndpointIps", "acrManagement", "S4-04A", "S4-04B", "关闭自动注册", "不用重跑platform或certificate-vault", "无需增加字段", "ACR登录端点解析到公网并返回403", "single-operator", "旧SHA Stage0–3账本", "当前SHA的匹配plan", "配置指纹匹配", "双人模式", "源镜像", "7天", "实际结论变化时仍须实测重验", "旧platform回执SHA", "不自动覆盖A记录", "只允许", "reused", "external"):
            self.assertIn(value, section)
        self.assertLess(guide.index("| S4-04B |"), guide.index("| S4-05 |"))
        settings = json.loads("{" + re.search(r"```json\n(.*?)\n```", section, re.S)[1] + "}")
        self.assertEqual(settings["runner-target-connectivity"], example["parameters"]["runner-target-connectivity"])
        for source in re.findall(r"```bash\n(.*?)\n```", section, re.S):
            self.assertEqual(subprocess.run(["bash", "-n"], input=source, capture_output=True, text=True).returncode, 0)

    def test_aks_ingress_role_recovery_is_narrow_and_requires_a_new_runtime_plan(self):
        from scripts.customer_migration import COMPONENTS

        guide = (ROOT / "docs/customer-migration-guide-zh.md").read_text()
        section = guide.split("**入口Service一直Pending、`lb-ready-api`超时的升级恢复：**", 1)[1].split("#### 4-B2.", 1)[0]
        self.assertEqual(COMPONENTS["aks-ingress-role"][:2], (4, "aks-ingress-role"))
        for filename in ("customer-deploy.yml", "customer-migration.yml"):
            workflow = yaml.load((ROOT / ".github/workflows" / filename).read_text(), Loader=yaml.BaseLoader)
            self.assertIn("aks-ingress-role", workflow["on"]["workflow_dispatch"]["inputs"]["component"]["options"])
        for value in (
            "identity.principalId", "不是kubelet身份", "CUSTOMER_CONFIG_JSON`不新增字段",
            "component=aks-ingress-role", "Microsoft.Authorization/roleAssignments", "Network Contributor",
            "专用目标VNet下的一条", "不能为通过What-if扩大", "新建S4-11", "当前部分落地状态",
            "不能复用旧S4-11", "无需重跑S4-01/02 platform", "Service仍Pending",
        ):
            self.assertIn(value, section)
        self.assertLess(section.index("operation=plan"), section.index("operation=deploy"))
        self.assertLess(section.index("operation=deploy"), section.index("新建S4-11"))

    def test_certificate_lock_permissions_are_explicit_and_target_scoped(self):
        import subprocess

        guide = (ROOT / "docs/customer-migration-guide-zh.md").read_text()
        section = guide.split("#### 4-A1. 证书Vault防删除锁权限", 1)[1].split("#### 4-B.", 1)[0]
        self.assertIn("#4-a1-证书vault防删除锁权限", guide)
        self.assertLess(guide.index("#### 4-A1."), guide.index("| S4-03 |"))
        definition = json.loads(re.search(r"```json\n(.*?)\n```", section, re.S).group(1))["properties"]
        self.assertEqual(definition["roleName"], "LLMGW Resource Lock Writer")
        self.assertEqual(definition["assignableScopes"], ["/subscriptions/REPLACE_TARGET_SUBSCRIPTION_ID/resourceGroups/REPLACE_TARGET_RESOURCE_GROUP"])
        self.assertEqual(definition["permissions"], [{
            "actions": ["Microsoft.Authorization/locks/read", "Microsoft.Authorization/locks/write"],
            "notActions": [], "dataActions": [], "notDataActions": [],
        }])
        template = (ROOT / "infra/certificate-vault/main.bicep").read_text()
        self.assertIn("resource deleteLock 'Microsoft.Authorization/locks@", template)
        for value in ("protect-key-vault-from-deletion", "CanNotDelete"):
            self.assertIn(value, template)
            self.assertIn(value, section)
        for required in ("AZURE_CLIENT_ID", "DEPLOY_OBJECT_ID", "不是Client ID本身", "不是模型所在RG", "roleDefinitions/write", "locks/delete", "不是只允许某一个Vault锁", "权限传播", "新建S4-03", "新SHA", "不需重跑已成功的platform", "唯一原因"):
            self.assertIn(required, section)
        snippets = re.findall(r"```bash\n(.*?)\n```", section, re.S)
        self.assertEqual(len(snippets), 1)
        source = snippets[0]
        result = subprocess.run(["bash", "-n"], input=source, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        for required in ("--subscription", '--assignee-object-id "$DEPLOY_OBJECT_ID"', '--scope "$TARGET_RG_SCOPE"', "--include-inherited", "--fill-principal-name false"):
            self.assertIn(required, source)
        for forbidden in ("role assignment create", "role definition create", "az lock", "get-access-token", "--debug"):
            self.assertNotIn(forbidden, source)

    def test_stage4_partial_network_failure_requires_replanning(self):
        guide = (ROOT / "docs/customer-migration-guide-zh.md").read_text()
        section = guide.split("**S4-02部分失败时：**", 1)[1].split("**S4-04之后", 1)[0]
        for required in ("Operation details", "AnotherOperationInProgress", "dependsOn", "冲突操作结束", "产生费用", "不能声称自动回滚", "新建S4-01", "新的成功plan ID", "不要回放Stage0", "新SHA"):
            self.assertIn(required, section)

    def test_stage4_model_authorization_matches_templates_and_safe_instructions(self):
        import subprocess

        guide = (ROOT / "docs/customer-migration-guide-zh.md").read_text()
        section = guide.split("#### 4-0. 模型账号的一次性授权", 1)[1].split("#### 4-A.", 1)[0]
        self.assertLess(guide.index("#### 4-0."), guide.index("| S4-01 |"))
        self.assertIn("#4-0-模型账号的一次性授权", guide)
        definition = json.loads(re.search(r"```json\n(.*?)\n```", section, re.S).group(1))["properties"]
        self.assertEqual(definition["roleName"], "LLMGW Model Deployment Operator")
        self.assertEqual(len(definition["permissions"]), 1)
        permissions = definition["permissions"][0]
        self.assertEqual(set(permissions["actions"]), {
            "*/read", "Microsoft.Resources/deployments/*",
            "Microsoft.CognitiveServices/accounts/privateEndpointConnectionsApproval/action",
        })
        for field in ("notActions", "dataActions", "notDataActions"):
            self.assertEqual(permissions[field], [])
        self.assertEqual(len(definition["assignableScopes"]), 2)
        for scope in definition["assignableScopes"]:
            self.assertRegex(scope, r"^/subscriptions/REPLACE_MODEL_SUBSCRIPTION_ID_[12]/resourceGroups/REPLACE_MODEL_RG_[12]$")
        role_id = "5e0bd9bd-7b93-4f28-af87-19fc36ad61bd"
        self.assertIn(role_id, (ROOT / "infra/modules/model-access-role/main.bicep").read_text())
        self.assertIn(role_id, section)
        for required in (
            "AZURE_CLIENT_ID", "不是`AZURE_RUNTIME_CLIENT_ID`", "Enterprise applications", "az ad sp show",
            "Assignable scopes", "roleDefinitions/write", "JSON页", "数据面", "同一Entra租户",
            "Constrain roles", "不限制接收者", "Constrain roles and principal types", "Service principals",
            "@Request", "@Resource", "roleAssignments/write", "roleAssignments/delete", "同时限制新增和删除",
            "顶层`principalType=ServicePrincipal`", "仅补Azure授权", "新SHA", "plan本身不要求前序验收",
        ):
            self.assertIn(required, section)
        workflow = yaml.load((ROOT / ".github/workflows/customer-deploy.yml").read_text(), Loader=yaml.BaseLoader)
        login = next(step for step in workflow["jobs"]["infrastructure"]["steps"] if step.get("uses", "").startswith("azure/login@"))
        self.assertEqual(login["with"]["client-id"], "${{ vars.AZURE_CLIENT_ID }}")
        snippets = re.findall(r"```bash\n(.*?)\n```", section, re.S)
        self.assertEqual(len(snippets), 2)
        for source in snippets:
            result = subprocess.run(["bash", "-n"], input=source, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            for forbidden in ("role assignment create", "role definition create", "get-access-token", "list-keys", "--debug"):
                self.assertNotIn(forbidden, source)
        for value in ("--include-inherited", "--assignee-object-id", "--fill-principal-name false", "conditionVersion:conditionVersion"):
            self.assertIn(value, snippets[-1])

    def test_certificate_workflow_example_and_manual_import_stay_aligned(self):
        import subprocess
        from scripts.customer_migration import COMPONENTS

        example = json.loads((ROOT / "config/customer.example.json").read_text())
        settings = example["parameters"]["certificate-vault"]
        self.assertEqual(set(settings), COMPONENTS["certificate-vault"][2])
        self.assertFalse(example["parameters"]["platform"]["createStage5KeyVaultPrivateDnsZone"])
        for filename in ("customer-deploy.yml", "customer-migration.yml"):
            workflow = yaml.load((ROOT / ".github/workflows" / filename).read_text(), Loader=yaml.BaseLoader)
            self.assertIn("certificate-vault", workflow["on"]["workflow_dispatch"]["inputs"]["component"]["options"])
        guide = (ROOT / "docs/customer-migration-guide-zh.md").read_text()
        section = guide.split("### 阶段4：", 1)[1].split("### 阶段5：", 1)[0]
        for value in (*settings, "Secrets User", "Secrets Officer", "certificateMaterialsImported=false", "25KB", "S4-03", "S4-04", "S4-11", "S4-12", "Object ID", "不打开Vault公网", "CA的私钥独立保管"):
            self.assertIn(value, section)
        snippets = re.findall(r"```bash\n(.*?)\n```", section.split("#### 4-C.", 1)[1], re.S)
        self.assertEqual(len(snippets), 7)
        for source in snippets:
            result = subprocess.run(["bash", "-n"], input=source, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
        for value in ("两个Secret，不是两张新证书", "admin-ca.key", "admin.csr", "没有中间链", "上传不需要登录Runner", "客户本机上传", "公有CA签发", "企业PKI签发", "不是已签发证书", "不返回值", "Secret Identifier"):
            self.assertIn(value, section)
        for value in ("specific virtual networks and IP addresses", "实际公网出口IPv4", "/32", "Microsoft.KeyVault/vaults/write", "无论上传成功、失败或中断", "Disable public access", "ipRules=[]", "bypass=None", "网络策略拒绝", "certificates.api/admin.secretId", "applied=true", "verified=true"):
            self.assertIn(value, section)
        self.assertLess(section.index("**6. 立即关闭公网"), section.index("**7. 验证上传材料"))
        commands = "\n".join(snippets)
        for forbidden in ("az network bastion", "scp -P", "AZURE_CONFIG_DIR", "az keyvault update", "az keyvault secret show"):
            self.assertNotIn(forbidden, commands)
        login = snippets[4]
        self.assertTrue(login.startswith("set -euo pipefail\n"))
        for value in ("az login --tenant", "az ad signed-in-user show", 'API_PEM_FILE="temp/certificate-import/api.pem"', "az keyvault secret list"):
            self.assertIn(value, login)
        upload = snippets[5]
        self.assertEqual(upload.count("az keyvault secret set"), 2)
        self.assertEqual(upload.count("--query '{id:id,enabled:attributes.enabled}'"), 2)
        self.assertNotIn("--value", upload)
        self.assertNotIn("certificate import", upload)
        self.assertIn("az keyvault show", snippets[6])
        self.assertIn("publicNetworkAccess:properties.publicNetworkAccess", snippets[6])
        reference = (ROOT / "docs/customer-deployment-workflows-zh.md").read_text()
        self.assertIn("customer-migration-guide-zh.md#4-a-共用证书vault的配置与取值", reference)
        self.assertIn("customer-migration-guide-zh.md#4-c-部署后手动导入两个secret", reference)
        self.assertIn("立即关闭公网并清除临时IP规则", reference)
        template = (ROOT / "infra/certificate-vault/main.bicep").read_text()
        self.assertIn("publicNetworkAccess: 'Disabled'", template)
        self.assertIn("bypass: 'None'", template)
        self.assertIn("infra/certificate-vault/main.bicep", (ROOT / "scripts/validate-stage4.sh").read_text())

    def test_documented_certificate_creation_produces_valid_csr_and_admin_leaf(self):
        import subprocess
        import tempfile
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization
        from cryptography.x509.oid import ExtendedKeyUsageOID
        from scripts.private_ingress import certificate_material

        section = (ROOT / "docs/customer-migration-guide-zh.md").read_text().split("#### 4-C.", 1)[1]
        snippets = re.findall(r"```bash\n(.*?)\n```", section, re.S)
        api_source = snippets[1].replace("REPLACE_BASE_DOMAIN_FROM_CUSTOMER_JSON", "customer.invalid")
        admin_source = snippets[2].replace("REPLACE_BASE_DOMAIN_FROM_CUSTOMER_JSON", "customer.invalid")
        self.assertIn("openssl req -x509 -newkey", admin_source)
        admin_source = admin_source.replace("openssl req -x509", "openssl req -passout pass:synthetic-test-only -x509", 1)
        admin_source = admin_source.replace("openssl x509 -req", "openssl x509 -passin pass:synthetic-test-only -req", 1)
        with tempfile.TemporaryDirectory() as directory:
            for source in (api_source, admin_source):
                result = subprocess.run(["bash"], input=source, cwd=directory, capture_output=True, text=True, timeout=60, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)
            api_directory = next((Path(directory) / "temp").glob("api-csr.*"))
            csr = x509.load_pem_x509_csr((api_directory / "api.csr").read_bytes())
            self.assertTrue(csr.is_signature_valid)
            self.assertEqual(csr.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName), ["llm-api.customer.invalid"])
            api_key = serialization.load_pem_private_key((api_directory / "api.key").read_bytes(), password=None)
            self.assertEqual(api_key.public_key().public_numbers(), csr.public_key().public_numbers())
            admin_directory = next((Path(directory) / "temp").glob("admin-pki.*"))
            self.assertEqual(admin_directory.stat().st_mode & 0o777, 0o700)
            self.assertEqual((admin_directory / "admin.key").stat().st_mode & 0o777, 0o600)
            self.assertIn(b"BEGIN ENCRYPTED PRIVATE KEY", (admin_directory / "admin-ca.key").read_bytes())
            certificate = x509.load_pem_x509_certificate((admin_directory / "admin.crt").read_bytes())
            self.assertFalse(certificate.extensions.get_extension_for_class(x509.BasicConstraints).value.ca)
            self.assertIn(ExtendedKeyUsageOID.SERVER_AUTH, certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value)
            bundle = (admin_directory / "admin.crt").read_text() + "\n" + (admin_directory / "admin.key").read_text()
            self.assertLess(len(bundle.encode()), 25 * 1024)
            material = certificate_material(bundle, "llm-admin.customer.invalid")
            self.assertRegex(material["sha256"], r"^[0-9a-f]{64}$")

    def test_manual_certificate_validation_outputs_metadata_only_and_rejects_oversize(self):
        import io
        from unittest.mock import patch

        guide = (ROOT / "docs/customer-migration-guide-zh.md").read_text().split("#### 4-C.", 1)[1]
        source = re.search(r"<<'PY'\n(.*?)\nPY", guide, re.S).group(1)
        material = {"certificate": "private-value-sentinel", "key": "private-value-sentinel", "sha256": "a" * 64, "expiresAt": "synthetic-expiry"}
        cases = ((b"synthetic-pem", None, True), (b"x" * (25 * 1024 + 1), None, False), (b"invalid", ValueError("private-value-sentinel"), False))
        for raw, error, success in cases:
            with self.subTest(success=success, size=len(raw)), patch("sys.argv", ["-", "customer.invalid", "api.pem", "admin.pem"]), patch("pathlib.Path.read_bytes", return_value=raw), patch("scripts.private_ingress.certificate_material", return_value=material, side_effect=error) as validate, patch("sys.stdout", new_callable=io.StringIO) as output:
                if success:
                    exec(compile(source, "manual-certificate-validation", "exec"), {})
                    self.assertEqual(validate.call_count, 2)
                    self.assertEqual([call.args[1] for call in validate.call_args_list], ["llm-api.customer.invalid", "llm-admin.customer.invalid"])
                    self.assertIn("sha256=" + "a" * 64, output.getvalue())
                else:
                    with self.assertRaisesRegex(SystemExit, "no private material printed") as failure:
                        exec(compile(source, "manual-certificate-validation", "exec"), {})
                    self.assertNotIn("private-value-sentinel", str(failure.exception))
                    self.assertEqual(output.getvalue(), "")
                self.assertNotIn("private-value-sentinel", output.getvalue())

    def test_stage_three_guide_matches_checks_and_stage_flags(self):
        from scripts.customer_migration import parameters_for, stage_checks
        from tests.test_customer_migration import customer_config

        path = "docs/customer-stage3-acceptance-checklist-zh.md"
        guide = (ROOT / path).read_text()
        self.assertIn("!" + path, (ROOT / ".gitignore").read_text().splitlines())
        main = (ROOT / "docs/customer-migration-guide-zh.md").read_text()
        section = main.split("### 阶段3：", 1)[1].split("### 阶段4：", 1)[0]
        self.assertIn(Path(path).name, section)
        config = customer_config()
        for check in stage_checks(3, config):
            self.assertRegex(guide, r"## [2-4]\. " + check + "：")
        self.assertIn(",".join(stage_checks(3, config)), guide)
        self.assertIn(",".join(stage_checks(3, config)), section)
        _, document = parameters_for(config, 3, "platform")
        for key, value in (("deployContainerRegistry", True), ("deployStage4", False), ("deployStage5", False), ("containerRegistryPublicNetworkAccess", "Disabled")):
            self.assertEqual(document["parameters"][key]["value"], value)
            self.assertIn(key, guide)
        workflow = yaml.load((ROOT / ".github/workflows/customer-acceptance.yml").read_text(), Loader=yaml.BaseLoader)
        for field in ("reviewed_run_id", "checked_items", "evidence_notes", "confirm_environment"):
            self.assertIn(workflow["on"]["workflow_dispatch"]["inputs"][field]["description"], guide)
        for step, operation in (("S3-03", "draft"), ("S3-05", "confirm")):
            self.assertIn(f"| {step} | {workflow['name']} | {operation} |", section)
        self.assertIn("S3-04", section)
        for prefix, count in (("O", 3), ("S", 4), ("T", 4)):
            for number in range(1, count + 1):
                self.assertIn(f"### {prefix}-{number} ", guide)

    def test_stage_three_guide_distinguishes_public_source_and_private_evidence(self):
        guide = (ROOT / "docs/customer-stage3-acceptance-checklist-zh.md").read_text()
        source = (ROOT / ".github/workflows/source-image-checks.yml").read_text()
        self.assertNotIn("workflow_security seal", source)
        workflow = yaml.load(source, Loader=yaml.BaseLoader)
        uploads = [step["with"] for step in workflow["jobs"]["source"]["steps"] if "upload-artifact@" in step.get("uses", "")]
        self.assertEqual(len(uploads), 1)
        for filename in uploads[0]["path"].splitlines():
            self.assertIn(Path(filename).name, guide)
        self.assertIn("不需要解密", guide)
        for required in ("--severity CRITICAL --ignore-unfixed --exit-code 1", "stageAccepted", "不是独立认证或数字签名", "不要求ACR私网拉取成功", "输出中的未来资源名不是部署成功证明", "--assignee-object-id", "--include-inherited", "networkRuleBypassOptions=None", "当前confirm不支持部分通过", "12至4000字符", "independentlyVerified=false", "acceptance-record-test-3-<run ID>"):
            self.assertIn(required, guide)
        infrastructure = (ROOT / ".github/workflows/customer-deploy.yml").read_text()
        self.assertIn("workflow_security seal", infrastructure)
        for filename in ("reviewed-plan.json", "deployment-receipt.json", "deployment-outputs.json"):
            self.assertIn(filename, infrastructure)
            self.assertIn(filename, guide)

    def test_stage_three_guide_links_and_shell_examples(self):
        import subprocess

        path = ROOT / "docs/customer-stage3-acceptance-checklist-zh.md"
        guide = path.read_text()
        for target in re.findall(r"\]\(([^)]+)\)", guide):
            link = urlsplit(target)
            if link.scheme or not link.fragment:
                continue
            with self.subTest(target=target):
                linked_path = path.parent / unquote(link.path)
                headings = re.findall(r"^#{1,6} (.+)$", linked_path.read_text(), re.M)
                anchors = {re.sub(r"[^\w -]", "", heading.lower()).replace(" ", "-") for heading in headings}
                self.assertIn(unquote(link.fragment), anchors)
        snippets = re.findall(r"```bash\n(.*?)\n```", guide, re.S)
        self.assertGreaterEqual(len(snippets), 9)
        for index, source in enumerate(snippets):
            with self.subTest(snippet=index):
                result = subprocess.run(["bash", "-n"], input=source, capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)
                for forbidden in ("get-access-token", "credential show", "role assignment create", "acr update", "acr login", "az aks stop", "docker pull", "rm -rf", "curl -k"):
                    self.assertNotIn(forbidden, source)

    def test_stage_two_guide_matches_native_checks_and_workflow_inputs(self):
        from scripts.customer_migration import stage_checks
        from scripts.native_audit import native_audit_settings
        from tests.test_customer_migration import customer_config

        path = "docs/customer-stage2-acceptance-checklist-zh.md"
        guide = (ROOT / path).read_text()
        self.assertIn("!" + path, (ROOT / ".gitignore").read_text().splitlines())
        main = (ROOT / "docs/customer-migration-guide-zh.md").read_text()
        section = main.split("### 阶段2：", 1)[1].split("### 阶段3：", 1)[0]
        self.assertIn(Path(path).name, section)
        examples = re.findall(r"```json\n(.*?)\n```", guide, re.S)
        self.assertEqual(len(examples), 1)
        config = customer_config()
        config.update(json.loads(examples[0]))
        self.assertEqual(native_audit_settings(config)["mode"], "native")
        for check in stage_checks(2, config):
            self.assertRegex(guide, r"## [2-6]\. " + check + "：")
        self.assertIn(",".join(stage_checks(2, config)), guide)
        self.assertIn(",".join(stage_checks(2, config)), section)
        acceptance = yaml.load((ROOT / ".github/workflows/customer-acceptance.yml").read_text(), Loader=yaml.BaseLoader)
        migration = yaml.load((ROOT / ".github/workflows/customer-migration.yml").read_text(), Loader=yaml.BaseLoader)
        for field in ("reviewed_run_id", "checked_items", "evidence_notes", "confirm_environment"):
            self.assertIn(acceptance["on"]["workflow_dispatch"]["inputs"][field]["description"], guide)
        for field, value in (("stage", "2"), ("mode", "config-check"), ("component", "none")):
            self.assertIn(value, migration["on"]["workflow_dispatch"]["inputs"][field]["options"])
            self.assertIn(f"{field}={value}", section)
        for step in ("S2-01", "S2-02", "S2-03", "S2-04"):
            self.assertIn(f"| {step} |", section)
            self.assertIn(f"| {step} |", guide)
        for prefix, count in (("N", 3), ("I", 3), ("D", 4), ("C", 4), ("P", 3)):
            for number in range(1, count + 1):
                self.assertIn(f"### {prefix}-{number} ", guide)

    def test_stage_two_guide_keeps_decision_and_runtime_evidence_separate(self):
        from scripts.customer_migration import stage_fingerprint
        from tests.test_customer_migration import customer_config

        guide = (ROOT / "docs/customer-stage2-acceptance-checklist-zh.md").read_text()
        for required in ("Stage2没有infrastructure或runtime部署动作", "不授予也不禁止某人读取Prompt", "nativeAuditRead", "AZURE_DATABASE_CLIENT_ID", "仅配置个人User管理员", "RPO", "RTO", "Salt", "WebSocket", "X-LiteLLM-API-Key", "Stage3/platform首次plan前", "不进入Stage2指纹", "当前confirm不支持部分通过", "12至4000字符", "independentlyVerified=false", "acceptance-record-test-2-<run ID>"):
            self.assertIn(required, guide)
        config = customer_config()
        before = stage_fingerprint(config, 2)
        config["parameters"]["platform"]["containerRegistryName"] = "changedregistry"
        self.assertEqual(stage_fingerprint(config, 2), before)
        self.assertIn("bootstrapWorkspaceName", guide)
        config["contentAudit"] = {"mode": "native", "retentionDays": 7, "contentPolicyAccepted": True}
        self.assertNotEqual(stage_fingerprint(config, 2), before)

    def test_stage_two_guide_links_and_readonly_shell_examples(self):
        import subprocess

        path = ROOT / "docs/customer-stage2-acceptance-checklist-zh.md"
        guide = path.read_text()
        for target in re.findall(r"\]\(([^)]+)\)", guide):
            link = urlsplit(target)
            if link.scheme or not link.fragment:
                continue
            with self.subTest(target=target):
                linked_path = path.parent / unquote(link.path)
                headings = re.findall(r"^#{1,6} (.+)$", linked_path.read_text(), re.M)
                anchors = {re.sub(r"[^\w -]", "", heading.lower()).replace(" ", "-") for heading in headings}
                self.assertIn(unquote(link.fragment), anchors)
        snippets = re.findall(r"```bash\n(.*?)\n```", guide, re.S)
        self.assertEqual(len(snippets), 3)
        for index, source in enumerate(snippets):
            with self.subTest(snippet=index):
                result = subprocess.run(["bash", "-n"], input=source, capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotRegex(source, r"\b(create|delete|update|deploy|apply|execute|set-secret)\b")
                self.assertNotIn("get-access-token", source)

    def test_stage_one_acceptance_guide_matches_checks_and_workflow_inputs(self):
        from scripts.customer_migration import stage_checks
        from tests.test_customer_migration import customer_config

        path = "docs/customer-stage1-acceptance-checklist-zh.md"
        guide = (ROOT / path).read_text()
        self.assertIn("!" + path, (ROOT / ".gitignore").read_text().splitlines())
        main = (ROOT / "docs/customer-migration-guide-zh.md").read_text()
        section = main.split("### 阶段1：", 1)[1].split("### 阶段2：", 1)[0]
        self.assertIn(Path(path).name, section)
        config = customer_config()
        config.pop("legacyAccess", None)
        for check in stage_checks(1, config):
            self.assertRegex(guide, r"## [2-4]\. " + check + "：")
        self.assertIn(",".join(stage_checks(1, config)), guide)
        config["legacyAccess"] = {"mode": "load-balancer", "allowedCidrs": ["192.0.2.10/32"], "accessImpactAccepted": True}
        self.assertIn(",".join(stage_checks(1, config)), guide)
        workflow = yaml.load((ROOT / ".github/workflows/customer-acceptance.yml").read_text(), Loader=yaml.BaseLoader)
        for field in ("reviewed_run_id", "checked_items", "evidence_notes", "confirm_environment"):
            self.assertIn(workflow["on"]["workflow_dispatch"]["inputs"][field]["description"], guide)
        for prefix, count in (("H", 4), ("A", 4), ("R", 4), ("S", 3)):
            for number in range(1, count + 1):
                self.assertIn(f"### {prefix}-{number} ", guide)
        for step, operation in (("S1-11", "draft"), ("S1-13", "confirm")):
            self.assertIn(f"| {step} | {workflow['name']} | {operation} |", section)
        self.assertIn("S1-12", section)
        for required in ("当前confirm不支持部分通过", "12至4000字符", "independentlyVerified=false", "acceptance-record-test-1-<run ID>"):
            self.assertIn(required, guide)

    def test_stage_one_guide_section_links_resolve(self):
        path = ROOT / "docs/customer-stage1-acceptance-checklist-zh.md"
        for target in re.findall(r"\]\(([^)]+)\)", path.read_text()):
            link = urlsplit(target)
            if link.scheme or not link.fragment:
                continue
            with self.subTest(target=target):
                linked_path = path.parent / unquote(link.path)
                headings = re.findall(r"^#{1,6} (.+)$", linked_path.read_text(), re.M)
                anchors = {re.sub(r"[^\w -]", "", heading.lower()).replace(" ", "-") for heading in headings}
                self.assertIn(unquote(link.fragment), anchors)

    def test_stage_one_guide_distinguishes_alert_and_snapshot_evidence(self):
        guide = (ROOT / "docs/customer-stage1-acceptance-checklist-zh.md").read_text()
        monitoring = (ROOT / "infra/monitoring/main.bicep").read_text()
        rules = re.findall(r"name: '(alert-litellm-[^']+)'", monitoring)
        self.assertEqual(len(rules), 7)
        for name in (*rules, "ag-litellm-stage1-owner"):
            self.assertIn(name, guide)
        for required in ("ContainerLogV2", "KubePodInventory", "InsightsMetrics", "_ResourceId =~ LegacyAksId", "ValidSamples", "没有磁盘样本不等于使用率0%", "Action Group测试不等于真实告警条件已经触发", "runtime-review.json", "before-postgres.stdout.txt", "before-litellm-mi-proxy.stdout.txt", "不包含这两个完整快照", "不能证明它产生于加固前", "Deployment的strategy", "当前没有`legacy-hardening-restore`", "sha256sum --check"):
            self.assertIn(required, guide)
        workflow = yaml.load((ROOT / ".github/workflows/customer-runtime.yml").read_text(), Loader=yaml.BaseLoader)
        seal_commands = [step["run"] for job in workflow["jobs"].values() for step in job.get("steps", []) if "workflow_security seal" in step.get("run", "")]
        self.assertEqual(len(seal_commands), 1)
        self.assertIn("runtime-review.json", seal_commands[0])
        self.assertNotIn("before-postgres.stdout.txt", seal_commands[0])
        self.assertNotIn("before-litellm-mi-proxy.stdout.txt", seal_commands[0])

    def test_stage_one_shell_examples_are_syntax_valid_and_non_destructive(self):
        import subprocess

        guide = (ROOT / "docs/customer-stage1-acceptance-checklist-zh.md").read_text()
        snippets = re.findall(r"```bash\n(.*?)\n```", guide, re.S)
        self.assertGreaterEqual(len(snippets), 8)
        for index, source in enumerate(snippets):
            with self.subTest(snippet=index):
                result = subprocess.run(["bash", "-n"], input=source, capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)
                for forbidden in ("--admin", "view --raw", "get secret", "rollout undo", "rollout restart", "--dry-run=server", "kubectl apply", "kubectl delete", "az aks stop", "enable-addons", "disable-addons", "rm -rf", "curl -k", "chmod 777"):
                    self.assertNotIn(forbidden, source)

    def test_stage_zero_acceptance_guide_is_actionable_and_matches_contracts(self):
        import ast
        from scripts.customer_migration import stage_checks
        from scripts.runtime_secrets import BACKEND_SECRETS
        from tests.test_customer_migration import customer_config

        path = "docs/customer-stage0-acceptance-checklist-zh.md"
        guide = (ROOT / path).read_text()
        self.assertIn("!" + path, (ROOT / ".gitignore").read_text().splitlines())
        self.assertIn("customer-stage0-acceptance-checklist-zh.md", (ROOT / "docs/customer-migration-guide-zh.md").read_text())
        for check in stage_checks(0, customer_config()):
            self.assertRegex(guide, r"## [2-5]\. " + check + "：")
        for prefix, count in (("I", 5), ("B", 4), ("K", 4), ("P", 4)):
            for number in range(1, count + 1):
                self.assertIn(f"### {prefix}-{number} ", guide)
        for required in (*BACKEND_SECRETS.values(), "fullRestoreSucceeded", "backupSha256", "getpass.GetPassWarning", "hmac.compare_digest", "--auth-mode login", "default_transaction_read_only=on", "当前没有专门的报告核验/密钥导出workflow", "当前confirm不支持部分通过", "不能假设等于Master", "12至4000字符"):
            self.assertIn(required, guide)
        workflow = yaml.load((ROOT / ".github/workflows/customer-acceptance.yml").read_text(), Loader=yaml.BaseLoader)
        for field in ("reviewed_run_id", "checked_items", "evidence_notes", "confirm_environment"):
            self.assertIn(workflow["on"]["workflow_dispatch"]["inputs"][field]["description"], guide)
        self.assertIn(",".join(stage_checks(0, customer_config())), guide)
        snippets = re.findall(r"\.venv/bin/python - <<'PY'\n(.*?)\nPY", guide, re.S)
        self.assertEqual(len(snippets), 1)
        ast.parse(snippets[0])

    def test_stage_zero_kubelogin_guidance_preserves_file_and_host_security(self):
        guide = (ROOT / "docs/customer-stage0-acceptance-checklist-zh.md").read_text()
        section = guide.split("### I-2 ", 1)[1].split("### I-3 ", 1)[0]
        for required in ('namei -l "$PRIVATE_KUBECONFIG"', "type -a kubelogin", "AppArmor", "/snap/bin/kubelogin", "journalctl -k", "az aks install-cli", '--client-version "$KUBECTL_VERSION"', "--kubelogin-version v0.2.19", '--kubelogin-install-location "$HOME/.local/bin/kubelogin"', '--install-location "$HOME/.local/bin/kubectl"', 'export PATH="$HOME/.local/bin:$PATH"', "hash -r", "不需要重新获取凭据", "不需要sudo"):
            self.assertIn(required, section)
        commands = "\n".join(re.findall(r"```bash\n(.*?)\n```", section, re.S))
        for forbidden in ("chmod 777", "sudo ", "--admin", "view --raw", "aa-disable", "systemctl stop apparmor"):
            self.assertNotIn(forbidden, commands)
        self.assertLess(commands.index('export PATH="$HOME/.local/bin:$PATH"'), commands.rindex("kubelogin convert-kubeconfig"))

    def test_stage_zero_key_comparison_example_does_not_expose_material(self):
        from contextlib import redirect_stdout
        import getpass
        import io
        from types import SimpleNamespace
        from unittest.mock import patch
        import warnings

        guide = (ROOT / "docs/customer-stage0-acceptance-checklist-zh.md").read_text()
        source = re.findall(r"\.venv/bin/python - <<'PY'\n(.*?)\nPY", guide, re.S)[0]
        saved = {"LITELLM_MASTER_KEY": "PRIVATE_SYNTHETIC_MASTER", "LITELLM_SALT_KEY": "PRIVATE_SYNTHETIC_SALT"}
        good = SimpleNamespace(returncode=0, stdout=json.dumps(saved), stderr="")
        cases = (
            ("match", [good, good], list(saved.values()), 0),
            ("mismatch", [good, SimpleNamespace(returncode=0, stdout=json.dumps({**saved, "LITELLM_SALT_KEY": "PRIVATE_DIFFERENT"}), stderr="")], list(saved.values()), 1),
            ("absent", [SimpleNamespace(returncode=0, stdout=json.dumps({"LITELLM_MASTER_KEY": saved["LITELLM_MASTER_KEY"]}), stderr="")], list(saved.values()), 1),
            ("empty", [SimpleNamespace(returncode=0, stdout=json.dumps({**saved, "LITELLM_SALT_KEY": ""}), stderr="")], list(saved.values()), 1),
            ("invalid-value", [SimpleNamespace(returncode=0, stdout=json.dumps({**saved, "LITELLM_SALT_KEY": 123}), stderr="")], list(saved.values()), 1),
            ("read-failed", [SimpleNamespace(returncode=1, stdout="PRIVATE_STDOUT", stderr="PRIVATE_STDERR")], list(saved.values()), 1),
            ("invalid-json", [SimpleNamespace(returncode=0, stdout="PRIVATE_INVALID_JSON", stderr="")], list(saved.values()), 1),
            ("no-tty", [], getpass.GetPassWarning("PRIVATE_UNSAFE_TTY"), 1),
        )
        environment = {"PRIVATE_KUBECONFIG": "/synthetic/private-kubeconfig", "LEGACY_NAMESPACE": "litellm", "BACKEND_PODS": "backend-one backend-two"}
        for name, responses, recovered, expected in cases:
            selected_environment = {**environment, "BACKEND_PODS": "backend-one backend-two" if len(responses) == 2 else "backend-one"}
            with self.subTest(case=name), warnings.catch_warnings(), patch.dict("os.environ", selected_environment, clear=True), patch("getpass.getpass", side_effect=recovered), patch("subprocess.run", side_effect=responses) as run, redirect_stdout(io.StringIO()) as output:
                with self.assertRaises(SystemExit) as exit_result:
                    exec(compile(source, "stage0-key-comparison-example", "exec"), {})
                self.assertEqual(exit_result.exception.code, expected)
                self.assertNotIn("PRIVATE_", output.getvalue())
                self.assertIn("MATCH:" if expected == 0 else "NOT VERIFIED:", output.getvalue())
                if name in {"match", "mismatch", "absent", "empty", "invalid-value"}:
                    field_status = {"match": "MATCH", "mismatch": "MISMATCH", "absent": "ABSENT", "empty": "EMPTY", "invalid-value": "INVALID"}[name]
                    self.assertIn("LITELLM_SALT_KEY: " + field_status, output.getvalue())
                    self.assertIn("LITELLM_MASTER_KEY: MATCH", output.getvalue())
                if name == "no-tty":
                    run.assert_not_called()
                for call in run.call_args_list:
                    self.assertTrue(call.kwargs["capture_output"])
                    self.assertEqual(call.kwargs["timeout"], 30)
                    arguments = call.args[0]
                    self.assertEqual(arguments[:5], ["kubectl", "--kubeconfig", environment["PRIVATE_KUBECONFIG"], "-n", environment["LEGACY_NAMESPACE"]])
                    self.assertNotIn("PRIVATE_", " ".join(arguments))

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

    def test_stage_zero_sequence_includes_truthful_acceptance_handoff(self):
        from scripts.customer_migration import stage_checks
        from tests.test_customer_migration import customer_config

        guide = (ROOT / "docs/customer-migration-guide-zh.md").read_text()
        section = guide.split("#### 0-C. 按顺序运行", 1)[1].split("#### 0-C1.", 1)[0]
        workflow = yaml.load((ROOT / ".github/workflows/customer-acceptance.yml").read_text(), Loader=yaml.BaseLoader)
        inputs = workflow["on"]["workflow_dispatch"]["inputs"]
        for step, operation in (("S0-09", "draft"), ("S0-11", "confirm")):
            self.assertIn(operation, inputs["operation"]["options"])
            self.assertIn(f"| {step} | {workflow['name']} | {operation} |", section)
        self.assertIn(",".join(stage_checks(0, customer_config())), section)
        for field in ("reviewed_run_id", "checked_items", "evidence_notes", "confirm_environment"):
            self.assertIn(field, inputs)
            self.assertIn(field, section)
        for required in ("S0-10", "这八步未自动检查", "当前confirm不支持部分通过", "不是S0-08的备份ID", "没有可以如实提交的完整confirm填写值", "不能用artifact key替代"):
            self.assertIn(required, section)

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