# LiteLLM PostgreSQL 扩容与 Spend Logs 保留策略

> **旧集群内PostgreSQL/PVC修复手册，不是当前数据服务维护入口。** 当前West US 3使用Entra-only PostgreSQL Flexible Server及LiteLLM `1.104.0`固定源码路径，不存在本文所假定的`pg-data`应用数据库PVC。当前正文批准、留存/备份、容量及恢复按[本地Stage2–9指南](../local_execution/stage2-9-guide-zh.md)或[客户迁移指南](../docs/customer-migration-guide-zh.md)执行；不要在当前集群执行下文PVC扩容、PG_PASSWORD注入或旧部署脚本重跑。原生发布/模式化门禁已实现，不代表实际日志留存与故障验收完成。

本文适用于旧LiteLLM `1.95.0`、AKS内单副本PostgreSQL、PVC `pg-data`部署。包含两种修复方式：

1. 客户尚未取得新版部署脚本时，通过 `kubectl` 手工修复；
2. 客户取得新版 `deploy_mi_aks_litellm.py` 后，通过脚本标准化执行。

本文命令不会显示 Master Key、数据库密码或 `DATABASE_URL`。执行前必须确认当前 Kubernetes context 指向目标客户集群。

## 1. 扩容是否影响已有数据

正常执行 PVC 扩容不会删除或重建 PostgreSQL 数据。

`kubectl patch pvc` 只会提高 PVC 的容量请求。AKS Azure Disk CSI Driver 随后扩展底层 Managed Disk，并由 Kubernetes 扩展文件系统。原 PVC、PV、磁盘、挂载路径和 PostgreSQL 数据目录保持不变。

当前客户环境已确认：

- PVC：`pg-data`；
- 当前容量：`1Gi`；
- StorageClass：`default`；
- Provisioner：`disk.csi.azure.com`；
- `allowVolumeExpansion=true`；
- 当前操作账号有 PVC、ConfigMap 和 Deployment patch 权限。

### 1.1 不会被扩容操作删除的数据

- Virtual Key、用户、团队和组织；
- 预算、RPM/TPM 限制和模型权限；
- UI 创建的模型、Router Settings 和 Guardrail；
- Spend Logs、费用与 token 统计；
- PostgreSQL Role、Schema 和 Prisma migration 记录。

### 1.2 需要区分的 retention 数据影响

PVC 扩容本身不删除数据，但配置以下保留策略后：

```yaml
maximum_spend_logs_retention_period: 7d
```

LiteLLM 会永久清理超过 7 天的历史 Spend Logs。这是预期行为，不会删除 Virtual Key、用户、团队、模型或 Guardrail 配置。需要长期审计的数据应先导出或备份，再启用 retention。

`store_prompts_in_spend_logs=false` 只影响后续请求，不会自动清除数据库中已经存在的 Prompt/Response 字段。

### 1.3 主要风险边界

- 删除 PVC、PV 或 Azure Disk 会造成数据丢失；
- PVC 只能扩容，不能缩容；
- 不应直接执行 `az disk update` 绕过 Kubernetes；
- StorageClass 不支持扩容时，patch 会失败，但不会因此删除原数据；
- 文件系统扩容如果要求重启 PostgreSQL，会产生短暂 503；
- 重跑完整部署脚本时，如果没有注入现有 Master Key 和 PG 密码，可能造成凭据漂移；
- retention 启用后，超过保留期的历史 Spend Logs 无法通过配置回滚恢复。

## 2. 执行前检查

```powershell
$ns = "litellm"

kubectl config current-context
kubectl -n $ns get deployment litellm-mi-proxy postgres
kubectl -n $ns get pods -o wide
kubectl -n $ns get pvc pg-data
```

确认 PVC 和 StorageClass 支持扩容：

```powershell
$storageClass = kubectl -n $ns get pvc pg-data `
  -o jsonpath='{.spec.storageClassName}'

kubectl get storageclass $storageClass `
  -o custom-columns='NAME:.metadata.name,ALLOW_EXPANSION:.allowVolumeExpansion,PROVISIONER:.provisioner'
```

只有 `ALLOW_EXPANSION` 为 `true` 时才继续。

## 3. 执行数据库备份

扩容通常不影响数据。对于数据库仍可接受连接的计划性扩容，应先创建逻辑备份。

```powershell
$ns = "litellm"
$pg = kubectl -n $ns get pod -l app=postgres `
  -o jsonpath='{.items[0].metadata.name}'
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$backupFile = ".\litellm-pg-$stamp.dump"

kubectl -n $ns exec $pg -- sh -c `
  'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc -f /tmp/litellm.dump'

kubectl -n $ns exec $pg -- pg_restore -l /tmp/litellm.dump
kubectl cp "${ns}/${pg}:/tmp/litellm.dump" $backupFile
kubectl -n $ns exec $pg -- rm /tmp/litellm.dump

Get-Item $backupFile | Select-Object FullName, Length, LastWriteTime
```

如果数据库处于正常服务状态，而 `pg_dump`、`pg_restore`、`kubectl cp` 或本地文件检查失败，应停止计划性扩容并排查备份问题。

如果数据库已经因 `No space left on device` 陷入 recovery/PANIC 循环，`pg_dump` 必然无法连接。此时不要反复执行 `pg_dump`，也不要删除数据库文件来腾空间；应按第 8.1 节先扩容原 PVC，使 PostgreSQL 完成 recovery，然后立即执行本节备份。PVC 扩容不会重建原 PV 或 Azure Disk。

如果客户变更流程强制要求扩容前留存，可在扩容前创建 Azure Disk/PVC 的崩溃一致性快照。该快照记录的是故障现场，不是事务一致的逻辑备份，恢复后仍需在有足够空间的卷上完成 PostgreSQL recovery；不能用它替代恢复后的 `pg_dump`。

## 4. 方式一：kubectl 手工修复

这是客户尚未取得新版部署脚本时的推荐方式。

### 4.1 在线扩容 PVC

以下示例将 `pg-data` 从 `1Gi` 扩到 `20Gi`：

```powershell
kubectl patch pvc pg-data -n litellm --type merge `
  -p '{"spec":{"resources":{"requests":{"storage":"20Gi"}}}}'
```

观察扩容状态：

```powershell
kubectl get pvc pg-data -n litellm `
  -o custom-columns='NAME:.metadata.name,STATUS:.status.phase,REQUESTED:.spec.resources.requests.storage,CAPACITY:.status.capacity.storage'

kubectl describe pvc pg-data -n litellm
```

验证 PostgreSQL 文件系统容量：

```powershell
$pg = kubectl -n litellm get pod -l app=postgres `
  -o jsonpath='{.items[0].metadata.name}'

kubectl -n litellm exec $pg -- df -h /var/lib/postgresql/data
```

如果 PVC 长时间显示 `FileSystemResizePending`，在维护窗口重启 PostgreSQL Deployment：

```powershell
kubectl rollout restart deployment/postgres -n litellm
kubectl rollout status deployment/postgres -n litellm --timeout=5m
```

重启期间 PostgreSQL 会短暂进入 recovery mode，LiteLLM 的 Virtual Key 认证可能返回 401/503。不要同时重启 PostgreSQL 和 LiteLLM。

### 4.2 通过 LiteLLM 管理 API设置 retention

当前 LiteLLM `1.95.0` 支持数据库持久化的隐藏管理接口 `/config/field/update`。该接口未包含在公开 OpenAPI schema 中，不应视为跨版本稳定契约。升级 LiteLLM 后必须重新执行本节预检，不能直接复用写入命令。

先确认运行中版本支持需要动态更新的字段：

```powershell
$pod = kubectl -n litellm get pod -l app=litellm-mi-proxy `
  -o jsonpath='{.items[0].metadata.name}'

kubectl -n litellm exec $pod -- python -c "from litellm.proxy._types import ConfigGeneralSettings; wanted={'maximum_spend_logs_retention_period','store_prompts_in_spend_logs'}; fields=set(ConfigGeneralSettings.model_fields); print('supported='+str(sorted(wanted.intersection(fields)))); print('missing='+str(sorted(wanted.difference(fields)))); assert wanted.issubset(fields)"
```

再确认数据库和管理员认证可访问配置接口：

```powershell
kubectl -n litellm exec $pod -- python -c "import os,requests; h={'Authorization':'Bearer '+os.environ['LITELLM_MASTER_KEY']}; r=requests.get('http://127.0.0.1:4000/config/list?config_type=general_settings',headers=h,timeout=10); print(r.status_code); r.raise_for_status()"
```

只有两条预检都成功时才继续。以下写入命令在 Pod 内读取现有 Master Key，不会把 Key 输出到终端：

```powershell
kubectl -n litellm exec $pod -- python -c "import os,requests; h={'Authorization':'Bearer '+os.environ['LITELLM_MASTER_KEY']}; s={'maximum_spend_logs_retention_period':'7d','store_prompts_in_spend_logs':False}; rs=[(n,requests.post('http://127.0.0.1:4000/config/field/update',headers=h,json={'config_type':'general_settings','field_name':n,'field_value':v},timeout=10)) for n,v in s.items()]; print([(n,r.status_code) for n,r in rs]); assert all(r.ok for n,r in rs)"
```

预期两个配置项均返回 HTTP `200`。LiteLLM `1.95.0` 的隐藏接口不接受 `maximum_spend_logs_retention_interval`；未显式配置时，清理间隔默认就是 `1d`。然后滚动重启 LiteLLM，使 retention 调度任务按新配置启动：

```powershell
kubectl rollout restart deployment/litellm-mi-proxy -n litellm
kubectl rollout status deployment/litellm-mi-proxy -n litellm --timeout=10m
```

单副本 LiteLLM 在滚动期间会有短暂中断，应安排维护窗口。当前 Deployment 没有 LiteLLM HTTP readiness probe，因此 Pod 显示 Ready 或 rollout 完成时，Prisma migration 和 Uvicorn 仍可能处于启动阶段。不要继续使用重启前保存的 `$pod` 变量。

### 4.3 验证 retention 配置

```powershell
$pod = kubectl -n litellm get pod -l app=litellm-mi-proxy `
  --field-selector=status.phase=Running `
  -o jsonpath='{.items[0].metadata.name}'

kubectl -n litellm logs $pod | `
  Select-String -SimpleMatch 'Application startup complete'

kubectl -n litellm exec $pod -- python -c `
  "import requests; r=requests.get('http://127.0.0.1:4000/health/liveliness',timeout=10); print(r.status_code, r.text); r.raise_for_status()"

kubectl -n litellm exec $pod -- python -c "import os,requests; h={'Authorization':'Bearer '+os.environ['LITELLM_MASTER_KEY']}; r=requests.get('http://127.0.0.1:4000/config/list?config_type=general_settings',headers=h,timeout=10); r.raise_for_status(); wanted={'maximum_spend_logs_retention_period','store_prompts_in_spend_logs'}; print({x['field_name']:x['field_value'] for x in r.json() if x.get('field_name') in wanted})"
```

如果日志中还没有 `Application startup complete`，或者 liveliness 返回连接拒绝，不要执行配置查询；等待 LiteLLM 完成 migration 和应用启动后，重新获取 `$pod` 并重试。

预期结果：

```text
maximum_spend_logs_retention_period = 7d
store_prompts_in_spend_logs = false
```

清理间隔使用 LiteLLM `1.95.0` 的默认值 `1d`。

验证 PostgreSQL 与 LiteLLM：

```powershell
$pg = kubectl -n litellm get pod -l app=postgres `
  -o jsonpath='{.items[0].metadata.name}'
$pod = kubectl -n litellm get pod -l app=litellm-mi-proxy `
  -o jsonpath='{.items[0].metadata.name}'

kubectl -n litellm exec $pg -- sh -c `
  'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"'

kubectl -n litellm exec $pod -- python -c `
  "import requests; r=requests.get('http://127.0.0.1:4000/health/liveliness',timeout=10); print(r.status_code, r.text); r.raise_for_status()"

kubectl -n litellm logs $pod --since=15m | `
  Select-String -Pattern 'database|recovery|spend.log|retention|error'
```

### 4.4 紧急停止逐请求 Spend Logs

LiteLLM `1.95.0` 的隐藏字段更新 API不支持动态修改 `disable_spend_logs`。只有在数据库容量已经接近耗尽，并且客户接受暂时失去逐请求审计明细时，才通过 ConfigMap 临时关闭。

先备份 ConfigMap：

```powershell
kubectl get configmap litellm-config -n litellm -o yaml > `
  .\litellm-config-before-disable-spend-logs.yaml
```

编辑 ConfigMap：

```powershell
kubectl edit configmap litellm-config -n litellm
```

在 `config.yaml: |` 内加入或合并以下顶层配置，不要删除原有 `model_list`、`litellm_settings` 或 `router_settings`：

```yaml
general_settings:
  disable_spend_logs: true
```

保存后重启 LiteLLM：

```powershell
kubectl get configmap litellm-config -n litellm `
  -o jsonpath='{.data.config\.yaml}'

kubectl rollout restart deployment/litellm-mi-proxy -n litellm
kubectl rollout status deployment/litellm-mi-proxy -n litellm --timeout=10m
```

恢复逐请求日志时将该值改为 `false`，再重启 LiteLLM。不要在未验证运行版本和业务影响时配置 `disable_spend_updates`，否则预算累计和用户、团队消费更新可能停止。

## 5. 方式二：使用新版部署脚本

新版 `deploy_mi_aks_litellm.py` 支持：

- 新建 PG PVC 默认 `20Gi`；
- 已有 PVC 显式扩容；
- Spend Logs 默认保留 `7d`；
- retention 默认每 `1d` 执行；
- 默认不保存 Prompt/Response 正文；
- 拒绝 PVC 缩容。

### 5.1 执行前准备

必须从客户批准的 Secret 管理流程注入当前生产值，不要在聊天、工单或 Git 中记录：

- `LITELLM_MASTER_KEY`；
- `PG_PASSWORD`；
- `AZURE_SUBSCRIPTION_ID`；
- 域名、证书邮箱和其他当前部署变量；
- 当前使用的 LiteLLM 镜像地址。

如果没有现有 Master Key 或 PG 密码，不要重跑脚本。否则脚本可能自动生成新 Master Key 或使用默认 PG 密码，造成认证和数据库连接漂移。

### 5.2 设置容量和 retention

```powershell
cd .\LiteLLM

# 必须先通过客户批准的安全流程注入现有凭据
$env:LITELLM_MASTER_KEY = "<从安全 Secret 流程注入>"
$env:PG_PASSWORD = "<从安全 Secret 流程注入>"

$env:STORE_MODEL_IN_DB = "true"
$env:PG_STORAGE = "20Gi"
$env:EXPAND_EXISTING_PG_PVC = "true"
$env:MAXIMUM_SPEND_LOGS_RETENTION_PERIOD = "7d"
$env:MAXIMUM_SPEND_LOGS_RETENTION_INTERVAL = "1d"
$env:STORE_PROMPTS_IN_SPEND_LOGS = "false"
$env:DISABLE_SPEND_LOGS = "false"
```

运行客户对应的本地配置：

```powershell
python .\deploy_mi_aks_litellm.py .\azure-openai.loc.json
```

脚本会协调 Azure、AKS、ConfigMap、Secret、PostgreSQL 和 LiteLLM Deployment，不是只修改 PVC。执行前应检查 Git 版本、配置文件和环境变量，并安排维护窗口。

### 5.3 脚本执行后验证

```powershell
kubectl get pvc pg-data -n litellm `
  -o custom-columns='NAME:.metadata.name,STATUS:.status.phase,REQUESTED:.spec.resources.requests.storage,CAPACITY:.status.capacity.storage'

$pg = kubectl -n litellm get pod -l app=postgres `
  -o jsonpath='{.items[0].metadata.name}'
kubectl -n litellm exec $pg -- df -h /var/lib/postgresql/data

kubectl get deployment litellm-mi-proxy postgres -n litellm
kubectl get pods -n litellm
```

然后执行第 4.3 节的 retention、PostgreSQL 和 liveliness 验证。

安全清除当前 PowerShell 进程中的敏感环境变量：

```powershell
Remove-Item Env:LITELLM_MASTER_KEY -ErrorAction SilentlyContinue
Remove-Item Env:PG_PASSWORD -ErrorAction SilentlyContinue
```

## 6. 回滚边界

### 6.1 retention 配置回滚

可以删除数据库中的 retention 字段，再重启 LiteLLM：

```powershell
$pod = kubectl -n litellm get pod -l app=litellm-mi-proxy `
  -o jsonpath='{.items[0].metadata.name}'

kubectl -n litellm exec $pod -- python -c "import os,requests; h={'Authorization':'Bearer '+os.environ['LITELLM_MASTER_KEY']}; names=['maximum_spend_logs_retention_period','store_prompts_in_spend_logs']; rs=[(n,requests.post('http://127.0.0.1:4000/config/field/delete',headers=h,json={'config_type':'general_settings','field_name':n},timeout=10)) for n in names]; print([(n,r.status_code) for n,r in rs]); assert all(r.ok for n,r in rs)"

kubectl rollout restart deployment/litellm-mi-proxy -n litellm
```

回滚配置不会恢复已经被 retention 删除的历史 Spend Logs，只能通过备份恢复。

### 6.2 PVC 扩容回滚

Kubernetes PVC 不支持缩容。扩到 `20Gi` 或 `50Gi` 后，应保持该容量。不要通过编辑 PV、修改 Azure Disk 或重建 PVC 强制缩小。

如果扩容请求失败：

1. 保留原 PVC 和 Pod；
2. 检查 `kubectl describe pvc pg-data -n litellm`；
3. 检查 namespace Event；
4. 不要删除 PVC；
5. 在确认原因后重新提交更大的容量请求。

## 7. 生产环境建议

`20Gi` 或 `50Gi` PVC 加 retention 可以降低短期写满风险，但 AKS 内单副本 PostgreSQL 仍存在节点维护、Pod 重调度、磁盘故障和数据库恢复期间全站认证不可用的问题。

高调用量生产环境应迁移到 Azure Database for PostgreSQL Flexible Server，并配置：

- Zone-redundant HA；
- 自动备份与时间点恢复；
- Private Endpoint 和 Private DNS；
- TLS；
- 存储自动增长；
- 数据库连接、容量、延迟和失败率告警。

迁移到托管 PostgreSQL 前，仍应保留本手册中的 Spend Logs retention、Prompt 正文关闭和容量监控策略。

## 8. 本次故障结论

本地验证集群的 PostgreSQL 文件系统使用率约为 `7%`，但该结果不能代表客户集群。

客户提供的 PostgreSQL 日志已经确认根因是磁盘空间耗尽：

```text
PANIC: could not write to file "pg_logical/replorigin_checkpoint.tmp": No space left on device
```

PostgreSQL 在 recovery 结束阶段需要写入 checkpoint，但文件系统没有可用空间，导致 checkpointer PANIC。主进程随后终止其他 server process 并再次自动恢复，形成持续的 recovery 循环。Kubernetes 仍可能把容器显示为 `Running`，因为 PostgreSQL PID 1 没有退出，而是在容器内部不断重新初始化。

`invalid record length ... expected at least 24, got 0` 出现在 WAL redo 末尾时通常表示读到当前 WAL 末端，不是本次故障的首要根因。明确的首要根因是后续的 `No space left on device`。

### 8.1 紧急恢复步骤

当前数据库无法完成 recovery，因此不能先执行 `pg_dump`。紧急处理顺序是：

1. 可选：按客户变更流程创建 Azure Disk/PVC 崩溃一致性快照；
2. 扩容原 PVC；
3. 等待 PostgreSQL recovery 完成；
4. 立即执行第 3 节逻辑备份；
5. 启用 Spend Logs retention。

先确认容量和 inode：

```powershell
$ns = "litellm"
$pg = kubectl -n $ns get pod -l app=postgres `
  -o jsonpath='{.items[0].metadata.name}'

kubectl -n $ns exec $pg -- df -h /var/lib/postgresql/data
kubectl -n $ns exec $pg -- df -i /var/lib/postgresql/data
kubectl -n $ns get pvc pg-data
```

确认 StorageClass 支持扩容后，将 PVC 扩到至少 `20Gi`：

```powershell
$storageClass = kubectl -n $ns get pvc pg-data `
  -o jsonpath='{.spec.storageClassName}'

kubectl get storageclass $storageClass `
  -o custom-columns='NAME:.metadata.name,ALLOW_EXPANSION:.allowVolumeExpansion,PROVISIONER:.provisioner'

kubectl patch pvc pg-data -n $ns --type merge `
  -p '{"spec":{"resources":{"requests":{"storage":"20Gi"}}}}'
```

观察 PVC 和文件系统扩容：

```powershell
kubectl get pvc pg-data -n $ns `
  -o custom-columns='NAME:.metadata.name,STATUS:.status.phase,REQUESTED:.spec.resources.requests.storage,CAPACITY:.status.capacity.storage'

kubectl describe pvc pg-data -n $ns
kubectl -n $ns exec $pg -- df -h /var/lib/postgresql/data
```

如果 PVC 长时间处于 `FileSystemResizePending`，或者 Pod 内文件系统容量仍未增加，在维护窗口重启 PostgreSQL：

```powershell
kubectl rollout restart deployment/postgres -n $ns
kubectl rollout status deployment/postgres -n $ns --timeout=5m
```

重新获取 Pod 名称并确认 recovery 完成：

```powershell
$pg = kubectl -n $ns get pod -l app=postgres `
  -o jsonpath='{.items[0].metadata.name}'

kubectl -n $ns logs $pg --tail=200 | `
  Select-String -Pattern 'ready to accept connections|PANIC|No space left|recovery|checkpoint'

kubectl -n $ns exec $pg -- sh -c `
  'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
```

只有看到 `database system is ready to accept connections` 且 `pg_isready` 成功后，才验证 LiteLLM。如果 LiteLLM 仍保留失效数据库连接，再滚动重启 LiteLLM：

```powershell
kubectl rollout restart deployment/litellm-mi-proxy -n $ns
kubectl rollout status deployment/litellm-mi-proxy -n $ns --timeout=10m
```

数据库恢复后应立即执行第 3 节备份，并按第 4.2 节启用 Spend Logs retention。

不要删除 `pg_wal`、`pg_logical`、checkpoint 文件、PVC、PV 或 Azure Disk来临时腾出空间，这些操作可能造成不可恢复的数据损坏。