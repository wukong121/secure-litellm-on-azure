# 客户既有LiteLLM迁移执行手册：架构阶段0与阶段1

> 核对日期：2026-09-13。本文是按当前workflow输入及控制代码核对的主操作手册，不是客户云上全流程已经验收的证明。
>
> 适用：已有LiteLLM on AKS，先加固旧环境，再并行新建、迁移、验证、切流和停旧。示例统一使用GitHub Environment `test`；客户实际用`prod`时须整套一致替换，不混用环境。
>
> 一期审计沿用2026-09-10的原生Spend Logs选择：针对高用量用户进行获批的工作用途抽查，原生Logs为主、私网PG只读查询补充。不是所有UI页面、所有Codex协议或独立L3平台都必须首发；必要管理操作和实际首发客户端仍须通过验证。
>
> 最终停写、最终目标库选择及切流后数据回退尚未形成完整workflow编排，见第5节。不能把未完成部分写成可以直接点击的按钮，不能用假passed跨过门禁。

阅读顺序：第1节辨认workflow → 第2节准备配置/身份/Runner → 第3节学会plan批准与附件审核 → 第4节逐Stage执行 → 第5节最终迁移 → 第6节排错。资源实现细节和可选增强流程见[部署参考](customer-deployment-workflows-zh.md)，机器准备见[Runner指南](customer-private-runner-preparation-zh.md)。客户资源值、日志正文和现场记录只保存在受控位置，不填进本文或公共Git。

## 1. 迁移原则与入口

采用并行新建、隔离验证、批准客户端试点、最终切流。旧网关、数据库、密钥/Salt和VMSS业务身份在回退窗口结束前保留。不得让1.98新版本自动迁移旧生产数据库；新旧网关不能无计划地同时写一个数据库。

**两种“阶段”不要混淆：** 架构阶段0=本手册Stage0–1（可恢复基线和旧环境加固）；架构阶段1=Stage2–9（决策、隔离新建、迁移与发布）。GitHub表单的`stage`填写仓库编号，不是架构阶段号。每个Stage完成验收后才能执行下一Stage的变更。

在客户自己的fork仓库进入 **Actions → 左侧workflow名称 → Run workflow**。下表链接打开源文件；不是让客户切到上游仓库执行。

| workflow显示名称 | 用途/执行机 | 表单输入 |
| --- | --- | --- |
| [Customer staged migration](../.github/workflows/customer-migration.yml) | 配置检查/阶段预检，GitHub托管Runner；不部署 | environment、stage、mode、component |
| [Customer private runner checks](../.github/workflows/customer-runner-checks.yml) | 检查自托管Runner工具、Docker、身份范围和AKS只读访问 | environment、check_target |
| [Customer infrastructure deployment](../.github/workflows/customer-deploy.yml) | Azure资源plan/deploy，GitHub托管Runner | environment、stage、component、operation、release、approved_run_id、approved_plan_sha256、confirm_environment |
| [Customer private runtime operations](../.github/workflows/customer-runtime.yml) | 私网备份/恢复、Kubernetes发布、身份/证书动作，自托管Runner | environment、stage、action、operation、approved_run_id、approved_plan_sha256、audit_continue_run_id、confirm_environment |
| [Customer stage acceptance](../.github/workflows/customer-acceptance.yml) | 检查清单与验收记录，GitHub托管Runner | environment、stage、operation、reviewed_run_id、checked_items、evidence_notes、confirm_environment |
| [Check public source image](../.github/workflows/source-image-checks.yml) | 固定公共源镜像SBOM/扫描，GitHub托管Runner | 只选分支，没有stage或environment |
| [Promote LiteLLM image](../.github/workflows/promote-litellm-image.yml) | 向私有ACR晋级/构建、扫描及签名，自托管Runner | environment、acr_name、source_image、target_tag、build_azure_runtime、build_auth_proxy；没有stage/plan/deploy |
| [Customer gateway isolation checks](../.github/workflows/customer-gateway-checks.yml) | 私网DNS/TLS/匿名拒绝检查，自托管Runner | 只有environment，没有stage |

`Customer staged migration`的`config-check`不要求前序账本；`preflight`要求前序验收，但仍不部署。其旧`what-if`入口也不能代替`Customer infrastructure deployment / plan`产生的可批准计划。主要workflow手动执行受保护默认分支，本文用`main`指代。

**特殊动作：** `backup-restore`直接execute；镜像晋级只有一次构建/导入运行；阶段验收用draft/confirm。这些不能机械套用“所有workflow都先plan”。

## 2. 客户首次准备

1. fork到客户控制的仓库，公开或私有均可；默认分支须有生效的branch protection/ruleset，并要求受审核的代码才能执行。主要迁移workflow仅允许手动运行受保护默认分支，不能让公共PR使用私网Runner。不要将客户配置提交到Git。
2. 先建立本次使用的GitHub Environment，例如`test`，限制部署分支；不必一次创建全部`dev`、`test`、`prod`。默认双人策略应配置维护者与验收审批者分离、禁止自我审批；仅一位运维时可按新工作流指南显式启用single-operator并记录客户风险接受，不能宣称职责分离。GitHub审批受套餐影响，缺失时用客户外部变更流程。
3. 为每个Environment准备分离的基础设施部署身份和私网运行身份，并配置Azure OIDC联邦信任。Federated Credential issuer为`https://token.actions.githubusercontent.com`，audience为`api://AzureADTokenExchange`，subject精确到`repo:<customer-org>/<customer-repo>:environment:<environment>`。两个Client ID分别填入下表，不能用个人Object ID或Client Secret替代。
4. 确认预算和配额，使用阶段0的bootstrap组件先plan、批准后deploy创建新目标RG和Log Analytics workspace；无需手工预建。已有同RG Workspace可显式选existing。目标RG必须与旧RG不同；跨RG共享资源仍需扩展和测试。旧网关没有日志库时由阶段1legacy-logging创建，再onboard监控。
5. 由网络Owner批准区域、VM SKU、Kubernetes版本、CIDR、路由及私网执行位置。示例CIDR、HA Disabled、Redis规格均不是客户默认架构决策。
6. 按[Runner准备说明](customer-private-runner-preparation-zh.md)部署或复用专用执行机、注册服务并设置仓库标签变量。Runner建机不创建上述OIDC身份或授予业务权限；本机执行过`az login`也不会让Actions自动登录。部署到Private AKS和访问私有ACR/Blob需要获准私网路径，不为GitHub-hosted runner开放生产公网。

**GitHub保护规则：** 在Settings → Rules → Rulesets创建Active规则，目标为默认分支，启用禁止强推/删除及通过PR合并。单人维护时不必要求另一位不存在的维护者审批；生产审核要求按获批策略另行设置。保护后直接`git push origin main`被拒绝是正常现象，按“工作分支 → commit → push工作分支 → PR → 合并”发布。客户workflow只从合并后的默认分支执行，不关闭保护绕过。

**Runner注册收尾：** 在发起Bastion连接的电脑安装Azure CLI的`bastion`和`ssh`两个扩展；它们不是GitHub Runner程序，也不是业务凭据。注册URL为客户fork的`https://github.com/OWNER/REPOSITORY`，临时Token从Settings → Actions → Runners → New self-hosted runner的Configure步骤取得，只在Runner终端提示中输入。没有专用组织Runner Group时使用Default；服务已启动且GitHub显示Idle后可以退出Bastion，但不能在作业期间停VM。详细安装及输出命令见Runner指南第1.1节。

### 2.1 Variables和Secrets

先准备下面的基础清单；表中“StageN前”表示在首次运行该动作前补齐，不是运行失败后再猜变量。`AZURE_TENANT_ID`与`AZURE_RUNTIME_CLIENT_ID`不是二选一。数据库备份镜像也不是从bootstrap输出中取得。

以下位置对应实际workflow读取方式：

- **Environment Variable / Secret**：仓库`Settings → Environments → 本次environment`下的`Environment variables`或`Environment secrets`。运行时选择`test`就配置在`test`，不是只配到`prod`。
- **Repository Variable**：仓库`Settings → Secrets and variables → Actions → Variables → New repository variable`。

| 名称 | 存放位置 | 使用时机与用途 |
| --- | --- | --- |
| `AZURE_CLIENT_ID` | Environment Variable | 基础设施plan/deploy、Azure What-if及镜像晋级使用的部署身份Client ID；Runner检查不读取它 |
| `AZURE_RUNTIME_CLIENT_ID` | Environment Variable | **Customer private runner checks必需**；普通私网运行操作也使用此身份Client ID，不是个人Object ID或VM资源ID |
| `AZURE_TENANT_ID` | Environment Variable | Runner检查及Azure操作必需；必须与客户配置中的tenantId一致 |
| `AZURE_SUBSCRIPTION_ID` | Environment Variable | Runner检查及Azure操作必需；必须与客户配置中的subscriptionId一致 |
| `CUSTOMER_CONFIG_JSON` | Environment Secret | 按[客户配置模板](../config/customer.example.json)填写完整JSON；主要迁移workflow只读取Secret，不再回退到同名Variable；不能含运行时凭据 |
| `WORKFLOW_ARTIFACT_KEY` | Environment Secret | 独立随机32字节的base64值，供计划、Runner检查结果及验收附件加密；生成及保管方法见[部署指南](customer-deployment-workflows-zh.md) |
| `MIGRATION_PRIVATE_RUNNER_LABELS` | Repository Variable | Runner选择器，填匹配实际标签的JSON数组；不能放在Secret，也不要只放Environment |
| `POSTGRES_RESTORE_IMAGE` | Environment Variable | **Stage0/backup-restore前必填**；与旧PG大版本兼容的完整`镜像仓库@sha256:...`，获取方法见0-B；不要求Runner只读检查使用它 |
| `AZURE_DATABASE_CLIENT_ID` | Environment Variable | Stage5/database-roles专用已授权PG管理员服务主体的Client ID；OIDC不能模拟个人用户管理员 |
| `MIGRATION_RESTORE_BLOB` | Environment Variable | Stage5/restore-target前；从Stage0报告`observations.backupBlob`取`pre-change/...dump`，不是Blob URL或SAS |
| `MIGRATION_BACKUP_SHA256` | Environment Variable | Stage5/restore-target前；从同一报告`observations.backupSha256`取得备份文件哈希；不是镜像digest |
| `AZURE_ENTRA_CLIENT_ID` | Environment Variable | Stage7/entra-apps及admin-credentials专用初始化身份，需预先获批Graph和admin Vault权限 |
| `AZURE_ENTRA_ACCESS_CLIENT_ID` | Environment Variable | Stage7/entra-access专用准入授权身份，与Entra初始化身份分离 |
| `AZURE_CERTIFICATE_CLIENT_ID` | Environment Variable | 选择Stage4/certificate-renew时需要，独立DNS挑战/API证书身份 |
| `MIGRATION_RELEASE_JSON` | Environment Secret | Stage9实际启用或改变流量前必需的发布报告，独立于阶段验收账本；模板字段见第4节Stage9 |

已有脚本/特殊分支兼容项，不要为了填满表单一律创建：

| 配置 | 仅何时需要 |
| --- | --- |
| `MIGRATION_EVIDENCE_JSON` | 旧账本兼容入口；本文Actions会自动读取验收artifact，无需反复更新 |
| `MIGRATION_REPORT_JSON`、`MIGRATION_REPORT_URL`、`MIGRATION_APPROVERS_JSON` | 使用acceptance的record外部报告/双人策略入口时；本文单人confirm不需要 |
| `MIGRATION_MANIFEST_YAML` | 旧手工清单路径；本文托管application发布不填，不与自动配置同时使用 |
| `POSTGRES_MIGRATION_USER` | 自管PG角色路径；本文配置databaseAccess后自动使用llmgw_migrator，不填 |
| `PRIVATE_API_INGRESS_CLASS`、`PRIVATE_ADMIN_INGRESS_CLASS` | 客户自管Ingress controller路径；本文private-ingress文件路由不需要 |
| `AZURE_AUDIT_GOVERNANCE_CLIENT_ID` | 仅选择增强L3治理；一期原生日志抽查不需要 |

`GH_TOKEN`、`GITHUB_SHA`、`MIGRATION_AUTO_EVIDENCE`等由workflow设置，不要求另建永久PAT或手填这些变量。真实Master/Salt、证书私钥和后台Key不进入上述JSON；GitHub Runner注册Token也不作为迁移Secret保存。

例如Runner标签变量的值为：

```json
["self-hosted", "Linux", "X64", "llmgw-test-private"]
```

**Runner检查的最小清单：** 三个Azure Variables（`AZURE_RUNTIME_CLIENT_ID`、`AZURE_TENANT_ID`、`AZURE_SUBSCRIPTION_ID`）、两个Environment Secrets和一个Repository标签变量；还要有已配置联邦信任及所需AKS读取权限的运行身份。这不是全部迁移阶段的权限清单。

当前[Runner检查workflow](../.github/workflows/customer-runner-checks.yml)使用`vars.AZURE_RUNTIME_CLIENT_ID`和`vars.AZURE_TENANT_ID`作为`azure/login`输入。只配置`AZURE_CLIENT_ID`不会自动替代运行身份；放入同名Secret、客户JSON或本机终端变量也不会被这个登录步骤读取。不要为补空值直接把两个身份合并；须确认各自Client ID及批准的授权范围。`SERVICE_PRINCIPAL`是OIDC路径的正常默认值，不需要改成`IDENTITY`或创建Client Secret。

首次先运行`Customer staged migration`的`stage=0, mode=config-check, component=none`；准备齐全后运行`Customer private runner checks`，首次`check_target=false`。变量和Secrets的名称/位置、分支保护及Runner Idle应先核对，不通过后续反复失败来猜配置。

**两个Secrets怎样填写：** `CUSTOMER_CONFIG_JSON`填完整JSON原文，不填文件路径、不base64编码、不包Markdown代码块。修改本地文件后必须同步更新所选Environment的Secret，Actions不会读取你本机文件。`WORKFLOW_ARTIFACT_KEY`在受控终端用`openssl rand -base64 32`生成，直接录入Secret及密码库，不发聊天、不进入Git或Actions输出；已生成过则继续使用同一个有效值，迁移期间不要随意轮换以免旧附件无法解密。

`MIGRATION_EVIDENCE_JSON`仅保留旧入口兼容，不是当前Actions首次运行的必填Secret。单人验收采用`draft → confirm`后，后续workflow自动读取加密账本，不要求逐阶段手改证据Secret，也不要求提供旧`record`入口的报告/审批人JSON。

配置中的`ownerEmail`用于资源标签、告警邮箱以及备份Owner描述，不用于推断RBAC。`backupOwnerPrincipalId`是客户明确批准的Entra用户Object ID；`backupAutomationPrincipalId`是`AZURE_RUNTIME_CLIENT_ID`对应服务主体的Object ID，两者不能替换。当前备份Owner模板的principalType为User，不能填workflow应用ID或用`az ad signed-in-user`推断。需组/服务主体作为人工保管人时先修改并测试该角色边界。两类ID的Portal/CLI获取、核验及回填步骤见[0-A1](#0-a1-备份身份的获取与核验)。

`baseDomain`自动生成`llm-api.<客户域名>`和`llm-admin.<客户域名>`；只有API可进入Front Door，admin保持私网。`legacy`记录实际旧RG、AKS、namespace和PG PVC；`parameters`按组件填写客户资源值。可留待后续阶段填写其他组件，但使用当前组件前必须清除其占位符。Stage3/4不校验尚未启用的`stage5Data`，Stage3不校验尚未使用的模型连接。

原环境Bicep参数中的`OWNER_EMAIL`、`LOG_ANALYTICS_WORKSPACE_NAME`、`AZURE_LOCATION`只用于独立编译/直接参数文件使用；其示例回退是离线检查用途。客户迁移workflow使用`CUSTOMER_CONFIG_JSON`生成显式ARM参数，不使用这些回退，也不默默读取个人域名配置。

运行时秘密由客户Key Vault承载：已有Master Key、Salt、数据库连接信息、管理OIDC client secret、会话加密密钥、受限管理Key等通过批准的CSI/Workload Identity接入；API客户端自带vkey，不再由代理保存内部Key。不要把它们写入`CUSTOMER_CONFIG_JSON`、ARM非secure参数、workflow inputs或日志。audit-foundation仅为选择增强L3时创建审计CMK及独立身份，不是基础版前置项；不自动轮换或删除客户已有加密材料。

### 2.2 Azure权限与日志

首次身份创建/授权由有权管理员一次性完成，不能依赖尚无法登录的Actions自行提权。可在既有管理RG中创建分离的用户分配托管身份，或使用获批的应用注册/服务主体；名称可按`<项目>-<环境>-deploy/runtime/database/entra-bootstrap/entra-access/certificate`规划，未用专项身份不必一次创建。UAMI的Overview可取Client ID和Principal ID，应用注册Overview取Application (client) ID，其企业应用Overview取服务主体Object ID，不能混用应用对象ID和服务主体ID。

为每个会在该环境登录的身份建立GitHub Actions联邦凭据，精确绑定客户仓库和Environment（例如`repo:OWNER/REPOSITORY:environment:test`），不是`ref:refs/heads/main`。issuer=`https://token.actions.githubusercontent.com`，audience=`api://AzureADTokenExchange`；大小写、仓库和环境须与实际运行一致。无需创建Azure Client Secret，不给Runner VM挂通用管理员身份。GitHub Azure登录成功只能证明身份可登录，不等于下面每项操作都有权限。

| 身份/阶段 | 要准备的权限范围 | 不能替代的权限 |
| --- | --- | --- |
| deploy，Stage0/bootstrap | 订阅范围的RG创建/嵌套部署及目标日志资源部署；plan的What-if也需相应权限 | Reader不够；不默认授予订阅Owner |
| deploy，backup/platform等 | 批准目标RG内的资源部署；涉及角色分配需相应受控RBAC管理权限；旧监控另外限定旧RG | Contributor不含角色分配；跨订阅模型角色须在对应账号范围另行授权 |
| runtime，Runner检查/备份 | 旧AKS读取、Cluster User凭据获取及Kubernetes Deployment/Pod读取；备份另需postgres exec/cp、目标部署输出读取、Blob容器数据读写 | ARM权限不等于Kubernetes RBAC或Storage数据权限 |
| runtime，新环境发布 | 目标AKS/命名空间发布、指定Vault访问、镜像读写及目标RG操作回执权限，随动作授权 | 不给日常应用身份DDL或管理权限 |
| database，Stage5/database-roles | 配置为PG Entra管理员服务主体或其获准管理员组成员，能建立新库角色及授权 | 个人User管理员不能通过服务主体OIDC模拟；不能直接填个人UPN |
| Entra bootstrap / access，Stage7 | 前者获批应用创建/凭据管理，后者获批角色分配/委托同意；具体Graph权限见Stage7参考 | Azure RG RBAC不能约束或代替租户级Graph授权 |
| certificate，选择自动签发时 | 批准DNS TXT及API证书/ACME状态Secret权限 | 不授予admin证书或后台Master/Salt读取权限 |

从[部署参考的身份说明](customer-deployment-workflows-zh.md#4-哪些-id-人工提供哪些自动输出)逐项核对实际授权；上述是分工，不是“一项角色覆盖全部动作”。权限缺口交给对应Owner，不通过关闭TLS/租户检查或扩大到订阅Owner解决。

主要迁移workflow支持公开fork，计划、运行结果和验收账本使用`WORKFLOW_ARTIFACT_KEY`认证加密后上传，通常保留7天；仅选用增强L3时的治理workflow仍有独立限制。绝不上传数据库dump、kubeconfig或原始stderr。公开仓库的运行元数据、workflow输入和非秘密Variables仍可能公开，不能在其中填写正文或凭据；加密附件不替代保护分支、Environment权限及内容审查。解密审核步骤见[客户部署与验收工作流](customer-deployment-workflows-zh.md)。

### 2.3 客户JSON分批准备

从[迁移示例](../config/customer.example.json)整理自己的受控文件；示例是字段参考，不是直接可运行的批准配置。只启用已决定的可选配置块，未准备好的`application/proxy/privateIngress`等不要以占位符顶层块提前启用，否则全局配置校验也可能拒绝前期检查。

| 最迟时机 | 客户JSON内容 | 来源/注意事项 |
| --- | --- | --- |
| 首次检查 | schemaVersion=1、environment、azure、location、baseDomain、ownerEmail、legacy、target、parameters | 来自实际客户租户/订阅、域名及旧部署；目标RG不同于旧RG，不能用Runner RG冒充目标RG |
| 首次验收前，建议开始即冻结 | governance | 单人confirm须有本人Entra用户Object ID、GitHub login及明确风险接受，例子如下；改governance会影响早期证据 |
| Stage0 | parameters.backup、可选parameters.bootstrap | 新备份网络/工作区与两类备份Owner；见0-A |
| Stage1 | parameters.monitoring、可选parameters.legacy-logging、legacyAccess | 旧日志工作区及真实批准来源；旧/新工作区不同不代表重复 |
| Stage2 | contentAudit | 本文采用native；此后正文决策绑定证据 |
| Stage3 | parameters.platform的ACR/日志名及stage4Network、stage4Aks | 这些网络/集群字段Stage3已要求提供，不能等Stage4才填写；stage5Data和模型连接可稍后补齐 |
| Stage4 | azureOpenAIConnections、privateIngress；可选certificates | 模型账号资源ID，API/admin证书和允许来源；自动证书配置才需证书身份 |
| Stage5 | stage5Data、databaseAccess、DNS归属开关 | 批准数据库SKU/HA/Entra管理员，runtime服务主体Object ID；不能用用户ID代替运行身份 |
| Stage6 | application | 派生后台镜像digest和模型组/真实deployment映射，见Stage6 |
| Stage7 | entra、proxy | 分离初始化/准入身份，代理镜像digest，用户或服务主体绑定，见Stage7 |
| Stage8 | 可选observability | telemetry所需固定collector镜像；原生日志不要求自建L3 |
| Stage9 | parameters.origin、parameters.edge；可选dns | 优先使用已实现的auto输出解析；域名/证书审批另行准备 |

单人操作时由客户批准后加入以下顶层块，替换占位符再更新Secret：

```json
"governance": {
  "approvalMode": "single-operator",
  "approverObjectIds": ["REPLACE_OPERATOR_USER_OBJECT_ID"],
  "singleOperatorRiskAccepted": true,
  "githubLogin": "REPLACE_GITHUB_LOGIN"
}
```

Client ID用于登录，Principal/Object ID用于RBAC或用户批准，模型`deploymentName`用于推理路由，这三类值不能互换。原生审计不要填写示例中的L3 writer/reader/retention为同一个个人ID来强行通过；未选增强L3时不执行audit-foundation/audit组件。

### 2.4 真正开始执行前

| 步骤 | workflow | 完整输入 | 成功后/失败时 |
| --- | --- | --- | --- |
| P-01 | Customer staged migration | branch=main，environment=test，stage=0，mode=config-check，component=none | 检查客户基础JSON，不查询Azure；失败先修配置 |
| P-02 | Customer private runner checks | branch=main，environment=test，check_target=false | 确认作业落到实际Runner并完成工具/身份/旧AKS只读检查；不是全部写权限或Blob私网证明 |
| P-03 | 人工核对 | 保护分支、Environment变量/Secrets、Owner授权、镜像/容量/网络清单 | 清楚下一步收费/作用域后进入S0-01 |

可在组件首次plan前重复P-01，把stage/component换成本次组件，提前发现参数问题。`guide/config-check/preflight`成功不能当作deploy已批准；Runner显示Idle也不等于读写权限和私网已经验证。

## 3. 每次运行的填写、审核与验收

### 3.1 基础设施表单：plan与deploy

打开`Customer infrastructure deployment`。下表是每个Stage表格共同使用的完整默认规则，Stage9的正式release另有明确例外。

| 表单字段 | plan | deploy |
| --- | --- | --- |
| Use workflow from | main | main，与计划同一完整Git SHA |
| environment | test | test，与计划一致 |
| stage | 按第4节表格 | 同一stage |
| component | 按第4节表格 | 同一component |
| operation | plan | deploy |
| release，页面显示Stage 9 edge traffic operation... | 不勾选 | 不勾选，除非Stage9明确批准发布 |
| approved_plan_sha256，页面显示**Legacy direct hash input; prefer approved_run_id** | **留空** | **留空，不把run ID填到这里** |
| approved_run_id，页面显示**Successful matching infrastructure plan run ID** | 留空 | 填成功且已解密审核的同组件plan运行ID |
| confirm_environment，页面显示Type the selected environment again for deploy | 留空 | 填test |

GitHub页面可能把description显示为标签而不是变量名。数字run ID来自计划运行URL的`/actions/runs/<数字>`，不是`/job/<数字>`、页面`#1`编号、artifact ID或64位SHA256。例如审核的是`/actions/runs/12345678901`，就在**Successful matching infrastructure plan run ID**框填`12345678901`，这是格式示例，不是可用于客户部署的真实批准ID。

### 3.2 私网运行表单：plan与execute

打开`Customer private runtime operations`。branch/environment/stage规则相同，选择`action`而不是component。

| 表单字段 | plan | execute |
| --- | --- | --- |
| action、stage | 按第4节 | 与计划相同 |
| operation | plan | execute |
| approved_plan_sha256，页面显示Non-audit runtime plan SHA256... | 留空 | 留空，使用下一框 |
| approved_run_id，页面显示Successful matching plan run ID to approve for execute | 留空 | 已成功且审核过的**runtime plan**的run ID，不是infrastructure plan |
| audit_continue_run_id | 留空 | 留空；仅增强L3分页恢复使用 |
| confirm_environment | 留空 | test |

**唯一本文备份例外：backup-restore只有execute，上述两个approved输入和audit_continue_run_id全部留空。** 不会把Stage0备份变成只读计划；审批窗口与恢复范围必须在执行前人工确认。

### 3.3 plan结果怎样审核

1. 等plan整个workflow成功，在运行Summary底部的Artifacts下载本次附件。失败运行或deploy执行记录不能充当下一次部署的plan。
2. 在受控机器解压到仓库忽略目录`temp/`；一般里面只有密文`sealed-artifact.json`，不是缺失报告。
3. 在同一终端安全注入与该Environment相同的`WORKFLOW_ARTIFACT_KEY`。不要把密钥写进命令参数、shell历史、Git或聊天；也不要临时生成新密钥解旧附件。
4. 使用以下命令，替换为你自己的仓库、plan运行ID、artifact名称和本地路径；命令只把明文写入受控文件，不打印客户内容：

```bash
.venv/bin/python -m scripts.workflow_security open \
  --file temp/downloaded/sealed-artifact.json \
  --repository OWNER/REPOSITORY \
  --run-id 12345678901 \
  --name infrastructure-test-0-bootstrap-12345678901 \
  --output-dir temp/reviewed-evidence
```

本机需准备仓库Python依赖；不要在公开Actions里执行解密后打印内容。`--name`必须是实际artifact名，不是ZIP文件名或workflow显示名称。

| 附件名称形式 | 解密后的主要内容 | 要检查什么 |
| --- | --- | --- |
| infrastructure-test-阶段-组件-runID | plan-summary.json、reviewed-plan.json、operation-receipt.json；执行后有deployment-receipt.json、deployment-outputs.json | 目标订阅/RG、实际资源/属性变化、费用、权限及网络；批准的是否plan且代码/配置一致 |
| runtime-test-阶段-runID | runtime-review.json、runtime-summary.json或acceptance-report.json，视动作而定 | 目标旧/新集群、动作范围、影响和结果；备份是否真实恢复/回读校验 |
| acceptance-draft-test-阶段-runID | acceptance-report.json | pending检查清单，不能直接当passed |
| acceptance-record-test-阶段-runID | migration-evidence.json及验收报告，confirm也使用此artifact名前缀 | 逐项实际核验、主体/配置/代码绑定 |
| runner-checks-test-runID / gateway-checks-test-runID | 就绪或入口检查结果 | 仅代表该探针范围，不代表全部权限/协议验收 |

审核后重新Run workflow选择deploy/execute并填写**这个plan的run ID**。计划与执行须同代码、同配置和实际状态；有变化就重新plan，不手改哈希。`Re-run jobs`沿用原运行代码，不会自动使用刚合并的修复；代码修复或输入改变后从Run workflow创建新运行。

### 3.4 每个Stage结束都做验收

以下是已配置single-operator的主路径；双人策略继续使用已审核外部报告的record入口，见部署参考，不虚构第二位批准人。

| 字段 | 生成清单 | 人工核验后确认 |
| --- | --- | --- |
| workflow | Customer stage acceptance | Customer stage acceptance |
| branch、environment、stage | main、test、当前Stage | 与draft相同 |
| operation | draft | confirm |
| reviewed_run_id | 留空 | 本Stage成功的**acceptance draft**运行ID，不是部署/备份run ID |
| checked_items | 留空 | draft Summary列出的全部check ID，逗号分隔，不漏项不重复 |
| evidence_notes | 留空 | 简短真实结果及安全证据引用，不放Prompt、密码、Token或敏感资源详情 |
| confirm_environment | 留空 | test |

draft可以在阶段操作前生成，用作检查清单；所有实测完成后才confirm。Stage0备份动作的acceptance-report是观察报告，不能把其runtime run ID直接填进reviewed_run_id。备份报告供核验，confirm引用单独生成的acceptance draft。

阶段N的`preflight`/原`what-if`及实际deploy/execute必须具备0至N-1的通过记录；独立config-check和部署plan不要求验收。记录包含阶段、环境、配置哈希、完整Git SHA、全部checks、与governance匹配的审批者、时间和报告引用。用Customer stage acceptance的draft生成pending报告，实测审核后由已配置的单人操作者confirm生成账本；record保留为外部报告/双人策略兼容入口。后续workflow自动读取同修订的成功加密账本，不需要更新证据Secret，也不需要先伪造passed才能开始阶段0。

新记录使用stage-config绑定，只覆盖当前及前序阶段相关配置，后续PLS/审计身份输出补填不使阶段0失效；旧full-config记录仍可使用。相关配置、审批策略或代码改变后需重新审核，不直接改哈希冒充验收。记录仍限最近7天，长期迁移要复核早期备份和回退有效性。新报告哈希是规范JSON哈希，由工具计算。

**这是人工验收记录，不是自动事实认证**：`independentlyVerified=false`。工具校验清单、代码/配置、时效和已配置操作者，不独立证明技术事实。不要把“workflow绿勾”“resource存在”“本机模拟测试通过”当作所有check均通过；记录实际证据后再确认。阶段N重录会使后续旧账本失效。

## 4. 分阶段操作

### 阶段0：盘点与可恢复备份

**开始前准备：** 第2.1节的基础配置已经就绪；使用`deploymentMode=migration`（省略也为迁移），先完成配置和Runner检查。盘点旧PG实际版本、扩展、容量、PVC、旧LiteLLM镜像digest、模型/Key/预算与Master/Salt保管位置，并批准低峰备份窗口。不得将旧生产凭据或正文上传Actions。

#### 0-A. 本阶段必须补充的配置

| 配置项 | 存放位置/来源 | 何时需要 |
| --- | --- | --- |
| `POSTGRES_RESTORE_IMAGE` | `test`的Environment Variable；按下面方法取得完整镜像digest引用 | **运行backup-restore前必填**，不是Secret、不是workflow输入，也不放进客户JSON |
| `target.resourceGroup`、`location` | `CUSTOMER_CONFIG_JSON`；批准的新目标RG名和区域 | bootstrap前；目标RG必须与旧RG不同 |
| `parameters.backup.logAnalyticsWorkspaceName` | 客户JSON；新日志工作区名，与platform等新环境组件统一 | bootstrap及backup前；不是旧监控工作区 |
| `parameters.backup.backupOwnerPrincipalId` | 客户JSON；备份保管人的Entra用户Object ID，按0-A1从客户租户Users取得 | backup前；不是Client ID，不自动选择当前登录用户 |
| `parameters.backup.backupAutomationPrincipalId` | 客户JSON；按0-A1由`AZURE_RUNTIME_CLIENT_ID`查询服务主体Object ID | 本文OIDC备份路径在backup前填写，模板授予备份容器范围Blob Data Contributor；仅在已另行批准并配置等效数据权限时才省略 |
| 备份VNet与PE子网名称/CIDR | 客户JSON的`parameters.backup` | backup前；与后续platform一致，不能和Runner/企业网络重叠 |

`parameters.bootstrap`可省略，默认创建工作区；可显式配置`{"workspaceMode":"create","logRetentionDays":30}`。已有同RG工作区并获准复用时用`workspaceMode=existing`；不要先删旧集群仍在使用的日志库来“去重”。

#### 0-A1. 备份身份的获取与核验

旧版[客户示例](../config/customer.example.json)漏列了`backupAutomationPrincipalId`。从旧版复制配置的客户应在`parameters.backup`内补上该字段，保留已有`backupOwnerPrincipalId`；不要把新字段建成同名GitHub Variable，也不要照抄其他租户的GUID。模板技术上允许省略它，以兼容另行管理RBAC的客户，但不会因此自动把人工Owner的权限给Actions。

| 值 | 代表谁 | 获取位置与回填位置 |
| --- | --- | --- |
| `AZURE_RUNTIME_CLIENT_ID` | 用于普通runtime workflow的OIDC登录身份 | 第2.2节预先准备的运行身份：应用注册的Application (client) ID，或用户分配托管身份的Client ID；填所选GitHub Environment Variable |
| `backupAutomationPrincipalId` | 上述登录身份在客户租户中的服务主体 | Enterprise applications的Object ID，或该托管身份的Principal ID；填客户JSON的`parameters.backup` |
| `backupOwnerPrincipalId` | 客户明确批准的人工备份保管人 | 客户租户Microsoft Entra ID → Users → 指定用户 → Object ID；填客户JSON的`parameters.backup` |

**取得自动化身份，先锁定Client ID再查Object ID：** 打开客户仓库Settings → Environments → 本次环境，记录`AZURE_RUNTIME_CLIENT_ID`的值。GitHub Variable不会自动成为你本机终端的环境变量；下面的`RUNTIME_CLIENT_ID`要显式填这个值，不取`AZURE_CLIENT_ID`，也不取Runner VM资源ID或登录VM的用户。如果尚无运行身份，先完成第2.2节的身份/OIDC准备，不临时用个人ID凑数。

Portal路径：切换到客户的Entra租户 → Enterprise applications（企业应用）→ All applications，按上述Application ID查找，必要时调整应用类型筛选。在Overview同时核对Application ID与Object ID，后者才是要回填的值。应用注册Overview中另一个Object ID属于application对象，**不是**这里的service principal对象，不能使用。

CLI路径：在已获准读取客户目录的管理终端执行。先核对`az account show`输出与客户JSON的`azure.tenantId`、`azure.subscriptionId`一致；不一致先登录/切换到正确租户和订阅，再继续。以下查询不创建身份、不生成密码、不授予权限：

```bash
az account show --query '{tenantId:tenantId,subscriptionId:id}' --output json
RUNTIME_CLIENT_ID="REPLACE_WITH_AZURE_RUNTIME_CLIENT_ID"
az ad sp show --id "$RUNTIME_CLIENT_ID" \
  --query '{displayName:displayName,clientId:appId,objectId:id}' --output json
```

检查输出`clientId`与输入完全一致，`displayName`对应客户批准的运行身份，把输出`objectId`填入`parameters.backup.backupAutomationPrincipalId`。Object ID是租户内标识，同一多租户应用在不同客户租户里的值也可能不同；不要使用`az ad app show`的`id`代替它。

若运行身份是**用户分配托管身份（UAMI）**，也可从Azure Portal → Managed Identities → 该身份 → Overview取Principal ID。下面的RG、身份名称和订阅来自这个身份资源本身，不默认是Runner RG或VM名称；同样核对返回的`clientId`等于GitHub中的运行Client ID：

```bash
az identity show --subscription "REPLACE_SUBSCRIPTION_ID" \
  --resource-group "REPLACE_IDENTITY_RESOURCE_GROUP" --name "REPLACE_IDENTITY_NAME" \
  --query '{tenantId:tenantId,clientId:clientId,objectId:principalId}' --output json
```

如果`az ad sp show`因目录读取权限失败或找不到对象，请客户Entra管理员在正确租户核验并提供匹配的两项ID，或使用已获准读取的UAMI资源查询。订阅Contributor不等于Microsoft Graph目录读取权限；不要为查询ID而给日常运行身份添加宽泛Graph权限，也不要随意新建一个同名应用。

**取得人工Owner：** 先由客户确认谁负责保管备份，再查询该用户。`ownerEmail`可能只是通知邮箱，当前CLI登录人也可能是顾问，不能自动当作Owner。Portal在客户租户Users中核验该用户的UPN、用户类型和Object ID；CLI可使用该用户在客户租户中的实际UPN：

```bash
BACKUP_OWNER_UPN="REPLACE_APPROVED_USER_UPN_IN_CUSTOMER_TENANT"
az ad user show --id "$BACKUP_OWNER_UPN" \
  --query '{displayName:displayName,userPrincipalName:userPrincipalName,objectId:id}' --output json
```

将这里的`objectId`填入`parameters.backup.backupOwnerPrincipalId`。来宾用户应取**客户租户内**的来宾对象ID与UPN（可能含`#EXT#`），不是其主租户Object ID，也不是仅按显示名称匹配的另一个用户。当前模板为该User授予Storage Account Contributor和Storage Blob Data Owner；自动化服务主体则仅获备份容器范围Storage Blob Data Contributor，两者须分别批准。

**这类故障中怎样反查实际调用者：** 如果已有workflow运行，可在旧AKS的Activity log中按运行时间定位`Microsoft.ContainerService/managedClusters/listClusterUserCredential/action`，检查事件JSON的`claims.appid`与`claims["http://schemas.microsoft.com/identity/claims/objectidentifier"]`，分别对应Client ID和服务主体Object ID，再与上面的身份查询交叉核验。不能只看`caller`的显示形式就猜是哪种ID。活动日志只证明该条管理操作由谁执行及其结果，不证明Blob上传或数据库恢复成功；全新客户无需先故意运行失败来获取ID。

**回填与授权后核验：** 修改受控客户JSON后，更新同一GitHub Environment的`CUSTOMER_CONFIG_JSON` Secret。Stage0内补填该字段后，对`component=backup`重新运行`plan`并审核RBAC变更，再用该plan的run ID执行`deploy`；不是重跑bootstrap或直接重用旧plan。已有阶段验收时按第3节复核受影响证据；Stage4以后不要回套Stage0网络模板，应单独审批授权变更。

可从Storage Account → Containers → litellm-postgresql → Access control (IAM)核验容器范围授权，或使用以下只读查询。`RUNTIME_OBJECT_ID`来自上述输出；`BACKUP_CONTAINER_SCOPE`中的Storage ARM资源ID从备份账户Portal的JSON View取得，后接实际容器路径（当前模板固定为`litellm-postgresql`），不是Blob URL：

```bash
RUNTIME_OBJECT_ID="REPLACE_RUNTIME_SERVICE_PRINCIPAL_OBJECT_ID"
BACKUP_CONTAINER_SCOPE="REPLACE_BACKUP_STORAGE_RESOURCE_ID/blobServices/default/containers/litellm-postgresql"
az role assignment list --assignee-object-id "$RUNTIME_OBJECT_ID" \
  --scope "$BACKUP_CONTAINER_SCOPE" --include-inherited --fill-principal-name false \
  --query '[].{role:roleDefinitionName,scope:scope,principalId:principalId}' --output json
```

核对容器范围的Storage Blob Data Contributor（或客户另行批准的等效数据权限）与运行Object ID匹配；新授权还需等待传播并验证实际访问。此查询不证明网络可达，也不完整评估组成员、自定义角色或条件权限，不能仅凭角色名称认定备份已可执行。继续完成S0-05的Runner私网/DNS/443核验，不打开Storage公网，也不授予订阅Owner代替这些步骤。

#### 0-B. 获取POSTGRES_RESTORE_IMAGE

先核对旧服务器和Pod内`pg_dump`的大版本，选匹配大版本及所需扩展的批准镜像。下面以PG16、Linux x64 Runner为例，在有Docker的受控机器上执行；客户不是PG16时必须换成相应批准版本，不能直接照抄。

```bash
IMAGE="postgres:16.15"
docker pull --platform linux/amd64 "$IMAGE"
docker image inspect "$IMAGE" --format '{{range .RepoDigests}}{{println .}}{{end}}'
```

把输出中对应镜像的完整`postgres@sha256:...`填入 **Settings → Environments → test → Environment variables → POSTGRES_RESTORE_IMAGE**。不是`docker images`的IMAGE ID，不是只填64位哈希。选定digest后用该完整引用运行`docker run --rm --network none --read-only --entrypoint pg_restore <完整镜像引用> --version`核对版本；不启动旧库或执行恢复。

本项目曾验证的PG16镜像为：

```text
postgres@sha256:e17e86066e5ef83e0952a9347f5c792b7ece00972e2aa787a6986f471b3dd3d5
```

这不是客户备份artifact中的值。环境兼容且获批时可以复用；它是完整PostgreSQL恢复容器，不是只有客户端工具的镜像。同大版本仍需核对扩展、locale/排序规则和实际恢复结果；digest锁定内容，不证明备份可恢复，也不会自动获得后续补丁。

#### 0-C. 按顺序运行

下面**每一行都是一次新的Run workflow**。所有行均选受保护的`main`和`environment=test`；其他输入按第3节通用填写规则。`S0-xx`只是本文步骤号，不是要填入GitHub的run ID。

| 步骤 | workflow显示名称 | stage | component或action | operation | approved_run_id | confirm_environment |
| --- | --- | --- | --- | --- | --- | --- |
| S0-01 | Customer infrastructure deployment | 0 | component=bootstrap | plan | 留空 | 留空 |
| S0-02 | Customer infrastructure deployment | 0 | component=bootstrap | deploy | S0-01成功且已审核的run ID | test |
| S0-03 | Customer infrastructure deployment | 0 | component=backup | plan | 留空 | 留空 |
| S0-04 | Customer infrastructure deployment | 0 | component=backup | deploy | S0-03成功且已审核的run ID | test |
| S0-05 | 人工网络/权限准备，不是workflow按钮 | - | 完成下述私网及权限核验 | - | - | - |
| S0-06 | Customer private runtime operations | 0 | action=backup-restore | execute | 留空 | test |

所有上述操作的`approved_plan_sha256`留空；基础设施的`release`不勾选；runtime的`audit_continue_run_id`留空。**backup-restore只支持execute，不运行plan，也不填bootstrap/backup的批准ID。** 执行前可以用Stage0验收draft查看检查范围，但draft不是自动批准备份。

**S0-05不能跳过：** Runner管理VNet与备份VNet要有获准双向Peering/Hub路由、NSG及Blob Private DNS关联或转发。VM同订阅不代表私网互通，NAT也不能代替Peering。由网络管理员从Runner确认备份Blob FQDN解析到该账户PE的私有IP且TLS/443可达；不得打开Storage公网解决。运行身份须能读旧AKS信息/获取用户凭据、读旧Deployment/Pod并执行postgres Pod的exec/cp，还须读目标RG部署输出、向备份容器上传/下载Blob。Azure Contributor不自动授予Blob数据或Kubernetes权限。

**成功后核对：** bootstrap只创建目标RG和新Log Analytics工作区；backup创建私有Storage、容器、VNet/PE/DNS及获批RBAC，但尚未导出旧库。backup-restore才会执行Pod内pg_dump、拷到Runner、在无网络/无宿主端口的临时Docker PG中恢复，再上传Blob并回读校验。它不会把数据恢复到新生产PG，也不覆盖旧库。

解密S0-06的`runtime-test-0-<run ID>`附件，查看`acceptance-report.json`的`observations`：`fullRestoreSucceeded`、`backupBlob`、`backupSha256`、`backupBytes`、`publicTableCount`和`restoreSeconds`。安全保留备份引用和SHA256，Stage5要用；`restoreSeconds`不是包含所有步骤的生产停机时间。无错误完成恢复也不代表角色/ACL或业务密文已验证，流程使用了`--no-owner --no-acl`。

**失败后：** 停在本阶段，先看错误分类及加密结果。可能已产生一次独立备份Blob，重跑会使用新的备份名；不要删旧库、清空PVC或将dump上传GitHub。临时容器和文件会按流程清理，但机器异常中断后的残留仍须受控检查。不要只凭`pg_restore -l`成功认定恢复成功。

**阶段验收：** 按第3节运行`Customer stage acceptance`，`stage=0, operation=draft`，实测后`confirm`。本阶段检查为`inventory`、`backup_restore`、`key_salt_recovery`、`protocol_baseline`，以该draft实际列出的ID为准。备份/密钥恢复或客户实际客户端基线未通过时不进入Stage1。详见[备份模板说明](../infra/backup-storage/README_ZH.md)。

### 阶段1：旧环境最小加固

**开始前：** Stage0已验收；批准维护窗口、原Deployment安全快照、通知接收人和来源限制方案。沿用基础Variables/Secrets，无新的GitHub必填Secret。deploy身份需旧RG的日志/告警权限，runtime需旧AKS监控配置及限定Deployment更新权限。

**客户JSON：** `parameters.monitoring.logAnalyticsWorkspaceName`是**旧RG**的日志工作区名，不是新工作区名。已有获准工作区可用`parameters.legacy-logging.workspaceMode=existing`；没有则create（默认）。不能把仍被AKS/App Insights/告警使用的手工资源当作重复资源直接删除。已有Container Insights指向不同工作区时，代码会停止，需先批准迁移目的地。

| 步骤 | workflow显示名称 | stage | component或action | operation | approved_run_id | confirm_environment |
| --- | --- | --- | --- | --- | --- | --- |
| S1-01 | Customer infrastructure deployment | 1 | component=legacy-logging | plan | 留空 | 留空 |
| S1-02 | Customer infrastructure deployment | 1 | component=legacy-logging | deploy | S1-01的plan ID | test |
| S1-03 | Customer private runtime operations | 1 | action=monitoring-onboard | plan | 留空 | 留空 |
| S1-04 | Customer private runtime operations | 1 | action=monitoring-onboard | execute | S1-03的plan ID | test |
| S1-05 | Customer infrastructure deployment | 1 | component=monitoring | plan | 留空 | 留空 |
| S1-06 | Customer infrastructure deployment | 1 | component=monitoring | deploy | S1-05的plan ID | test |
| S1-07 | Customer private runtime operations | 1 | action=legacy-hardening | plan | 留空 | 留空 |
| S1-08 | Customer private runtime operations | 1 | action=legacy-hardening | execute | S1-07的plan ID | test |
| S1-09 | Customer private runtime operations | 1 | action=legacy-access-restrict | plan | 留空；仅选择来源限制时 | 留空 |
| S1-10 | Customer private runtime operations | 1 | action=legacy-access-restrict | execute | S1-09的plan ID | test |

S1-04之后先验证真实Container Insights日志已到达，再预览依赖这些表的告警。当前查询要求旧namespace=`litellm`、PG PVC=`pg-data`；不匹配须调整查询并测试，不修改真实资源名掩盖差异。S1-08只补缺少的探针并设PG Recreate，不自动锁定旧镜像digest、轮换密钥或完成管理面隔离；可能触发Pod滚动重启，需检查原有业务。

选择S1-09/10时，先在顶层配置`legacyAccess`，根据真实入口选择`nginx-ingress`或`load-balancer`，`allowedCidrs`填批准客户端的实际出口，`accessImpactAccepted=true`。NGINX模式只支持本项目既定Ingress/Service结构，额外入口/snippet等会阻断。限制后必须分别验证批准来源成功、未批准来源拒绝；API与管理路径共用入口时，整个入口白名单不等于独立管理隔离。

**回退：** 来源规则恢复使用同workflow、stage=1、action=legacy-access-restore，另跑plan再execute并引用该恢复plan ID；不能把restrict的plan用来restore。探针等回退按受控旧快照批准处理，不自动删除PVC、重置数据库或轮换Salt。

**阶段验收：** draft/confirm，stage=1；`legacy_health`、`alerts_received`、`rollback_snapshot`，配置legacyAccess后另有`legacy_source_access`。此外，批准的一期旧管理路径、废弃凭据及业务连续性必须实际检查；workflow做不到的必需控制仍须补齐，不能以探针成功代替整套加固。

### 阶段2：冻结客户决策

本阶段没有infrastructure组件部署。确定区域/SKU配额、VNet/Pod/Service/Docker网段、模型私网与访问身份、API/admin域名、PG认证/HA/RTO、实际客户端版本、必要管理操作、正文读取者及留存策略。开发人数不能代替数据库大小或一小时停机可行性实测。

一期原生审计在客户JSON顶层加入以下决策，示例7天须由客户批准：

```json
"contentAudit": {
  "mode": "native",
  "retentionDays": 7,
  "contentPolicyAccepted": true
}
```

此时不必提前提供application/proxy镜像；实际Stage8发布才要求两者齐全。不要同时开启auditRuntime/auditGovernance或L3 audit_reader/auditTeamId。历史已启用L3的环境需要独立迁移批准，不能直接删除其绑定。

运行`Customer staged migration`：branch=main、environment=test、stage=2、mode=config-check、component=none；随后`Customer stage acceptance`的stage=2 draft，完成决策核验后confirm。原生模式检查为`network_capacity`、`identity_owners`、`pg_auth_ha`、`content_audit_policy`、`protocol_scope`；不再把增强L3的旧`l3_policy`当作必选。

### 阶段3：供应链与目标基础

**开始前：** Stage2已验收；准备deploy身份、ACR唯一名称、目标Workspace以及完整stage4Network/stage4Aks配置。Stage3只创建平台早期资源，不创建新PG，不关闭旧模型账号的访问路径；Stage4才建立私网连接。

先运行`Check public source image`，只选main，没有environment/stage。核对`source-image-<run ID>`中的source-summary、SBOM及扫描结果；当前阻断策略为可修复CRITICAL，不是零漏洞或上游发布者签名证明。

| 步骤 | workflow显示名称 | stage | component或action | operation | approved_run_id | confirm_environment |
| --- | --- | --- | --- | --- | --- | --- |
| S3-01 | Customer infrastructure deployment | 3 | component=platform | plan | 留空 | 留空 |
| S3-02 | Customer infrastructure deployment | 3 | component=platform | deploy | S3-01的plan ID | test |

**检查结果：** 新ACR属于目标RG、公网禁用，旧环境不受影响。此时不为测试拉取而打开ACR公网；源镜像扫描与目标ACR签名/拉取是两个不同阶段的检查。

**阶段验收：** stage=3 draft/confirm；`oidc_scope`、`source_image_sbom_scan`、`target_isolation`。旧名称`image_signature_sbom`不再是Stage3的清单，具体用draft输出。

### 阶段4：私网、Private AKS与身份

**开始前：** Stage3已验收；目标VNet/PE子网由Stage0建立。核对AKS版本、节点SKU、区域限制/配额、网络段和Firewall出站；模型连接填真实账号Resource ID及别名。共享模型账号的公网/Local Auth关闭不能先于旧业务依赖核对，不能误伤其他使用者。

**本阶段配置：** 顶层privateIngress分别提供API/admin的`tlsSecretId`和`allowedCidrs`，结构见[私有入口参考](customer-deployment-workflows-zh.md#stage4自动私有入口)。证书应已在获准的证书Vault中可读，值为对应域名的PEM证书链及未加密私钥；API/admin材料分离，至少有效7天，Runner信任CA。不要依赖尚未创建的Stage5/7业务Vault提供Stage4证书。

自动API签发可选：另配`AZURE_CERTIFICATE_CLIENT_ID`、已委派Azure DNS Zone以及顶层`certificates={zoneResourceId,termsAccepted:true,publicApiHostnameAccepted:true}`。API tlsSecretId必须无版本；admin证书由企业PKI准备。未选择自动签发则跳过S4-07/08，但证书准备本身不能跳过。来源CIDR覆盖实际Runner私网路径和未来PLS NAT地址，admin只覆盖批准管理网段。

| 步骤 | workflow显示名称 | stage | component或action | operation | approved_run_id | confirm_environment |
| --- | --- | --- | --- | --- | --- | --- |
| S4-01 | Customer infrastructure deployment | 4 | component=platform | plan | 留空 | 留空 |
| S4-02 | Customer infrastructure deployment | 4 | component=platform | deploy | S4-01的plan ID | test |
| S4-03 | Customer private runtime operations | 4 | action=cluster-bootstrap | plan | 留空 | 留空 |
| S4-04 | Customer private runtime operations | 4 | action=cluster-bootstrap | execute | S4-03的plan ID | test |
| S4-05 | Customer private runtime operations | 4 | action=monitoring-onboard | plan | 留空 | 留空 |
| S4-06 | Customer private runtime operations | 4 | action=monitoring-onboard | execute | S4-05的plan ID | test |
| S4-07 | Customer private runtime operations | 4 | action=certificate-renew | plan | 留空；可选自动API签发 | 留空 |
| S4-08 | Customer private runtime operations | 4 | action=certificate-renew | execute | S4-07的plan ID | test |
| S4-09 | Customer private runtime operations | 4 | action=private-ingress | plan | 留空 | 留空 |
| S4-10 | Customer private runtime operations | 4 | action=private-ingress | execute | S4-09的plan ID | test |

**S4-02之后、S4-03之前：** 由网络Owner完成管理VNet到新AKS/ACR/证书Vault的路由、Private DNS和允许端口。确认ARM读取、Kubernetes用户凭据及命名空间权限；运行`Customer private runner checks`，environment=test、check_target=true。失败先处理连通/权限，不给私有AKS开放公网。

`cluster-bootstrap`实际创建litellm和两个ingress命名空间，不安装完整应用或自动授予所有Kubernetes权限。`private-ingress`会扫描/晋级固定Traefik镜像，创建API/admin两套私有入口并核验TLS、Host和内部LB前端；它不是Entra登录或模型调用测试。certificate-renew只更新Vault，不发布证书到入口，S4-09/10仍需运行。

**Stage4验收前的镜像步骤：** 私有ACR可达后运行`Promote LiteLLM image`，environment=test、acr_name=客户ACR名称、source_image保持仓库固定源digest、target_tag=`litellm-azure:rehearsal-1`（示例，按发布版本命名）、build_azure_runtime=true、build_auth_proxy=false。没有stage/approved_run_id输入。deploy身份需批准的ACR推送权限；Runner需访问源registry、扫描库和Sigstore。记录成功输出的完整`ACR/repository@sha256:...`，下一阶段应用配置使用它；扫描失败时即使已推送也不能作为已批准镜像。

**阶段验收：** stage=4 draft/confirm；`private_dns_egress`、`private_runner`、`target_image_signature_sbom`、`workload_identity`、`private_ingress`。目标镜像签名/拉取以及WI正反向访问要实际验证，不能用公共源扫描替代。Stage4之后不得重放Stage0 backup/network模板覆盖已扩展VNet。

### 阶段5：数据服务与迁移演练

**开始前：** Stage4已验收；在JSON中补齐`parameters.platform.stage5Data`（新PG库名/SKU/批准HA模式、Entra管理员Object ID/名称/类型、Redis SKU）及DNS Zone归属开关。HA Disabled不是已验证HA；区域不支持或配额不足时先批准调整，不自行退回更弱认证。

**本阶段新增Variables：** `AZURE_DATABASE_CLIENT_ID`、`MIGRATION_RESTORE_BLOB`、`MIGRATION_BACKUP_SHA256`。后两项必须来自同一个已核对Stage0备份报告。顶层另加`"databaseAccess":{"migrationPrincipalId":"REPLACE_RUNTIME_SERVICE_PRINCIPAL_OBJECT_ID"}`，填runtime身份的Principal ID而非其Client ID。

**PG管理员注意：** `database-roles`使用专项OIDC服务主体，必须已是该PG的Entra管理员或获准管理员组成员。若PG配置只填某个人的User Object ID，该个人不是此OIDC服务主体，不能把个人UPN填入变量就完成初始化。先与DBA确认受支持管理员配置/组成员，再运行；Azure RBAC不自动产生数据库内角色。

| 步骤 | workflow显示名称 | stage | component或action | operation | approved_run_id | confirm_environment |
| --- | --- | --- | --- | --- | --- | --- |
| S5-01 | Customer infrastructure deployment | 5 | component=platform | plan | 留空 | 留空 |
| S5-02 | Customer infrastructure deployment | 5 | component=platform | deploy | S5-01的plan ID | test |
| S5-03 | Customer private runtime operations | 5 | action=database-roles | plan | 留空 | 留空 |
| S5-04 | Customer private runtime operations | 5 | action=database-roles | execute | S5-03的plan ID | test |
| S5-05 | Customer private runtime operations | 5 | action=backend-secrets | plan | 留空 | 留空 |
| S5-06 | Customer private runtime operations | 5 | action=backend-secrets | execute | S5-05的plan ID | test |
| S5-07 | Customer private runtime operations | 5 | action=restore-target | plan | 留空 | 留空 |
| S5-08 | Customer private runtime operations | 5 | action=restore-target | execute | S5-07的plan ID | test |
| S5-09 | Customer private runtime operations | 5 | action=schema-migrate | plan | 留空 | 留空 |
| S5-10 | Customer private runtime operations | 5 | action=schema-migrate | execute | S5-09的plan ID | test |

S5-02之后先确认Runner到新PG/Redis/后台Vault的Private DNS及TLS连接、数据库管理员权限，再执行S5-03。database-roles创建独立llmgw_migrator和llmgw_app并授予应用所需数据权限；backend-secrets从旧litellm-env导入原Master/Salt到新Vault，目标已有不一致值或旧Salt不明确时停止，不生成替代值冒充恢复。

restore-target只恢复到Stage5输出绑定的**空目标库**，不使用手填任意DATABASE_URL；非空/失败残留须由DBA评估，不自动DROP或加`--clean`。schema-migrate之后才可发布新版应用，固定1.95→1.98合成升级已测试，但客户实际历史指纹/扩展/密文必须核对；未知历史不能baseline/reset绕过。

**输出/失败：** 后台密钥与schema动作保存ARM操作回执，后续application检查其SHA和配置，不手写这些回执。失败可能已部分创建角色、Vault版本或提交迁移；先检查现场再新plan，禁止删除旧数据或密钥以求重跑成功。

**阶段验收：** stage=5 draft/confirm；`pg_migration_restore`、`key_salt_decryption`、`redis_entra`、`csi_rotation`。需要的身份、密文、TLS、备份恢复及CSI/Redis验证不能只靠ARM存在证明；若需受控临时探针，由实施人员提供并获批，不能提前运行要求Stage5通过的Stage6 execute来绕过依赖。

### 阶段6：HA、路由与容量

**开始前：** Stage5已验收；无需新的GitHubSecret。确认Stage4得到的派生后台镜像签名仍绑定当前仓库、代码SHA和environment；若代码变过，应重新处理相关证据与镜像，不只改镜像字段。未提前构建时先按Stage4的Promote填写方式构建，不能使用上游默认入口镜像代替Azure派生运行时。

客户JSON增加application，填成功晋级后的digest和实际模型映射：

```json
"application": {
  "backendImage": "REPLACE_ACR/litellm-azure@sha256:REPLACE_BUILT_DIGEST",
  "models": [{
    "modelGroup": "coding",
    "connectionAlias": "primary",
    "deploymentName": "REPLACE_EXISTING_MODEL_DEPLOYMENT",
    "id": "primary-coding",
    "apiVersion": "v1"
  }]
}
```

`connectionAlias`必须匹配platform.azureOpenAIConnections中的alias，deploymentName是该账号实际部署名，不是模型展示名。不要填写MIGRATION_MANIFEST_YAML；schema/后台密钥/无密码数据库连接与CSI从前序回执生成。

| 步骤 | workflow显示名称 | stage | component或action | operation | approved_run_id | confirm_environment |
| --- | --- | --- | --- | --- | --- | --- |
| S6-01 | Customer private runtime operations | 6 | action=application | plan | 留空 | 留空 |
| S6-02 | Customer private runtime operations | 6 | action=application | execute | S6-01的plan ID | test |

核对新后台双副本、无DDL应用角色、模型调用、Redis共享状态及真实身份续期。此时API/admin代理还没发布，不能把从外网调API失败误判为后台部署失败，也不开放后台公网临时测试。

**阶段验收：** stage=6 draft/confirm；`replica_failure`、`load_affinity`、`capacity_limits`、`no_legacy_db_writes`。负载、故障与费用测试须批准；确认没有把新版连到旧库。失败修复新环境，不切旧流量。

### 阶段7：Entra和双域名授权

**开始前：** Stage6已验收；补`AZURE_ENTRA_CLIENT_ID`和`AZURE_ENTRA_ACCESS_CLIENT_ID`两个Environment Variables并完成对应Graph授权，不能以runtime/PG管理员代替。先构建代理镜像，再冻结entra/proxy配置，避免凭据回执因后补digest反复失效。

运行`Promote LiteLLM image`：environment=test、acr_name=客户ACR、source_image保持固定默认、target_tag=`auth-proxy:rehearsal-1`（示例）、build_auth_proxy=true、build_azure_runtime=false。两个构建选项不能同时勾选，输出代理digest不等于后台digest。

客户JSON的entra填两个**服务主体Object ID**，proxy填实际客户端Application Client ID、API准入主体和管理用户；角色/模型不使用通配符。建议首次即决定nativeUi及哪些管理员可读正文，避免后续扩大已有管理Key合同：

```json
"entra": {
  "bootstrapPrincipalId": "REPLACE_ENTRA_BOOTSTRAP_SERVICE_PRINCIPAL_OBJECT_ID",
  "accessPrincipalId": "REPLACE_ENTRA_ACCESS_SERVICE_PRINCIPAL_OBJECT_ID",
  "credentialLifetimeDays": 90
},
"proxy": {
  "image": "REPLACE_ACR/auth-proxy@sha256:REPLACE_BUILT_DIGEST",
  "nativeUi": true,
  "apiClientIds": ["REPLACE_CALLING_APPLICATION_CLIENT_ID"],
  "bindings": [
    {"oid": "REPLACE_API_USER_OBJECT_ID", "principalType": "User", "plane": "api", "role": "internal_user"},
    {"oid": "REPLACE_ADMIN_USER_OBJECT_ID", "principalType": "User", "plane": "admin", "role": "proxy_admin_viewer", "models": ["coding"], "nativeAuditRead": true}
  ]
}
```

这是字段示例；viewer只读，不代表客户必要管理写入已经满足。管理models引用application.modelGroup；**API绑定不填models或内部Key**，模型/预算权限由客户端vkey在LiteLLM中决定。apiClientIds不是deploy/runtime Client ID清单，而是实际调用API的获准客户端应用。

| 步骤 | workflow显示名称 | stage | component或action | operation | approved_run_id | confirm_environment |
| --- | --- | --- | --- | --- | --- | --- |
| S7-01 | Customer infrastructure deployment | 7 | component=proxy-foundation | plan | 留空 | 留空 |
| S7-02 | Customer infrastructure deployment | 7 | component=proxy-foundation | deploy | S7-01的plan ID | test |
| S7-03 | Customer private runtime operations | 7 | action=entra-apps | plan | 留空 | 留空 |
| S7-04 | Customer private runtime operations | 7 | action=entra-apps | execute | S7-03的plan ID | test |
| S7-05 | Customer private runtime operations | 7 | action=entra-access | plan | 留空 | 留空 |
| S7-06 | Customer private runtime operations | 7 | action=entra-access | execute | S7-05的plan ID | test |
| S7-07 | Customer private runtime operations | 7 | action=admin-credentials | plan | 留空 | 留空 |
| S7-08 | Customer private runtime operations | 7 | action=admin-credentials | execute | S7-07的plan ID | test |
| S7-09 | Customer private runtime operations | 7 | action=proxy-credentials | plan | 留空 | 留空 |
| S7-10 | Customer private runtime operations | 7 | action=proxy-credentials | execute | S7-09的plan ID | test |
| S7-11 | Customer private runtime operations | 7 | action=application | plan | 留空 | 留空 |
| S7-12 | Customer private runtime operations | 7 | action=application | execute | S7-11的plan ID | test |

proxy-foundation创建身份及Vault，不等于已生成登录凭据；entra-apps创建应用，entra-access才授予精确准入；admin-credentials生成管理OIDC和会话秘密，proxy-credentials初始化代理管理Key，**不替客户签发普通API vkey**。生成秘密写Vault，客户不复制进Actions；实际CA/MFA和客户端登录仍需客户身份管理员配合。Graph与Vault的部分成功可能留下待处理凭据，失败后按[轮换/恢复参考](customer-deployment-workflows-zh.md#管理员oidc轮换与孤立凭据恢复)处理，不能反复生成或删除状态。

**实际验证：** API同时携带`Authorization: Bearer <企业Token>`和`X-LiteLLM-API-Key: <客户端vkey>`；缺任一凭据、过期Token、无效/撤销Key、越权模型或预算超限应拒绝。记录客户实际Codex CLI、VS Code扩展及SDK版本，验证连续对话、工具调用、JSON/SSE、取消/重连和Token续期。不是要求实现全部Codex协议，但当前代理拒绝WS升级、部分对象引用/加密上下文；所选客户端确实依赖时仍是切流阻断，不能绕过企业认证。

管理员从私网登录原生UI，测试批准的日常Key/用户/Team/模型/预算操作。Logs核心读取已有本地测试，但未开放的必需管理写入不能用直改PG替代。`Customer gateway isolation checks`只有environment=test，待域名确实指向候选网关后运行；若API DNS还指向旧入口，结果不能算新网关验收。

**阶段验收：** stage=7 draft/confirm；`tenant_negative_tests`、`object_ownership`、`admin_private`、`required_protocols`。以客户真实首发范围验证，不要求所有插件/移动端页面；不支持的对象能力应明确拒绝并记录范围，不能填虚假的“对象授权已完成”。

### 阶段8：原生正文留痕与观测，增强L3可选

**开始前：** Stage7已验收；Stage2已配置并批准contentAudit native、正文留存1–30天、读取者、备份保留和容量/IO预算。一期用途是先按用户/时间统计Token费用，再由授权人员抽查高用量用户的工作内容；不是默认建设独立Blob/HSM或不可变取证平台。

**普通原生路径没有新增GitHub身份或Secret。** 托管application在Stage8生成`store_prompts_in_spend_logs=true`、`disable_spend_logs=false`及留存配置；不需要auditRuntime，也不走旧静态L3 overlay。Stage6/7正文仍默认关闭，不能只配置contentAudit而不发布Stage8就认为已采集。

选择遥测collector时，先把固定版本0.148.0镜像晋级到目标ACR，并加入顶层`observability.collectorImage`；批准AMPLS/Private DNS的影响。Promote的两个build开关均为false、source_image使用`otel/opentelemetry-collector-contrib@sha256:8164eab2e6bca9c9b0837a8d2f118a6618489008a839db7f9d6510e66be3923c`、target_tag用独立collector仓库标签。目标引用须保留此digest。未选择collector时跳过S8-01/02，仍保留基础日志和约定故障监测。

| 步骤 | workflow显示名称 | stage | component或action | operation | approved_run_id | confirm_environment |
| --- | --- | --- | --- | --- | --- | --- |
| S8-01 | Customer infrastructure deployment | 8 | component=observability | plan | 留空；仅选择collector时 | 留空 |
| S8-02 | Customer infrastructure deployment | 8 | component=observability | deploy | S8-01的plan ID | test |
| S8-03 | Customer private runtime operations | 8 | action=application | plan | 留空 | 留空 |
| S8-04 | Customer private runtime operations | 8 | action=application | execute | S8-03的plan ID | test |

**审计实测：** 使用真实首发客户端发送获准测试内容，等原生日志写入后，在原生UI Logs按时间/用户/Key/request ID查请求和响应；固定版本Chat/SSE正文主要在PG的`proxy_server_request`与`response`，`messages`为空不代表没Prompt。UI只展示元数据时，由明确获批的IT实名只读PG身份查询限定日志表/视图；该身份和网络访问需DBA另行配置，**当前没有一个自动授予PG审计只读权限的workflow按钮**。不要给审计者数据库超级管理员或共用llmgw_app/llmgw_migrator。

核对实际企业主体与Key指纹的关联；共享Key的Owner不能代表实际调用者。高Token也可能来自代码上下文、重复历史、工具结果或重试，不能只凭用量或不完整片段认定不当使用；按获批用途、时段和人员范围抽查，结论由人工结合工作任务核实。

**正常能力与边界：** 正文随Spend Logs写PG，不需要另一套Prompt存储。开关之前的历史不会自动补齐；异步日志队列在故障时可能丢失，脱敏/超大载荷截断及过期清理也可能使原文不完整。一期不承诺逐帧零丢失，但须确认正常请求能入库、日志写入故障可发现、留存足够覆盖抽查周期。备份中的正文可能比在线数据保留更久，恢复会重新出现，权限和处理策略应一致。

已有管理凭据的nativeAuditRead/nativeUi合同改变不会静默扩权；需独立批准的凭据更新，不能删旧Key/Vault重新初始化绕过。已有L3/guardrail/遥测不能因原生发布静默关闭；选择增强L3的客户另按[增强流程](customer-deployment-workflows-zh.md#stage8可选增强l3交付核心进展)执行，不把它列为一期默认。

**阶段验收：** stage=8 draft/confirm；原生模式为`native_spend_logs`、`native_audit_access`、`native_retention_recovery`、`guardrail_scope`，加`telemetry_received`或`telemetry_disabled`（取决于是否配置observability）。不是要求全部UI页面兼容；授权查询及客户实际使用的必要能力未通过仍不能切流。

### 阶段9：试点、切流与回退窗口

**开始前：** Stage0–8均已验收；已完成必要客户端、管理操作和日志抽查；最终数据同步/停写/回退方案已批准。Stage9前半可准备禁用流量的边缘资源，但第5节未完成时不得启用业务流量。

**客户JSON：** origin的VNet/ingress子网沿用目标网络。使用托管privateIngress时，`apiLoadBalancer.resourceGroupName/name/frontendName`可以填`auto`，代码从Stage4回执及实际LB解析API前端，不选择admin。edge.privateOrigin.privateLinkServiceId可填`auto`，privateLinkLocation填批准区域，后续从origin部署输出核对。不要手写猜测的节点RG/LB名称或把ARM资源ID填到FDID字段。

| 步骤 | workflow显示名称 | stage | component或action | operation | approved_run_id | confirm_environment |
| --- | --- | --- | --- | --- | --- | --- |
| S9-01 | Customer infrastructure deployment | 9 | component=origin | plan | 留空 | 留空 |
| S9-02 | Customer infrastructure deployment | 9 | component=origin | deploy | S9-01的plan ID | test |
| S9-03 | Customer infrastructure deployment | 9 | component=edge | plan | 留空；release=false | 留空 |
| S9-04 | Customer infrastructure deployment | 9 | component=edge | deploy | S9-03的plan ID；release=false | test |
| S9-05 | Customer private runtime operations | 9 | action=edge-bind | plan | 留空 | 留空 |
| S9-06 | Customer private runtime operations | 9 | action=edge-bind | execute | S9-05的plan ID | test |

origin创建API Private Link Service；edge默认流量禁用、WAF Detection，不改DNS。随后由Owner确认Private Link连接审批、API自定义域验证/边缘证书，以及源站TLS与来源限制。edge-bind只给API代理写实际FRONT_DOOR_ID并rollout，不绑定admin、不启流量；后续托管application发布保留该绑定。

**独立发布报告：** 实际启用流量前，将获批报告填入Environment Secret `MIGRATION_RELEASE_JSON`。它不是验收账本，也不能直接把acceptance JSON复制进去。字段与检查集由[发布校验器](../scripts/stage9_release.py)定义，主要字段如下：

| 字段 | 来源与含义 |
| --- | --- |
| environmentName、baseDomain、logAnalyticsWorkspaceName、rateLimitPerMinute | 与客户配置保持一致 |
| phase、wafMode | prepare/Detection保持禁用；canary/Detection启流量；production/Prevention正式发布 |
| privateOrigin | origin实际PLS ID和区域，不保留auto或REPLACE |
| frontDoorId | edge输出中的profileId，是Front Door GUID，不是CDN profile的ARM Resource ID |
| revision、configSha256 | 当前同版本Stage9 plan-summary中的revision和configSha256，不手算也不用镜像digest代替 |
| auditMode、telemetryEnabled | 本文auditMode=native；telemetryEnabled与是否配置observability一致 |
| changeTicket、approvedBy | 获准变更引用及governance对应Entra批准人Object ID；单人1位、双人2位 |
| checks | 当前phase必需的实际结果；每项含passed=true、report安全引用、带时区observedAt，须7天内有效；未通过不填写true |

native canary要求prepare检查加企业认证、实际协议、数据库恢复、WAF/试点和原生日志/受控查询/留存恢复证据；未选collector时用telemetry_disabled，不伪造telemetry_alerts。生产另需canary SLO、WAF Prevention评估、DNS切流/回退证据。当前没有自动采集所有这些证据并生成完整发布报告的按钮，实施负责人需整理真实结果；不得用空报告推进。

| 发布phase | 必须具备的checks名称，沿用前一行集合再增加 |
| --- | --- |
| prepare | private_origin_tls、origin_bypass_denied、admin_private_isolation、waf_diagnostics_privacy、private_link_approval、rollback_plan |
| native canary | entra_backend_acl、required_protocol_matrix、database_restore、waf_detection_review、approved_pilot_clients、native_spend_logs、native_audit_access、native_retention_recovery；启用collector用telemetry_alerts，否则telemetry_disabled |
| native production | canary_slo、waf_prevention_review、dns_cutover_and_rollback |

只有第5节的最终数据及发布条件满足后才执行下列行。每次phase变化先更新已审核MIGRATION_RELEASE_JSON并重新plan；普通S9-03的禁用计划不能批准启流量。

| 步骤 | workflow显示名称 | stage | component或action | operation | approved_run_id | confirm_environment |
| --- | --- | --- | --- | --- | --- | --- |
| S9-07 | Customer infrastructure deployment | 9 | component=edge | plan | 留空；**release=true**，报告为本次获批phase | 留空 |
| S9-08 | Customer infrastructure deployment | 9 | component=edge | deploy | S9-07的plan ID；**release=true** | test |
| S9-09 | Customer private runtime operations | 9 | action=dns-publish | plan | 留空；仅已批准Azure DNS路径 | 留空 |
| S9-10 | Customer private runtime operations | 9 | action=dns-publish | execute | S9-09的plan ID | test |

启流量并不自动限制canary用户，试点名单/网络/Key必须实际受控。DNS动作只支持同订阅已委派Azure DNS中的`llm-api.<baseDomain>` CNAME，需提前配置顶层`dns={zoneResourceId,ttl}`和runtime DNS写权限；ttl范围60–3600秒。已有正式API记录不能为了通过检查提前指向新边缘；应在批准窗口操作。其他DNS提供方走明确的人工变更，不临时修改脚本猜API；admin记录绝不公开。

DNS回退为同runtime、stage=9、action=dns-rollback，另跑plan/execute并引用恢复plan ID；这是DNS恢复，不是数据库回退。canary验收通过后，生产报告使用phase=production/wafMode=Prevention，重复S9-07/08；若DNS已指向同一获准目标，不为重复操作而重做DNS切换。

候选入口实际就绪后运行`Customer gateway isolation checks`，environment=test，结合正向客户端测试；匿名拒绝探针不证明业务成功。**阶段验收：** stage=9 draft/confirm；`enabled_what_if`、`origin_tls_private_link`、`pilot_regression`、`rollback_rehearsal`，加single_operator_release或dual_owner_release。stage acceptance不能替代MIGRATION_RELEASE_JSON及最终停写/数据核验。

## 5. 最终停写、切流、观察和停旧

**本节是批准后必须完成的迁移检查点，不是现有workflow已覆盖的按钮清单。** 目前最终写入冻结、最终空目标库选择/切换及数据回退编排尚未完整实现。不能拿Stage0旧时间点备份加一次DNS更新当作无损迁移；未补齐并演练下面步骤时，停在Stage9禁用流量准备，不执行S9-07及之后的启流量操作。

| 顺序 | 责任/执行方式 | 继续条件 |
| --- | --- | --- |
| F-01 演练和窗口 | 实施负责人/DBA核对实际库大小、备份/传输/恢复/升级/核验/切流耗时；现有Stage0/5提供部分操作 | 批准停机窗口、超时阈值和回退条件；不把开发者人数或合成测试秒数当RTO |
| F-02 最终写入冻结 | **尚无完整workflow动作**；实施人员须提供并批准实际入口停写、排空及后台写入停止步骤 | 旧推理、管理、预算和后台写入均已停止，保留原恢复状态 |
| F-03 最终备份和目标恢复 | **最终目标选择/接线待补齐**；DBA明确干净最终库，用批准备份恢复/迁移流程核验 | 不能对非空演练库直接重复restore-target，不自动DROP；保留原Master/Salt |
| F-04 数据/功能核对 | 对账用户/Team/Key/预算/模型配置和必要历史密文；用真实Codex/SDK和管理路径测试 | 新库包含最终数据；认证、权限、正常请求及正文查询符合一期范围 |
| F-05 批准切流 | 完成本节前置后，按S9-07/08发布edge，再按S9-09/10或批准人工DNS变更 | 新系统独占业务写入，不随机将用户分到两个不同步的预算库 |
| F-06 稳定观察 | 人工确定观察期及频率，检查真实业务错误/延迟/预算、PG连接、正文写入、用户反馈 | 覆盖实际业务使用，未达到通过标准不宣布迁移完成 |
| F-07 停旧 | **当前无stop-legacy workflow按钮**；Owner按单独批准的停用方案执行并验证 | 新系统不依赖旧资源；保留旧PG/PVC、备份及密钥恢复材料，停用不是删除 |

切流前失败继续使用旧系统；窗口内新库未产生独有业务写入时，可按已演练步骤恢复旧入口/旧写入。新库已产生独有Key、预算或日志等写入后，先暂停并对账，再决定修复新系统或数据回退；**只改DNS不保证无损回退**。不混用新旧schema，不自动跨schema回迁。

不把本次演练中的手工旧监控清理推广成客户前置步骤。现有工作区、App Insights、DCR、告警、网络/共享模型必须先查依赖；没有逐项授权不删除。删除旧RG、清理日志正文或移除共享身份是另一项变更，不是workflow成功后的自动收尾。

## 6. 常见错误与重跑规则

| 现象/日志 | 首先核对 | 下一步 |
| --- | --- | --- |
| authorize的GITHUB_REF_PROTECTED检查失败 | main是否匹配Active规则，是否手动从默认分支运行 | 修复分支保护；不删门禁 |
| runs-on / fromJSON: empty input | MIGRATION_PRIVATE_RUNNER_LABELS是否为Repository Variable、有效JSON数组 | 不放Secret或仅Environment；标签匹配实际Idle Runner |
| 等待Runner | VM/systemd服务、标签、仓库/Runner Group准入 | 先恢复执行机，不更换业务集群 |
| azure/login: Not all values are present | 当前workflow读取的是deploy还是runtime Client ID；tenant/subscription Variables是否在所选环境 | 不能把AZURE_CLIENT_ID当作所有动作的身份，也不能只放同名Secret |
| OIDC没有匹配联邦凭据 | issuer/audience/subject以及仓库和environment大小写 | 修正已批准身份的信任；不改成VM IDENTITY或长期Client Secret |
| WORKFLOW_ARTIFACT_KEY/CUSTOMER_CONFIG_JSON在env中空白 | 两者是否为本次Environment Secrets，而不是Variables、文件路径或本机变量 | 补齐后新运行；密钥已生成则不随意轮换 |
| Workflow evidence protection failed | base64解码后密钥是否32字节，附件是否同key/仓库/run ID/name | 不打印密钥，检查存放位置及元数据；不用新key解旧附件 |
| backup-restore: execution-failed且POSTGRES_RESTORE_IMAGE为空 | Stage0新增镜像Variable，完整repo@sha256引用 | 按0-B获取，不填备份SHA或IMAGE ID，不运行plan |
| backup-restore在plan失败 | 该动作只有execute | 先完成备份前置，再直接execute并确认环境 |
| deploy/execute批准字段为空或run ID填到hash框 | 页面两个description标签 | 把数字填入Successful matching ... plan run ID，Legacy direct hash框留空 |
| stale / plan changed / 签名或回执版本不符 | plan/execute是否同代码、配置、实际资源；镜像签名是否当前revision/environment | 修配置后重新plan/审核；不复制新哈希越过检查 |
| Azure 403 / Kubernetes Forbidden / PG拒绝 | 本动作实际身份和具体资源范围，ARM/数据平面/Kubernetes/PG分别授权 | 找对应Owner补最小权限；不因为登录成功就授予Owner兜底 |
| Blob解析公网IP或上传超时 | 管理VNet到备份PE路由、Private DNS、NSG及Blob数据角色 | 不开放Storage公网、不把NAT视为私网接通 |
| bootstrap plan在旧版本报通用执行错误 | 是否已包含处理What-if delta:null的修复 | 合并修复后新Run workflow，不能重跑旧SHA任务 |
| 只有execution-failed，无法确定原因 | 保留run链接、输入、SHA和已解密的operation-status/计划状态 | 不能仅凭同一句摘要认定都是配置错；先定位配置、权限或脚本缺陷 |
| Git push main报GH013 | main要求PR | 推工作分支，通过PR合并，不强推或关闭保护 |
| Bastion命令缺少ssh扩展 | 发起连接机器的Azure CLI扩展 | 同时具备bastion和ssh，不重建Runner |

公开日志中的`***`是遮罩，不表示脚本真的含这些字符；不要照着遮罩日志复制命令。Secrets空白和`***`也不是一回事。不要启用公开shell trace、打印环境变量或上传dump/原始客户配置来定位失败。

当前原始stdout/stderr和命令诊断会被保留在作业私有临时目录，结束时清理，**不在加密artifact中承诺保留完整失败诊断**。作业结束后可能只剩通用状态，应由实施人员在同版本/权限的受控环境做最小复现；本机管理员身份的成功不能代替Actions运行身份。不要为定位错误盲目重跑有副作用的恢复/凭据动作。

### 6.1 每次运行的记录表

在受控位置维护下表即可，不提交客户信息到公开Git，不要求另建证据平台：

| 日期/环境 | 本文步骤号 | workflow及stage/action | Git SHA | plan run ID | execute/deploy run ID | 实际观察/失败处理 | 验收draft/confirm run ID |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 待填写 | 待填写 | 待填写 | 待填写 | 待填写 | 待填写 | 待填写 | 待填写 |

**进入下一Stage前：** 前序适用检查全部真实通过，验收账本有效且同修订；所需新Variables/Secrets/JSON字段已准备；计划已解密审核；知道会修改哪些旧/新资源和怎样处理失败。文档完整、CI通过或单个workflow成功，都不等于客户迁移已经完成。