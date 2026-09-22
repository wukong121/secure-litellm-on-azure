# Stage2客户决策冻结与验收操作指南

> 核对日期：2026-09-14。适用于既有LiteLLM迁移、一期选择`contentAudit.mode=native`的路径，对应[主执行手册的Stage2](customer-migration-guide-zh.md#阶段2冻结客户决策)。
>
> 本阶段五项：`network_capacity`、`identity_owners`、`pg_auth_ha`、`content_audit_policy`、`protocol_scope`。这是方案、责任和实施前提的人工验收，不是新环境已经部署或通过业务测试的证明。

阅读顺序：准备 → 网络与容量 → 身份分工 → 数据库认证/恢复目标 → 正文审计政策 → 客户端范围 → config-check/draft/confirm。**Stage2没有infrastructure或runtime部署动作，不需要为了验收先部署新AKS、PG或代理。** 已有Stage0/1证据可以引用，但要确认范围和时效仍适用。

## 1. 开始前准备

### 1.1 本阶段到底要完成什么

| 检查ID | 本阶段交付的真实记录 | 不代表什么 |
| --- | --- | --- |
| network_capacity | 获批区域/规格/预算、配额核对、无冲突网段和私网/DNS方案 | 不代表未来SKU容量已预留，或新AKS私网已实测 |
| identity_owners | 已知身份核验、分工/最小权限/OIDC方案、每项后续授权的Owner和完成节点 | 不代表未来数据库权限、Graph同意或应用登录已自动完成 |
| pg_auth_ha | PG认证/管理员路径、HA选择、RPO/RTO和备份/恢复/数据迁移方案 | 不代表故障切换、真实全量恢复或最终停机时长已通过 |
| content_audit_policy | 原生模式、留存/读取者/用途/备份处理/容量与记录限制获得批准 | 不代表Prompt已开始采集，或某位管理员自动拥有读取权 |
| protocol_scope | 实际客户端/版本/认证方式、必需协议与管理功能、对应后续测试计划 | 不代表新代理已兼容全部所选能力 |

“已决定，但按流程在后续阶段实施”可以记录实施节点和负责人；“尚未决定”不能写成通过。区域/网络冲突、必需能力明确不兼容却没有获批解决路线、数据库认证路径不可行等问题，应停在相应决策项处理，不把阻断简单写成后续关注。

准备材料：有效Stage0/1账本及受控盘点/回归记录、客户配置、网络/IPAM资料、资源Owner/Entra管理员/DBA/安全与业务负责人提供的决策记录。单人演练可由已批准操作者承担多项职责，但不能声称已实现双人职责分离；客户现场仍由实际责任人批准。

### 1.2 操作位置和取值来源

Azure查询在满足客户设备/条件访问要求的管理终端或Portal完成，使用有权读取目标订阅及相关资源的身份。JSON检查在有本仓库及依赖的管理机或GitHub托管Runner完成；本阶段不需要读取密码、导出Secret或执行PG写入。CLI查询成功仅证明本次读取，不代替将来的Actions身份授权。

下面是本机Bash变量，不是新增GitHub Variable，不会从GitHub Secret自动同步。客户值只保存在受控记录，不写入公共文档或聊天。

```bash
set +x
set -o pipefail
SUBSCRIPTION_ID="REPLACE_SUBSCRIPTION_ID"
REGION="REPLACE_APPROVED_AZURE_REGION"
TARGET_RG="REPLACE_TARGET_RESOURCE_GROUP"
TARGET_VNET="REPLACE_EXISTING_TARGET_VNET"
az account show --query '{tenantId:tenantId,subscriptionId:id}' --output json
```

| 值/材料 | 从哪里取得 | 怎样核对 |
| --- | --- | --- |
| SUBSCRIPTION_ID、tenantId | 客户JSON的azure.subscriptionId/tenantId；Portal订阅Overview/Entra Overview | 与当前账号及实际资源所属订阅/租户比较，不能沿用顾问个人订阅 |
| REGION | 客户批准的区域和JSON的location；Portal相关服务区域选择器 | CLI使用区域标识，例如westus格式而非显示名；不是从示例自动接受某一区域 |
| TARGET_RG、TARGET_VNET | target.resourceGroup、parameters.backup.virtualNetworkName；Stage0备份部署输出与实际VNet | 是Stage0已建立的目标网络，不是旧AKS节点VNet或Runner VNet |
| Runner/Hub/企业网段与DNS | Runner NIC关联VNet、Hub网络资源、客户IPAM/网络Owner记录 | 查完整地址空间、有效路由和转发方案，不能只取Runner单个IP |
| 新AKS版本/规格/节点数 | N-1获批选择，回填parameters.platform.stage4Aks | 来自目标区域/订阅的支持和配额信息，不照抄旧集群或示例 |
| PG/Redis规格及HA | D-1与DBA批准记录，回填parameters.platform.stage5Data | 服务区域/SKU支持和恢复目标匹配，不把示例Disabled当生产结论 |
| 身份Client ID和Object ID | 既有身份资源或客户Entra管理员，按I-1核对 | Client ID用于登录，服务主体Object ID用于授权，个人用户ID不能互换 |
| 正文保留天数与读取者 | 安全/业务/数据责任人批准的政策 | 不是Azure查询返回值，也不是由ownerEmail推断授权 |
| 客户端/模型/必需功能 | Stage0实际客户端配置、版本输出、业务Owner清单 | 不把产品宣传的全部功能当首发范围，也不遗漏客户实际依赖 |
| DRAFT_RUN_ID | 成功Stage2 acceptance draft URL中actions/runs/后的数字 | 不是config-check、Stage1 draft、job或artifact的ID |

无权读取配额/目录/网络时，请对应Owner提供带时间与范围的核验结果；不能用扩大到订阅Owner、复制Token或关闭条件访问来凑检查。

### 1.3 配置与受控决策记录分别放什么

现在把已批准的`contentAudit`放入完整客户JSON并同步当前Environment的`CUSTOMER_CONFIG_JSON` Secret。该块和字段解释见C-1。原artifact key保持不变。

平台网络/AKS参数按N项准备，**Stage3/platform首次plan前**须完整回填`containerRegistryName`、`logAnalyticsWorkspaceName`、`stage4Network`和`stage4Aks`，不能到Stage4才补。PG管理员/规格等stage5Data可以在Stage5部署前回填，但Stage2要先有可行、获批的选择与责任人。

不必在Stage2提前填写application/proxy镜像digest、未来PLS/LB ID或生成Vault凭据。未来需要的主体尚未建立时，在受控任务记录写负责人、创建/授权节点和批准方案，不向JSON塞假GUID/占位符顶层块。此文的RPO/RTO、Owner表和测试矩阵是外部记录，**不是让你新增未受支持的customer.json字段**。

## 2. network_capacity：网络、区域与容量

### N-1 核对区域、版本、规格和配额

1. 列出拟用区域、AKS系统/用户节点VM SKU与数量、PG SKU/HA、Redis SKU、ACR及可选Firewall/Front Door费用。规格取服务Portal的创建参数页或官方可用性资料，只查看选项，不提交创建。
2. Portal搜索Quotas，按目标订阅、区域及服务筛选。核对AKS节点所需VM系列vCPU和区域总vCPU的当前使用/限制，计入旧环境与Runner并行占用、升级临时节点和计划扩容余量；已有VM与新节点跨区域时分别核算。
3. 确认所选VM SKU在该订阅/区域/可用区没有限制；PG和Redis的SKU/HA模式也要按各自服务核对。配额通过不代表即时容量保证，创建前仍要复核；缺配额时记录申请结果，不靠降低既定安全/HA要求通过。
4. 从当前区域支持列表选择获批AKS版本，并核对项目镜像/Ingress及版本升级策略，不把“列表最新版本”自动当作批准版本。

```bash
az aks get-versions --subscription "$SUBSCRIPTION_ID" --location "$REGION" --output table
```

节点vCPU需求按各节点池“节点SKU的vCPU数 × 批准的峰值节点数”汇总，峰值包含实施方确认的升级/扩容余量。这里只做有依据的预算和容量核算；实际负载与单副本故障能力在后续阶段验证。

### N-2 核对网段不冲突及可扩展性

读取Stage0实际目标VNet，查询不会创建或调整子网：

```bash
az network vnet show --subscription "$SUBSCRIPTION_ID" --resource-group "$TARGET_RG" --name "$TARGET_VNET" \
  --query '{id:id,prefixes:addressSpace.addressPrefixes,dnsServers:dhcpOptions.dnsServers,subnets:subnets[].{name:name,prefix:addressPrefix,prefixes:addressPrefixes}}' \
  --output json
```

与客户IPAM/网络Owner共同检查以下清单，不把VNet、Pod或Docker示例CIDR当作通用默认值：

| 要核对的范围 | 记录内容/标准 |
| --- | --- |
| 目标VNet | 与Stage0实际地址空间一致，与需互通的Runner/Hub/企业/旧网络无冲突；不能为了复用文档重建Stage0网络 |
| PE、系统节点、用户节点、Ingress、Firewall子网 | 前缀位于目标VNet内，子网之间无重叠，容量覆盖节点扩容/升级、内部LB/PE等用途和Azure保留地址；已有PE子网须保留 |
| podCidr、serviceCidr、dnsServiceIp | 符合选定AKS网络模式及Azure约束，避免与互通网络冲突；DNS Service IP属于Service CIDR且符合保留地址要求 |
| Runner Docker地址池 | 与备份/目标/企业访问路径无冲突；已有备份恢复探针成功不证明未来新子网也无冲突 |
| 跨区域/跨订阅 | 记录路由/DNS归属与费用；当前Stage0 runner-connectivity仅支持同订阅，不把不同订阅资源当作已自动连通 |

`stage4Network`逐项对应示例中的system/user/ingress子网、firewallSubnetPrefix、podCidr、serviceCidr和dnsServiceIp；备份VNet地址空间来自parameters.backup。由网络Owner给出容量核算，不凭“都是10.x”认定互通，也不把地址不重叠等同于路由已配置。

### N-3 明确私网路径、DNS、出口和域名归属

在客户网络记录上逐条标明源、目标、协议/端口、路由、DNS解析Owner、实施节点及验证人：

| 路径/依赖 | 本阶段要确定 |
| --- | --- |
| Runner → 新AKS API、ACR、Vault、PG/Redis、备份Blob | 获批管理路径、Private DNS/转发、NSG/Firewall及回程；不能以GitHub-hosted Runner公网访问代替 |
| 新应用 → 模型账号 | 实际模型资源ID/区域/访问身份、私网接入与共享依赖；关闭共享账号公网或Local Auth前必须保证旧业务不受影响 |
| Runner构建/扫描及工作负载出口 | 必需依赖FQDN/服务和批准出站方案；不使用全开放出口、跳过TLS或关闭扫描兜底 |
| Private DNS | 沿用现有zone还是新建、谁负责link/转发；createStage5*PrivateDnsZone开关须与实际归属一致，不重复创建冲突zone |
| API/Admin域名与证书 | baseDomain归属和DNS管理人；两个域使用独立Front Door endpoint，Admin严格mTLS；两份公有CA源站证书、客户端PKI/吊销服务、边缘信任Vault及Owner，不依赖后续业务Vault |

**network_capacity通过标准：** 区域/规格/预算选择有依据，所需配额已核对，网段/容量无未处理冲突，私网/DNS/证书与出口路线明确且获批。尚未建设的链路记录后续实施与实测节点，不声称此时已通过连接测试。

## 3. identity_owners：身份与授权责任

### I-1 逐个核实已存在的身份

打开仓库Settings → Environments → 本次环境，核对已使用的AZURE_CLIENT_ID、AZURE_RUNTIME_CLIENT_ID、租户与订阅Variables。再到客户租户应用注册/Enterprise applications或Managed Identities交叉核对，不把变量名称当作身份真实存在的证据。

| 标识 | 获取位置与用途 |
| --- | --- |
| Client ID | App registrations Overview的Application (client) ID，或UAMI Overview的Client ID；用于OIDC登录 |
| 服务主体Object ID | Enterprise applications Overview的Object ID，或UAMI Principal ID；用于资源授权，不取应用注册对象的Object ID |
| 用户/组Object ID | 客户租户Entra Users/Groups指定对象；用于获批人工身份或管理员组，不替代workflow服务主体 |
| GitHub联邦信任 | 身份Federated credentials页面；issuer=https://token.actions.githubusercontent.com，audience=api://AzureADTokenExchange，subject精确绑定客户仓库和Environment |

具体ID获取命令和例子见[主手册0-A1](customer-migration-guide-zh.md#0-a1-备份身份的获取与核验)。已有运行的登录和资源访问证据可复用；不能从ownerEmail、当前CLI登录人或某个显示名推断主体。订阅Contributor不自动具有Graph、Kubernetes、Blob数据或PG内部权限。

### I-2 填写身份责任表

表格保存在受控位置，填真实责任人、已存在主体ID/核验来源、需要的最小权限范围、尚待实施的授权及完成节点。不是要求此刻无差别创建全部专项身份。

| 分工 | 要确认的路径 | 最晚核验节点 |
| --- | --- | --- |
| deploy | Azure基础设施/What-if、私有ACR晋级与必要受控RBAC；与普通runtime分离 | 各组件首次plan/deploy前 |
| runtime | 私网集群发布、备份Blob数据操作、指定Vault及所需资源回执读取 | 各私网动作前；不能由个人登录替代 |
| database | AZURE_DATABASE_CLIENT_ID对应服务主体如何成为PG Entra管理员或获准管理员组成员 | Stage5/database-roles前，见D-2 |
| Entra bootstrap / access | 应用初始化与准入授权分工，Graph权限/同意由谁批准 | Stage7前，不能用Azure RG角色代替Graph同意 |
| certificate（选自动签发时） | 独立DNS挑战与API证书权限；人工证书方案则确定PKI交付责任 | Stage4证书/Ingress前 |
| 管理员、正文读取者及回退保管人 | 谁可登录、谁可读取内容、谁保管原密钥/备份、谁批准变更 | 决策现在确定，具体应用绑定与读取测试在Stage7/8完成 |

### I-3 确认治理和权限变更流程

复核默认分支保护、Environment限制、OIDC subject、受信Runner使用边界、governance的批准模式及实际操作者。既有宽权限应如实记录，明确后续最小化方案；不能称为已完成最小权限。若当前授权违反客户必需控制，则应先解决，不能仅写一条风险备注通过。

**identity_owners通过标准：** 已使用身份的ID/用途核对一致，关键分工无歧义，后续专项权限有可实施的最小权限方案、明确Owner和完成节点，审批模式与实际一致。不把未来身份预填成个人Object ID，也不授予通用Owner掩盖缺口。

## 4. pg_auth_ha：数据认证、可用性与恢复目标

### D-1 与DBA确定目标PG和Redis选择

从Stage0盘点记录取得旧PG大版本、扩展、数据库实际容量、增长、原Master/Salt依赖以及备份恢复结果。DBA再核对目标区域支持的PG SKU/容量/HA模式与Redis SKU，明确备份保留、性能/IO和费用预算。

parameters.platform.stage5Data中的postgresqlSkuName、postgresqlHighAvailabilityMode、redisSkuName需有批准依据。`Disabled`只代表不启用该PG HA模式，不是已验证高可用；演练允许时记录风险与恢复方案，不能自动照搬到生产。HA不是备份，备份也不能替代自动故障切换。

### D-2 确认PG Entra管理员路径可行

必须区分三类主体：

| 主体 | 作用与获取方法 |
| --- | --- |
| PG Entra管理员 | Stage5创建目标PG时的postgresqlEntraAdministratorObjectId/PrincipalName/PrincipalType；由DBA/Entra Owner明确User、Group或ServicePrincipal及实际对象 |
| 初始化workflow身份 | GitHub Environment的AZURE_DATABASE_CLIENT_ID；按I-1取得其服务主体Object ID，确认它本身或其获准管理员组路径可在目标PG建立角色 |
| 应用与迁移角色 | database-roles建立独立llmgw_app、llmgw_migrator；databaseAccess.migrationPrincipalId使用普通runtime服务主体Object ID，不是上述Client ID或个人用户ID |

**仅配置个人User管理员，不能让另一个OIDC服务主体以该用户身份登录PG。** 选择管理员组时，由DBA与Entra Owner核验服务主体成员关系和该PG方案的支持条件，不仅看组显示名。现在明确方案，Stage5目标库实际登录/建角色时再验证；不宣称这一步已经建立数据库权限。

明确应用无DDL权限、迁移身份与日常应用分离，PG/Redis采用项目规定的身份认证和TLS/私网路径；不得因认证未准备就默认退回共享管理员密码或公开数据库。现有方案尚不可行时先修订并获批，不等到Stage5反复运行失败。

### D-3 明确RPO、RTO和最终数据迁移边界

RPO是可接受的数据丢失窗口，RTO是服务恢复时间目标；由业务Owner给出，而不是从“用户不多”推导。用Stage0已有实际容量与备份/隔离恢复耗时做初步估算，列出仍待Stage5及最终迁移演练验证的部分。

停机评估需包括停止写入/排空、最终备份、传输、恢复、schema迁移、对账、业务验证和切流，不只使用报告restoreSeconds。记录超时回退阈值及最终停写批准人；如要求一小时窗口，必须后续以完整链路实测证明，现在不能填写“一小时RTO已通过”。

### D-4 确认可恢复性与已知实施缺口

1. 保持旧PG/PVC和原加密材料，禁止让新版自动迁移旧生产库；Stage5先用隔离、空目标库演练，不反复覆盖非空演练库。
2. 原Salt未显式设置时，由DBA/实施方核对固定旧版本的实际加密回退规则及目标兼容验证，不能临时生成Salt，也不能只凭字段缺失认定历史数据不可解密。尚未验证的兼容问题记录为Stage5阻断。
3. 本仓库的最终停写、最终干净目标库选择/切换、数据回退以及stop-legacy编排尚不完整。按[主手册第5节](customer-migration-guide-zh.md#5-最终停写切流观察和停旧)确定补齐责任与演练节点；不得在未完成时启用Stage9业务流量。
4. 决定新环境写入后的数据回退方式；仅DNS回退不能保证新Key、预算及日志等数据无损回迁，不允许新旧系统无计划地同时写不同步的数据副本。

**pg_auth_ha通过标准：** 认证/管理员路径可实施，规格/HA/RPO/RTO选择得到批准，数据和密钥恢复依赖明确，已知缺口有负责人、验证方案及切流阻断条件。这不是Stage5恢复或Stage6 HA测试通过记录；没有解决路线的关键数据风险不能按“以后再说”验收。

## 5. content_audit_policy：正文留痕与读取政策

### C-1 明确三个字段及生效阶段

下面是顶层块的结构示意，合并到现有完整客户JSON，**不是用这段替换整个配置文件**：

```json
{
  "contentAudit": {
    "mode": "native",
    "retentionDays": 7,
    "contentPolicyAccepted": true
  }
}
```

| 字段 | 功能 | 值从哪里来 |
| --- | --- | --- |
| mode=native | 使用LiteLLM原生Spend Logs及PG保存正文，不要求独立增强L3平台 | 本次获批的一期技术方案；当前此块仅支持native |
| retentionDays=7 | 配置Spend Logs保留7天，当前实现每天执行一次清理；允许1至30的整数 | 客户安全/业务批准的保留期，结合容量预算确定，不是查询Azure取得 |
| contentPolicyAccepted=true | 显式确认接受正文采集、留存、备份及记录能力限制 | 真实审批决定；未批准不能为了通过校验填true |

Stage2只冻结决策，Stage8/application发布时才生成`store_prompts_in_spend_logs=true`、`disable_spend_logs=false`、`maximum_spend_logs_retention_period=7d`及清理间隔`1d`。配置不立即改变旧环境、不补录过去正文；清理也不是满168小时即时删除，不改变Log Analytics或备份保留期。实际采集和清理在Stage8实测。

### C-2 确认用途、人员、权限与访问边界

记录允许采集的工作用途、覆盖对象、必要告知/批准流程、谁可抽查及何时允许导出。先按用户/时间分析Token和费用，再按获批范围抽查；高Token可能来自上下文、工具结果或重试，不能单独作为不当使用结论。

`contentPolicyAccepted`不是管理员权限开关，不授予也不禁止某人读取Prompt。新管理代理的正文路径另由管理员binding的`nativeAuditRead`控制，配合nativeUi、身份会话与后台权限；仅具有proxy_admin角色也不自动通过代理的正文路径。计划的读取者在Stage7配置/授权，Stage8验证实际读取与拒绝。

直接访问后台、PG或备份的运维权限须另行约束，不把代理开关当全局数据隔离。原生Logs为首选；若UI不能展示实际需要的正文，批准的IT实名PG只读查询是补充方案，DBA限定表/视图、人员和私网访问，不共享llmgw_app或管理员密码。**当前没有自动授予PG审计只读权限的workflow按钮。**

### C-3 确认留存、备份、容量及记录限制

| 政策项 | 要明确的决定 |
| --- | --- |
| 在线留存与清理 | 保留期覆盖抽查周期，谁核对每日清理及失败监测；删除到期日志不一定即时回收PG磁盘文件空间 |
| 备份与恢复 | 备份可能保留更久，正文恢复后重新出现；谁能读取备份、保留多久、恢复后的过期数据怎样处理 |
| 数据规模 | 用已有请求量和批准测试样本估算正文/索引/备份增长及IO预算；Token数量不能直接当作数据库字节数 |
| 内容完整性 | 异步写入、异常中断、脱敏、超大载荷截断可能造成缺失；一期不承诺逐帧零丢失或不可变取证 |
| 故障可发现性 | 谁确认日志写入/清理失败可发现，Stage8用什么获批方法验证；容器日志存在不等于Spend Logs正文已成功写库 |

### C-4 核对模式不冲突并保留批准记录

原生模式不与顶层auditRuntime/auditGovernance或L3 audit_reader/auditTeamId绑定混用。示例中保留的parameters.audit不等于必须部署增强L3，不为填满字段而创建审计平台；历史已启用L3/guardrail/遥测时需独立批准迁移，不静默关闭。

**content_audit_policy通过标准：** 模式、用途/读取者、在线及备份保留、容量、记录限制与后续验证责任得到真实批准，JSON与批准内容一致。true只是机器检查的声明，不等于合规审批自动完成、实际数据已采集或授权已部署。

## 6. protocol_scope：冻结实际首发范围

### P-1 盘点真正使用的客户端和管理操作

以Stage0的protocol_baseline及Stage1变更后回归记录为基础，从实际客户端About/版本命令、VS Code扩展详情和生效连接配置取得版本、协议、模型别名及认证方式。后台模型deploymentName不是客户端modelGroup，按真实映射记录；不保存完整Key、Token或Prompt。

在客户受控记录中为每个实际首发客户端填一行：

| 客户端/版本 | 调用协议与路径 | 认证/续期方式 | 模型组与必需功能 | 旧基线证据 | 新入口测试节点/负责人 |
| --- | --- | --- | --- | --- | --- |
| 待填写 | 待填写 | 待填写 | 待填写 | 待填写 | 待填写 |

由业务Owner区分“首发必需”“明确不适用”“后续需求”。不是全UI/全SDK/全协议都必须支持，但客户日常必需的用户/Team/Key/模型/预算管理操作也要列出，不能只列推理接口。

### P-2 核对新入口认证和当前兼容边界

1. 新API合同是Entra Token加客户端LiteLLM vkey，即Authorization Bearer和X-LiteLLM-API-Key同时存在。明确客户端如何携带两项、获取/续期企业Token，Entra/CA/MFA由谁配合；不把client ID、Token与vkey混为一谈。
2. LiteLLM继续负责vkey的模型ACL和预算，不强制把Entra调用者绑定为该Key的Owner；共享Key不能单独证明实际调用者，审计关联另行核对。
3. 从客户所选协议核对连续对话、SSE、工具调用、结构化输出、取消/重连、对象引用及续期。当前代理拒绝WebSocket升级、部分对象引用/加密上下文；明确依赖这些能力时，不得用“旧入口能用”证明新入口可用。
4. 必需能力存在缺口时，先取得获批的实现/适配路线或与业务确认可接受的首发方案，并列为Stage7及切流前阻断；没有可接受路线则不确认该项。不得删除必需测试或绕过企业认证以得到通过结果。

### P-3 写出后续正反向验收计划

至少明确：真实客户端正常调用/连续对话/所需流式与工具能力；缺Token/缺vkey/过期Token/无效或撤销Key的拒绝；越权模型与预算限制；Admin无客户端证书、错误FQDN、过期/吊销证书和错误密码的拒绝；获准正文读取和未获准读取拒绝。每项记录预期结果、测试人、无敏感测试数据及费用上限。

Stage4验证网络/TLS和镜像，Stage5验证数据恢复/密文，Stage6验证后台/容量，Stage7验证新入口身份与实际协议/管理范围，Stage8验证原生日志与读取。测试客户端必须关联到真实候选入口，不能悄悄回退官方服务或旧网关后算通过。

**protocol_scope通过标准：** 首发客户端/版本、认证实现、协议与必要管理功能完整列明，旧基线有依据，已知缺口有获批解决路线和阻断条件，后续实测矩阵与责任人确定。新系统的“尚待实测”不是“已通过”，Stage2确认不解除后续测试门禁。

## 7. 记录、配置检查和阶段确认

### 7.1 保存受控决策记录

| 检查ID | 应关联的记录 | 版本/日期/批准人 | 结论与未完成实施项 |
| --- | --- | --- | --- |
| network_capacity | 区域/规格/配额、IPAM容量表、路由/DNS/域名与费用方案 | 待填写 | 待核验 |
| identity_owners | 主体核验、身份责任与权限矩阵、OIDC/治理批准 | 待填写 | 待核验 |
| pg_auth_ha | PG认证/HA、RPO/RTO、备份/密钥及最终迁移路线 | 待填写 | 待核验 |
| content_audit_policy | 用途/读取者、在线和备份保留、容量及记录限制批准 | 待填写 | 待核验 |
| protocol_scope | 客户端版本/首发功能、认证方式、缺口解决与后续实测矩阵 | 待填写 | 待核验 |

用客户工单/受控文档保存足够的内容和版本，不要求新建证据平台。公开验收说明仅引用允许公开的编号；不附内部网络清单、主体名单、密钥库URL或日志正文。

**冻结边界：** Stage2的stage-config指纹包含contentAudit，但parameters.platform中的ACR/后续网络/AKS等配置不进入Stage2指纹。新工作区名称是例外：bootstrapWorkspaceName会从backup（没有该块时从platform）派生并单独绑定，不能随意更改。文档/审批中的网络、规格和协议决定不会自动全部被JSON哈希锁定；实施前必须人工将实际参数与批准记录逐项核对，变化走复核流程，不以“旧账本仍能校验”代替批准。

单独新增contentAudit不会改变Stage0/1的stage-config指纹；改governance、基础范围或代码SHA是另一回事，仍会影响证据适用性。后续代码变更、相关配置变更或超过时效时，按主手册重新审核受影响账本，不手改报告哈希。

### 7.2 按顺序运行，未完成决策不confirm

所有行使用受保护默认分支main、environment=test、stage=2。客户用其他Environment时整套一致替换。更新本地客户JSON后须先同步完整CUSTOMER_CONFIG_JSON Secret，Actions不读本机文件。

| 步骤 | workflow/执行方式 | 输入 | 结果 |
| --- | --- | --- | --- |
| S2-01 | Customer staged migration | mode=config-check、component=none | 只验证配置，不部署、不查询Azure配额或自动批准五项决策；prepare成功且preview跳过是正常的 |
| S2-02 | Customer stage acceptance | operation=draft；所有确认字段留空 | 生成pending清单，记录本Stage的draft运行ID |
| S2-03 | 人工完成N/I/D/C/P项并核对7.1 | 不是workflow按钮 | 五项决定与证据真实完成；未实施部分有明确后续节点，未决定/不可行部分继续处理 |
| S2-04 | Customer stage acceptance | operation=confirm，按7.3填写 | 记录已完成的人工决策验收，后续才进入Stage3 |

草稿可提前作为清单使用。Stage2没有对应的infrastructure component，也不运行runtime execute或what-if来代替上述步骤。config-check绿勾不能证明配额、网段、身份批准、RTO或协议兼容已经验证。

### 7.3 confirm字段逐项填写

沿用已批准single-operator路径，双人策略继续使用主手册record流程。确认Stage0/1账本仍在7天内且环境、阶段配置、策略和操作者匹配；single-operator允许这些前序记录来自先前Git SHA。Stage2的draft与confirm仍须同环境、完整Git SHA和适用配置，并使用原artifact key。若代码改动影响前序验收结论则先重验；双人模式仍按主手册要求前序账本同SHA。

| 页面字段/说明 | confirm时填写 |
| --- | --- |
| Use workflow from / environment / stage | main / test / 2，与draft一致 |
| operation | confirm |
| For confirm, successful draft run ID for this stage and revision | 有效Stage2 draft的DRAFT_RUN_ID，不是S2-01的config-check或Stage1验收ID |
| For confirm, every check ID personally verified; comma separated as shown in the draft Summary | checked_items：五项全部完成后填下面字符串，与实际draft一致 |
| For confirm, actual observations and evidence references; no secrets or prompt content | evidence_notes：普通文本，12至4000字符，逐项真实决定与允许公开的证据编号，不是JSON |
| For confirm, type the selected environment again | confirm_environment：test |

原生模式五项全部完成时，checked_items为：

```text
network_capacity,identity_owners,pg_auth_ha,content_audit_policy,protocol_scope
```

若draft仍列l3_policy，先检查Environment Secret是否含正确contentAudit、所选代码版本及实际模式，不随意改ID提交。计划采用增强L3的客户不使用本指南的原生清单，不为简化验收静默切模式。

evidence_notes按以下内容组织，不照抄成“已全部部署成功”：网络/容量方案及配额核验引用；身份责任和权限路径批准；PG认证/HA/RPO/RTO选择与后续实测边界；原生留存/读取/备份政策批准；实际客户端和必需功能范围及缺口处理引用。需后续实施的任务写清范围，不编造已通过的连接、故障切换或协议测试。

**当前confirm不支持部分通过。** 任一必需决定未完成、关键方案不可行或批准材料缺失时，不提交confirm，也不只填已有ID绕过。成功记录为`acceptance-record-test-2-<run ID>`，后续自动读取，不手动更新MIGRATION_EVIDENCE_JSON；`independentlyVerified=false`，它不是独立审计、最终数据迁移完成或业务切流批准。

## 8. 进入Stage3前的最后核对

1. Stage2决定已真实通过并完成confirm；所有账本仍满足当前代码/环境/配置与时效要求。
2. parameters.platform的ACR名称、新工作区名、stage4Network和stage4Aks已按获批决策完整填写，无REPLACE占位符，并同步Environment Secret。需要唯一性/容量复核的值仍须在实际plan/deploy前检查。
3. 模型连接、stage5Data、privateIngress证书及专项身份等按其最晚节点补齐，不因此提前创建未知顶层块；部署参数与本次受控决定有差异先复核。
4. 按主手册运行Check public source image，再进行Stage3/platform的plan与审核；没有Stage2基础设施deploy步骤，不重建Stage0资源来“补验收”。