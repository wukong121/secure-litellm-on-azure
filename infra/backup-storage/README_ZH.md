# LiteLLM PostgreSQL 备份存储部署说明

> 客户模板：不包含任何已部署客户环境的默认身份或资源组。
> 入口：[客户迁移指南](../../docs/customer-migration-guide-zh.md)，阶段0可选backup组件。
> 本模板管理初始专用VNet；阶段4添加子网后禁止重新套用bootstrap模板。不能接管已有共享VNet。

## 1. 目的

本目录提供客户新目标资源组中的 PostgreSQL 逻辑备份存储 IaC。

部署模板位于 `infra/backup-storage/main.bicep`，负责创建：

- 一个专用 `StorageV2` Storage Account；
- 一个 LiteLLM 专用安全 VNet；
- 一个专用 Private Endpoint subnet 及 NSG；
- 私有 Blob 容器 `litellm-postgresql`；
- Blob Private Endpoint；
- Blob Private DNS Zone 及 VNet Link；
- Blob Versioning 和 14 天 Soft Delete；
- 日常、每周、每月、变更前和恢复测试备份的生命周期规则；
- Storage 与 Blob 诊断日志到现有 Log Analytics workspace；
- 指定 Owner 的管理面和数据面 RBAC。

本模板不会创建或修改 APIM，也不会把数据库备份、Secret 或对象 ID写入仓库。

部署过程中首次创建容器时，Azure 要求在启用 `denyEncryptionScopeOverride` 的同时显式设置默认加密范围。模板已设置 `$account-encryption-key`，随后同名增量部署成功；不需要删除或重建 Storage Account。

## 2. 客户环境前提

以下网络值仅为设计示例，Owner、日志工作区和备份主体必须由客户传入：

| 项目 | 默认值 |
| --- | --- |
| 目标资源组 | 客户独立target.resourceGroup |
| Region | 客户批准的区域 |
| VNet | 新建 `litellm-security-vnet` |
| VNet CIDR | `10.30.0.0/16` |
| Private Endpoint subnet | 新建 `snet-private-endpoints` |
| Private Endpoint subnet CIDR | `10.30.8.0/24` |
| Private Endpoint NSG | 新建 `nsg-litellm-private-endpoints` |
| Log Analytics | 客户参数logAnalyticsWorkspaceName |
| Backup Owner | 客户参数backupOwnerUpn及backupOwnerPrincipalId |
| Storage redundancy | `Standard_GRS` |

不要把备份Private Endpoint放入未经批准的其他业务项目VNet，避免生命周期、权限和故障域耦合。本模板新建独立VNet、Private Endpoint subnet和NSG；采用客户Hub/Spoke时需先审查并调整模块。

示例`10.30.0.0/16`没有针对客户订阅、本地网络或Hub完成冲突检查。正式部署前必须由网络Owner确认全部CIDR。

推荐优先级：

1. 本模板新建的 `litellm-security-vnet` 作为 LiteLLM 安全环境的初始 Spoke；
2. 后续 Private AKS、入口和其他平台子网应由独立网络 IaC 模块添加，避免本备份模块承担完整平台职责；
3. 若客户最终要求使用 Hub/Shared Services VNet，应在部署前覆盖网络设计，而不是部署后临时移动 Private Endpoint；
4. 不使用其他业务项目 VNet，也不将 Private Endpoint 放入 AKS node subnet。

当前 AKS `litellm-mi-aks` 位于 AKS 托管资源组中的 VNet。新建的 `litellm-security-vnet` 默认不会自动与当前 AKS VNet建立 Peering，因此现有 AKS 不能自动访问 Blob。直接修改 AKS 托管资源组或把 Private Endpoint 放入节点 subnet 会增加平台耦合，也不作为默认方案。

第一次上传前必须从以下任一批准路径验证 DNS 和 TCP 443：

- 位于 `litellm-security-vnet` 或已批准对等连接网络中的受控 VM/Job；
- 企业 VPN/ExpressRoute 已接入该 VNet 的受控终端；
- 后续新 Private AKS 中具有显式 egress 和 DNS 权限的备份 Job。

不得为了方便首次上传而长期启用 Storage Public Network Access 或 Shared Key。

## 3. Owner 与 RBAC

由客户分别指定Backup Owner、Database Owner、恢复执行人与审批人。IaC不通过邮箱或当前OIDC执行身份推断用户，在部署时显式传入批准用户的Entra object ID。当前模板principalType为User，不支持直接填组或服务主体。

模板为该用户在新 Storage Account 范围分配：

- `Storage Account Contributor`：管理专用 Storage Account；
- `Storage Blob Data Owner`：上传、读取、恢复和删除 Blob 数据。

模板没有授予订阅或资源组级 `Owner`。这可以满足当前备份管理职责，同时避免不必要的权限扩大。

执行部署的身份必须具有创建 Role Assignment 的权限，例如目标范围的 `Owner` 或 `User Access Administrator`。如果仅有 `Contributor`，资源可能创建成功但 RBAC assignment 会失败。

## 4. 安全控制

模板默认启用：

- `publicNetworkAccess: Disabled`；
- `allowBlobPublicAccess: false`；
- `allowSharedKeyAccess: false`；
- Microsoft Entra/OAuth 作为默认认证；
- HTTPS only；
- TLS 1.2 minimum；
- 禁用 SFTP 和 Local User；
- 禁止跨租户对象复制；
- 平台托管密钥和基础设施双重加密；
- `Standard_GRS` 冗余；
- Blob 和 Container Soft Delete 14 天；
- Blob Versioning；
- Private Endpoint 和 Private DNS；
- StorageRead、StorageWrite、StorageDelete 和 Transaction 诊断数据。

当前使用 Microsoft-managed keys。若客户后续强制要求 CMK，应在独立变更中增加 Key Vault/Managed HSM、专用 Managed Identity、密钥轮换和灾难恢复验证，不建议未经演练直接切换。

当前没有启用强制 Immutable/WORM Policy。原因是 Legal Hold、提前删除和保留锁定要求尚未冻结。确认治理要求后，可以在独立工作包中启用不可变版本级存储，避免误锁后无法执行合法删除。

## 5. 保留目录与生命周期

所有 Blob 必须按照以下前缀上传，否则不会命中对应的基础 Blob 删除规则：

| 前缀 | 用途 | 生命周期删除时间 |
| --- | --- | ---: |
| `daily/` | 每日逻辑备份 | 最后修改后 14 天 |
| `weekly/` | 每周逻辑备份 | 最后修改后 56 天（8 周） |
| `monthly/` | 每月逻辑备份 | 最后修改后 365 天（12 个月） |
| `pre-change/` | 重大变更前备份 | 最后修改后 90 天 |
| `restore-test/` | 恢复演练临时副本 | 最后修改后 1 天 |

Blob 旧版本和快照在创建后 14 天进入删除。生命周期删除后，Soft Delete 仍提供 14 天恢复窗口，因此物理清除时间可能晚于表中的生命周期时间。

当前阶段 0 dump 应上传到 `pre-change/`，保留策略为 90 天；本地副本不能在上传、校验和、下载及恢复验证全部通过前删除。

Legal Hold 对象不应依赖以上普通生命周期规则，应在治理批准后使用单独容器或不可变策略管理。

## 6. 部署前验证

部署前确认：

1. Azure CLI 当前订阅正确；
2. 当前部署身份在批准范围内，备份Owner用户object ID经客户核实；
3. `10.30.0.0/16` 与企业 Hub、本地网络、其他订阅及未来 LiteLLM 网络不重叠；
4. `10.30.8.0/24` 保留给 Private Endpoint，不承载 AKS node、Application Gateway 或其他委派服务；
5. VNet、Private Endpoint subnet、NSG 和未来 Peering/路由设计已经过网络 Owner批准；
6. 部署身份具有资源创建和 Role Assignment 权限；
7. Storage Account 默认名称在全局可用；
8. 已确定用于上传和恢复测试的私网执行位置。

模板中的 Storage Account 默认名称由当前 subscription 和 resource group 确定性生成。模板不会输出或保存 subscription ID。

## 7. What-if 与部署

推荐由Customer staged migration workflow生成参数和What-if。手动运行时，从客户配置管理系统注入以下非秘密变量，不能用当前OIDC主体代替备份Owner：

```bash
: "${TARGET_RESOURCE_GROUP:?customer target resource group required}"
: "${LOG_ANALYTICS_WORKSPACE_NAME:?customer workspace required}"
: "${BACKUP_OWNER_OBJECT_ID:?approved user object ID required}"
: "${OWNER_EMAIL:?customer owner email required}"
```

先执行编译和 What-if：

```bash
az bicep build --file infra/backup-storage/main.bicep

az deployment group what-if \
  --resource-group "$TARGET_RESOURCE_GROUP" \
  --template-file infra/backup-storage/main.bicep \
  --parameters backupOwnerPrincipalId="$BACKUP_OWNER_OBJECT_ID" \
    backupOwnerUpn="$OWNER_EMAIL" logAnalyticsWorkspaceName="$LOG_ANALYTICS_WORKSPACE_NAME"
```

审查 What-if，确认只创建计划中的 Storage、Blob、Private DNS、Private Endpoint、Diagnostic Settings 和两个 Storage scope Role Assignment。确认无误后部署：

```bash
az deployment group create \
  --name litellm-pg-backup-storage \
  --resource-group "$TARGET_RESOURCE_GROUP" \
  --template-file infra/backup-storage/main.bicep \
  --parameters backupOwnerPrincipalId="$BACKUP_OWNER_OBJECT_ID" \
    backupOwnerUpn="$OWNER_EMAIL" logAnalyticsWorkspaceName="$LOG_ANALYTICS_WORKSPACE_NAME"
```

完成后清理 shell 变量：

上述短例仍使用模板示例CIDR，只适合已批准完全相同网络设计的首次创建。客户一般应使用迁移入口生成的完整参数；任何Modify/Delete或共享资源接管须停止复核，不重复应用到已扩展的VNet。

```bash
unset BACKUP_OWNER_OBJECT_ID
```

不要将 object ID、Token、Storage Key 或数据库凭据写入参数文件或 Git。

## 8. 阶段 0 dump 上传

只允许从可以解析并访问 Storage Blob Private Endpoint 的受控执行环境上传。

先从部署输出或 Azure Resource Graph 获取 Storage Account 名称，并使用 Entra 登录，不使用 Account Key。以下 `$STORAGE_ACCOUNT` 只应在当前 shell 中赋值：

```bash
az storage blob upload \
  --auth-mode login \
  --account-name "$STORAGE_ACCOUNT" \
  --container-name litellm-postgresql \
  --name "pre-change/$BACKUP_OBJECT_NAME" \
  --file "$BACKUP_FILE" \
  --overwrite false
```

上传后至少验证：

1. Blob存在且大小与客户本次备份一致；
2. 下载副本SHA-256与本次备份前记录的本地SHA-256一致，校验值保存到客户私有证据系统；
3. 使用下载副本执行 `pg_restore -l`；
4. 在隔离数据库执行完整 `pg_restore --exit-on-error`；
5. 恢复后的 public schema 表数量和阶段 0 基线一致；
6. 验证完成后删除恢复测试临时数据库和本地下载副本。

不得将上述 SHA-256 当作加密或身份认证手段；它只用于完整性比对。

## 9. 运行责任

客户应书面指定并分离以下责任：

| 责任 | Owner |
| --- | --- |
| Backup Owner | 客户备份保管人 |
| Database Owner | 客户数据库Owner |
| Restore Operator | 批准的恢复执行人 |
| LiteLLM 恢复验收 | 客户业务验收人 |
| 保留期审批 | 客户合规审批人 |
| 访问审批 | 与申请人分离的审批者 |

避免单人同时拥有备份读取、策略修改和永久删除能力；本模板集中给一个用户的Storage scope权限需要客户审查，不能代替职责分离设计。

## 10. 验收标准

- Bicep 编译无 Error/Warning；
- What-if 仅包含预期资源；
- Storage Public Network Access 和 Shared Key 均禁用；
- Blob 匿名访问禁用；
- Private Endpoint 为 Approved；
- 私网 DNS 将 Storage Blob 名称解析到私有地址；
- 客户批准的备份用户具备经过审查的Storage scope权限；
- 非授权身份无法列出、上传、下载或删除 Blob；
- 五类前缀的生命周期规则存在；
- Versioning 和 14 天 Soft Delete 生效；
- Storage/Blob 访问日志进入指定 Log Analytics workspace；
- 阶段 0 dump 完整性与恢复验证通过；
- 本地备份只在正式 Blob 备份及恢复证据通过后按审批删除。

## 11. 已知边界

- 本模板不创建自动 `pg_dump` Job；它只创建安全存储目标；
- 本模板不移动当前本地 dump，需在私网连通后单独上传；
- 本模板创建 LiteLLM 专用 VNet和 Private Endpoint subnet，但不创建新 AKS、VNet Peering、VPN、Firewall或完整生产网络；
- 本模板不启用 CMK、Immutable Blob 或 Legal Hold；
- 本模板不替代 PostgreSQL Flexible Server 自动备份和 PITR；
- Owner可以是批准的本租户或来宾用户，使用其在客户租户内的object ID，而不是按邮箱动态查找；
- 生命周期运行不是实时任务，删除可能晚于阈值执行；
- Soft Delete 会使已删除数据在额外 14 天内保持可恢复。
