> 发布说明：本文是去标识化的参考环境历史记录；资源名/Owner为示例，不是客户参数或客户已通过的验收。实际客户请从[迁移指南](customer-migration-guide-zh.md)重新盘点和备份。

# LiteLLM 安全增强阶段 0 基线与证据记录

> 文档状态：阶段 0 已完成（含一项依赖新 Private AKS 的延期验证）  
> 采集日期：2026-09-01  
> 对应路线：`docs/litellm-security-hardening-implementation-roadmap-zh.md`  
> 范围：现状冻结、数据库备份与恢复验证、密钥/密文基线、镜像与配置基线、功能与性能基线  
> 安全说明：本文不包含订阅 ID、租户 ID、资源 ID、IP、Endpoint、Token、Key、数据库密码、连接串或 Prompt/Response 原文

## 1. 执行摘要

阶段 0 的技术采集和核心验证已完成。当前结论如下：

- Azure 登录有效，目标 AKS 控制面可访问；
- AKS 两个节点均为 Ready，LiteLLM 和 PostgreSQL 各一个副本且当前 Ready；
- PostgreSQL 当前可接受连接，PVC 为 `100Gi`，文件系统和 inode 使用率均低于 `1%`；
- 已创建新的 PostgreSQL 自定义格式逻辑备份，`pg_restore` 目录检查和临时数据库完整恢复均成功；
- 临时恢复数据库已在验证后删除，Pod 内临时 dump 已清理；
- LiteLLM `1.95.0` 的健康端点、受保护模型配置接口、公网模型列表和最小 Chat 推理均返回 HTTP 200；
- 真实 Codex 10 轮 affinity 基准完成 10/10，后端切换为 0；
- 当前没有独立的 `LITELLM_SALT_KEY`，但已有数据库模型配置当前仍可读取；这是后续变更前必须关闭的高风险项；
- 当前 LiteLLM 仍使用默认 ServiceAccount、节点 VMSS 业务 UAMI、公网 ingress、单副本和整份 Secret `env_from` 注入；
- 本次没有修改任何 Azure/Kubernetes 资源，也没有读取或输出 Secret 值；唯一集群侧临时写入是备份恢复验证数据库，验证后已删除。

阶段 0 当前状态：**已完成并允许进入阶段 1**。正式备份存储基础设施已经部署；阶段 0 dump 的私网上传、下载及恢复验证经 Owner批准延期到新 Private AKS 建成后执行，该延期不阻塞现有环境紧急加固。Salt 迁移已选择路径 A。

## 2. Git 与仓库基线

### 2.1 Git 状态

| 项目 | 结果 |
| --- | --- |
| 当前分支 | `main` |
| 当前 HEAD | `85a5536 chore: keep superpowers documents local` |
| 相对 `origin/main` 的领先提交 | `0` |
| 已跟踪修改 | `.gitignore` |
| 未跟踪文档 | `docs/litellm-security-hardening-implementation-roadmap-zh.md`、本文 |
| 本地忽略内容 | `.venv/`、`temp/`、Python cache 等 |

说明：`.gitignore` 的变更用于允许安全实施路线和阶段 0 脱敏证据文档进入 Git；`temp/` 和 `*.dump` 继续保持忽略。

### 2.2 本地配置文件

采集时以下本地忽略配置不存在：

- `LiteLLM/litellm.config.yaml`；
- `LiteLLM/azure-openai.loc.json`。

集群中存在 `litellm-config` ConfigMap，其数据键为 `config.yaml`。本次只记录名称和键名，没有将可能包含内部配置的数据复制到本文。

## 3. Azure 与 AKS 基线

### 3.1 Azure 范围

| 项目 | 当前值 |
| --- | --- |
| 当前订阅状态 | `Enabled` |
| 目标资源组 | `rg-example-legacy` |
| AKS | `litellm-mi-aks` |
| 区域 | `westus` |
| Kubernetes 版本 | `1.35`（Server `v1.35.6`） |

订阅 ID、Tenant ID、资源 ID 和 Endpoint 未记录。

目标资源组内还存在 APIM、Container Apps、Flexible Server、Key Vault、Private Endpoint 等其他资源。它们的存在不代表已经纳入 LiteLLM 安全目标架构，也没有证据证明现有 Flexible Server/Key Vault 属于本次 LiteLLM 生产改造。本次未创建、修改或调用任何 APIM 资源。

### 3.2 AKS 安全与网络

| 控制项 | 当前状态 |
| --- | --- |
| Private Cluster | 否 |
| Local Accounts 禁用 | 否 |
| Entra Azure RBAC for Kubernetes | 否 |
| OIDC Issuer | 已启用 |
| Workload Identity 集群能力 | 已启用 |
| LiteLLM Pod 实际使用 Workload Identity | 否 |
| Network Plugin | Azure CNI Overlay |
| Network Policy | 未启用 |
| Outbound Type | LoadBalancer |
| Defender | 已启用 |
| 节点池 | 单个 System Pool |
| 可用区 | 未配置 |

集群已有 Workload Identity 平台能力，但 LiteLLM Deployment 使用默认 ServiceAccount，未设置 Workload Identity Pod 标签，因此不能将“集群已启用 Workload Identity”描述为“LiteLLM 已迁移到 Workload Identity”。

### 3.3 节点状态

| 数量 | Ready | VM SKU | Kubernetes 版本 |
| ---: | ---: | --- | --- |
| 2 | 2 | `Standard_D2s_v3` | `v1.35.6` |

节点 IP 未记录。

### 3.4 身份现状

- LiteLLM 当前 UAMI 名称：`litellm-managed-identity`；
- 节点 VMSS 仍附加该业务 UAMI；
- 节点 VMSS 同时附加 AKS agent pool 身份；
- LiteLLM Deployment 使用默认 ServiceAccount；
- LiteLLM Pod 未设置 `azure.workload.identity/use: "true"`。

结论：当前上游模型身份仍属于节点级 Managed Identity 模式。SEC-06 必须遵循“先验证 Pod Workload Identity，再移除 VMSS 业务 UAMI”的顺序。

## 4. Kubernetes 工作负载基线

### 4.1 核心工作负载

| 工作负载 | 副本 | Ready | 镜像 |
| --- | ---: | ---: | --- |
| `litellm-mi-proxy` | 1 | 1 | `litellm:1.95.0` |
| `postgres` | 1 | 1 | `postgres:16-alpine` |
| ingress-nginx controller | 1 | 1 | 已运行 |
| cert-manager | 3 个组件 | 全部 Ready | 已运行 |

### 4.2 镜像摘要

| 组件 | 当前运行摘要 |
| --- | --- |
| LiteLLM | `sha256:af806882b7a6ced41658db5b6a7e98ed7b9b51d03b935e0417bf1c8552d688af` |
| PostgreSQL | `sha256:cf78e76683b9ca8c5733cbbdce6c9262b45b6767934dd0a95e671f9a0fc20685` |

当前 Deployment 仍按 Tag 声明镜像，运行时 image ID 可确定上述 digest。后续 SEC-17 应将 Deployment 显式固定到批准 digest。

### 4.3 LiteLLM Pod 控制

| 控制项 | 当前状态 |
| --- | --- |
| 副本 | 1 |
| ServiceAccount | default |
| startup probe | 无 |
| readiness probe | 无 |
| liveness probe | 无 |
| CPU/内存 requests/limits | 无 |
| Pod/Container securityContext | 未配置 |
| Secret 注入 | `env_from` 引用整份 `litellm-env` |
| HPA | 无 |
| LiteLLM namespace PDB | 无 |
| LiteLLM namespace NetworkPolicy | 无 |
| ResourceQuota/LimitRange | 无 |

集群其他 namespace 存在少量 NetworkPolicy/PDB，但 `litellm` namespace 内没有这些控制，不能作为 LiteLLM 已受保护的证据。

### 4.4 PostgreSQL Pod 控制

| 控制项 | 当前状态 |
| --- | --- |
| 副本 | 1 |
| ServiceAccount | default |
| probes | startup/readiness/liveness 均无 |
| requests | CPU `100m`、内存 `128Mi` |
| limits | CPU `500m`、内存 `256Mi` |
| securityContext | 未配置 |

### 4.5 入口现状

| 项目 | 当前状态 |
| --- | --- |
| ingress controller Service | 公网 `LoadBalancer` |
| Internal Load Balancer 注解 | 未设置 |
| 对外端口 | 80/443 |
| LiteLLM Service | `ClusterIP:4000` |
| PostgreSQL Service | `ClusterIP:5432` |

结论：当前公网流量可以直接进入 ingress-nginx，尚未达到 SEC-03 的 WAF 唯一入口和私有源站要求。

### 4.6 事件

采集时全 namespace 没有 Warning Event。该结果只代表采集时刻，不替代持续监控。

## 5. PostgreSQL 容量、备份与恢复证据

### 5.1 当前可用性与容量

| 项目 | 结果 |
| --- | --- |
| `pg_isready` | accepting connections |
| PVC | `pg-data` |
| PVC 状态 | Bound |
| 请求容量 | `100Gi` |
| 实际容量 | `100Gi` |
| Access Mode | `ReadWriteOnce` |
| StorageClass | `default` |
| 文件系统已用 | 约 `67.8MiB` |
| 文件系统可用 | 约 `98.3GiB` |
| inode 已用 | `1,730 / 6,553,600` |

当前容量健康，但 PostgreSQL 仍是集群内单副本，不能据此关闭 SEC-08/SEC-19。

### 5.2 逻辑备份

| 项目 | 结果 |
| --- | --- |
| 格式 | PostgreSQL Custom Format (`pg_dump -Fc`) |
| 本地位置 | `temp/litellm-stage0-20260901T082757Z.dump` |
| 文件大小 | `291,569` bytes |
| SHA-256 | `dc4d242f7e1f00b3842f07e5e6b1ea3be05059efb73dfd824d99099170fd3939` |
| Git 状态 | 被 `temp/` 和 `*.dump` 忽略，不进入 Git |
| `pg_restore -l` | 通过 |

备份文件包含生产配置数据，必须按敏感备份管理，不得通过聊天、Git、普通附件或不受控共享渠道传输。

### 5.3 完整恢复验证

执行方式：

1. 将上述 dump 复制到 PostgreSQL Pod 临时目录；
2. 创建一次性临时数据库；
3. 使用 `pg_restore --exit-on-error` 完整恢复；
4. 验证恢复后的 public schema 表数量；
5. 删除临时数据库及 Pod 内 dump。

结果：

| 项目 | 结果 |
| --- | --- |
| 完整恢复 | 通过 |
| 恢复后的 public 表数量 | 69 |
| 临时恢复数据库 | 已删除 |
| Pod 内临时 dump | 已删除 |

限制：本次在现有 PostgreSQL 实例内使用独立临时数据库验证，不等同于 Flexible Server、跨区域或完全隔离基础设施恢复。SEC-08/SEC-19 仍需在目标 Flexible Server 和隔离恢复环境中重新演练。

### 5.4 正式备份存储基础设施

已通过 `infra/backup-storage/main.bicep` 在 `rg-example-legacy` 部署：

- `litellm-security-vnet`：`10.30.0.0/16`；
- `snet-private-endpoints`：`10.30.8.0/24`；
- Private Endpoint subnet 专用 NSG；
- `Standard_GRS` 专用 Storage Account；
- 私有容器 `litellm-postgresql`；
- Blob Private Endpoint 和 Private DNS；
- Versioning、14 天 Blob/Container Soft Delete；
- 6 条生命周期规则；
- Storage/Blob 诊断设置；
- 示例环境Owner 的 `Storage Account Contributor` 和 `Storage Blob Data Owner`。

验证结果：

| 项目 | 结果 |
| --- | --- |
| ARM deployment | Succeeded |
| VNet/Subnet | 已创建，CIDR符合模板 |
| Storage Public Network Access | Disabled |
| Shared Key | Disabled |
| Blob Public Access | Disabled |
| TLS minimum | TLS 1.2 |
| Private Endpoint | Approved |
| Private DNS VNet Link | Completed |
| 容器 Public Access | None |
| 容器加密范围覆盖 | 禁止，使用账户默认加密范围 |
| Blob Versioning | Enabled |
| Blob/Container Soft Delete | 14 天 |
| 生命周期规则 | 6 条 |
| Owner RBAC | 两项均已分配 |
| Storage/Blob diagnostics | 均已配置 |

部署前不存在同名前缀 Storage Account，因此没有旧 Storage Account需要删除或移动。Azure Storage Account本身不位于 subnet 中；位于 `snet-private-endpoints` 中的是其 Blob Private Endpoint。

当前 AKS和执行终端尚未建立到新 VNet 的批准私网路径，因此阶段 0 dump仍保留在本地忽略目录，尚未上传。不得为绕过该问题临时长期开放 Storage公网或 Shared Key。

## 6. 密钥、Salt 与数据库配置基线

### 6.1 Secret 键名

本次只读取并记录 `litellm-env` 的键名，没有读取或输出值：

- `AZURE_API_VERSION`；
- `AZURE_CLIENT_ID`；
- `AZURE_CREDENTIAL`；
- `AZURE_SCOPE`；
- `DATABASE_URL`；
- `LITELLM_MASTER_KEY`；
- `STORE_MODEL_IN_DB`。

关键发现：**不存在独立的 `LITELLM_SALT_KEY` 键**。

### 6.2 数据库关键对象数量

| 对象 | 数量 |
| --- | ---: |
| Models | 5 |
| Teams | 0 |
| Users | 4 |
| Budgets | 0 |
| Guardrails | 1 |
| SSO Configs | 1 |
| Credentials | 0 |

对象数量用于迁移后核对，不包含对象名称、用户标识、Endpoint、凭据或正文。

### 6.3 脱敏可读性验证

| 检查 | 结果 |
| --- | --- |
| Pod 内 liveliness | HTTP 200 |
| Pod 内 readiness | HTTP 200 |
| 使用当前 Master Key 访问受保护 `/model/info` | HTTP 200 |
| 公网 `/v1/models` | HTTP 200，返回 4 个模型别名 |

当前模型别名：

- `gpt-5.4-pro`；
- `gpt-5.6-luna`；
- `gpt-5.6-sol`；
- `gpt-5.6-terra`。

这些结果证明当前运行实例可以读取模型配置，但不能证明任意新 Salt 或 Master Key 能读取旧密文。

### 6.4 风险结论

当前没有稳定、独立的 `LITELLM_SALT_KEY`。在完成 SEC-07 的 Salt/密文迁移方案前：

- 不得轮换或覆盖当前用于数据库密文解密的有效 key；
- 不得直接设置一个新 Salt 后期待旧密文自动恢复；
- 不得重跑可能覆盖生产 Secret 的部署流程；
- 必须保留当前数据库备份和有效解密材料；
- 必须在隔离数据库副本中验证旧 key 到永久 Salt 的迁移或重加密路径。

### 6.5 Salt 迁移决策

2026-09-01，Owner批准选择路径 A：沿用当前有效数据库加密材料作为永久 `LITELLM_SALT_KEY`。当前 Master Key没有已知安全隐患，因此不执行高风险的批量解密/重加密迁移。

批准的实施顺序：

1. 当前环境继续保留有效 Master Key，不直接新增或切换 Salt；
2. 新 Private AKS、Key Vault 和隔离 PostgreSQL就绪后，恢复阶段 0数据库备份；
3. 将当前有效加密材料作为永久 Salt安全导入 Key Vault；
4. 在 LiteLLM `1.95.0` 隔离实例显式设置该 Salt，验证模型、SSO、Guardrail等旧对象；
5. 重启后再次验证对象可读性；
6. Salt保持不变，使用新的测试 Master Key验证两者已经解耦；
7. 使用目标 LiteLLM版本重复兼容性验证；
8. 全部通过后，才安排生产 Salt显式化和 Master Key独立轮换。

若后续发现当前材料存在泄漏、来源不明或合规问题，必须停止路径 A并重新评估重加密迁移。

## 7. 功能与性能基线

### 7.1 基础功能冒烟

| 测试 | 结果 |
| --- | --- |
| 公网 liveliness | HTTP 200 |
| 公网模型注册表 | HTTP 200，4 个模型别名 |
| `gpt-5.6-sol` 最小 Chat 请求 | HTTP 200 |
| Chat 响应结构 | 有 choices、有 usage、无 error |

测试使用合成 Prompt，响应正文未写入本文，临时响应文件已删除。

### 7.2 Codex 10 轮 affinity 基线

| 项目 | 结果 |
| --- | --- |
| Codex 版本 | `0.152.0` |
| 模型 | `gpt-5.6-sol` |
| 路由标签 | affinity |
| 请求负载 | 约 4,096 tokens |
| 完成轮次 | 10/10 |
| 最终轮缓存率 | `89.22%` |
| 最后三轮稳态缓存率 | `88.05%` |
| 聚合缓存率 | `82.79%` |
| Prefix continuity | `100%` |
| Spend Log 后端记录 | 10 |
| 唯一 `model_id` | 1 |
| 后端切换 | 0 |
| 总体结果 | 通过 |

机器可读结果保存在被忽略的本地文件：

`temp/stage0-codex-affinity-2026-09-01.json`

该基线用于后续比较 LiteLLM 目标版本、双副本、共享 Redis 和 `usage-based-routing-v2`。不能使用两轮请求替代此多轮基线。

### 7.3 本地单元测试

执行 `tests.test_litellm_subscription` 共发现 8 个测试：

- 7 个通过；
- 1 个失败；
- 失败原因为测试期望 Azure CLI 命令为 `az`，当前环境实际解析为 `/bin/az`；
- 该失败属于环境路径断言差异，不是本次集群或生产功能失败；
- 本次阶段 0 没有修改测试代码来掩盖该基线差异。

Python 工作区现有 `.venv` 可以导入项目依赖。系统 `/usr/bin/python3.11` 与编辑器报告的包状态不一致，后续验证应明确使用工作区 `.venv/bin/python`，并单独修复解释器选择或测试断言的可移植性。

## 8. 已确认差距与后续入口条件

### 8.1 P0 风险

1. 缺少独立、稳定的 `LITELLM_SALT_KEY`；
2. LiteLLM 与 PostgreSQL 均为单副本；
3. LiteLLM 没有 probes、resources 或 securityContext；
4. LiteLLM 使用 default ServiceAccount 和整份 Secret `env_from`；
5. 节点 VMSS 仍附加 LiteLLM 业务 UAMI；
6. ingress-nginx 仍是公网 LoadBalancer，源站未由 WAF 私有保护；
7. AKS 不是 Private Cluster，local accounts 未禁用，Azure RBAC 未启用；
8. `litellm` namespace 没有 NetworkPolicy、PDB、HPA、ResourceQuota 或 LimitRange；
9. 镜像 Deployment 声明仍使用 Tag，而非固定 digest；
10. 当前 PostgreSQL 是集群内单副本，虽然容量健康且备份可恢复，仍不满足生产 HA/PITR 目标。

### 8.2 进入阶段 1/2 前必须保护的资产

- 当前 PostgreSQL dump 及其校验和；
- 当前有效 Master Key/解密材料；
- 当前 LiteLLM 与 PostgreSQL 镜像 digest；
- 当前数据库关键对象数量；
- 当前 Codex 10 轮结果；
- 当前 Git 工作区中的路线文档和用户已有变更。

### 8.3 尚待治理确认

- 备份存储、专用 `litellm-security-vnet` 和 Private Endpoint 已通过 IaC 部署；尚待企业网络 CIDR最终确认、建立受控私网执行路径、上传阶段 0 dump 及完成 Blob 下载恢复测试；
- Salt 路径已确认采用 A；待新 Private AKS、Key Vault和隔离 PostgreSQL建成后执行兼容性与解耦验证；
- 阶段 0 配置导出的正式受控存储位置；
- 回退 Owner、RTO/RPO 和变更冻结审批流程；
- 是否将本文和实施路线纳入正式 Git 提交。

## 9. 阶段 0 完成判定

| 完成条件 | 状态 | 说明 |
| --- | --- | --- |
| Azure/AKS/入口/身份/工作负载盘点 | 已完成 | 已脱敏记录 |
| PostgreSQL 逻辑备份 | 已完成 | 本地忽略文件 |
| PostgreSQL 完整恢复验证 | 已完成 | 临时数据库恢复成功并已清理 |
| 数据库对象数量基线 | 已完成 | 仅记录计数 |
| 当前模型配置可读性 | 已完成 | 受保护接口 HTTP 200 |
| 当前镜像 digest | 已完成 | LiteLLM 与 PostgreSQL 均已记录 |
| 基础功能冒烟 | 已完成 | 健康、模型列表、Chat 均通过 |
| Codex 10 轮性能基线 | 已完成 | 阶段0 `gpt-5.6-sol`验证缓存连续性；阶段1已用3后端 `gpt-5.6-terra`补充亲和证明 |
| 本地单元测试 | 部分完成 | 7/8；1 个 CLI 路径断言差异 |
| 永久 Salt 决策与迁移 Runbook | 决策完成、实施延期 | 路径 A；新 Private AKS隔离环境就绪后执行验证 |
| 正式备份保管与 Owner | 基础设施已部署、待数据验证 | 专用 VNet、私有 Blob、示例环境Owner Owner和生命周期已生效；dump尚未上传 |
| 变更冻结与回退 Owner | 未完成 | 需业务/平台 Owner 确认 |

阶段 0 Gate结论：上述 Owner/RTO/RPO治理项继续进入阶段 1/2跟踪；阶段 0 dump私网上传验证和 Salt隔离验证均依赖新 Private AKS，已由 Owner接受延期。现有技术基线、逻辑备份、本地恢复、镜像、功能和性能证据足以支持阶段 1的低风险紧急加固。

## 10. 推荐下一步

按路线进入阶段 1 和阶段 2 的准备工作，但在 Salt 风险关闭前不执行生产密钥轮换：

1. 为 D01 至 D22 指定 Owner 和目标决策日期；
2. 优先完成永久 Salt/密文迁移 Runbook；
3. 建立到 `litellm-security-vnet` 的批准私网执行路径，上传阶段 0 dump，并完成访问控制、完整性和恢复验证；
4. 修复或记录本地 Python 解释器与 Azure CLI 路径断言的可移植性问题；
5. 锁定当前 `1.95.0` 镜像 digest，作为正式回退基线；
6. 在非生产环境启动LiteLLM `1.98.0`、Flexible Server、Managed Redis、客户自有Entra认证代理和OSS/Azure Guardrail技术Spike；
7. 在完成阶段 2 决策前，不创建正式生产入口、不迁移生产数据库、不切换 Router、不启用 L3 原文审计。