# GitHub Actions与客户迁移门禁

客户从[分阶段迁移指南](../docs/customer-migration-guide-zh.md)开始。配置源为各Environment的`CUSTOMER_CONFIG_JSON` Variable和`MIGRATION_EVIDENCE_JSON` Secret；不在源码内填写邮箱、域名、订阅或凭据。

## `customer-migration.yml`

选择dev/test/prod、阶段0至9、guide/preflight/what-if以及组件。指导和预检无Azure写入；What-if job使用精确Environment OIDC。前序阶段需配置/代码版本绑定、近期双人批准的验收记录。实际部署、数据库迁移、DNS切换和退役由客户受控变更流程执行，workflow不会自动执行这些操作。

原始配置/计划不作为artifact上传。详细变量、Secret、权限、每阶段准备和回退见迁移指南。应用Secret继续由客户Key Vault/CSI提供，不放入JSON非秘密参数。

## `ci.yml`

在Pull Request和`main` push时执行：

- Python单元测试、客户配置/证据门禁及README导航检查；
- 所有Bicep和Bicep参数编译；
- dev/test/prod Kustomize渲染；
- manifest安全策略检查；
- LiteLLM方案范围检查、个人信息和明显凭据扫描；
- Node认证/审计测试及固定版本OSS回调矩阵；
- Pull Request依赖审查。

CI不登录Azure，不部署资源。

## `iac-what-if.yml`

旧入口不再直接登录Azure或预览默认关闭的模板，运行时明确失败并引导至`customer-migration.yml`，防止绕过阶段门禁。新入口需要`AZURE_CLIENT_ID`、`AZURE_TENANT_ID`、`AZURE_SUBSCRIPTION_ID`及上述配置/证据，均在客户Environment管理。

## `promote-litellm-image.yml`

仅手动触发且绑定`prod` Environment审批：

1. 拒绝未固定SHA-256 digest的源镜像；
2. 使用`az acr import`导入目标ACR；
3. 解析目标不可变digest；
4. 生成SPDX JSON SBOM；
5. 使用Trivy阻断未修复Critical漏洞；
6. 使用GitHub OIDC/Cosign签名digest；
7. 上传30天SBOM证据；
8. 不直接部署Kubernetes，overlay变更必须走Pull Request。

首次使用前必须验证目标ACR网络路径、GitHub-hosted runner访问策略、OIDC角色最小权限和Cosign签名验证策略。如果ACR只允许Private Endpoint，应改用受控self-hosted runner，不得长期开放生产ACR公网。

## Action版本治理

工作流中的所有第三方Action均固定到已验证的完整commit SHA，行尾保留对应版本标签注释。Dependabot每月提出升级，升级必须经过Dependency Review和Pull Request审批，不直接跟随可移动标签。
