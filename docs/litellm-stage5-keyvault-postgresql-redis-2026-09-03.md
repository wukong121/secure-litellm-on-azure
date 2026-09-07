# LiteLLM 安全增强阶段 5：Key Vault、PostgreSQL Flexible Server 与 Azure Managed Redis

> 文档状态：设计、IaC和本地静态验证完成；云资源尚未部署  
> 完成日期：2026-09-03  
> 前置条件：阶段4代码Gate通过，阶段4云资源仍未部署  
> 目标：建立不含明文凭据、私网可达、可恢复且支持多副本的状态服务目标代码  
> 明确边界：不创建或修改APIM；不修改当前生产AKS/VMSS UAMI；不迁移生产数据；不向Git/Bicep写入Secret；不部署高成本资源

## 1. 执行摘要

阶段5代码改造已完成：

- Key Vault Premium、RBAC、90天Soft Delete、Purge Protection、删除锁、诊断和Private Endpoint；
- PostgreSQL Flexible Server 16、Entra-only、Private Endpoint、128 GiB自动扩容、14天PITR和诊断；
- Azure Managed Redis Balanced B0、HA、TLS 1.2、AOF、禁用Access Key、Entra访问策略、Private Endpoint和诊断；
- Key Vault、PostgreSQL和Redis Private DNS Zone及VNet Link；
- AKS Key Vault Secrets Provider addon和5分钟自动轮换；
- Stage5 Kustomize组件、Secrets Store CSI、Redis Entra/TLS配置；
- Stage5静态安全、Bicep编译、Kustomize渲染和仓库Secret扫描门禁。

所有环境保持`deployStage4=false`和`deployStage5=false`。阶段5只有两个开关同时为`true`才会创建资源，因此当前生产和Azure资源均未改变。

启用Stage4/5的最终What-if成功：Create 42、Modify 0、Delete 0、Ignore 54、Unsupported 2。两项Unsupported是运行时才能解析Workload Identity principal ID的Key Vault RBAC和Redis数据访问策略，不是对现有资源的变更。现有VNet、Private Endpoint subnet、Blob Private Endpoint、APIM、当前AKS和VMSS身份均无Modify/Delete。

## 2. 关键架构决策

### 2.1 PostgreSQL使用Private Link而非VNet Integration

目标Server以非VNet注入模式创建，禁用Public Network Access，再通过Private Endpoint接入现有`snet-private-endpoints`。不同时配置delegated subnet，避免混用两种互斥网络模型。

Private Link配置：

- Group ID：`postgresqlServer`；
- Private DNS：`privatelink.postgres.database.azure.com`；
- 应用始终连接服务FQDN，不连接Private Link别名或IP；
- 最终`DATABASE_URL`必须设置`sslmode=verify-full`。

### 2.2 PostgreSQL采用Entra-only控制面引导

IaC不接收管理员密码。Server配置：

- `activeDirectoryAuth=Enabled`；
- `passwordAuth=Disabled`；
- Entra管理员对象ID、显示名和类型只允许从受保护部署变量注入；
- committed参数中的空值仅允许安全编译，不能作为可用部署配置。

LiteLLM/Prisma的长期应用身份兼容性仍需实测。受控引导流程应由DBA Entra管理员创建最小权限应用角色和`litellm`数据库对象。如果目标LiteLLM不能可靠刷新PostgreSQL Entra Token，可为应用角色生成随机密码，将完整`DATABASE_URL`直接写入Key Vault；密码不得进入Bicep参数、输出、终端历史、GitHub日志或聊天记录。

### 2.3 Redis采用Entra认证和无Access Key模式

Azure Managed Redis配置：

- API：`Microsoft.Cache/redisEnterprise@2025-07-01`；
- SKU：`Balanced_B0`；
- HA：Enabled；
- TLS最低版本：1.2；
- Public Network Access：Disabled；
- Database：`default`、NoCluster、端口10000、AllKeysLRU、AOF每秒；
- `accessKeysAuthentication=Disabled`；
- LiteLLM Workload Identity通过`accessPolicyAssignments`获得默认数据访问策略。

Private Link配置：

- Group ID：`redisEnterprise`；
- Private DNS：`privatelink.redis.azure.net`；
- 应用连接`<name>.<region>.redis.azure.net:10000`并执行TLS主机名校验。

LiteLLM 1.98.0配置使用Workload Identity Object ID作为`REDIS_USERNAME`，并启用`azure_redis_ad_token`。正式启用前必须实测初次Token、到期前刷新、连接池重认证、planned maintenance和failover重连。

## 3. Key Vault与Secret边界

Key Vault目标配置：

- Premium；
- Azure RBAC；
- Public Network Access Disabled；
- Network ACL默认Deny且不允许Azure Services绕过；
- Soft Delete 90天；
- Purge Protection；
- CanNotDelete锁；
- AuditEvent和AllMetrics进入Log Analytics；
- LiteLLM Workload Identity仅获得vault范围`Key Vault Secrets User`。

IaC不创建任何Secret值。部署后由受控、无日志引导流程创建：

| Key Vault Secret名称 | 用途 |
| --- | --- |
| `litellm-master-key` | LiteLLM Master Key |
| `litellm-salt-key` | 永久Salt，沿用阶段2 Path A且不得轮换丢失 |
| `litellm-database-url` | 完整PostgreSQL TLS连接串 |

Salt迁移必须先备份、校验和双环境验证。不得生成新Salt覆盖现有值，否则历史数据库密文可能不可解密。

当前资源组已存在Key Vault和PostgreSQL Private DNS Zone。阶段5以共享基础设施方式复用它们，只创建缺失的VNet Link，不接管或修改其Tags；Azure Managed Redis Zone当前缺失，由阶段5创建。该处理将初次What-if中的2项DNS Tag Modify降为0。若部署到全新资源组，必须显式启用环境入口中的对应`createStage5*PrivateDnsZone`参数，或先由中心网络团队建立Zone。

## 4. CSI与Kubernetes Secret补偿控制

Private AKS启用Azure Key Vault Secrets Provider和5分钟轮询。Stage5组件使用Workload Identity访问Key Vault，Pod只读挂载CSI卷。

LiteLLM当前仍以环境变量读取Master Key、Salt和`DATABASE_URL`，因此组件通过`secretObjects`逐项同步到`litellm-runtime-secrets`。该设计：

- 不使用`envFrom`；
- 只同步3个必要值；
- 不同步Redis凭据，因为Redis禁用Access Key；
- 不表示Secret“不落etcd”。

部署前必须启用/确认AKS etcd静态加密、namespace最小RBAC、禁止普通开发者读取Secret、审计Secret访问，并实测轮换后Pod滚动策略。若后续LiteLLM支持稳定的文件型Secret消费，应移除Kubernetes Secret同步。

## 5. 数据迁移与引导顺序

本阶段不执行迁移。正式顺序：

1. 部署Stage4/5新资源但不切流；
2. 注入独立DBA Entra管理员并验证私网管理路径；
3. 创建最小权限LiteLLM应用角色和数据库；
4. 将永久Salt、Master Key和`DATABASE_URL`写入Key Vault；
5. 从隔离Pod验证Key Vault CSI、PostgreSQL `verify-full`和Redis Entra；
6. 对空库运行LiteLLM/Prisma migration；
7. 执行`pg_dump/pg_restore` dry run并核对schema、行数、Virtual Key、Team、预算、SSO和数据库密文；
8. 新LiteLLM双Pod运行只读/影子验证；
9. 维护窗口内执行最终增量迁移和切流；
10. 旧PostgreSQL进入只读保护窗口，不立即删除；
11. 完成PITR隔离恢复、Redis故障、PG重连和Salt解密验收后再结束回退窗口。

## 6. 安全与故障验收

### Key Vault

- 未授权Pod和身份读取Secret失败；
- Workload Identity只可读取Secret，不可写入、删除或管理Vault；
- Private DNS解析到`10.30.8.0/24`；
- 公网路径失败；
- 轮换和Pod重启不会改变永久Salt。

### PostgreSQL

- DNS只解析Private Endpoint；
- `sslmode=verify-full`成功，禁用证书校验时测试不计为通过；
- 公网连接失败；
- 空库migration和目标数据恢复通过；
- PITR恢复到隔离Server并完成数据校验；
- HA模式须按区域能力和RTO/RPO单独批准。当前West US默认`Disabled`，不得宣称已具备HA。

### Redis

- Access Key不可用；
- Entra Token首次认证、刷新和连接池重认证通过；
- TLS证书链和主机名校验通过；
- 双Pod的限流、冷却、affinity和适用auth cache一致；
- Redis不可用时不会绕过认证、租户隔离或硬预算；
- Redis中不保存Prompt/Response正文、完整Key或PII。

## 7. 部署前Gate

以下全部完成前保持`deployStage5=false`：

1. 阶段4网络和Private AKS部署获批；
2. PostgreSQL Entra管理员由客户提供并通过受保护变量注入；
3. West US PostgreSQL SKU、HA模式和Redis Balanced B0配额/成本批准；
4. Key Vault恢复、Purge Protection和删除锁运维流程批准；
5. Secret无日志引导和Salt保全流程演练完成；
6. 私网runner或管理跳板可解析并访问三个Private Endpoint；
7. LiteLLM 1.98.0 PostgreSQL/Redis协议兼容性实测通过；
8. NetworkPolicy、Firewall FQDN和端口与实际流量一致；
9. What-if再次证明现有VNet、Blob PE、当前AKS、APIM和VMSS UAMI无Modify/Delete；
10. 数据迁移、只读回退、PITR和故障注入窗口获批。

## 8. 阶段状态

阶段5的设计、目标代码、本地Gate和Azure What-if已完成，但Azure资源、Kubernetes组件、Secret和数据迁移均未部署。准确状态是：

> Key Vault、PostgreSQL Flexible Server和Azure Managed Redis的私网、身份、诊断与应用接入代码已建立；What-if证明现有资源零Modify/Delete；默认开关关闭，等待成本审批和协议实测后部署。
