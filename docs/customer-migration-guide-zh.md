# 客户既有LiteLLM网关安全增强迁移指南

> 适用：客户已有LiteLLM on AKS网关，计划建设隔离的新安全环境并逐步替换。
> 不适用：直接在旧生产AKS/数据库上执行整套模板，或把参考阶段记录当作客户验收报告。
> 当前交付：可运行的阶段指导、客户配置预检、只读IaC What-if、离线验证，以及人工受控执行的操作流程。不是已完成全部集成的一键生产升级器。

## 1. 迁移原则与入口

采用并行新建、隔离验证、批准客户端试点、最终切流。旧网关、数据库、密钥/Salt和VMSS业务身份在回退窗口结束前保留。不得让1.98新版本自动迁移旧生产数据库；新旧网关不能无计划地同时写一个数据库。

入口为GitHub Actions **Customer staged migration**，选择`environment`、`stage`、`mode`、`component`。每次只处理一个阶段；所有真实资源值来自客户自己的GitHub Environment，不从个人本地配置读取。

| mode | 行为 | 不会执行 |
| --- | --- | --- |
| `guide` | 输出阶段目标、退出证据名称和本文入口，无需Azure登录或客户配置 | 不证明前置条件已满足 |
| `preflight` | 校验客户配置、订阅/租户、旧新隔离和前序证据；选IaC组件时生成私有参数，阶段7至9选none时生成域名overlay | 不查询Azure真实状态，不补齐身份/Secret/镜像占位符 |
| `what-if` | 重新执行预检，用OIDC做选定组件的真实Azure What-if，仅输出变更数量 | 不执行部署、数据库迁移、Kubernetes apply、DNS/WAF切流或资源删除 |

原Azure IaC what-if入口已停用直接预览，只提示使用新入口，不能绕过阶段门禁。当前不提供通用自动apply：入口controller、PG认证、L3可靠交付等仍有[收尾阻断项](litellm-code-completion-backlog-2026-09-07.md)，自动执行会掩盖这些缺口。

## 2. 客户首次准备

1. 将仓库导入客户控制的私有仓库；保护主分支，要求PR审查及CI通过。不要将客户配置提交到这个公共项目。
2. 建立`dev`、`test`、`prod` GitHub Environments。生产Environment要求审批人、禁止自我审批及受限部署分支；配置维护者与验收审批者分离。GitHub审批功能受客户套餐影响，缺失时使用等效外部双人变更流程，不宣称单次workflow审批等于双人签字。
3. 为每个Environment配置独立Azure OIDC身份。Federated Credential issuer为`https://token.actions.githubusercontent.com`，audience为`api://AzureADTokenExchange`，subject精确到`repo:<customer-org>/<customer-repo>:environment:<environment>`。不要使用个人用户登录或client secret。
4. 客户预先创建新专用目标资源组和需要的Log Analytics workspace，确认预算/配额。此版本要求目标RG与旧RG不同，引用的workspace/VNet/私有DNS/CMK Vault应符合模块的同RG假设；跨RG共享资源需先扩展模块并测试，不通过修改旧资源规避限制。
5. 由网络Owner批准区域、VM SKU、Kubernetes版本、CIDR、路由及私网执行位置。示例CIDR、HA Disabled、Redis规格均不是客户默认架构决策。
6. 安装或批准Azure CLI/Bicep、kubectl、Python及Docker验证工具。部署到Private AKS和访问私有ACR/Blob需要客户受控私网终端或隔离self-hosted runner，不为GitHub-hosted runner开放生产公网。

### 2.1 Variables和Secrets

| GitHub Environment配置 | 类型 | 用途与限制 |
| --- | --- | --- |
| `AZURE_CLIENT_ID` | Variable | 本Environment的OIDC应用/托管身份client ID，不是用户object ID |
| `AZURE_TENANT_ID` | Variable | 必须与客户配置中的tenantId一致 |
| `AZURE_SUBSCRIPTION_ID` | Variable | 必须与客户配置中的subscriptionId一致 |
| `CUSTOMER_CONFIG_JSON` | Variable | 按[客户配置模板](../config/customer.example.json)填写完整JSON；只放非秘密元数据，不超过GitHub单变量限额 |
| `MIGRATION_EVIDENCE_JSON` | Secret | 按[证据模板](../config/migration-evidence.example.json)保存阶段验收元数据；阶段0可为空数组；不要放报告原文、下载Token或备份 |

配置中的`ownerEmail`用于资源标签、告警邮箱以及备份Owner描述，不用于推断RBAC。`backupOwnerPrincipalId`是客户明确批准的Entra用户object ID；当前备份模板的principalType为User，不能填workflow应用ID或用`az ad signed-in-user`推断。需组/服务主体时先修改并测试该角色边界。

`baseDomain`自动生成`llm-api.<客户域名>`和`llm-admin.<客户域名>`；只有API可进入Front Door，admin保持私网。`legacy`记录实际旧RG、AKS、namespace和PG PVC；`parameters`按组件填写客户资源值。可留待后续阶段填写其他组件，但使用当前组件前必须清除其占位符。Stage3/4不校验尚未启用的`stage5Data`，Stage3不校验尚未使用的模型连接。

原环境Bicep参数中的`OWNER_EMAIL`、`LOG_ANALYTICS_WORKSPACE_NAME`、`AZURE_LOCATION`只用于独立编译/直接参数文件使用；其示例回退是离线检查用途。客户迁移workflow使用`CUSTOMER_CONFIG_JSON`生成显式ARM参数，不使用这些回退，也不默默读取个人域名配置。

运行时秘密由客户Key Vault承载：已有Master Key、Salt、数据库连接信息、管理OIDC client secret、会话加密密钥、受限后端Key等通过批准的CSI/Workload Identity接入。不要把它们写入`CUSTOMER_CONFIG_JSON`、ARM非secure参数、workflow inputs或日志。目前workflow不初始化或搬运运行时秘密；若客户另建密钥初始化job，必须用独立环境Secret/Key Vault权限、私网runner和禁止输出的流程，不能把秘密当作资源标签。

### 2.2 Azure权限与日志

指导和离线预检job没有`id-token: write`。仅What-if job申请OIDC；Azure What-if仍要求目标资源的相应部署权限，不等于Azure Reader即可。由客户平台管理员按官方What-if权限要求授予目标范围的必要权限，涉及RBAC的模块单独审批；不要因为一个权限错误授予订阅Owner。workflow只调用what-if，不代表其Azure身份在RBAC上天然无法部署。

参数/原始What-if/诊断仅写运行器内忽略目录，权限0700/0600，不上传artifact，结束清理。公开日志只显示阶段、配置哈希和变更数量。客户需检查Azure login/action本身日志的组织保密要求；建议私有仓库。详细失败诊断可在私有受控终端按下述相同CLI复现，不启用shell trace或把文件上传公开工单。

## 3. 证据与批准机制

阶段N的`preflight`/`what-if`必须具备0至N-1每个阶段的通过记录。运行`guide`获得每阶段精确的`checks`名称。记录包含：阶段、environment、配置SHA-256、完整Git SHA、passed状态、全部checks、两名不同审批者object ID、UTC时间、私有HTTPS报告引用及报告SHA-256。

先运行阶段0预检获取配置哈希。所有记录绑定同一配置和代码版本，最近7天内才有效；代码、订阅、租户、区域、域名、资源或配置变化后，相关Owner必须重新评审并更新记录，不直接改哈希冒充验收。长期迁移期间需要重新确认早期备份/回退仍有效，不必未经判断重复操作生产。

**这是声明结构门禁，不是自动事实认证**：脚本不会读取/核实报告，不会证明审批者拥有真实权限，也不会写入“已部署”的状态。客户变更系统是证据来源，GitHub Secret发布权限是信任边界；不能由部署执行人自行填写虚假passed记录。配置/证据不是程序可执行代码，shell仅接收固定choices，JSON通过环境变量传入。

## 4. 分阶段操作

### 阶段0：盘点与可恢复备份

**客户准备**：旧网关Owner、数据库Owner、备份保管人、业务验收人和回退责任人；维护窗口；私有备份位置及访问路径。

**执行**：先`stage=0, mode=guide/preflight, component=none`。在旧环境只读盘点实际版本/digest、模型/路由、预算/Key、SSO、Secret/Salt路径、PVC、流量与网络；敏感导出不入Git。用客户批准的方式做PG逻辑备份并在独立数据库完整恢复，验证表/行数、密文可读性、关键API/WS/Codex基线及RTO。不要只以`pg_restore -l`成功证明恢复完成。

没有专用备份存储时，可选`component=backup, mode=what-if`预览新目标RG的私有存储和初始VNet。模板会创建并管理VNet，不用于导入已有共享VNet；阶段4增加子网后禁止重新套用该bootstrap模板。已有合规备份服务可复用，不强制再建一份。

**退出证据**：`inventory`、`backup_restore`、`key_salt_recovery`、`protocol_baseline`。保留旧环境，备份失败不进入后续阶段。详见[备份模板说明](../infra/backup-storage/README_ZH.md)。

### 阶段1：旧环境最小加固

**客户准备**：阶段0证据、部署快照、告警接收人、已工作的Container Insights及收件验证方式。

**执行**：`stage=1, component=monitoring, mode=what-if`只针对旧RG预览告警。由Owner在维护窗口逐项审核probes、资源限额、PG Recreate/PVC容量和永久Salt保护，每改一项验证健康与核心协议；不要把旧部署脚本当作幂等升级器直接重跑。当前告警KQL要求namespace=`litellm`、PVC=`pg-data`及现有命名规则，不匹配时先改查询并测试，预检会拒绝已知不匹配配置。

**退出证据**：`legacy_health`、`alerts_received`、`rollback_snapshot`。失败时用已审查的工作负载快照回退；不删除PVC、不旋转Salt、不移除业务UAMI。[监控说明](../infra/monitoring/README_ZH.md)中的历史成功不代表客户邮件已收到。

### 阶段2：冻结客户决策

**客户准备**：网络、安全、身份、数据库、业务、合规和成本Owner。

**执行**：`stage=2, component=none, mode=preflight`。批准目标区域/配额/CIDR与私网、API/admin域名、Entra App Role/CA/MFA责任、PG认证/HA/RTO/RPO、L3采集与故障策略/保留/保全/审批，以及首发Codex协议。排除APIM和LiteLLM Enterprise依赖；免费回调不等于可靠审计已经完成。

**退出证据**：`network_capacity`、`identity_owners`、`pg_auth_ha`、`l3_policy`、`protocol_scope`。关键决策未定时停在此阶段，不用参考环境的邮箱、资源名、无HA或West US替客户作决定。

### 阶段3：供应链与目标基础

**客户准备**：保护分支、OIDC最小权限、独立目标RG/workspace、镜像审批和漏洞例外流程；确认目标RG没有同名共享资源。

**执行**：跑CI及`make validate-oss-callbacks`；`stage=3, component=platform, mode=what-if`预览默认无公网的ACR。Private Endpoint在阶段4创建，ACR网络/导入路径未就绪前不能宣称镜像晋级完成。镜像晋级workflow会写ACR，需单独审批；在网络就绪后完成固定digest、扫描、SBOM和签名。禁止直接把1.98指向旧1.95数据库。

**退出证据**：`oidc_scope`、`image_signature_sbom`、`target_isolation`。若ACR路径依赖阶段4，可在预先批准的受控镜像库完成阶段3供应链证据，阶段4再复制到目标ACR；不能为推进阶段长期打开ACR公网。

### 阶段4：私网、Private AKS与身份

**客户准备**：目标VNet/PE子网、私有DNS归属、路由/Firewall规则、AKS版本/SKU配额、可到达私网的执行机；阶段3证据。

**执行**：`stage=4, component=platform, mode=what-if`。确认模板引用的VNet和PE子网已在目标RG建立；审查新增子网不重叠。批准后由平台团队执行模板，验证DNS、外部依赖出口、ACR拉取、WI正负向访问及AKS无公网管理路径。另行部署受支持的私有ingress controller/API和admin内部LB及证书；本仓库尚未自动编排该controller/LB。

**退出证据**：`private_dns_egress`、`private_runner`、`workload_identity`、`private_ingress`。未满足时旧服务继续运行；不修改旧AKS网络或移除旧VMSS UAMI。

### 阶段5：数据服务与迁移演练

**客户准备**：新Vault/PG/Redis配置、Entra数据库管理员、各身份及CSI映射、Master/Salt安全恢复方式、私网恢复执行机。DNS zone创建开关按客户归属设置，已有zone不得重复接管。

**执行**：`stage=5, component=platform, mode=what-if`。当前PG模板为Entra-only；令牌续期/Prisma连接必须实测。批准密码补偿时必须实现数据库passwordAuth与连接/轮换全链路，不能仅新建密码。把旧库备份恢复到新隔离库，再运行候选版本schema migration、密文和数据回归；审查版本差异，不直接迁移旧库。验证Redis Entra/TLS和CSI轮换。

**退出证据**：`pg_migration_restore`、`key_salt_decryption`、`redis_entra`、`csi_rotation`。不得提前改旧生产连接串；失败仅处理新隔离库，保留旧库及备份。

### 阶段6：HA、路由与容量

**客户准备**：阶段5证据、模型资源权限和私网、已签名镜像、RPM/TPM/并发值、隔离负载与故障注入方案。

**执行**：`stage=6, component=none, mode=preflight`，运行`make validate-stage6`；平台团队在私网runner渲染、审核并应用客户专用overlay。验证双副本/PDB/HPA、drain、Redis共享状态、节点/Pod故障、429/TTFT、亲和/缓存及费用。不得把带占位符的validation目录直接apply。

**退出证据**：`replica_failure`、`load_affinity`、`capacity_limits`、`no_legacy_db_writes`。失败回退新环境镜像/配置，旧流量不动。

### 阶段7：Entra和双域名授权

**客户准备**：两套Entra应用/角色、授权客户端、tenant/team/model映射、独立API/admin UAMI/Vault/Key、私网admin DNS、证书和CA/MFA管理员。

**执行**：`stage=7, component=none, mode=preflight`生成域名overlay；用客户值补齐策略、身份、CSI和镜像引用，运行`make validate-stage7`及真实租户负向测试。验证跨tenant/用户/model/对象访问拒绝、后端直连拒绝、admin不公开。当前只支持有限管理路由，不是完整LiteLLM原生UI桥接。WS、对象引用、加密多轮、Files/MCP等未完成授权的能力保持关闭。

**退出证据**：`tenant_negative_tests`、`object_ownership`、`admin_private`、`required_protocols`。客户首发协议未覆盖就不能切换现有Codex入口，不得仅因普通Chat通过而放行。

### 阶段8：L3审计与观测

**客户准备**：合规批准的采集范围/故障策略、CMK Vault/Key、writer/reader/retention身份、双人审批和保全发布者、访问日志期限、恢复目标及Monitor接入。

**执行**：`stage=8, component=audit, mode=what-if`预览独立私有审计账户；`component=none, mode=preflight`生成域名配置。运行`make validate-stage8`及`make validate-oss-callbacks`。必须完成受信接收器、持久化恢复、流完整性、正文脱敏、hold/delete竞态和云端权限验收后才开启强制生产采集。当前RAM链路及未接入部署的OSS适配器不足以满足该门槛；回调success或HTTP200不证明原文完整/可靠交付。

**退出证据**：`durable_audit_recovery`、`audit_governance`、`telemetry_received`、`guardrail_scope`。L3是首发必需项，不能用“默认关闭”作为通过证据；未完成则停止试点。普通日志、Trace和Spend Logs不得出现原文或凭据。

### 阶段9：试点、切流与回退窗口

**客户准备**：前8阶段证据、内部API LB/PLS/TLS、真实协议矩阵、WAF策略、DNS权限、两名发布Owner、数据库写入冻结/最终同步和回退窗口方案。

**执行**：先`stage=9, component=origin, mode=what-if`，再`component=edge`。edge预览会打开资源创建但保持`enableApiTraffic=false`和WAF Detection，不修改DNS。检查LB/子网快照、PLS手动批准、源站证书、FDID补充检查及长流超时。获准后遵循[Stage9发布Runbook](litellm-stage9-edge-cutover-preparation-2026-09-07.md)和release脚本，先限定客户端试点，再单独审批DNS/流量切换与WAF Prevention。

**退出证据**：`enabled_what_if`、`origin_tls_private_link`、`pilot_regression`、`rollback_rehearsal`、`dual_owner_release`。具体切流顺序：冻结模型/Key/预算管理写入，按已演练方式同步新库，验证密文与记录，批准切换客户端/DNS，观测错误率/长流/L3/账务，异常达到客户阈值则恢复旧入口。若新库已产生独有写入，先评估数据/预算一致性，不能只改DNS假定无损回退。不要让旧新schema混写或把真实用户随机分到不一致数据库。

回退窗口结束后另立退役变更：确认无旧流量/依赖、备份可恢复、成本/Owner/保留要求，逐个删除专属资源；不整组删除共享RG，不自动解除保护锁，不把[历史清理示例](../LiteLLM/RESOURCE_CLEANUP_ZH.md)当作客户所有权清单。

## 5. 私网受控终端执行

workflow输出不提供原始参数artifact。需要完整What-if审查或批准后执行时，在客户私有终端检出同一完整Git SHA，通过客户Secret/配置管理器注入同名环境变量，再运行同一模块：

```bash
export MIGRATION_REVISION="$(git rev-parse HEAD)"
python -m scripts.customer_migration --stage 4 --environment test --mode preflight --component platform
python -m scripts.customer_migration --stage 4 --environment test --mode what-if --component platform
```

`CUSTOMER_CONFIG_JSON`、`MIGRATION_EVIDENCE_JSON`、`AZURE_TENANT_ID`、`AZURE_SUBSCRIPTION_ID`须事先注入；不要把值粘到shell历史、工单或CI YAML。stage/component每次使用单独输出目录，避免将后一组件参数误用于前一模板。审查私有输出的资源清单与批准的订阅/RG一致。

预览出现Modify/Delete/Unsupported/未知类型时退出非零；只有Ignore/NoChange也不算新阶段创建批准。已有资源更新可能合法，但必须在私有报告逐资源审查，明确所有权和风险，走客户独立变更审批，不通过篡改What-if JSON或放宽全局门禁解决。RBAC运行时principalId导致Unsupported同样需现场验证。

真正部署由客户授权执行人，在同一订阅、同一代码、同一参数和批准窗口下操作。例如平台阶段使用：

```bash
az account set --subscription "$AZURE_SUBSCRIPTION_ID"
az deployment group create \
  --resource-group "$TARGET_RESOURCE_GROUP" \
  --template-file infra/environments/main.bicep \
  --parameters @temp/customer-migration/parameters.json \
  --mode Incremental --output none
```

此命令不是预检的一部分，只有完整审查通过后人工执行；`TARGET_RESOURCE_GROUP`由批准配置的target.resourceGroup取得，不能自由换成旧RG。备份、监控、审计、PLS、edge分别使用对应模板，监控使用legacy RG；不能跨组件复用参数。执行结束在客户受控位置保存deployment ID/脱敏结果，再记录阶段验收。Kubernetes部署、PG恢复/迁移、DNS和Secret初始化依各阶段Runbook执行，无完成接线和验收证据则停止。

## 6. 验证与当前边界

本仓库CI包含Bicep/Kustomize/Node/Python离线检查、固定版本OSS回调矩阵、迁移控制器负向测试和可发布个人信息/明显凭据检查。后者不读取忽略配置，不替代专用DLP/Secret Scanning。新workflow定义和本地测试通过不等于客户Actions、OIDC或Azure部署已经验证。

完整工程仍有受信审计交付、协议所有权、私有controller/LB、身份/Vault接线、PG令牌与HA、DCR/DCRA等未完成项，见[收尾台账](litellm-code-completion-backlog-2026-09-07.md)。客户可以先完成准备、盘点、决策及预览；不可跳过这些阻断项直接声称安全增强生产迁移完成。