> 发布说明：示例环境Owner不是客户默认Owner；以下决策必须由客户对应责任人重新批准。入口见[迁移指南](customer-migration-guide-zh.md)。

# LiteLLM 安全增强阶段 2：决策冻结与技术 Spike

> 文档状态：第一轮决策与本地技术 Spike 已完成；云集成 Spike 待新环境  
> 启动日期：2026-09-02  
> 前置条件：阶段 0、阶段 1 当前环境 Gate 已通过  
> 目标：在阶段 3 IaC 和新生产资源建设前，冻结架构边界并验证高风险兼容性  
> 明确边界：不升级当前生产 LiteLLM、不迁移生产数据库、不切换 Router、不创建或修改 APIM

> 2026-09-03架构决策更新：JWT-based Auth已确认属于LiteLLM Enterprise付费能力，因此不采购、不启用且不再作为上线门禁。数据面Entra JWT改由客户自有、部署在AKS中的独立认证代理验证；阶段2保留原始调查结论作为排除付费依赖的证据。

## 1. 执行摘要

阶段 2 第一轮已完成：

- D01-D22 均形成决定、默认结论或客户环境阻塞说明；
- LiteLLM `1.98.0` 精确 wheel 和官方镜像已验证；
- 官方镜像可在 UID `10001`、只读根文件系统及受限 `/tmp` 下启动并通过 liveliness；
- 目标 `usage-based-routing-v2` 和模型组分层 affinity 配置可被 `1.98.0` 解析；
- 已静态验证 Azure Managed Redis Entra凭据 Provider每次取用 Token并保留 username；
- Prisma接受包含 `sslmode=verify-full` 的 PostgreSQL URL和目标 schema；
- `1.95.0` 到 `1.98.0` Prisma schema确有变化，新增4个模型，正式迁移必须在隔离数据库演练；
- JWT-based Auth官方文档明确标为LiteLLM Enterprise，因此已排除并改用客户自有Entra认证代理；
- Guardrail核心能力使用OSS路径；仅Enterprise提供的高级治理能力不进入方案，由Azure AI Content Safety、客户自有策略组件或其他Azure原生控制替代；
- PG/Redis Entra真实认证、Workload Identity、Flexible Server、Managed Redis、数据库迁移、独立认证代理、OSS/Azure Guardrail和审计回调的云集成测试等待新Private AKS环境。

阶段 2 当前判断：**允许进入阶段 3的IaC与供应链建设，但不允许因本地静态/容器Spike通过而直接升级生产或迁移数据库。**

## 2. 决策冻结记录

当前方案 Owner为 示例环境Owner；标记“客户门禁”的决策必须由客户对应 Owner在生产上线前书面确认。

| ID | 决策 | 冻结结论 | Owner/状态 |
| --- | --- | --- | --- |
| D01 | 网关互联网或内网 | 互联网可达 | 当前Owner确认 |
| D02 | 公网入口 | Azure Front Door Premium + WAF + Private Link私有源站 | 当前Owner确认 |
| D03 | Admin UI独立域名 | 是，独立内网管理域名 | 方案冻结 |
| D04 | 数据面Entra JWT | 由客户自有独立认证代理验证；Virtual Key只在受控内部链路使用 | 方案冻结，待客户租户验收 |
| D05 | LiteLLM原生JWT | 禁止使用；已确认属于Enterprise付费能力 | 方案冻结 |
| D06 | 配置事实源 | 模型/Router/安全策略在Git；Team/Key/预算在DB；Secret在Key Vault | 当前Owner确认 |
| D07 | Prompt/Response落盘 | 默认关闭；批准范围内进行7天L3短期试点 | 当前Owner确认 |
| D08 | PostgreSQL认证 | 优先Entra；不兼容时使用Key Vault托管密码，不降低TLS | 当前Owner确认，待Spike |
| D09 | Redis认证 | 优先Entra；不兼容时使用Key Vault托管凭据，不降低TLS | 当前Owner确认，待Spike |
| D10 | Guardrail故障 | 按数据分类；高敏fail-closed，普通低风险fail-open并告警 | 当前Owner确认 |
| D11 | Guardrail范围 | 至少中文、英文及全部生产协议逐项验证 | 方案冻结 |
| D12 | RTO/RPO/区域 | 首期West US单区域；RTO 60分钟、RPO 15分钟 | 当前Owner确认；客户上线前复核 |
| D13 | 迁移方式 | 新 Private AKS迁移，不大规模原地改造 | 已确认 |
| D14 | 日志与CMK | L1进入Log Analytics；L3独立私有Blob/ADLS，目标CMK，7天 | 客户数据治理门禁 |
| D15 | 自动响应 | 默认只允许低风险、可逆动作；高影响动作人工批准 | 方案冻结 |
| D16 | LiteLLM版本/产品边界 | `1.98.0`为候选；固定精确镜像digest；不采购或依赖Enterprise能力 | 方案冻结，待完整OSS测试 |
| D17 | 上下文审计目的 | 仅安全、合规、数据保护和Agent治理；禁止默认绩效排名 | 已确认 |
| D18 | 审计范围 | messages、Responses items、tool call/result及批准文件/流式协议 | 客户治理门禁 |
| D19 | L3位置/期限 | 独立加密Blob/ADLS，默认关闭，试点保留7天 | 当前Owner确认，客户复核 |
| D20 | L3访问 | 独立角色、PIM、工单、理由、双人审批和全量审计 | 客户治理门禁 |
| D21 | 强制审计/no-log | 受监管范围内强制；先验证反绕过和协议完整性 | 客户治理门禁 |
| D22 | 员工告知/Legal Hold | 由客户Legal、HR、Privacy和Data Governance批准 | 客户门禁 |

## 3. LiteLLM 1.98.0候选证据

### 3.1 Wheel

| 项目 | 结果 |
| --- | --- |
| Package | `litellm` |
| Version | `1.98.0` |
| Python范围 | `>=3.10, <3.15` |
| Wheel SHA-256 | `150993180bf049feafa3e20cf46ca0978c69cc66e2ef1639a47e852c682a4721` |
| 安装到当前`.venv` | 否 |

wheel只下载到被Git忽略的 `temp/litellm-1.98.0-spike/`，没有污染当前生产工具环境。

Proxy extra元数据包含：

- `azure-identity>=1.25.2,<2.0`；
- `cryptography>=49.0.0,<51.0`；
- Prisma位于 extra-proxy依赖；
- Redis高级能力依赖Redis客户端及相关包。

### 3.2 官方镜像

| 项目 | 结果 |
| --- | --- |
| 镜像 | `docker.litellm.ai/berriai/litellm:1.98.0` |
| RepoDigest | `sha256:20b5044b619055374061a6d5b7b08754cad75aeabbf82ddf4f69cc0cf80ddaf4` |
| 平台 | `linux/amd64` |
| 暴露端口 | `4000/tcp` |
| 镜像默认User | root |
| 实际非Root运行 | UID `10001`成功 |
| 只读根文件系统 | 成功 |
| `/tmp` tmpfs | 成功 |
| liveliness | HTTP 200 |
| 权限/只读文件系统错误 | 0 |

结论：镜像元数据默认root是安全差距，但Kubernetes `securityContext`可以强制UID `10001`和只读根文件系统。本地无数据库最小配置已证明这一运行方式可启动；带数据库、迁移、回调和证书时仍需新环境验证必要可写路径。

## 4. Router与Affinity Spike

使用两个合成Azure deployment、无真实凭据和无生产网络的配置进行启动验证：

- `routing_strategy: usage-based-routing-v2`；
- `routing_strategy_args.ttl: 60`；
- `enforce_model_rate_limits`；
- `model_group_affinity_config`；
- `encrypted_content_affinity`；
- `responses_api_deployment_check`；
- `session_affinity`；
- `deployment_affinity`；
- `deployment_affinity_ttl_seconds: 3600`。

结果：

- 容器liveliness HTTP 200；
- 未发现未知affinity flag、路由字段验证或配置加载错误；
- 非Root、只读根文件系统下仍可启动。

源码证据：

- `DeploymentAffinityCheck`支持模型组配置；
- 有效flag集合包含上述4种affinity；
- Responses、session和API Key affinity统一处理；
- `previous_response_id`优先级高于session和API Key；
- `EncryptedContentAffinityCheck`独立运行并可按模型组启用；
- affinity使用Redis原子claim逻辑。

限制：配置成功不等于容量和故障行为通过。真实Redis、两Pod、多Foundry资源和Codex多轮A/B仍是新环境门禁。

## 5. PostgreSQL兼容性 Spike

### 5.1 Prisma URL与TLS

候选镜像使用包含以下参数的占位PostgreSQL URL执行离线验证：

- `sslmode=verify-full`；
- `connect_timeout=5`。

结果：

- URL格式可解析；
- 镜像包含Prisma CLI；
- 候选 `schema.prisma` 验证成功。

该结果只证明配置和schema语法可接受，不证明Flexible Server CA、DNS、连接池、故障转移或Token刷新已通过。

### 5.2 Schema差异

| 项目 | `1.95.0` | `1.98.0` |
| --- | ---: | ---: |
| Prisma models | 68 | 72 |

`1.98.0`新增：

- `LiteLLM_AutoRouterSession`；
- `LiteLLM_DailyGatewayRequests`；
- `LiteLLM_ShadowEvalAttempt`；
- `LiteLLM_ShadowEvalJob`。

没有发现被删除的model，但完整schema hash已经变化。因此：

- 不能直接让生产 `1.98.0`连接当前数据库并自动迁移；
- 必须从阶段0 dump恢复隔离数据库；
- 记录migration SQL和前后schema；
- 验证全部关键对象、密文和回退；
- schema回退不能仅靠降级镜像，必要时恢复迁移前数据库。

### 5.3 PostgreSQL Entra认证

候选代码以单一 `DATABASE_URL`供Prisma使用。本轮没有证据证明LiteLLM/Prisma会为Azure PostgreSQL连接池持续刷新Entra Token。

冻结结论：

- `sslmode=verify-full`继续作为硬要求；
- Entra认证在Flexible Server中实测；
- 如果Token刷新/连接池不兼容，首期使用Key Vault托管的高强度数据库密码；
- 不允许将密码散落在Git、脚本或命令历史中；
- 记录例外期限并在后续版本复测Entra认证。

## 6. Azure Managed Redis兼容性 Spike

`1.98.0`包含 `AzureADCredentialProvider`：

- 使用Azure SDK credential获取Redis scope Token；
- 每次凭据请求调用credential，继承Azure SDK缓存和静默刷新；
- 配置username时返回username和Token；
- 避免把静态Token固化在连接池；
- 原始client ID/tenant ID/secret在生成连接函数后从Redis kwargs移除。

使用合成Credential执行单元式验证：

- username保持：通过；
- 连续两次凭据请求重新调用credential并获得不同测试Token：通过。

仍待新环境验证：

- AKS Workload Identity首次认证；
- Managed Redis数据访问策略与Object ID username；
- TLS、证书、Private DNS和端口；
- Token过期后的连接池重连；
- Planned maintenance/failover；
- Redis不可用时跨Pod affinity、auth cache和预算一致性。

在上述项目通过前，Redis不得成为硬预算或跨Pod认证一致性的唯一权威状态。

## 7. Workload Identity Spike状态

当前AKS已启用OIDC issuer和Workload Identity平台能力，但生产LiteLLM仍使用节点VMSS业务UAMI。阶段1没有移除该身份。

本轮不在旧集群创建新的Federated Identity Credential，原因：

- 目标路径是新Private AKS；
- 旧环境继续作为回退基线；
- 在同一身份上原地实验会扩大变更面；
- 新环境需要同时验证ServiceAccount、Federation、Private Endpoint和最小模型RBAC。

该Spike转阶段4新环境执行，必须遵循“先Pod身份成功，再移除节点身份”。

## 8. Entra认证、管理SSO与产品边界

官方JWT文档明确标注JWT-based Auth属于LiteLLM Enterprise。该能力因此从本方案排除，不进入采购建议、配置模板或生产Gate。

当前环境已验证：

- Microsoft SSO登录链路；
- 正确callback；
- App Role定义。

由于微软内部租户权限限制，当前环境不能完成：

- Enterprise Application显式App Role assignment；
- `roles` claim验证；
- Conditional Access/MFA；
- 普通用户负向权限测试。

客户上线前必须确认：

- 客户自有Entra认证代理在Chat、Responses HTTP/WebSocket、SSE、Embeddings、Files和MCP/Tools上的统一认证；
- issuer、audience、tenant、signature、expiry、roles/scopes；
- App Role、Team、模型ACL、预算和内部受限Virtual Key映射；
- 管理路由与团队路由负向测试；
- 客户伪造身份/内部凭据Header会被代理删除；
- 如任一协议不能由代理统一授权，则禁用该协议，不回退到LiteLLM付费JWT或弱认证。

## 9. Guardrail边界

官方文档和 `1.98.0`源码确认：

- OSS可定义并调用基础Guardrail；
- `pre_call`、`post_call`、`during_call`和 `logging_only`可配置；
- 部分按Key、模型、标签、动态参数和防用户关闭等治理控制属于Enterprise，均不进入本方案；
- system/tool message skip统一路径主要覆盖Chat和Anthropic Messages；
- 文档明确指出其他direct hook以及Responses、embeddings、speech等路由不能由该行为自动推断。

因此本轮不把本地关键词过滤或配置存在当作企业Guardrail已完成。新环境必须构建协议证明矩阵，并按已冻结策略执行：

- 高敏请求Guardrail故障时fail-closed；
- 普通低风险请求可fail-open，但必须告警和记录审计缺口；
- 至少中文、英文；
- 无法执行目标Guardrail的协议必须禁用、限制低风险数据或有书面补偿控制。

Presidio依赖服务和Azure AI Content Safety尚未建设，真实Spike转阶段8或相关平台工作包。

## 10. L3短期审计试点

冻结结论：

- L1元数据默认全量；
- L2脱敏标签/摘要按批准范围；
- L3默认关闭，只做受控7天试点；
- L3不长期写入PostgreSQL；
- 目标为私有Blob/ADLS、CMK、独立RBAC/PIM和访问日志；
- 员工告知、Legal Hold、导出和访问审批由客户治理团队批准；
- 不用于默认个人绩效排名。

真实callback、Responses/WebSocket完整性、流式去重、失败告警和retention删除演练等待独立审计存储及客户治理批准。

## 11. Spike完成矩阵

| Spike | 当前结果 | 是否允许生产上线 |
| --- | --- | --- |
| `1.98.0` wheel/镜像真实性 | 通过 | 否，仅候选 |
| 非Root+只读根文件系统 | 最小配置通过 | 否，需完整配置验证 |
| 目标Router字段解析 | 通过 | 否，需Redis/双Pod/A-B |
| Affinity源码行为 | 通过静态审计 | 否，需真实故障测试 |
| Prisma `verify-full` URL | 离线通过 | 否，需Flexible Server |
| `1.95.0 -> 1.98.0` schema | 确认有变化 | 否，需隔离迁移/回退 |
| PostgreSQL Entra | 未证明持续刷新 | 否 |
| Redis Entra Provider | 合成Token刷新通过 | 否，需Managed Redis |
| Workload Identity | 当前集群能力存在 | 否，需新Pod/私网模型 |
| 数据面Entra认证 | LiteLLM Enterprise JWT已排除 | 否，需客户自有认证代理和客户租户验证 |
| Guardrail | 基础/高级边界确认 | 否，需协议矩阵 |
| 审计callback | 设计冻结 | 否，需L3试点 |

## 12. 阶段Gate与下一步

### 12.1 已满足

- D01-D22均有结论或明确客户阻塞；
- 候选版本、镜像digest和Python范围已知；
- 目标Router配置在候选版本可解析；
- 数据服务主要风险和fallback已经明确；
- LiteLLM Enterprise JWT及高级Guardrail已排除，替代路径进入实现和验收要求；
- 所有未完成Spike都有后续阶段和上线门禁。

### 12.2 仍然禁止

- 不升级当前生产到 `1.98.0`；
- 不让候选版本直接迁移生产数据库；
- 不切换当前Router；
- 不在无共享Redis的双Pod环境宣称跨Pod affinity；
- 不因离线URL验证而宣称PostgreSQL Entra或TLS已端到端通过；
- 不因源码存在而宣称认证、Guardrail或审计协议覆盖完成，也不得启用LiteLLM Enterprise配置键。

### 12.3 进入阶段3

阶段3可以启动：

1. 将Azure平台拆成Bicep模块；
2. 建立dev/test/prod参数边界；
3. 建立ACR Premium、digest、SBOM、签名和扫描；
4. 建立Helm/Kustomize应用层；
5. 建立PR/What-if/审批和drift detection；
6. 为阶段4/5的Private AKS、Key Vault、Flexible Server和Managed Redis准备IaC。

云集成Spike将在新环境资源创建后继续，不改变本文件的“候选而非生产批准”结论。
