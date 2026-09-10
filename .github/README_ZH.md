# GitHub Actions与客户迁移门禁

客户从[部署与验收工作流指南](../docs/customer-deployment-workflows-zh.md)开始。配置源为各Environment的`CUSTOMER_CONFIG_JSON`（Secret推荐，兼容Variable）和`MIGRATION_EVIDENCE_JSON` Secret；不在源码内填写真实客户信息或凭据。

## 实际执行与验收入口

- `customer-deploy.yml`：私有仓库、默认分支、受保护Environment；bootstrap初始化RG/日志库，各阶段plan/deploy，绑定审批计划哈希。阶段9release另需发布报告。
- `customer-runtime.yml`：专属Linux私网runner、独立runtime OIDC身份；阶段0备份/隔离恢复、阶段1探针与监控、阶段4命名空间与监控、阶段5目标空库恢复、阶段6~8成品清单发布。
- `customer-acceptance.yml`：生成pending报告或从真实审核结果生成证据账本；支持显式单人模式，不会自动伪造passed，不申请管理Secret的PAT。

新入口只在客户私有仓库保存7天审查计划/部署输出/验收元数据artifact，绝不保存dump、kubeconfig、原始stderr或参数。现有运行时集成缺口仍需完成，部署成功不等于可以生产切流。

API认证已采用企业Token与客户端vkey双凭据，模型/预算只由LiteLLM管理。客户API bindings不再填写models，proxy-credentials动作仅初始化管理端Key，application不再发布API内部Key挂载。旧配置/镜像需配套迁移并刷新计划与回执，客户端Token自动续期和真实权限仍需验收；不自动清理旧Key/Vault/RBAC。见[认证契约及迁移步骤](../auth-proxy/README_ZH.md)。

## `customer-migration.yml`

第一阶段选择原生Spend Logs，不默认执行自建L3的audit-foundation/audit部署及audit治理/恢复workflow。当前Stage8/application、observability配置与阶段证据仍有L3依赖，原生模式尚待代码适配；`guide`或验收草稿中的旧检查不能视为基础版最终要求，也不能手填passed跳过。基础版仍需正文、查询权限、清理/备份、容量和故障验收，见[部署状态表](../docs/customer-deployment-workflows-zh.md)。本轮未修改workflow行为或启用客户正文日志。

选择dev/test/prod、阶段0至9、guide/config-check/preflight/what-if以及组件。config-check允许无前序证据检查后续配置；preflight/what-if保持前序门禁。实际部署入口独立，不修改此workflow的只读行为。

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

该入口现在使用MIGRATION_PRIVATE_RUNNER_LABELS指定的私网runner，仅私有仓库默认分支允许运行，并核对目标ACR与客户配置一致。首次使用前验证私网路径、Docker、OIDC最小权限和Cosign签名验证策略，不得开放生产ACR公网。

## Action版本治理

工作流中的所有第三方Action均固定到已验证的完整commit SHA，行尾保留对应版本标签注释。Dependabot每月提出升级，升级必须经过Dependency Review和Pull Request审批，不直接跟随可移动标签。
