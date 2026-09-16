# Stage0–1 本地手工执行包

> 核对日期：2026-09-16
>
> 适用：客户无法运行GitHub Actions，但已取得本仓库受审核代码，并需要完成Stage0可恢复备份和Stage1旧集群最小加固。

本目录是一套独立入口：

```text
local_execution/
├── README_ZH.md          # 本指南
├── customer.example.json # Stage0–1最小配置模板
├── __main__.py           # python -m local_execution入口
└── runner.py             # 本地顺序执行器
```

维护者可独立验证本包，不运行GitHub Actions工作流测试：

```bash
make validate-local-execution
```

它不读取GitHub Environment Variables/Secrets，不需要run ID、artifact、evidence ledger或人工计划哈希。每个变更步骤会先实时预览，再在同一次命令中直接执行。底层继续复用仓库现有Azure/Kubernetes实现，以保持资源范围、私网目标、恢复校验和幂等行为一致。

本路径只覆盖Stage0–1，不部署Stage2以后资源，不迁移目标数据库，不切换流量，也不停用旧集群。

## 1. 执行拓扑

客户电脑不在Azure私网时，不能直接访问私有AKS和Storage Private Endpoint。复用已经创建且带Bastion的Runner VM作为**离线手工执行主机**，不再创建第二台VM：

| 位置 | 用途 |
| --- | --- |
| 客户外部电脑 | 登录Azure、发起Bastion SSH、必要时挂载/卸载临时UAMI |
| 现有Runner VM | Runner服务停止后，手工执行本目录命令 |

### 1.1 让Runner离线

确认没有Actions作业运行，然后在Bastion会话停止服务：

```bash
cd /srv/runner/agent
sudo ./svc.sh stop
sudo ./svc.sh status || true
```

在GitHub仓库Settings → Actions → Runners确认该机器显示Offline。VM尚未注册GitHub时跳过此项，且不要执行注册命令。手工执行期间不得重新启动服务或派发作业。

### 1.2 Azure登录

优先在VM内使用实名客户管理员登录，设备码在客户合规电脑完成：

```bash
az login --tenant "REPLACE_TENANT_ID" --use-device-code
az account set --subscription "REPLACE_SUBSCRIPTION_ID"
az account show \
  --query '{tenantId:tenantId,subscriptionId:id,identity:user.name,type:user.type}' \
  --output json
```

不要使用`sudo az login`，不要创建长期Client Secret，也不要把凭据写入配置。

**Global Administrator不是Azure所有权限的同义词。** Microsoft Entra Global Administrator不会自动获得Azure订阅Owner/Contributor、Azure RBAC角色分配、AKS Cluster User/Kubernetes RBAC或Storage Blob数据权限。实际登录账号需要：

- 在目标订阅/RG执行ARM What-if和Stage0资源部署，并在旧RG部署Stage1日志与告警。
- 获取旧AKS用户凭据，在旧namespace读取Deployment/Pod、执行PostgreSQL Pod的exec/cp、更新两个既定Deployment及可选入口来源设置。
- 对Stage0备份容器执行Blob列举、上传和下载。

个人登录时，`backupOwnerPrincipalId`填写该用户在客户租户内的Object ID，Stage0 backup模板会为其建立备份管理和Blob数据授权。

如果条件访问阻止VM内的个人登录，只能在Runner保持Offline期间临时挂载专用手工执行UAMI。在线Runner VM绝不能挂载高权限UAMI，否则Actions作业可能通过IMDS取得Token。

客户外部电脑执行：

```bash
az vm identity assign \
  --ids "REPLACE_RUNNER_VM_RESOURCE_ID" \
  --identities "REPLACE_MANUAL_EXECUTION_UAMI_RESOURCE_ID"
```

VM内执行：

```bash
az login --identity --client-id "REPLACE_MANUAL_EXECUTION_UAMI_CLIENT_ID"
az account set --subscription "REPLACE_SUBSCRIPTION_ID"
```

使用UAMI时，在`parameters.backup`额外填写其Principal ID：

```json
"backupAutomationPrincipalId": "REPLACE_MANUAL_EXECUTION_UAMI_PRINCIPAL_ID"
```

完成后必须先退出Azure，再由客户外部电脑卸载UAMI：

```bash
az vm identity remove \
  --ids "REPLACE_RUNNER_VM_RESOURCE_ID" \
  --identities "REPLACE_MANUAL_EXECUTION_UAMI_RESOURCE_ID"
```

## 2. 网络前提

Runner VM已有Bastion和管理VNet，但Stage0创建的备份VNet仍需连接。`execution-host-connectivity`步骤会使用配置中的执行主机VNet ID，创建两端Peering和Blob Private DNS链接。

旧AKS为私有集群时，执行主机VNet还必须已有到旧AKS API的路由和DNS。旧AKS为受限公网API时，允许来源须包含该VM实际NAT出口。Azure管理员权限不能穿透网络，不得临时开放Storage公网来规避Private Endpoint。

## 3. 准备现有VM

客户外部电脑安装Azure CLI扩展，然后使用Runner部署输出中的`bastionSshCommand`：

```bash
az extension add --name bastion
az extension add --name ssh
REPLACE_BASTION_SSH_COMMAND_FROM_RUNNER_DEPLOYMENT_OUTPUT
```

VM需要Linux Bash、Git、Azure CLI、Bicep、Python 3.10或更高版本、Docker、`kubectl`、`kubelogin`、`curl`和`getent`：

```bash
command -v git az docker kubectl kubelogin curl getent
python3 --version
docker version --format '{{.Server.Version}}'
az bicep install
```

Runner模板默认只把`actions-runner`加入Docker组，工作盘父目录也属于该组。由VM管理员把手工登录用户加入本机组，退出并重新通过Bastion登录：

```bash
sudo usermod -aG docker,actions-runner "$USER"
```

在加密工作盘建立手工目录，不使用Actions工作目录：

```bash
sudo install -d -m 0700 -o "$USER" -g "$USER" /srv/runner/manual
cd /srv/runner/manual
```

## 4. 取得代码和配置

在VM执行：

```bash
git clone "REPLACE_CUSTOMER_REPOSITORY_URL" secure-litellm-on-azure
cd secure-litellm-on-azure
git fetch --tags --prune
git checkout --detach "REPLACE_REVIEWED_FULL_COMMIT_SHA"
git --no-pager show --no-patch --format='commit=%H%nsubject=%s' HEAD

python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
```

创建本地配置：

```bash
cp local_execution/customer.example.json local_execution/customer.json
chmod 600 local_execution/customer.json
```

`local_execution/customer.json`已被Git忽略。配置不包含密码、Token、Master Key或Salt。

### 4.1 配置字段

| 配置 | 含义与来源 |
| --- | --- |
| `azure`、`location`、`environment` | 客户租户、订阅和批准区域；与当前`az account show`一致 |
| `legacy` | 旧RG、AKS、namespace和PostgreSQL PVC |
| `target.resourceGroup` | 与旧RG不同的新备份资源RG |
| `parameters.backup` | 备份Workspace、Owner、VNet和PE子网；UAMI路径可增加`backupAutomationPrincipalId` |
| `parameters.monitoring` | 旧环境Log Analytics Workspace名称 |
| `parameters.legacy-logging` | 创建或复用旧日志Workspace的模式 |
| `legacyAccess` | 可选来源限制；只有真实CIDR和影响已批准时才加入 |
| `localExecution.postgresRestoreImage` | 与旧PG大版本匹配的完整`postgres@sha256:...` |
| `localExecution.executionHost.virtualNetworkId` | Runner部署输出`managementVnetId`；复用子网模式从`runnerSubnetId`去掉`/subnets/<名称>` |

恢复镜像必须使用完整RepoDigest：

```bash
IMAGE="postgres:REPLACE_MATCHING_MAJOR_AND_PATCH"
docker pull --platform linux/amd64 "$IMAGE"
docker image inspect "$IMAGE" --format '{{range .RepoDigests}}{{println .}}{{end}}'
docker run --rm --network none --read-only --entrypoint pg_restore \
  "REPLACE_POSTGRES_REPO_AT_SHA256" --version
```

## 5. 命令格式

所有命令从仓库根目录执行：

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step REPLACE_STEP
```

每个变更步骤会读取配置、记录Git提交、生成实时预览并立即执行。结果写入`temp/local-stage01/`。出现Delete、Unsupported、越界资源、配置漂移或底层校验失败时仍会停止，这些是目标安全校验，不是GitHub审批门禁。

执行器会在成功或失败后删除输出目录中的`kubeconfig`、`database.dump`和`downloaded.dump`；Azure Blob正式备份不会删除。

## 6. Stage0顺序

### S0-L01 配置检查

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json --step config-check
```

### S0-L02 Bootstrap

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json --step bootstrap
```

确认新RG和目标Log Analytics Workspace成功，旧RG未修改。

### S0-L03 备份基础设施

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json --step backup
```

确认私有Storage、容器、备份VNet、PE/DNS及备份身份授权。此步骤尚未导出数据库。

### S0-L04 执行主机网络

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json --step execution-host-connectivity
```

只应创建或复用执行主机VNet与备份VNet的Peering和Blob DNS链接。

### S0-L05 连通性检查

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json --step connectivity-check
```

检查旧AKS读取、备份PE DNS/TLS及Blob列举。结果在`local-readiness.json`；Pod exec/cp和Blob写入由下一步真实验证。

### S0-L06 备份和隔离恢复

在批准的低峰窗口执行：

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json --step backup-restore
```

成功条件：旧Pod真实`pg_dump`、无网络临时容器完整`pg_restore`、存在公共业务表、私有Blob上传/回读及SHA256一致。将`acceptance-report.json`中的备份引用、哈希、大小、表数量和耗时记录到客户受控位置，不记录dump正文或凭据。

### S0-L07 人工核验

实际核对：

- 旧环境盘点完整。
- 备份可完整恢复并回读一致。
- Master Key与Salt可从受控材料恢复。
- 旧入口的实际客户端协议基线通过。

未完成上述项目不进入Stage1。

## 7. Stage1顺序

开始前导出**变更前**两个Deployment配置，确认告警接收人、维护窗口和回退负责人。

### S1-L01 旧日志Workspace

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json --step legacy-logging
```

### S1-L02 Container Insights接入

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json --step monitoring-onboard
```

确认旧namespace真实日志到达，而不只是addon显示Enabled。

### S1-L03 告警规则

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json --step monitoring
```

触发批准的测试条件并确认通知真实送达。

### S1-L04 Deployment加固

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json --step legacy-hardening
```

脚本为既定PostgreSQL和LiteLLM Deployment补健康探针，并将PG策略设为Recreate。执行后检查rollout、Pod、PVC、旧入口和实际客户端。

### S1-L05 可选来源限制

仅配置了真实`legacyAccess`时执行：

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json --step legacy-access-restrict
```

需要回退时：

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json --step legacy-access-restore
```

分别验证批准来源成功、未批准来源拒绝。

### S1-L06 人工核验

确认旧服务健康、告警真实送达、回退快照可用；启用来源限制时另核对正反向来源测试。Stage1完成后继续保留旧集群、PVC、备份及密钥材料。

## 8. 失败与重跑

- ARM步骤失败可能已部分创建资源，先看Azure Deployment operations，再重跑同一步。
- runtime失败先查实际Deployment、Pod和checkpoint，不假定自动回滚。
- `backup-restore`重跑会生成新Blob，不自动删除旧备份。
- 修改代码或配置后，从受影响步骤重新执行并记录新提交和配置哈希。
- `temp/`仍含明文客户元数据，审核后按客户保留策略清理。
- 本地退出0不是GitHub Actions验收记录；以后恢复Actions时按其流程重新建立对应证据。

## 9. 恢复Runner服务

先清除VM内Azure登录：

```bash
az logout
az account clear
```

删除`local_execution/customer.json`和不再需要的手工输出。若挂载过UAMI，从客户外部电脑卸载并确认VM不再关联该身份。确认无Azure凭据、高权限UAMI、dump、kubeconfig或手工进程后，恢复服务：

```bash
cd /srv/runner/agent
sudo ./svc.sh start
sudo ./svc.sh status
```

最后在GitHub确认Runner重新Idle。不得让手工执行与Actions作业同时使用这台VM。