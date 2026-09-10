# 客户分阶段部署与验收工作流

本指南适用于客户控制的私有仓库和经批准的旧网关迁移。代码包含实际写入 Azure/Kubernetes 的入口；不要在公共示例仓库执行。运行成功不代表安全增强已完成：协议兼容、身份授权、可靠审计等运行时能力仍必须实测，未完成的项目见收尾台账。

## 交付目标与两条路径

### 第一阶段审计选择与实现边界（2026-09-10）

迁移和从零部署都采用**原生 Spend Logs 正文留痕优先**：批准范围内的Prompt/Response进入私有PostgreSQL，自建L3分片、独立Blob/HSM、恢复、双审批和保全只在明确选择增强方案时使用。基础版仍需受控查询、留存/备份、容量、写入故障及双凭据身份归因验收；不等同于取消审计。方案与报价见[基础版说明](litellm-content-audit-phase1-customer-brief-zh.md)及[审计成本口径](litellm-bom-cost-comparison-zh.md#6-员工与-agent-上下文审计成本影响)。

| 项目 | 基础版交付目标 | 当前实现与待改事项 |
| --- | --- | --- |
| 原生正文开关 | 获批后启用`store_prompts_in_spend_logs`，控制配置覆盖与采集范围 | 默认仍为false；生成器与静态禁止正文门禁尚待适配，不提供未经实现的新JSON开关 |
| 原文查看 | 原生UI/API或经过验证的受控查询路径，限定读取者 | 代理仍未开放原生UI/Spend Logs查询；自建`/audit`读的是独立Blob，不是原生查询替代品 |
| Stage8发布与验收 | 原生留痕、归因、容量/清理、故障和基础监控验收；增强L3按选项验收 | 当前Stage8/application仍要求`auditRuntime`，Stage2/8证据仍含`l3_policy`、`durable_audit_recovery`等旧项；需改代码后再提供可执行基础版流程 |
| 运维遥测 | 必要监控保留，collector按需，不复制正文 | 当前托管observability仍依赖auditRuntime；去耦合尚待实现，不能为上监控被迫开启L3 |
| 旧环境与回退 | 保留旧库、Key/Salt、备份及获批审计数据 | 不自动删除Blob/HSM Key/保全登记；关闭后续正文不删除历史，恢复可能使已清理正文重新出现 |

**本次是文档方案对齐，不是原生模式已上线。** 不跳过Stage8进入Stage9，不将L3未执行项伪造为passed，也不删除已有强制审计binding来绕过失败关闭。下文`audit-foundation`、`audit`、`auditRuntime`、`auditGovernance`及audit-pause/recover/resume的操作说明仅适用于可选增强L3，不是基础版默认执行顺序。基本实现适配应由项目代码与workflow完成，不要求客户手改YAML/SQL或编写检查脚本。

目标是客户只提供必要的身份/既有资源ID、域名、模型连接和网络/治理决策，通过workflow完成配置检查、资源部署、应用发布、真实测试、证据保存和经批准的发布/回退。不把编写Bicep、Kubernetes YAML、SQL、协议测试代码或手算证据哈希作为客户职责。初始信任和权限仍需有权管理员批准，自动化不能自行提升权限。

| 路径 | 配置与适用阶段 | 数据及旧环境保护 |
| --- | --- | --- |
| 已有网关迁移 | `deploymentMode=migration`，省略时保持此模式；参考[迁移示例](../config/customer.example.json)；Stage0至9 | Stage1仅做受控旧环境加固，其余增强在隔离新环境验证后切流；不原地重建旧数据库/PVC或更换旧密钥 |
| 从零部署 | `deploymentMode=greenfield`；参考[新建示例](../config/customer.greenfield.example.json)；Stage0、2、3、4、5、6、7、8、9 | 不填写`legacy`，不部署旧监控，不执行旧库备份/恢复；Stage5应初始化新库/新密钥，Stage9是首次发布 |

模式由同一份`CUSTOMER_CONFIG_JSON`控制，现有部署/运行/验收workflow共用该选择，不另设容易冲突的第二个模式开关。模式被证据哈希绑定；不能通过切换模式绕过迁移备份要求。

新建Stage0使用infrastructure的`bootstrap → network`，无需备份Owner或旧AKS/PVC。`network`创建目标VNet和PE子网；`backup`也管理初始VNet，两者不得同时配置。VNet及PE子网名称应与platform、audit和origin一致，Stage4后不得重放初始网络部署。私网runner访问新VNet所需的对等连接、路由和DNS仍必须具备。

新建模式的Stage0检查为订阅范围、部署权限、私网runner和域名控制权；Stage5检查为数据库schema、应用密钥恢复、Redis Entra和CSI轮换；Stage6使用目标数据库范围检查，避免索要不存在的旧库证据。Stage1直接拒绝，不生成虚假的passed记录。新环境上线后的数据库备份/恢复能力仍须验证，不能把“没有旧数据”解释成不需要灾备。

**当前状态不是完整产品交付**：模式路由、基础设施入口、新建网络、独立schema迁移和Azure应用入口已经实现；应用清单自动生成、身份/秘密完整接线及各阶段云端实测尚未全部完成。下文原Stage0/1/5的备份恢复操作仅适用于迁移。`MIGRATION_MANIFEST_YAML`和手工报告是过渡接口，不能作为上述目标完成的依据。

## 1. 四个入口

| Actions 名称 | 模式/操作 | 作用 |
| --- | --- | --- |
| Customer staged migration | guide / config-check / preflight / what-if | 指引、无前序证据的配置检查、带证据的就绪检查、原有保守预览；不部署 |
| Customer infrastructure deployment | plan / deploy | 对选定阶段和组件预览，批准后真正执行 ARM 部署 |
| Customer private runtime operations | plan / execute | 私网 runner 上备份恢复、旧环境探针加固、监控接入、目标库恢复、客户应用清单发布 |
| Customer stage acceptance | draft / record | 生成 pending 报告骨架，或由已审核的真实报告生成证据账本 |

`plan` 不要求前序证据，便于前期评审，但有实际资源依赖的模板必须等依赖就绪；计划成功不是进入该阶段的批准。`deploy` / `execute` 要求前序阶段已验收。阶段0没有前序证据，Secret 初始使用 `[]`。阶段2是决策和验收，没有资源部署。

### 单人运维

客户确实只有一位运维时，在私有客户配置顶层加入：

```json
"governance": {
  "approvalMode": "single-operator",
  "approverObjectIds": ["REPLACE_OPERATOR_USER_OBJECT_ID"],
  "singleOperatorRiskAccepted": true
}
```

由客户负责人明确批准单人职责合并风险。该模式不宣称职责分离或双人签字；若客户法规要求双人审批，不能使用。省略 `governance` 保持原双人策略；双人模式可用 `approvalMode=dual` 和至少两个允许的 Object ID。生产 Environment 的审批规则也必须按选择的模式配置；单人模式若禁止唯一运维自审批，将无法执行，可用客户已有外部变更审批作为补偿措施。Object ID 清单只是记录校验，不独立认证人员；保护配置/Secret 管理权限和默认分支是必要信任边界。

该开关仅适用于迁移实施/发布审批。选用增强L3时，不改变其原文双审批、反自审批和保全规则；基础版原生查询按已验证的原生权限和客户批准的有限读取者治理，不宣称自动具备L3逐次审批。只有一位IT运维也不意味着所有人都可读正文，业务/合规人员可参与授权决策。

## 2. 一次性前置准备

1. 将项目导入客户私有仓库，启用默认分支保护、PR/CI 和 `prod` Environment。新增写入 workflows 只允许私有仓库的默认分支，GitHub Environment 审批是否可用取决于套餐。不要为方便移除仓库/分支检查。
2. 平台管理员创建部署 OIDC 身份及私网运维 OIDC 身份。issuer 为 `https://token.actions.githubusercontent.com`，audience 为 `api://AzureADTokenExchange`，subject 为 `repo:组织/仓库:environment:prod`。仓库改名后同步信任关系。
3. 初始化目标 RG 需要订阅级创建资源组和嵌套部署的权限；后续基础设施尽量限定目标 RG。旧监控、旧 AKS 操作单独授权旧范围。涉及角色分配需要对应范围的受控 RBAC 管理权限，不能仅靠 Contributor，也不默认授予订阅 Owner。跨订阅模型账号的角色分配分别授权对应账号范围。
4. 准备专属、短生命周期 Linux x64 私网 self-hosted runner，能访问旧 AKS API、新 Private AKS、备份 Blob PE、PG/Redis/Key Vault PE 和私有 ACR，同时有获准出站。只接收受保护私有仓库作业，不开放生产服务公网。Windows 管理员仍可通过浏览器触发 Actions；本地复现使用受控 Linux/WSL，不能把 chmod 等 Linux 操作当作 Windows 原生支持。
5. runner 安装 Python 3.13、PyYAML、Azure CLI/Bicep、kubectl、kubelogin、Docker；恢复步骤另需匹配服务器版本的 PostgreSQL 客户端及支持系统 CA 的 libpq。Docker 需要足够磁盘/内存；备份含客户数据，runner 应使用加密磁盘。Docker 权限近似主机管理权限，隔离此 runner。

### Variables / Secrets

| 名称 | 存放位置 | 内容 |
| --- | --- | --- |
| `CUSTOMER_CONFIG_JSON` | Environment Secret 推荐，兼容 Variable | 完整客户 JSON，不是文件路径；Secret 优先。建议改用 Secret 避免 Actions 自动打印 step env |
| `AZURE_CLIENT_ID` | Environment Variable | 基础设施部署身份的应用/托管身份 Client ID |
| `AZURE_RUNTIME_CLIENT_ID` | Environment Variable | 私网运行操作身份的 Client ID，不能填个人用户 ID |
| `AZURE_DATABASE_CLIENT_ID` | Environment Variable | 仅database-roles使用的已授权数据库管理员OIDC Client ID；需为配置的PG管理员服务主体，或获准管理员组成员；不支持模拟个人用户登录 |
| `AZURE_ENTRA_CLIENT_ID` | Environment Variable | entra-apps/admin-credentials专用OIDC Client ID；与entra.bootstrapPrincipalId对应，不能与数据库迁移身份合并；需获准Graph自有应用管理权限及admin Vault权限 |
| `AZURE_ENTRA_ACCESS_CLIENT_ID` | Environment Variable | entra-access/entra-revoke使用；对应entra.accessPrincipalId，与应用初始化/数据库迁移身份分离。具有明确批准的Graph授权管理权限，不需要Vault或数据库权限 |
| `AZURE_AUDIT_GOVERNANCE_CLIENT_ID` | Environment Variable | 仅增强L3治理使用；读取维护状态并CAS更新audit-approvals，不授予Blob正文、后台管理或其他命名空间写权限；基础版不默认要求 |
| `AZURE_CERTIFICATE_CLIENT_ID` | Environment Variable | 独立API证书身份，仅批准的DNS挑战TXT读写、API证书及ACME状态Secret读写；不授予admin证书或后台Vault访问 |
| `AZURE_TENANT_ID` / `AZURE_SUBSCRIPTION_ID` | Environment Variable | 与客户配置一致 |
| `MIGRATION_PRIVATE_RUNNER_LABELS` | Repository Variable | 例如 `["self-hosted","linux","x64","llmgw-prod"]`；prod/test 使用隔离的 runner group/标签，不接收不可信 PR |
| `MIGRATION_EVIDENCE_JSON` | Environment Secret | 仅旧入口兼容；当前客户workflow自动读取同修订的成功acceptance-record artifact，不再逐阶段替换 |
| `POSTGRES_RESTORE_IMAGE` | Environment Variable | 与旧库大版本一致的官方或批准 PG 镜像，完整 `@sha256:`，不能使用 latest |
| `MIGRATION_MANIFEST_YAML` | Environment Secret | 仅旧手工发布路径；Stage6/7、Stage8审计及显式配置observability的collector自动生成，不填此Secret |
| `PRIVATE_API_INGRESS_CLASS` / `PRIVATE_ADMIN_INGRESS_CLASS` | Environment Variable | 仅客户自管controller路径使用；本项目private-ingress使用固定文件路由，不创建/监听IngressClass |
| `POSTGRES_MIGRATION_USER` | Environment Variable | 使用databaseAccess时自动为llmgw_migrator，可省略；旧自管角色路径需提供已授权的Entra角色名 |
| `MIGRATION_RESTORE_BLOB` / `MIGRATION_BACKUP_SHA256` | Environment Variable | 从阶段0报告取得的 pre-change blob 名和文件哈希 |
| `MIGRATION_REPORT_JSON` | Environment Secret | 填写完实测结果的验收报告 JSON；四项/本阶段全部真实通过才能 record |
| `MIGRATION_REPORT_URL` | Environment Secret | 固定的受访问控制 HTTPS 报告引用，无凭据、SAS 或查询串 |
| `MIGRATION_APPROVERS_JSON` | Environment Secret | 实际批准人的用户 Object ID 数组；数量与 governance 匹配 |
| `MIGRATION_RELEASE_JSON` | Environment Secret | 阶段9单独的发布报告，字段参照 stage9_release.py；另含当前 revision 和阶段9 configSha256 |

运行身份需要：AKS Cluster User 获取凭据权限、对应集群中受限 Kubernetes 操作权限；阶段0需要读取旧部署/Pod及 postgres exec，阶段1允许两个旧 Deployment 的受控 patch；新集群允许目标命名空间发布。Blob 操作需要数据平面权限；PG 恢复需要数据库内角色权限。Azure RBAC 不自动授予数据库内权限。首次 OIDC 信任、初始授权、公司 DNS/Entra 审批不由应用 workflow 自行提升权限。

## 3. 每阶段操作清单

所有infrastructure组件和普通runtime动作先plan并审查私有计划；新运行用approved_run_id选择成功计划的运行编号、确认environment，工具自动读取计划哈希。旧approved_plan_sha256仍兼容，但不必手抄哈希。操作回执绑定workflow、阶段/动作、代码/配置及计划文件，执行记录不能冒充计划。同一Git SHA、配置和实际状态重新规划后才能写入。Delete/Unsupported/未知基础设施变更仍拒绝；What-if不是锁定事务。backup-restore仍是显式有副作用操作，不伪装成只读plan。

| 阶段 | 具体入口与顺序 | 仍需人工准备/验证 |
| --- | --- | --- |
| 0 | infrastructure: bootstrap → backup；runtime: backup-restore / execute；acceptance: draft/record | 备份 Owner、runtime 身份 Object ID、私网 runner、旧库版本；业务协议、密文读取、关键行数及角色权限复核 |
| 1 | infrastructure: legacy-logging（无旧日志库时）→ runtime: monitoring-onboard → infrastructure: monitoring；runtime: legacy-hardening | 沿用真实旧 Workspace，不自动重定向；两个旧工作负载的变更窗口、告警邮件实际收到、回滚快照保管 |
| 2 | acceptance: draft → 客户架构决策；record需匹配已适配的审计证据契约 | 网络、成本、身份Owner、PG认证/HA、协议与原生留痕范围/留存/故障策略；L3增强另行选择，当前l3_policy字段尚待适配 |
| 3 | infrastructure: platform；既有 Promote LiteLLM image 在网络就绪后执行 | 签名/SBOM/扫描；私有 ACR 拉取需要受控私网执行；不打开 ACR 公网解决问题 |
| 4 | infrastructure: platform；runtime: cluster-bootstrap、monitoring-onboard、private-ingress | 私网runner/路由、SKU/出站、两份TLS PEM Secret ID、API/admin来源CIDR；入口实际TLS验证自动执行，后端认证另验收 |
| 5 | infrastructure: platform；runtime: database-roles → backend-secrets → restore-target（仅迁移）→ schema-migrate；均先plan再execute | 迁移身份需读旧litellm-env；后台Vault初始化权限由Stage5按databaseAccess派生；缺少旧Salt或目标密钥不一致时阻断。真实Azure/CSI验收仍需执行 |
| 6 | Promote LiteLLM image选择build_azure_runtime；runtime: application（plan → execute），配置application后自动生成 | 提供批准的派生镜像digest和模型部署映射；不填整份YAML。schema/密钥回执及签名自动验证，负载/故障/Redis/亲和仍需实测 |
| 7 | infrastructure: proxy-foundation；runtime: entra-apps → entra-access → admin-credentials → proxy-credentials → application，均先plan再批准执行 | 提供分离的Entra初始化/授权身份、客户端及准入主体；只有管理绑定填写模型权限。API客户端自带vkey，模型/预算在LiteLLM管理；自动建立精确角色分配和单用户同意，真实登录及双凭据续期仍待验收 |
| 8 | 基础版原生模式发布流程待适配；仅增强L3执行audit-foundation → audit → application | 基础版验证Spend Logs正文/身份关联、读取权限、清理/备份、容量与故障；选择增强后再验L3可靠交付/治理。不能跳过旧门禁或用L3关闭冒充原生留痕通过 |
| 9 | infrastructure: origin → edge（初始不启流量）；完成发布报告后 edge + release=true（plan → deploy） | PLS连接人工批准、源站证书、FDID/来源限制、限定试点客户端、数据库最终同步；外部DNS变更另行审批执行 |

### 阶段0从零启动

`bootstrap` 是订阅级部署，会真正创建目标 RG 和 Log Analytics Workspace，不要求先手工创建。`parameters.bootstrap` 可省略或设置 `{"workspaceMode":"create","logRetentionDays":30}`。它使用 backup 的 `logAnalyticsWorkspaceName`（无 backup 时使用 platform）；应把新环境各组件的 Workspace 名统一。已有同RG日志库用 `workspaceMode=existing`，先核实资源存在；当前仍不支持跨RG共享 Workspace。

随后部署 `backup` 建立私有备份存储和初始 VNet。`parameters.backup.backupAutomationPrincipalId` 填 runtime 身份的服务主体 Object ID，模板给它仅在备份容器范围的 Blob Data Contributor；个人备份保管人仍用 `backupOwnerPrincipalId`。未配置自动化身份时，需客户另行授予已批准的数据权限。

`backup-restore` 不会运行在新 AKS：使用私网 runner 对旧 postgres 执行 pg_dump，临时文件位于旧容器 `/tmp`，拷贝到受控 runner；在 network=none、无宿主端口的临时 Docker PG 中完整恢复，再上传 Blob 并下载校验 SHA256。临时容器和远端 dump 会清理；workflow 清理本地 dump/kubeconfig。运行时会占用旧库 IO、旧容器临时空间及 runner 磁盘，不是纯只读无负载操作，应安排低峰。恢复使用 `--no-owner --no-acl`，故不能证明角色/权限或业务密文已恢复；报告保持 pending。失败后没有有效备份不进入下一阶段。已有合规备份可走原客户流程，不强制使用此动作。

### Stage1变更范围

`legacy-hardening` 读取现有 Deployment，仅补缺失的健康探针，并将 postgres 策略设为 Recreate；保留镜像、env、Secret引用、PVC和已有资源限额。逐个工作负载等待 rollout，不做密码轮换、PVC重建或数据库清理。资源Version条件防止覆盖并发变更；失败后保留现场，不自动回滚到未经审查状态。客户应在执行前通过原受控快照流程保管旧清单；Actions 不上传含旧密码的 Deployment 快照。

### Stage4自动私有入口

在客户配置顶层添加以下结构，替换示例Vault及来源CIDR；CIDR必须覆盖实际私网runner地址，API还需覆盖未来PLS NAT来源，admin仅允许获准管理网段：

```json
"privateIngress": {
  "api": {"tlsSecretId": "https://customer-vault.vault.azure.net/secrets/api-tls", "allowedCidrs": ["10.30.4.0/24"]},
  "admin": {"tlsSecretId": "https://customer-vault.vault.azure.net/secrets/admin-tls", "allowedCidrs": ["10.40.1.0/24"]}
}
```

Secret值必须是对应llm-api/llm-admin域名的PEM证书链加未加密私钥；可指定版本，未指定时读取版本并绑定计划。runner与Front Door必须信任签发链，至少还有效7天。当前入口不自动向任意PKI申请证书；证书续期后重新plan/execute会滚动发布，不能仅更新Vault就假定集群已经轮换。

操作顺序：先完成Stage4 platform和cluster-bootstrap，再运行private-ingress plan、审查runtime-review、execute。runtime身份需读取指定证书Secret、向目标ACR推送/读取镜像、管理两个入口namespace内对象及TLS Secret、读取AKS/LB/子网，并在目标RG写入部署回执；不授予读取litellm应用Secret的权限。runner另需openssl、skopeo、Trivy、Syft及批准的镜像/扫描库出站。

workflow扫描并晋级[固定Traefik镜像](../deploy/private-ingress-image.json)，生成两套2副本、只读非root、无Kubernetes API凭据的文件路由网关。仅暴露私有443，分别转发到API/admin代理；不监听应用namespace、不启用访问正文日志。TLS Secret使用不可变的证书指纹名称，旧证书不自动清理。与之配套的应用发布不要额外创建未受管Ingress。

自动检查源站证书指纹、域名/信任链、错误Host拒绝、独立私有IP及真实Standard LB前端，然后把非秘密输出保存为Stage4部署回执。配置privateIngress后，Stage9的origin从回执选择API前端并核对实际LB状态；不猜测admin前端。该测试不证明Entra认证、全部协议或L3审计已通过。

### Stage5数据库身份初始化

配置顶层`"databaseAccess": {"migrationPrincipalId": "REPLACE_RUNTIME_SERVICE_PRINCIPAL_OBJECT_ID"}`，填AZURE_RUNTIME_CLIENT_ID对应的服务主体Object ID，不能填Client ID或个人用户ID。应用UAMI自动从成功的Stage5部署读取；两者不得相同。database-roles只在Stage5可用，migration/greenfield均适用。

AZURE_DATABASE_CLIENT_ID必须已被配置为该服务器的Entra管理员，或是已授权管理员组成员；workflow不会自授数据库管理权，也不能用个人User管理员的UPN完成OIDC服务主体登录。先plan，后在同一代码/配置/数据库状态下execute。Entra映射在同一新服务器的postgres管理库创建，授权只作用于配置的新应用库。

自动建立llmgw_migrator（目标库/公共schema建对象权限）和llmgw_app（连接、公共schema使用、表读写、序列使用权限）；迁移角色后续建表自动赋予应用权限。为维护默认授权，数据库管理员获得迁移角色成员关系；应用不获得管理员或迁移角色成员关系。不会设置数据库密码，不接管现有密码角色或其他Object ID，不自动改非迁移角色拥有的表。

角色映射与目标库授权分两次数据库事务；若后者失败，已建角色可能保留，需要重新plan再继续，不能自动DROP清理。此动作不初始化LiteLLM schema，schema初始化由独立schema-migrate执行。配置databaseAccess后restore-target自动使用llmgw_migrator；应用使用下述专用入口禁用DDL，不得仍使用上游默认入口。

### Stage5独立schema迁移与应用启动

新建路径：platform → database-roles → schema-migrate。迁移路径：platform → database-roles → restore-target → schema-migrate。schema-migrate在Customer private runtime operations中选择Stage5，先plan后execute；使用AZURE_RUNTIME_CLIENT_ID对应的llmgw_migrator，不使用数据库管理员执行日常迁移。

两条路径在database-roles之后、应用发布之前还要完成backend-secrets；数据恢复与密钥恢复必须共同完成后才能发布新应用。

该动作从成功的Stage5部署读取目标私有、Entra-only服务器，只连接配置的新数据库；客户不提供自由数据库URL。runner需要Docker，读取平台/服务器信息及在目标RG写入部署回执的权限，并能访问私有PG与固定源镜像。短期Token只写入600权限临时文件并只读挂载，不能出现在Docker命令参数、review或artifact；结束后删除。部署前Token须剩余至少35分钟，迁移执行限制30分钟，接近到期则拒绝启动而非冒险执行。

计划绑定固定LiteLLM镜像、运行时代码、schema/151个上游迁移文件/视图代码指纹、数据库迁移历史和当前SHA。执行前再次核对状态；只运行prisma migrate deploy，再准备八个上游视图，并用prisma migrate diff只读验证数据库与datamodel一致。成功后保存Stage5 schema-migrate ARM回执，输出databaseSchema，供后续自动发布读取；成功仍不自动通过整个Stage5验收。

未知或修改过checksum的历史、未完成迁移、缺失历史的旧数据库、未先恢复的migration模式均拒绝；不自动baseline、migrate resolve、db push、reset或删除重试。若客户旧版本历史不匹配，需要在隔离副本上开发并审核对应升级策略，不能把当前通用动作视为已验证所有历史版本。失败后数据库可能保留已经提交的部分迁移，先调查和重新规划，不自动回滚数据。

专用应用镜像定义见[Dockerfile](../LiteLLM/runtime/Dockerfile)。它以10001非root运行[Azure应用入口](../LiteLLM/runtime/application.py)，单进程4000端口，通过显式Workload Identity取Token，并安装有源码指纹保护的Prisma适配。应用配置必须有general_settings.disable_prisma_schema_update=true；启动前只读检查必要表/视图和未完成迁移，应用侧视图检查禁止DDL。数据库角色固定llmgw_app，仅能读取迁移历史，迁移结束及角色重跑都会撤销其对_prisma_migrations的写权限，应用启动也检查这一点。不允许迁移角色、备用数据库URL、存储密码或未支持的read-replica配置。

AZURE_DATABASE_URL_TEMPLATE是后续应用清单生成器应从部署输出生成的无密码模板，包含新服务器、库、llmgw_app和严格TLS参数，不是客户需要手工填写的Token。应用UAMI/CSI/密钥、该模板和派生镜像签名晋级将在A05/A06接通；当前通用manifest发布不会自动替换上游镜像。不能只设置IAM_TOKEN_DB_AUTH后继续使用原镜像，否则启动仍可能走AWS逻辑。

本地非root/只读容器已实测新库全部迁移、datamodel一致性、真实ASGI启动、无DDL应用角色以及凭据换新时真实Prisma引擎切换和在途事务完成。数据库凭据是隔离测试合成值，不代表真实Azure Token、AKS联邦、私网DNS或旧版本迁移已经验收。

### Stage5后台密钥与CSI

运行backend-secrets，先plan再execute。它从Stage5部署读取后台Vault及身份，不接受任意目标Vault。配置databaseAccess后，Stage5模板给migrationPrincipalId授予后台Vault范围的Key Vault Secrets Officer；应用UAMI仍只有Secrets User。Officer包含修改/删除等权限，不是只能create的角色，需在部署计划中批准并限制身份使用；初始化结束后的降权流程仍待完善。旧Stage5输出不含身份字段时，先审查并重新部署Stage5。

- greenfield：仅在新库尚无用户表时创建缺失的litellm-master-key和litellm-salt-key，使用加密安全随机数。重复执行保留现值，部分失败重跑只补缺失项。数据库已有表却丢失密钥时停止，必须恢复原密钥，不能生成替代值。
- migration：从旧AKS/namespace读取litellm-env的LITELLM_MASTER_KEY与LITELLM_SALT_KEY，原值只在内存处理；源Secret资源版本绑定计划，目标已有值必须相同，否则拒绝。旧版本没有显式Salt时先完成兼容性恢复策略，不生成新Salt假装恢复。
- 不接管没有本流程标记的既有目标密钥；值必须启用且无隐式到期。迁移导入不强制改变旧值长度或编码。

初始化使用目标数据库会话锁串行化本流程，并在写前/完成后复核Vault版本和值；本实现没有Key Vault原子create-if-absent保证，不能阻止其他管理员并发写入。执行窗口内其他写入方必须冻结。计划不提前生成密钥，review/artifact不含密钥、密码哈希或旧Secret数据，回执只保存Vault ID和版本。

成功后生成backend-access.json并保存Stage5 backend-secrets回执。CSI只挂载两项密钥，绑定后台UAMI和已验证版本；不持久化数据库Token，不生成secretObjects副本。应用从/mnt/backend-secrets读取，环境值冲突就拒绝启动。版本固定，不自动追随Vault最新值；密钥轮换需验证密文兼容并单独发布，不以CSI轮询代替密钥迁移。API/admin代理不共享这个Vault或Master/Salt。

### Stage6自动生成后台清单

客户配置顶层新增application，backendImage填派生构建的真实digest；connectionAlias对应platform.azureOpenAIConnections中已批准账号：

```json
"application": {
  "backendImage": "REPLACE_APPROVED_ACR/litellm-azure@sha256:REPLACE_BUILT_DIGEST",
  "models": [{
    "modelGroup": "coding",
    "connectionAlias": "primary",
    "deploymentName": "REPLACE_EXISTING_MODEL_DEPLOYMENT",
    "id": "primary-coding",
    "apiVersion": "v1"
  }]
}
```

运行Promote LiteLLM image，选择目标environment（dev/test/prod）、build_azure_runtime=true及独立target_tag，例如litellm-azure:release-1。此模式使用固定Dockerfile，source_image不改变其基础镜像；构建、推送后扫描、生成SBOM和签名，扫描失败不签名。环境、目标ACR及签名环境标记必须一致；默认仍为prod。推送成功不等于镜像获准发布。

随后运行Stage6 application。生成器校验schema-migrate/backend-secrets回执的SHA、Stage5配置、库和Vault，自动读取后台Client ID、Principal ID、数据库FQDN、Redis地址和PE子网。镜像签名须来自当前仓库默认分支的promote workflow，且带llmgw.runtime=azure、当前llmgw.revision和llmgw.environment。runner需Cosign、ACR读取权限和批准的Sigstore出站；临时DOCKER_CONFIG验证后清理。

自动生成无DDL应用配置、模型映射、双副本Deployment、PDB/HPA、ServiceAccount、固定版本CSI、默认拒绝与仅API/admin代理可进入的网络策略。无密码数据库URL由代码生成，不引用旧DATABASE_URL Secret。生成结果仍经过服务端dry-run、计划批准、apply及rollout，不能因为自动生成而省略门禁。

新增application仅改变Stage6及之后指纹，不使Stage5回执失效；proxy/entra仅影响Stage7及之后，auditRuntime仅影响Stage8及之后。配置后不得同时提供MIGRATION_MANIFEST_YAML。Stage7代理按下一节自动生成；Stage8审计专用清单按下文显式配置生成，不可删除配置绕过尚未完成的集成验收。真实Redis续期/CSI/模型权限和负载仍需客户隔离环境测试。

### Stage7代理身份基础设施

proxy-foundation从平台配置及databaseAccess派生参数，创建API/admin各自UAMI、AKS联邦、独立私有Vault、PE/DNS、诊断和删除锁，自动输出proxyFoundation。各pod身份只能读取本平面Vault；迁移初始化身份在两个Vault获得Secrets Officer，不能把它用于代理pod。默认联邦分别绑定litellm中的llm-api-proxy和llm-admin-proxy。

上述是foundation保留的资源与权限配置：双凭据API运行时已不再读取或挂载API Vault里的用户Key，凭据初始化也仅访问admin Vault。旧API库、PE和角色不自动清理，是否退役需另行验证其他依赖并批准；不宣称当前API身份已无该Vault权限。

配置entra后，此组件另外给entra.bootstrapPrincipalId授予仅admin Vault范围的Secrets Officer，用于OIDC及会话密钥；API Vault不向该身份授权。该组件本身不生成业务Key，后续动作如下。审计writer/reader与代理的联合接线仍待完成，不能以基础设施成功作为Stage7验收。

### Stage7应用注册、凭据和自动发布

2026-09-10起，API采用企业Token与客户端vkey双凭据，详见[认证契约与迁移步骤](../auth-proxy/README_ZH.md#api双凭据契约2026-09-10)。API仅配置准入主体，不再填写models或由代理保存Key；仅管理项的模型名引用application.models的modelGroup。oid是实际用户/服务主体Object ID，不是应用Client ID。image来自本仓库auth-proxy镜像构建后的digest：

```json
"entra": {
  "bootstrapPrincipalId": "REPLACE_ENTRA_BOOTSTRAP_SERVICE_PRINCIPAL_OBJECT_ID",
  "accessPrincipalId": "REPLACE_ENTRA_ACCESS_SERVICE_PRINCIPAL_OBJECT_ID",
  "credentialLifetimeDays": 90
},
"proxy": {
  "image": "REPLACE_APPROVED_ACR/auth-proxy@sha256:REPLACE_BUILT_DIGEST",
  "apiClientIds": ["REPLACE_APPROVED_CALLING_APPLICATION_CLIENT_ID"],
  "bindings": [
    {"oid": "REPLACE_API_USER_OBJECT_ID", "principalType": "User", "plane": "api", "role": "internal_user"},
    {"oid": "REPLACE_ADMIN_USER_OBJECT_ID", "principalType": "User", "plane": "admin", "role": "proxy_admin_viewer", "models": ["coding"]}
  ]
}
```

按下列顺序执行；每个runtime动作都先plan再execute，并保留原阶段证据门禁：

1. **创建基础设施**：proxy-foundation，随后检查并批准新身份/Vault权限。Entra初始化身份与databaseAccess.migrationPrincipalId必须不同，不把个人登录或数据库管理员代替应用初始化身份。
2. **entra-apps**：使用AZURE_ENTRA_CLIENT_ID，通过Graph创建本方案专用、单租户API/admin应用及要求角色分配的service principal。固定作用域llm.invoke、应用角色Llm.Invoke、用户角色Llm.User和管理角色，自动配置admin回调域名。前版API应用仅缺少Llm.User且其他配置完全一致时，可经计划批准追加此角色；其他权限/Owner/回调漂移仍拒绝覆盖。完成后执行下述entra-access，再继续凭据与发布步骤。
3. **admin-credentials**：同一Entra初始化身份在Graph生成admin应用密码，并立即写入私有admin Vault；另生成32字节会话加密密钥。默认有效期90天，可设置30-180天；已有值不轮换，剩余不足7天阻断初始化和发布。原值不进入artifact，回执只含Secret版本、应用ID及到期时间。
4. **proxy-credentials**：切回AZURE_RUNTIME_CLIENT_ID，验证目标后台Deployment镜像/就绪状态与Service选择器，通过只监听127.0.0.1的kubectl port-forward调用用户/Key API。Master Key从Stage5回执绑定的后台Vault版本读取，只用于bootstrap，不复制进代理Vault或Pod。仅为启用的proxy_admin/proxy_admin_viewer创建管理用户及独立Key，精确限制管理角色、模型及路由；API准入主体和audit_reader均不获得该流程生成的Key。API客户端vkey通过LiteLLM原生受控管理流程签发和轮换，不复用旧代理Key，不由本动作代发。
5. **构建代理镜像**：Promote LiteLLM image选择build_auth_proxy=true、build_azure_runtime=false及独立target_tag。使用现有固定Node基础镜像和npm锁文件，扫描/SBOM后签名，标记llmgw.runtime=auth-proxy、当前代码及环境；两个构建选项互斥。普通导入镜像没有此标记，自动Stage7发布不会接受。
6. **application / Stage7**：读取entra-apps、entra-access、admin-credentials、proxy-credentials回执和proxy-foundation输出，验证SHA/配置/应用/Vault/凭据版本与镜像签名，生成后台与API/admin工作负载、策略、CSI、PDB和网络策略。使用已验证的文件路由私有入口，不额外创建Ingress；仅管理代理挂载本平面版本固定的秘密，API代理不挂载内部Key；不同步成Kubernetes Secret。仍执行服务端dry-run、计划比较、apply及三个Deployment rollout。

若步骤5得到新的proxy.image，先更新配置再对步骤2-4重新plan/execute确认；同值重跑不会重建Key或应用，但新配置需要新回执。也可先完成镜像构建再初始化，减少重复确认。后台与代理镜像分别构建，不能用同一digest代替。

**授权边界**：Entra初始化身份需要租户管理员批准的Graph Application.ReadWrite.OwnedBy（支持应用创建、读取和自有应用凭据管理），目标RG读取/写回执权限以及admin Vault Secrets Officer；不会自行授予这些权限。runtime初始化管理凭据需要目标AKS凭据/Deployment和Service读取、pods/portforward权限、后台Master版本读取、admin Vault Secrets Officer及回执写入，不再访问API Vault Secret。foundation仍保留旧API Vault和角色配置；不使用不等于已撤权，须另行审批回收。Secrets Officer含写入/删除权，需限制使用窗口，自动降权尚未实现。

**登录授权**：entra-apps仍只定义应用；实际角色分配和委托同意通过独立entra-access执行。代理bindings不等于Entra授权，目录授权记录也不等于真实登录成功。CA/MFA策略、真实客户端/用户登录负向测试仍需客户隔离环境验收，不得为得到成功结果移除appRoleAssignmentRequired、租户或主体校验。

**中断与重跑**：管理Key先写Vault pending，再创建后台用户/Key，只有后台信息验证一致才标记ready；请求结果不确定时，重跑按Key哈希查询，不重复创建。已ready但后台Key消失视为可能撤销，拒绝重新创建；角色/模型/路由不匹配或被block时不覆盖/解封。撤销及权限变更需独立流程，删除binding目前只使代理拒绝该主体，不等于后台Key已删除。

**客户端验收与旧配置切换**：请求同时携带`Authorization: Bearer <企业access token>`和`X-LiteLLM-API-Key: <vkey>`。代理只验证企业准入，剥离企业Token后将vkey送往固定后台；模型、Team、用户、Key权限和预算由LiteLLM裁决。Key与企业身份不做强制归属绑定，实际企业主体与Key指纹单独记录，不将Key所有者当作实际调用者。必须验证缺少任一凭据、错误Token、无效/撤销vkey、越权模型及预算超限被拒绝；Token自动续期、Key轮换和客户端协议需实测。旧版本API models/keyFile配置不兼容，新旧版本不能混合承接生产入口；隔离验证及批准切流前维持阻断，不能为了兼容开放Key-only旁路。管理UI桥接仍未完成，客户端vkey签发应走现有受控管理流程，不开放全部管理路由。本次未实现新的自动发Key工作流或完整Codex/WS支持。

Graph addPassword只返回一次秘密。如果Graph成功、Vault失败，普通初始化仍停止；使用下述独立恢复动作审核并处理孤立凭据，不盲目重试创建。Graph/Vault没有跨系统事务，也没有对任意外部管理员写入的原子锁保证；执行窗口要禁止其他写入者。所有初始化状态均保留待审查，而不是自动签发阶段验收。

### Stage7精确客户端授权

在runtime workflow选择entra-access，先plan后execute。它使用AZURE_ENTRA_ACCESS_CLIENT_ID，对应配置entra.accessPrincipalId；与bootstrapPrincipalId、数据库迁移身份均不同。此身份需要管理员事先批准Graph Application.Read.All、AppRoleAssignment.ReadWrite.All、DelegatedPermissionGrant.ReadWrite.All和User.Read.All，并具备目标RG写入回执权限。上述Graph应用权限影响租户范围，不能通过Azure RG RBAC把它们限制到两个应用；本项目通过独立身份、受保护workflow及精确资源/负载门禁限制使用，不能宣称权限本身已资源级隔离。流程不会自行授予这些权限。

- principalType省略时按User处理；管理员只能是User，API可为User或ServicePrincipal。Group暂不支持，也不自动扩展组成员。服务主体oid必须是本租户对应已批准apiClientIds的企业应用Object ID。
- API User获得本方案API资源的Llm.User角色，并为每个获准客户端创建consentType=Principal、principalId=该用户、scope=llm.invoke的委托同意；绝不创建AllPrincipals。同意的clientId字段使用客户端service principal Object ID，不是Application Client ID。
- 可为API User设置clientIds子集（必须属于proxy.apiClientIds）；省略时表示批准该用户使用全局允许列表中的所有客户端，计划会逐项展示。生成的代理策略也执行同一子集限制。
- API ServicePrincipal只获得Llm.Invoke，不获得委托同意或管理角色；admin User只获得其声明的proxy_admin、proxy_admin_viewer或audit_reader角色。
- 流程读取实际目录对象及启用状态，要求当前应用信任配置匹配。对本方案API/admin资源发现不在目标清单中的授权、重复记录、全租户同意或额外scope时停止，不自动合并、覆盖或删除；撤销/降权是下一项独立工作，不能靠删配置假定目录授权已经撤销。

批准后只补缺失记录，再读Graph确认；目录复制延迟或中途失败时没有成功回执，重新plan检查实际状态后继续，不反复POST同一授权。支持限定同一Graph集合的分页，拒绝跨主机nextLink。成功输出entraAccess，directoryVerified=true、loginVerified=false；Stage7生成清单必须具备同SHA/配置/应用的授权回执，真实登录验收仍单独完成。

### 管理员OIDC轮换与孤立凭据恢复

两个动作均选择Stage7，先plan再execute，使用AZURE_ENTRA_CLIENT_ID而非授权管理身份：

1. **admin-credentials-rotate**：要求当前OIDC及会话秘密已初始化，允许OIDC临近到期或已经过期，但不复活被禁用/撤销的当前凭据。创建新Graph密码和新Vault版本，保留旧Graph密码及所有Vault版本，不修改会话密钥。写入并校验成功后刷新admin-credentials回执；随后必须重新plan/execute Stage7 application，让固定版本CSI及Pod实际使用新值。仅更新Vault不代表轮换完成。
2. **admin-credentials-recover**：列出本流程固定标记的Graph凭据及admin Vault全部历史版本的keyId引用。仅在计划批准后移除完全没有任何Vault版本引用的孤立keyId；不碰当前、历史已引用或非本流程凭据。删除前再次检查，出现新引用就停止。若首次创建在写Vault前失败、尚无任何Secret，也可用该动作清理已确认的孤立项，再执行普通admin-credentials初始化。

轮换写Vault失败会留下可检查的孤立凭据，不能重复执行轮换来绕过；先恢复再重新规划。若新值已写入但保存回执失败，运行普通admin-credentials重建回执，避免再次轮换。回收结果也只代表目录操作已确认，不证明任何用户登录成功。

跨Graph/Vault不存在原子事务，外部写入方仍必须冻结；扫描历史Vault版本不代表能检测管理员在外部系统保存的凭据副本，删除批准人须确认此边界。旧版本的最终退役、会话密钥轮换及权限自动降级尚未实现；当前最多保留5个本流程Graph凭据，达到上限停止新轮换，要求单独审查退役而非自动清空。

**验证边界**：本地固定LiteLLM+PostgreSQL/TLS实测用户/Key API、精确路由白名单及用户Key不能创建其他Key；Graph/Vault/CSI/OIDC服务使用mock和静态契约验证，未访问客户租户。上游key_type的llm_api/management/read_only会覆盖路由集合，因此本项目使用default加显式allowed_routes，不能改成预设类型假定权限相同。Stage7未启用完整L3交付，要求审计的binding在L3未就绪时仍拒绝请求；Stage8审计发布不等于完整Stage8验收，全公司Coding切流继续阻断。

### Stage8可选增强L3交付核心进展

本节及其自动发布、恢复说明仅适用于经批准的增强L3方案。第一阶段原生Spend Logs不走这条正文采集链路；其发布模式、查询与阶段门禁待实现，见文首状态表。代码保留L3模块是为了可选能力和已有数据恢复，不表示基础版客户必须购买/部署它们。

已新增显式`persist-before-forward`模式：受信请求先保存，SSE完整事件组在Blob确认后转发，JSON在最终审计提交后转发；分片序号/哈希链、结束标记和索引分别保存。写入确认丢失不会按旧内存状态伪造结束标记。捕获完整、模型终态和客户端接收是不同事实，客户端接收始终标为未确认。

恢复核心支持有界扫描、审批计划哈希、缺失索引修复、已持久化终态重建以及没有结束标记时的部分恢复。留存删除已覆盖分片并保留保全语义，失败后可以续跑。本地验证包括真实HTTP/SSE、故障注入及子进程SIGKILL后的磁盘恢复；未进行Azure故障实验。

**审计专用自动清单和恢复workflow已接入，完整Stage8验收仍未完成**：独立恢复UAMI和容器级RBAC、持久暂停检查点、恢复Job及单独批准的恢复服务动作已实现。现有API只写权限未扩大，不能用管理员审计身份代替恢复身份。超过15分钟的请求仍必须在已核验的暂停窗口内修复，不能仅凭年龄认定没有活动写入。新生成器保持API/admin的ServiceAccount身份与管理端CSI凭据，API已无内部Key的CSI挂载；审计SDK另行指定writer/reader Client ID，同一ServiceAccount的投射令牌分别匹配独立UAMI的联邦配置；它们并非合并成一个拥有所有权限的身份。尚未进行真实Azure/AKS验收，collector和治理审批发布仍待接入。

#### Stage8增强L3自动发布（非基础版步骤）

在现有application、proxy及audit基础设施参数之外，增加顶层决策配置：

```json
"auditRuntime": {
  "retentionDays": 7,
  "captureEnabled": true,
  "retentionEnabled": false,
  "deliveryPolicyAccepted": true
}
```

deliveryPolicyAccepted表示客户批准当前有界JSON/SSE、存储前置延迟/成本和内容处理政策，不是代替实测的验收结果。captureEnabled=true还要求至少一个启用API绑定有auditTeamId；采集仍按绑定选择。captureEnabled=false只关闭采集服务，不会使原本要求审计的绑定绕过门禁。retentionEnabled=true将启动真实删除作业，须单独评估保全和删除政策；默认示例为暂停。

先通过部署workflow完成audit-foundation和audit，四个PrincipalId使用auto且必须不同；再使用runtime的Stage8/application执行plan、按approved_run_id批准、execute。无需填写整份YAML，也不手抄自动生成的存储URL、Client ID或计划哈希。

生成器读取并核验实际AKS、四个UAMI及精确联邦、已部署的存储角色参数、私网Blob连接/子网和版本/软删除策略，复用Stage5/7回执及签名镜像门禁。自动生成两端审计ConfigMap、原文查看挂载、专用留存ServiceAccount/CronJob与网络策略，后端及CSI内容保持原契约。采集开关/配置变化通过内容哈希引用触发API滚动更新。生成和rollout成功仅表示发布完成，不能代替Blob访问或审计端到端验证。

首次发布只初始化空的approvals/holds，默认没有原文查看批准；已存在的audit-approvals仅校验并保留，不纳入应用apply覆盖。治理改用第10节的专用双审批workflow。telemetry仅在配置observability后开启，未配置时拒绝关闭已有遥测；已有guardrail仍不允许被静默关闭。

#### 审计恢复维护窗口

仅对已启用增强L3的Blob日志生效，不用于原生Spend Logs或PostgreSQL备份恢复；基础版不能为清理PG日志而执行这些动作。

恢复会暂停整个API代理，不是在线无中断操作。客户批准停机/排空窗口后，使用`Customer private runtime operations`，stage选择8。初始前提是已部署并验证的durable Stage8工作负载；默认关闭L3的模板或仅Stage7环境不能直接运行恢复。

1. 在audit参数中增加`recoveryPrincipalId: "auto"`，通过现有部署workflow重新plan/deploy `audit-foundation`，再plan/deploy `audit`。工具从实际输出解析第四个身份，不需要客户复制自动生成的Client ID。旧配置不含此字段时不会授予恢复权限。
2. `audit-pause / plan`审查原副本数和留存状态，再运行`audit-pause / execute`，`approved_run_id`填写该成功计划的GitHub运行编号，确认environment。无需填写`approved_plan_sha256`。执行先保存集群ConfigMap检查点，再暂停CronJob、把API副本置零，等待现有写入Pod和留存Job结束；失败保留检查点，可重新plan后继续暂停或单独批准恢复服务。
3. `audit-recover / plan`在暂停窗口中启动专用只读扫描Job，每批最多25条pending记录，同时写访问轨迹；因此plan并非完全无副作用，但不修复审计正文/索引。审查报告后运行`audit-recover / execute`，通过`approved_run_id`选择同动作、同代码修订的成功计划。工具自动下载并核对计划artifact，绑定镜像、存储、检查点和日志哈希，不要求客户编写脚本或计算哈希。
4. 下一批`audit-recover / plan`可用`audit_continue_run_id`选择上一批成功计划，自动取下一页游标；没有下一页则明确拒绝。失败批次先重新扫描当前页，不应跳到下一页。执行成功仍保持API和留存暂停，不自动恢复流量。
5. 最后运行`audit-resume / plan`，审查原状态，再以该运行编号执行`audit-resume / execute`。存在活动恢复Job或未结束恢复Pod时拒绝恢复。工具恢复原副本数和CronJob状态、验证API rollout成功后才删除检查点；恢复过程中失败则保留检查点供重新规划续跑。

审批artifact必须来自同私有仓库、当前受保护分支和相同Git SHA的成功手动workflow运行，且操作、阶段、环境匹配；过期、执行记录冒充计划或哈希不符均拒绝。artifact默认7天，代码更新后需重新plan。失败时可保留元数据供诊断，但失败运行不能批准execute。普通application发布在维护检查点存在时被阻断。

**权限与运行约束**：恢复UAMI仅对content/index读写、pending只读、access只写，无Blob删除、Key Vault或后台管理权限；其联邦仅信任`litellm/l3-recovery`。恢复Pod仅有指定检查点/Deployment/CronJob的get及namespace内Pod/Job的list，不得写Kubernetes对象。runtime身份负责固定Job/ConfigMap/ServiceAccount/NetworkPolicy创建、指定Role/RoleBinding安装以及暂停/恢复工作负载，不授予它Blob正文读取权。安装RoleBinding可能需要预先批准的Kubernetes bind权限，不应直接给cluster-admin。GitHub token增加actions:read，只读下载本仓库计划artifact。

Job使用批准的ACR摘要镜像并核验签名，不挂载后台密钥。网络限制DNS、私网Blob子网、AKS私有API地址和HTTPS出站；公网443仍依赖既有防火墙FQDN规则限制到Entra等必要服务，Kubernetes网络策略本身不是FQDN白名单。真实CNI对API服务DNAT的策略行为必须验收。此版拒绝API HPA和额外写入控制器；外部GitOps、管理员手工scale、其他持有Blob写权限的程序必须在批准窗口停止，检查点不是分布式锁。恢复Job逐条修复前重新核验暂停状态，但不能阻止拥有更高权限的外部并发修改。

Job失败没有自动重试，最长30分钟；runner中断后由Job截止时间控制，暂停检查点保留，确认无活动Job/Pod后才能恢复服务。成功Job保留元数据一天后自动清理，成功扫描的输入ConfigMap清理；失败作业/输入对象可能保留，需要后续受控清理，不包含正文或凭据。原文保全/双审批规则未改变，恢复不会签发Stage8验收成功。

静态示例模板未默认启用新模式；只有显式auditRuntime决策的生成清单启用相应服务。协议范围仍是严格的未压缩JSON/SSE，默认2MiB/1024分片/16并发；跨SSE事件拆分凭据的脱敏、可信模型侧回调、私网身份、成本/延迟与损失预算均需完成验证和客户批准。详见[代理审计核心边界](../auth-proxy/README_ZH.md#持久化前置交付与恢复核心)。不以此批本地测试宣称Stage8验收完成。

### Stage5恢复边界

restore-target 从成功的 Stage5平台部署获取新服务器名称，读取该服务器实际 FQDN；不会使用人工自由填写的旧数据库地址。下载并校验指定备份，用 runtime 身份获取 Entra 数据库 Token，SSL verify-full，检查目标没有用户表后才 pg_restore；不会执行 DROP/--clean。若失败产生部分表，停止并由 DBA 在新隔离库评估，不能自动删除重试。此动作不执行 LiteLLM应用的版本迁移，不证明现有 Prisma 能持续更新 Entra token。

## 4. 哪些 ID 人工提供，哪些自动输出

| 项目 | 来源/类型 | 后续用途 |
| --- | --- | --- |
| Azure tenant/subscription | 客户租户/订阅 | 全部作用域 |
| deploy/runtime Client ID | 平台管理员创建的 OIDC 应用或UAMI | workflow登录；不是用户 Object ID |
| runtime Principal/Object ID | 企业应用服务主体或UAMI principalId | backupAutomationPrincipalId / RBAC / PG角色映射 |
| 运维审批 Object ID | 客户租户 Entra 用户 | governance、验收审批，可单人；不能冒充运行服务主体 |
| backupOwnerPrincipalId | 客户租户用户 Object ID | 人工备份保管人，模板当前类型为User |
| PG管理员 Object ID/UPN/类型 | 用户或组，明确 User/Group | 建立数据库管理与应用角色，不让应用用个人DBA |
| Azure OpenAI账号 Resource ID | 每个客户现有账号的ARM ID | PE及模型调用RBAC，非deployment名称 |
| 新AKS节点RG | 自动读取实际AKS nodeResourceGroup | origin支持auto；自定义短名放stage4Aks.nodeResourceGroupName，创建后不可更改 |
| LB/frontend | private-ingress创建并验证，或客户自管入口提供 | 托管路径从Stage4回执读取API前端后核对真实LB；自管路径仍需唯一匹配和API-only确认 |
| PLS Resource ID | origin成功部署输出 | edge.privateOrigin.privateLinkServiceId设auto可自动读取 |
| 审计Vault/Key | audit配置中预定名称，由audit-foundation创建 | purge保护、HSM RSA key、PE及受信Azure Storage访问；重用企业CMK时跳过foundation并独立审查 |
| 审计writer/reader/retention/recovery | audit-foundation自动创建四个不同UAMI | 对应PrincipalId设auto；recovery权限需显式配置后部署audit；不能合并身份或用同一用户替代 |
| Entra API/admin应用、角色与策略 | 租户管理员批准创建 | 客户认证策略，不自动赋予全租户权限 |
| TLS证书、业务域名/DNS | 客户DNS/PKI负责人 | 私网admin与API入口；不是AKS dnsPrefix |

审计Vault默认deny、启用可信Azure服务例外供Storage CMK，启用purge protection；该例外不是任意公网客户端放行。需网络合规Owner批准和实际负向验证。该Vault不同于应用凭据Vault。不要因只有一位运维就合并应用身份或审计职责权限。

## 5. 验收不再手拼Secret

1. `Customer stage acceptance` 选择本阶段 `draft`，下载 pending报告；阶段0backup-restore还会附带机器观察结果。
2. 在客户受控环境补完真实测试，填写每个check的status/evidence、实际observedAt，保存最终JSON报告到私有文档服务。不在evidence中放密码、token、Prompt正文或备份内容。
3. 把最终JSON、受控URL、批准人数组分别放入 `MIGRATION_REPORT_JSON`、`MIGRATION_REPORT_URL`、`MIGRATION_APPROVERS_JSON`，运行 `record`。
4. 成功record运行上传acceptance-record artifact；preflight、runtime和deploy自动读取同仓库/分支/修订的最新记录，校验环境、配置和有效期。无需更新MIGRATION_EVIDENCE_JSON或申请PAT。重录阶段只保留前置阶段并使其后证据失效。当前报告和批准人提交接口仍是过渡方案，完整技术探针/自动报告尚未完成。

新记录使用 `binding=stage-config`：只绑定当阶段及之前的配置，补后续PLS/身份输出不会使阶段0全部失效；修改已验收范围、governance或代码仍要重新审核。stage3排除stage4模型连接与stage5数据设置。报告的 `reportSha256` 采用与脚本一致的规范JSON序列化SHA256，不是带缩进文件字节的哈希；手工报告账本旧格式仍支持原校验。所有记录仍限最近7天，禁止仅更新时间或哈希假装验收。

重新record阶段N会保留0到N-1并替换本阶段记录，同时移除所有后续记录，要求后续阶段重新审核；例如旧阶段0过期时，先复核并record阶段0，再逐阶段重新确认。不要把过期记录当作可自动延长的凭据。

单人模式Stage9使用 `single_operator_release`，默认双人模式使用 `dual_owner_release`。报告工具仅验证结构、范围和声明，无法替代真实测试或客户审批。

## 6. 私有审查、失败与回退

部署及运行workflows要求私有仓库；7天artifact可能包含资源ID、IP、IAM配置及脱敏测试结果，应限制仓库读取权限。基础设施artifact包含完整已准入范围的What-if变更和显式模板输出，所有这些模板禁止运行时秘密输出。绝不上传database.dump、kubeconfig、原始stderr或参数文件。应用发布artifact来自已经禁止Secret/常见凭据字面量的客户清单；仍需客户审查ConfigMap不含隐蔽秘密，这不是DLP保证。

Azure失败只打印有限错误码；原始诊断留运行器作业目录并在结束清理，必要时在同权限私有终端复现，不开shell trace。部署会重新比较计划，不通过随意接受新哈希掩盖漂移。新旧环境数据、身份、网络仍需明确隔离；Delete/Unsupported不能用全局忽略开关越过。

运行应用发布使用显式目标AKS临时kubeconfig、server-side dry-run和无force-conflicts的apply；不prune、不删除PVC、不部署任意集群RBAC。阶段6~8提供的是客户成品清单发布机制，当前示例中的身份/CSI/数据库令牌/协议/L3接收器仍需完成接线，缺少内容会失败，不会自动将模板占位符替换成猜测值。

Stage9 `release=true` 只控制Front Door流量与WAF，不自动改任意DNS，也不证明“canary”已限制真实客户端；发布报告必须证明确有限定访问。当前源码的非POST规则及协议边界不满足所有Coding客户端，先补齐并回归再切流。回退依客户已审查镜像/数据策略执行，不自动回滚数据库或删除新环境。旧入口在回退窗口结束前保留。

## 7. 本轮交付与仍阻塞项

已交付部署/运行/证据自动化入口不等于完整生产方案验收。private-ingress已实现双平面私有TLS网关及Stage9输出传递，database-roles实现Entra角色映射与分权授权；schema-migrate和专用Azure应用入口已通过本地真实PG/TLS迁移、启动和换池测试。仍需完成PKI/DNS生命周期、runner/入口实际连通、API/admin应用和Vault接线、真实Azure身份/续期与客户旧版本升级验收、原生Spend Logs发布/查询/清理/故障和阶段证据适配，以及Codex协议/对象授权；仅选择增强方案才要求可靠L3专项交付。上游原镜像仍是RDS IAM路径，只有经过测试的派生入口安装了本项目Azure适配。未在客户Azure环境部署或执行客户数据库备份。

### 后续实现顺序与完成标准

1. 平台启动闭环：网络/私网runner连通、私有ACR镜像晋级、受支持的API/admin入口和TLS。消除“Stage3验收依赖Stage4私网才能导入目标ACR”的循环；源镜像供应链验证与目标ACR晋级分开。
2. 数据和身份闭环：按模式执行恢复或初始化，自动建立应用数据库角色，验证schema迁移、密钥持久保存和令牌过期后的重新连接。不得悄悄退回长期数据库密码来回避Entra集成。
3. 应用发布闭环：从受控客户配置、已批准镜像及真实部署输出生成客户清单，自动部署CSI、分离身份和代理。正常流程不再要求`MIGRATION_MANIFEST_YAML`；未实现的接线不能用占位符替代。
4. 运行验证闭环：实现并通过协议/对象权限、可靠审计、故障/恢复、租户负向测试；workflow采集真实结果，任何失败或未实现检查都阻断验收。客户负责批准业务范围和变更，不负责写测试脚本。
5. 发布闭环：针对明确支持的DNS/PKI提供方实现授权后的自动操作，限定试点、验证来源限制、保存证据并自动传递阶段状态；高风险发布/回退保留确认。未支持的外部提供方明确拒绝，不能返回成功。

最终必须分别证明：空白客户环境可按workflow首次部署并验证；既有客户环境可在保留旧数据和密钥的前提下逐阶段增强、切流并回退。只有模板编译、mock测试或workflow按钮覆盖Stage0至9，不构成这两项端到端验收。

## 8. 完整改造计划（2026-09-09）

2026-09-10范围修订：基础版改为原生Spend Logs，A08以原生配置/查询/留存/容量/故障与模式化验收为先；原自建L3可靠交付和治理工作仅适用于增强分支。其既有代码与测试保留，不冒充原生模式已实现。A09必须解除基础监控与L3采集的必然依赖，不能为通过旧门禁强制客户部署独立正文存储。

本节是后续逐批执行的总计划，编号A01-A12是开发批次，不改变客户Stage0-9编号。代码状态、离线测试状态、云端验收状态分开记录。当前所有批次均未获得客户Azure环境验收；本地测试数量不是生产完成度。数据库适配、Stage6/7自动发布及Stage8审计专用生成已实现，接下来补collector/治理/自动验收证据，并解决A02/A03的启动与发布依赖。不要求客户自己补工程实现；明确的基本完成条件见第9节。

### 批次、依赖和验收

| 批次/客户阶段 | 当前状态 | 必须交付的内容 | 通过标准/依赖 |
| --- | --- | --- | --- |
| A01 统一输入与双路径（0-2） | 模式、审批和部分输出读取已实现 | 配置schema/版本升级；迁移和greenfield输入矩阵；生成可用默认名称；初始OIDC/最小RBAC安装流程；单人风险接受；失败信息不泄露秘密 | 两套合成配置通过；无旧环境的新客户不被索要旧ID；权限不足在写入前明确拒绝；客户只做身份授权和业务决策 |
| A02 私网启动与供应链（0、3、4） | RG/VNet/AKS/ACR模板已实现，启动依赖未闭环 | runner创建/注册/销毁流程；受控出站、DNS/路由/对等连接；所需CLI固定版本；基础镜像/派生镜像构建、SBOM/扫描/签名和私网晋级；源镜像验证与目标ACR晋级分阶段 | 不依赖尚未创建的runner/VNet执行其自身初始化；Stage3验收不循环依赖Stage4；无公网ACR补洞；不可信PR无法运行生产runner；依赖A01 |
| A03 私有入口和证书（4、9） | 文件路由双入口及本地TLS验证已实现 | 真实AKS/LB/PLS连通测试；锁定镜像更新；API/admin来源隔离；支持的DNS/PKI签发与续期接口；证书版本/指纹回执；旧证书受控清理 | 长流/WS通过入口但不得绕过后端授权；错误SNI/Host、公开admin、源站旁路均拒绝；证书轮换不中断在途请求；依赖A02 |
| A04 数据库初始化、迁移和续期（5） | schema workflow及专用应用入口已实现；新库/真实换池离线通过，Azure及旧版本升级待验收 | Azure Workload Identity凭据提供器；保留TLS参数的Prisma重建；独立schema迁移任务；新建初始化与旧库升级分支；迁移锁/结果回执；应用启动不执行DDL | 固定镜像实测Token换新、过期、断网、连接池并发/事务；应用仅DML；1.95到目标版本迁移、密文和关键数据校验；失败不连接/清理旧库；依赖A02和角色初始化 |
| A05 应用身份与秘密（5、7、8） | 凭据/版本CSI及OIDC轮换/孤立恢复已接通；Graph/Vault云端验收、历史退役、会话轮换及collector待完成 | API/admin/backend/collector/审计各自身份与最小RBAC；联邦和CSI；新建随机Master/Salt及BFF会话密钥；迁移导入已有密钥且重跑不轮换；角色客户端初始化 | 不要求客户复制自动生成ID；无Secret进入清单/artifact；CSI挂载及应用实际消费轮换通过；跨平面读取和角色越权拒绝；依赖A02/A04 |
| A06 应用与Redis自动发布（6、7） | Stage6后台、Stage7双代理及Stage8审计专用自动生成已接入；collector及Redis/CSI云端验收未完成 | 从配置/真实输出渲染成品清单；模型deployment映射；Entra Redis续期、配额/路由/亲和；资源/PDB/HPA/网络策略；镜像签名准入；rollout就绪验证 | 常规流程不需要MIGRATION_MANIFEST_YAML；新建/迁移均能启动；Redis令牌过期和重连、双副本共享状态通过；未初始化schema/密钥不启动；依赖A03-A05 |
| A07 Entra、协议与对象授权（7） | 应用注册、精确角色/单用户同意、主体类型校验已接通；真实登录、授权撤销及完整Coding协议仍未完成 | 应用注册/应用角色/同意配置流程；API/管理员会话、CSRF/撤销；明确Chat/Responses/SSE/WS/Coding/工具/文件范围；租户、主体、对象与会话归属；边缘方法/升级规则 | 真实首发客户端矩阵通过；引用/加密上下文/文件/WS不得跨租户；取消/断流/重试不串会话；不支持的能力明确拒绝而非匿名放行；依赖A06 |
| A08 原生留痕与可选增强审计（8） | 原生开关已确认，但基础版生成/查询/门禁待适配；增强L3已有独立组件及局部验证 | 基础版配置优先级与采集范围、受控查询、身份/Key归因、清理/容量/备份、失败监控及模式化证据；增强分支另做可信回调/分片恢复/脱敏/审批保全 | 基础版实测JSON/SSE/异常/长上下文的实际落库与缺口、越权查询拒绝、no-log不可绕过、清理及恢复后留存；不承诺零丢失，不能拿L3回调测试代替；依赖A05-A07 |
| A09 观测与自动验收（各阶段，8汇总） | 部分诊断/监控和报告工具已实现 | DCR/DCRA/collector权限、指标/Trace关联、告警实际投递；机器可执行阶段探针；绑定SHA/配置/输出的报告；受限证据存储与自动加载；只保留必要人工批准 | 客户不手写技术报告、哈希或反复替换账本Secret；部署成功和测试成功分开；失败/未实现检查阻断；过期/重放/环境混淆拒绝；从A01起逐阶段接入，不等全部开发结束 |
| A10 试点、首次发布与迁移切流（9） | 禁用边缘和受控启用入口已实现 | PLS批准与TLS/FDID/来源校验；明确支持DNS提供方的自动更新；真实试点客户端限制；最终增量同步/冻结窗口；SLO门禁和发布回执 | 新建首次发布与迁移切流分别验收；canary确实限制访问范围；admin永不公开；旧入口/密钥保留；成本/发布获批后执行；依赖A03、A07-A09 |
| A11 回退、灾备和日常维护（上线前演练，上线后持续） | 局部备份/恢复测试已实现 | 镜像/配置/流量受控回退；数据库版本兼容与切流后写入处置；PITR/备份恢复；密钥/证书轮换；容量/成本告警；漏洞更新、账本归档和独立退役 | RTO/RPO达到批准目标；不能把DNS改回等同于数据回退；失败不自动DROP或重建PVC；需要人工决策时workflow明确停住；依赖A04-A10，演练在生产放行前完成 |
| A12 双路径客户交付验收（0-9全链路） | 未完成 | 一套空白租户/订阅场景、一套旧网关迁移场景；最小授权安装；可重复运行/中断恢复/失败指引；仅ID、域名和决策输入的操作手册 | 在获授权隔离Azure环境从头跑通两条路径，完成真实Entra/客户端/负载/审计/恢复验收；所有适用门禁通过后才宣布可交付；依赖A01-A11 |

### 每批的实施步骤

本次A04增量：[Azure凭据适配](../LiteLLM/runtime/azure_postgresql.py)已安装到专用应用入口，覆盖启动URL和运行期刷新。除wrapper契约测试外，新增隔离PostgreSQL/TLS端到端测试，验证151个迁移、八个视图、datamodel一致性、无DDL ASGI启动、真实查询引擎换池和旧事务排空；失败/取消契约仍有独立测试。尚未获取真实Azure Token或完成AKS/CSI/旧版本恢复验收，不宣称生产可用；A05/A06需将身份、密钥、镜像及生成清单接线后再跑客户环境门禁。

1. 先固定输入、资源作用域、身份权限、失败/重试和客户决策边界。未明确的DNS/PKI、协议范围、RTO/RPO、审计故障策略记录为待批准输入，不猜测客户政策。
2. 在拥有该行为的模块实现最小闭环，立即运行能推翻假设的定向测试；不先添加一个实际能力为空的workflow按钮。
3. 接入workflow的plan/批准/execute/verify和自动输出。普通验收探针只读；故障注入、恢复、切流等验证需要显式批准，但仍由workflow执行。
4. 增加幂等、中断恢复、权限不足、秘密脱敏、版本漂移和失败不签发验收的测试，再补离线容器契约与CI。
5. 文档记录变量来源、人工授权点、自动生成输出及准确状态；进入获授权隔离Azure环境实测，费用/数据/生产变更分别批准。只有代码和云端门禁均通过才关闭生产阻塞项。

### 语言与工具决策

- workflow负责触发、权限、审批和作业依赖；Bicep负责Azure资源；Kubernetes声明式清单负责应用部署。不要把所有内容重写成某一种脚本语言。
- 本项目继续用Python实现配置、计划、Azure/Kubernetes编排与验证：现有测试/解析工具和LiteLLM集成均基于Python，维护成本低；耗时主要在ARM、镜像和数据库，不是解释器计算。
- Bash只用于少量命令连接和工具检查；Windows操作指引用PowerShell。不要把业务校验堆进长shell脚本。
- Go通常用于可分发的单二进制CLI、Kubernetes controller/operator或长期运行的高并发服务，不是默认的workflow脚本替代品。若以后确需这些能力，再按模块评估，不为语言统一重写现有已验证自动化。
- GitHub Actions常见组合包括YAML、shell、Python，以及JavaScript/TypeScript Action；Go常作为被调用CLI。没有一个适用于所有团队的“主流唯一语言”，此处不作未经统计的市场占比判断。
- 固定runner/镜像和Python依赖版本、收集脱敏失败诊断、提升测试与重跑可靠性，比换成Go更能改善当前交付质量。原有Node认证代理继续沿用，除非实际性能/可维护性证据要求改变。

## 9. 基本完成的交付条件

“基本完成”不是继续无限增加功能，也不是所有入口都有workflow按钮。它指在明确且经客户批准的首发范围内，下表八项都有可重复执行的workflow和真实隔离Azure环境证据。不得通过临时缩减已要求的客户端、降低审计政策或关闭失败检查来满足定义。

| 必须收尾的内容 | 完成判断 | 当前剩余工作 |
| --- | --- | --- |
| 首次安装与私网启动 | 空白环境可以从批准身份开始运行，不需要已存在的目标runner/VNet/ACR来创建它们自身 | 最小OIDC/RBAC安装、runner生命周期和Stage3/4镜像晋级依赖闭环 |
| 成品清单与安全接线 | 客户只填ID、域名和决策；无手写YAML/SQL/脚本/自动生成ID搬运 | collector/观测资源及完整Stage8接线；镜像输出自动传递；真实WI/CSI/Redis/数据库验证 |
| 身份与客户端范围 | 所有首发客户端及所需协议可用，跨租户/主体/对象访问被拒绝 | 真实Entra登录、撤销/降级、会话/旧凭据生命周期；按批准矩阵补Coding/WS/对象权限 |
| 审计与治理 | 基础版原生正文、受控查询、清理/容量/备份和故障策略有workflow及证据；增强L3另按批准要求验收 | 原生模式生成与阶段门禁、禁止双写/客户端绕过、字段/权限实测、PG告警/留存及恢复验证；增强分支才要求Blob可靠交付/双审批/保全 |
| 自动检查与证据 | 每阶段自动执行技术检查，真实失败阻断，下一阶段自动读取受限证据 | collector/告警实际投递、阶段探针、证据存储/加载；统一计划批准，消除手工报告/哈希/账本Secret替换 |
| 首次发布与迁移切流 | 支持的DNS/PKI自动操作、限定试点、SLO门禁、审批切流可重复 | DNS/证书签发续期、PLS和来源验证、真实试点限制及迁移最终同步窗口 |
| 回退与最低运维保障 | 上线前证明流量/镜像/配置回退、数据恢复及必要轮换满足RTO/RPO | 切流后写入处置、灾备演练、容量/成本告警、失败作业与旧凭据受控清理 |
| 双路径端到端验收 | 一套从零部署和一套旧环境迁移均由workflow跑通，含中断重跑/权限不足/回退 | 在明确授权与费用范围的隔离Azure环境执行，保留旧数据/PVC/Master/Salt并核对证据 |

客户仍负责租户授权、域名归属、协议/审计政策、费用和高风险变更批准；单人运维模式不要求虚构第二位运维人员。基础版正文读取须单独授权，不能冒充已具备逐次双审批；增强L3沿用其独立审批规则，技术实施和验证由workflow完成。

只有以上适用条件完成后，才可称“基本交付完成”。更多DNS提供方、超出首发矩阵的协议、可视化操作台和更高级容量优化可作为后续增强；核心安全边界、必要数据恢复与证据自动化不能后移。审计工作的当前优先级是原生模式/受控查询/留存监控与模式化验收，collector和L3治理按需；私网启动、首发协议、生命周期、发布回退和两条真实环境验收仍需完成。

## 10. 本轮代码Plan与实际状态

本节保留前批代码进展。2026-09-10已选择原生Spend Logs基础版，下列自建L3治理/可靠性是可选增强进展，不再作为所有客户的必做项；原生模式尚待接线，不能把“未选增强项”与“未完成基础版”混为一谈。

本节更新前文台账中collector、治理发布和账本传递的状态；八项并未全部完成。确认的首发范围：一次管理员Azure登录后由安装器配置后续环境；域名可注册在阿里云、DNS采用Azure DNS；API公共CA证书、管理入口企业Key Vault证书；支持普通模型API与Codex客户端，不在网关内开发Agent规划或MCP工具执行。

| 顺序 | 代码任务 | 当前状态 |
| --- | --- | --- |
| 1 | 首次身份安装、短生命周期私网runner及Stage3/4启动依赖 | 初始身份安装器、私网VM模板和JIT生命周期核心已实现；runner实际API适配/调度、工具链镜像生成、精细授权和启动依赖仍未完成 |
| 2 | collector/私网观测基础设施与应用清单 | 已接入；固定二进制配置及合成数据流验证；实际Azure接收/告警另待验收 |
| 3 | 身份生命周期与完整Codex安全协议 | 主体撤销、会话密钥轮换、过期非当前凭据退役已接入；响应归属封装核心有测试但未接路由，完整WS/Codex和权限降级仍缺实现 |
| 4 | 审计治理发布、可靠性及脱敏 | 双实际审批发布已接入；跨SSE事件凭据脱敏、可信模型回调等仍缺实现 |
| 5 | 通用计划批准与证据自动流转 | 运行编号批准、账本自动加载及入口隔离自动检查已接入；全部阶段探针/自动报告仍缺实现 |
| 6 | DNS/证书/切流与回退维护 | API CNAME发布/条件回退与API ACME续期已接入；不确定订单处置、受控重新发布周期、数据级回退/PITR完整编排仍缺实现 |
| 7 | 两条端到端场景的可执行自动验收 | 两种模式全部阶段的记录/打包/读取合同测试已实现；完整云端场景执行器仍缺实现，合成passed不是客户验收 |
| 8 | 离线门禁和使用说明 | 本轮已改模块均有定向回归；不能据此勾选前述缺失实现 |

上述未完成项是工程代码工作，未被取消或降低要求。不能以关闭WebSocket、透明放行对象引用、将检查写成pending或DNS改回，分别替代Codex兼容、对象授权、自动验收或数据恢复。

### 私网观测

collector是可选遥测，不采集正文。以下是现有实现路径，当前仍要求auditRuntime，基础版去耦合待改；不要为记录Prompt而开启collector或增强L3。

顶层observability.collectorImage使用目标ACR内的collector引用，必须保留固定版本0.148.0的摘要`sha256:8164eab2e6bca9c9b0837a8d2f118a6618489008a839db7f9d6510e66be3923c`。通过镜像晋级流程导入、扫描和批准后，部署Stage8/observability，再发布Stage8/application。AKS、Workspace、VNet和PE子网从platform派生，不搬运ConnectionString或托管身份ID。

新组件创建独立collector UAMI、仅Application Insights范围的Monitoring Metrics Publisher、关闭local auth及公网采集/查询的Application Insights、AMPLS和DNS/PE。collector双副本、只读根文件系统、有界内存队列。ConnectionString在禁用local auth后只是资源路由标识。AMPLS PrivateOnly及DNS会影响链接VNet的Monitor解析，部署前须确认目标VNet/Workspace的影响范围，不能直接套到企业共享网络。

固定collector通过真实容器配置和合成OTLP数据流测试。字段白名单之外的资源/span/scope属性、span名称、状态消息、trace state、事件、链接和schema URL会被清理。该版本事件/链接setter先清空目标再复制专用slice，因此使用同类型赋值；升级必须重跑数据流测试，单纯validate配置不足以证明脱敏。队列不是持久队列，Pod丢失可能丢遥测。实际Azure接收、私网DNS、告警规则与投递仍未验收。

### 治理发布

本节仅适用于增强L3的Blob原文审批/保全，不控制原生Spend Logs的读取权限或清理。基础版不默认要求此专用身份、双审批Environment或暂停/恢复作业。

顶层auditGovernance.reviewers配置至少两个不同GitHub登录名到Entra用户Object ID的映射。Customer audit governance依次经过`<environment>-audit-approval-1`和`<environment>-audit-approval-2`保护环境，代码再核对实际审批记录。无记录、同人审批两次、发起人或原文接收人自审批均拒绝；保护环境必须配置reviewer，套餐不支持时不能移除门禁。单人实施模式不放宽原文治理。

支持grant/revoke/hold/release-hold。客户提供ID、准确team、工单、理由和时间窗；grant自动生成批准ID，不填写approvedBy或哈希。查询窗口最多7天且不能面向未来，访问批准最多24小时，保全最多一年。变更前须audit-pause；确认无活动写入/留存/恢复Job后CAS更新，成功后另行audit-resume。计划一小时有效，等待审批过久需重跑。只修改治理元数据，不读取或上传原文；独立发布身份须预先获得限定的集群读取与audit-approvals更新权限，当前安装器尚未自动配置这些授权。

### 目录撤销

Stage7新增entra-revoke：先在配置中显式disabled主体，plan后按运行编号批准，使用原独立Entra access身份回收本项目资源应用上该主体的精确角色和Principal/llm.invoke同意。发现AllPrincipals或更广scope停止，不清理其他用户。允许所有API绑定均disabled用于紧急隔离，但仍需发布代理配置并验证传播；目录回收不撤销已签发Token。角色自动降级和即时会话全局吊销尚未完成。

admin-credentials-session-rotate生成新的32字节会话密钥Vault版本，保留旧值，不改变OIDC密码，刷新adminCredentials回执。随后重新发布application让admin滚动更新；新Pod不接受旧Cookie，但旧Pod停止前仍有过渡窗口，不能将Vault写入成功视为即时全局吊销。admin-credentials-retire-expired仅移除已过期至少一天且非当前的本项目OIDC密码，保留Vault历史版本和仍有效旧密码；逐次复读引用和到期状态，遇到漂移停止。有效旧凭据提前退役仍需额外使用证明，未自动实现。

### Azure DNS

顶层dns包含同订阅Azure DNS公共zoneResourceId和60到3600秒的ttl。域名注册商可仍为阿里云，但目标区域/子域须已委派到Azure DNS。zone必须包含`llm-api.<baseDomain>`，不支持apex CNAME，不公开admin记录。DNS区创建/委派尚未自动化；API证书签发入口如下，admin继续使用企业证书。

Stage9/runtime的dns-publish/dns-rollback使用plan和approved_run_id。目标必须来自本项目Front Door输出，实际API route已启用、默认域已关闭；写入前保存原CNAME/TTL/metadata，使用ETag/If-None-Match条件更新。回退恢复原记录，无原记录则条件删除；外部修改或范围变化拒绝覆盖，回执失败可重跑。检查点不抹除，已回退后再次发布的周期管理仍未实现。DNS回退不代表数据回退、缓存已过期或旧入口已经恢复。

技术参考：[固定collector Azure身份配置](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/v0.148.0/extension/azureauthextension/README.md)、[Azure Monitor exporter认证](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/v0.148.0/exporter/azuremonitorexporter/AUTHENTICATION.md)。

### 首次身份安装器基础

[安装器](../scripts/install_workflows.py)使用管理员已有的Azure CLI和GitHub CLI登录，不自动申请订阅Owner，也不创建密码型服务主体。管理员需有目标RG创建/身份/角色分配权限和私有GitHub仓库管理员权限。默认分支必须已保护。先执行`python -m scripts.install_workflows --config <受控配置路径> --repository <组织/私有仓库> --operation plan`，审查生成的安装计划；execute使用`--approved-plan`指定同一计划文件，工具自动核对哈希。

它为deploy/runtime/database/Entra bootstrap/Entra access/audit governance/certificate创建七个独立UAMI，信任严格绑定`repo:<仓库>:environment:<环境>`，基础角色仅在目标RG范围。Client ID和tenant/subscription自动写入GitHub Environment Variables，不输出或写入密码。已有保护环境仅核验分支策略，保留reviewers/wait规则，不通过PUT覆盖；现有信任不一致时拒绝扩张。

当前安装器不是完整零到一安装：订阅级bootstrap部署权限、Graph批准、PG管理员、Kubernetes细粒度角色、DNS/证书Vault角色、私网runner注册仍需后续安装自动化。摘要始终`readyForDeployment=false`并列出缺口。新建保护环境也仍需配置必要reviewers。这些缺口没有被视作“客户只需登录”。

私网runner新增[VM模板](../infra/private-runner/main.bicep)和[生命周期核心](../scripts/runner_lifecycle.py)：无公网IP/入站、无持久云身份、Trusted Launch和主机加密、准确gallery镜像版本。核心核对资源所有权/VM ID/私网接口，将GitHub单次JIT配置仅放入Managed Run Command protectedParameters，回执不含注册材料，忙碌或替换runner拒绝清理。它目前不是可直接触发的完整runner workflow：实际GitHub App/ARM适配、工具链镜像生成、资源调度和异常VM清理仍需实现，不要求客户手写这些部分。普通GITHUB_TOKEN不能创建JIT runner，需要独立GitHub App安装授权。

响应引用新增[jose封装核心](../auth-proxy/response-context.mjs)，绑定tenant/subject/model和有效期，支持跨副本解封、流式同ID稳定性以及缓存/嵌套上限；它没有接入代理运行配置或HTTP/WS路由，不能声称Codex兼容。固定LiteLLM源码中响应归属hook只对特定加密ID格式检查，普通ID会继续流转，因此不能直接开放对象引用并依赖后端。必须完成封装密钥生命周期、实际HTTP/WS转发、审计与Codex协议合同后才能启用。

### API证书签发与续期

顶层certificates配置zoneResourceId、termsAccepted=true、publicApiHostnameAccepted=true；后两个字段是客户对Let's Encrypt服务条款和API域名公开证书透明度记录的明确接受。privateIngress.api.tlsSecretId必须是版本不固定的批准Key Vault Secret地址，admin引用不得相同。签发目录固定为Let's Encrypt生产ACME v2，仅申请`llm-api.<baseDomain>`，不申请通配符或管理域名。未启用此配置不会联系CA。

Stage4/runtime选择certificate-renew，先plan后通过approved_run_id执行。有效期超过30天的现有证书保持不变；新签发/续期将账户密钥、待处理私钥与订单状态保存到同一个批准Vault的专用Secret。Key Vault角色必须包含这些固定名称；状态和私钥不进入artifact。依赖固定acme 5.8.0、josepy 2.2.0及dnspython 2.8.0，复用Azure CLI身份。

DNS-01只向准确挑战TXT添加自身值并以ETag条件写入，清理时只删自身值，保留其他TXT值、TTL和metadata。不使用覆盖整个TXT的第三方插件。订单状态先持久化再调用CA，已保存订单继续原订单；若创建订单请求结果不确定且没有拿到订单URI，明确停止而不是重复下单。该情况的自动对账/受控清理动作尚未实现，需要独立核查，不得手工清空状态后盲重试。

签发结果校验证书/私钥匹配、主机名和有效期，写入新的API Secret版本，保留旧版本。然后仍须运行private-ingress plan/execute，应用新证书并执行实际TLS信任/指纹/Host检查；certificate-renew本身不改变在用入口，ingressUpdated=false。重复运行、订单恢复和CSR已做本地合同测试，未联系真实CA、DNS或Vault。API自动续期完整的定时调度/失败通知、CA额度和旧证书退役仍待完善。

### 入口隔离检查

Customer gateway isolation checks不使用客户Token或真实模型输入。在私网runner上逐DNS地址验证TLS，要求API未认证请求和错误Host被拒绝、admin域名仅解析到私网且未认证访问被拒绝。报告不保留响应正文/Token，失败使workflow失败，但stageAccepted始终false：这只是明确命名的负向入口检查，不覆盖正常客户端登录、Codex、对象归属、L3完整性或数据恢复。

两种部署模式另有离线跨阶段合同测试，实际执行生成pending、拒绝未通过检查、记录合成结果、打包artifact和下一阶段读取。合成测试中的passed只用于测试fixture，绝不写入客户账本。全套真实场景执行器与各阶段技术检查的实现仍是未完成代码项。