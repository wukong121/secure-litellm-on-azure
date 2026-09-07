> 客户安全增强迁移期间不要执行本文删除步骤。以下资源名与所有权判断为已去标识化的参考案例，不能证明客户资源可删除。先完成[迁移与回退窗口](../docs/customer-migration-guide-zh.md)，再另行批准退役并重新盘点所有权。

# 删除 LiteLLM 部署资源并验证清理完成

本文用于删除 [`deploy_mi_aks_litellm.py`](./deploy_mi_aks_litellm.py) 创建或修改的 Azure 与 Kubernetes 资源，并验证不存在残留计费资源、权限和 DNS 指向。

> **这是不可逆操作。** PostgreSQL PVC 中的用户、Virtual Key、预算、Spend Logs、UI 模型配置和 Router Settings 会被永久删除。执行前应确认备份和资源归属。

## 1. 脚本的资源边界

部署脚本采用“创建或复用”模式，不能仅凭资源名称判断所有权。

| 资源 | 脚本行为 | 删除注意事项 |
|---|---|---|
| `apim_resource_group` 指定的 Resource Group | 创建或复用 | 只有专用且不含其他资源时才能整组删除 |
| User Assigned Managed Identity | 创建或复用 | 删除前先保存 Principal ID、清理 RBAC；共享身份不能删除 |
| AKS Cluster | 创建或复用 | 删除 AKS 会同时删除 AKS 托管的节点 Resource Group |
| AKS 节点 VMSS | 把 UAMI 附加到 VMSS | 保留 AKS 时必须先从所有 VMSS 移除该身份 |
| Azure OpenAI / Foundry RBAC | 在各 Account Scope 添加 `Cognitive Services OpenAI User` | Azure OpenAI Resource 本身不应删除，只删除确认属于 LiteLLM UAMI 的 Role Assignment |
| Kubernetes namespace | 创建或复用，默认 `litellm` | 包含 LiteLLM、PostgreSQL、Secret、ConfigMap、PVC、Ingress 和证书资源 |
| ingress-nginx | HTTPS 模式通过 Helm 安装或升级 | 可能被其他应用共享，不能默认卸载 |
| cert-manager | HTTPS 模式通过 Helm 安装或升级，并安装 CRD | 可能被其他应用共享；Helm 卸载后 CRD 通常仍存在 |
| `letsencrypt-prod` ClusterIssuer | 创建或更新 | 删除前确认没有其他 Certificate/Ingress 使用 |
| Azure Load Balancer/Public IP/Disk/VMSS/VNet/NSG | 由 AKS 和 Kubernetes Service/PVC 间接创建 | 删除 AKS 时由节点 RG 清理；保留 AKS 时需单独验证云资源回收 |

脚本**不会创建**以下资源，不应作为默认清理目标：

- `azure-openai-list` 中的 Azure OpenAI / Foundry Account；
- Account 中已有的模型 Deployment；
- 客户 ACR 及其镜像；
- 阿里云或其他 DNS Provider 中的 A 记录；
- 本地 `azure-openai.loc.json`。

DNS 记录和 ACR 可能由其他操作指南单独创建，只能在确认不再使用后人工删除。

## 2. 当前环境的重要警告

当前配置使用：

```text
Deployment Resource Group : rg-example-legacy
AKS                       : litellm-mi-aks
AKS Node Resource Group   : MC_rg-example-legacy_litellm-mi-aks_westus
Managed Identity          : litellm-managed-identity
Kubernetes Namespace      : litellm
```

`rg-example-legacy` 还包含 Azure OpenAI/Foundry Resource、Application Insights、Log Analytics、ACR `exampleacr` 和其他资源。

> **当前环境禁止执行 `az group delete -n rg-example-legacy`。** 应使用“场景 B：保留共享 RG，删除专用 AKS”或按实际所有权使用“场景 C：保留共享 AKS”。

节点 Resource Group `MC_rg-example-legacy_litellm-mi-aks_westus` 的 `managedBy` 指向 `litellm-mi-aks`。其中的 VMSS、VNet、NSG、Load Balancer、Public IP、节点 Managed Identity 和 PostgreSQL PVC Disk 都属于该 AKS，可以全部随网关删除。

> **不要在 AKS 仍存在时直接删除 `MC_...` 节点 Resource Group 或其中的单个资源。** 这会破坏仍被 Azure 控制面管理的集群，并可能留下不一致状态。正确顺序是先删除 `litellm-mi-aks`，等待 Azure 自动删除整个节点 Resource Group；只有 AKS 已不存在但节点 RG 异常残留时，才人工删除残留的 `MC_...` Resource Group。

### 当前截图中的删除清单

| Portal 中的资源 | 是否可删 | 判断依据 |
|---|---|---|
| `litellm-mi-aks` | **可以删除** | LiteLLM 专用 AKS；删除后节点 Resource Group 及其中的 VMSS、VNet、NSG、Load Balancer/Public IP 和 PostgreSQL Disk 会随之清理 |
| `litellm-managed-identity` | **可以删除** | 只附加在该 AKS 节点 VMSS；当前直接 Role Assignment 是 `example-foundry-resource` Scope 的 `Cognitive Services OpenAI User`，应先删除该权限 |
| `exampleacr` | **可以删除，但放在 AKS 和备份之后** | 当前只有 repository `litellm` 和旧 tag `mi-fix-image-gen`，无 Webhook/Task；直接 `AcrPull` Principal 是该 AKS kubelet；运行中的 LiteLLM 已改用外部官方镜像 `docker.litellm.ai/berriai/litellm:1.95.0` |
| `example-foundry-resource` | **不可删除** | Foundry/Azure OpenAI 业务资源，不是 LiteLLM 脚本创建的 |
| `example-project` Project | **不可删除** | 属于上述 Foundry Resource |
| `example-secondary-foundry-resource` | **不可删除** | 独立 Foundry 业务资源 |
| `example-secondary-project` Project | **不可删除** | 属于上述 Foundry Resource |
| `example-appinsights` | **不可删除** | Foundry/Application 的监控资源，不是 LiteLLM 专属资源 |
| `example-workspace` | **不可删除** | Log Analytics Workspace，不是 LiteLLM 专属资源 |
| `Application Insights Smart Detection` | **不可单独删除** | 与 Application Insights 关联的 Action Group，不属于 LiteLLM 网关清理范围 |

因此，Azure Portal 中应保留 Resource Group，只选择删除下面三个顶层资源：

```text
litellm-mi-aks
litellm-managed-identity
exampleacr
```

推荐顺序是：停止 DNS 流量和备份 -> 删除 UAMI 的外部 RBAC -> 删除 AKS并等待节点 RG 消失 -> 删除 UAMI -> 删除 ACR。

## 3. 前置条件与变量

在项目根目录打开 PowerShell：

```powershell
$configPath = ".\LiteLLM\azure-openai.loc.json"
$config = Get-Content $configPath -Raw | ConvertFrom-Json

$subscriptionId = "<AKS 所在订阅 ID>"
$resourceGroup = $config.apim_resource_group
$aksName = "litellm-mi-aks"
$identityName = if ($config.managed_identity) {
  $config.managed_identity
} else {
  "litellm-managed-identity"
}
$namespace = "litellm"
$hostname = "litellm.example.com"

az account set --subscription $subscriptionId

$actualSubscription = az account show --query id -o tsv
if ($actualSubscription -ne $subscriptionId) {
  throw "Azure subscription mismatch: $actualSubscription"
}
```

如果部署时覆盖过 `AKS_NAME`、`MI_NAME` 或 `AKS_NAMESPACE`，必须把变量改成部署时的实际值。

确认工具和登录状态：

```powershell
az account show --query "{subscription:id,user:user.name}" -o table
kubectl config current-context
helm version --short
```

## 4. 删除前盘点与备份

### 4.1 解析实际 Azure 资源

```powershell
$aks = az aks show `
  --resource-group $resourceGroup `
  --name $aksName `
  -o json | ConvertFrom-Json

$nodeResourceGroup = $aks.nodeResourceGroup
$aksId = $aks.id

$identity = az identity show `
  --resource-group $resourceGroup `
  --name $identityName `
  -o json | ConvertFrom-Json

$identityId = $identity.id
$principalId = $identity.principalId

[pscustomobject]@{
  Subscription      = $subscriptionId
  ResourceGroup     = $resourceGroup
  AKS               = $aksName
  AKSId             = $aksId
  NodeResourceGroup = $nodeResourceGroup
  Identity          = $identityName
  IdentityId        = $identityId
  PrincipalId       = $principalId
  Namespace         = $namespace
} | Format-List
```

### 4.2 检查 Resource Group 是否共享

```powershell
az resource list `
  --resource-group $resourceGroup `
  --query "[].{name:name,type:type,location:location}" `
  -o table

az lock list --resource-group $resourceGroup -o table
az lock list --resource-group $nodeResourceGroup -o table
```

只要看到 Azure OpenAI、ACR、Application Insights、Log Analytics 或其他业务资源，就不能整组删除。

不要为绕过失败而直接删除 Resource Lock。应先确认 Lock 的所有者和保护目的。

### 4.3 盘点 Kubernetes 和 Helm

```powershell
kubectl get all,pvc,configmap,secret,ingress,certificate,certificaterequest `
  --namespace $namespace

helm list --all-namespaces
kubectl get ingress --all-namespaces
kubectl get certificates,issuers --all-namespaces
kubectl get clusterissuer
```

如果 ingress-nginx 或 cert-manager 还服务于其他 namespace，不要卸载对应 Helm Release。

### 4.4 保存外部 Role Assignment 清单

脚本可能跨订阅为同一个 UAMI 授权。下面按配置中的每个 Azure OpenAI Account Scope 查询，不依赖当前 Azure CLI 默认订阅：

```powershell
$roleAssignments = @()

foreach ($account in $config.'azure-openai-list') {
  $accountSubscription = if ($account.subscription_id) {
    $account.subscription_id
  } else {
    $subscriptionId
  }
  $accountName = ([uri]$account.endpoint).Host.Split('.')[0]
  $scope = "/subscriptions/$accountSubscription/resourceGroups/$($account.resource_group)/providers/Microsoft.CognitiveServices/accounts/$accountName"

  $items = az role assignment list `
    --subscription $accountSubscription `
    --assignee-object-id $principalId `
    --scope $scope `
    --fill-principal-name false `
    -o json | ConvertFrom-Json

  foreach ($item in $items) {
    $roleAssignments += [pscustomobject]@{
      SubscriptionId = $accountSubscription
      Account         = $accountName
      Scope           = $scope
      Role            = $item.roleDefinitionName
      AssignmentId    = $item.id
    }
  }
}

$roleAssignments | Format-Table -AutoSize
$roleAssignments |
  ConvertTo-Json -Depth 10 |
  Set-Content ".\litellm-role-assignments-backup.json" -Encoding utf8
```

如果 UAMI 在部署前已经存在或被其他工作负载共享，不能批量删除其全部 Role Assignment。必须逐条确认 `AssignmentId` 的所有权。

当前 JSON 只代表现在的模型配置。为了发现旧配置、已移除 Account 或其他订阅留下的权限，再按所有可访问订阅做一次 Principal 全量审计：

```powershell
$allIdentityAssignments = @()
$enabledSubscriptions = az account list `
  --query "[?state=='Enabled'].id" `
  -o tsv

foreach ($candidateSubscription in $enabledSubscriptions) {
  $items = az role assignment list `
    --subscription $candidateSubscription `
    --assignee-object-id $principalId `
    --all `
    --fill-principal-name false `
    -o json | ConvertFrom-Json

  foreach ($item in $items) {
    $allIdentityAssignments += [pscustomobject]@{
      SubscriptionId = $candidateSubscription
      Scope           = $item.scope
      Role            = $item.roleDefinitionName
      AssignmentId    = $item.id
    }
  }
}

$allIdentityAssignments | Format-Table -AutoSize
$allIdentityAssignments |
  ConvertTo-Json -Depth 10 |
  Set-Content ".\litellm-all-identity-role-assignments-backup.json" -Encoding utf8
```

如果该 UAMI 确认为 LiteLLM 专用，删除集合应包含审计出的全部 Assignment；如果身份被共享，只能挑选确认由本部署引入的 Assignment：

```powershell
# 默认采用当前配置 Scope 中确认过的权限。
$assignmentsToDelete = @($roleAssignments)

# 仅当 UAMI 确认为 LiteLLM 专用时，才改为：
# $assignmentsToDelete = @($allIdentityAssignments)

$assignmentsToDelete | Format-Table -AutoSize
```

### 4.5 可选：备份 LiteLLM 数据

```powershell
$backupDir = ".\litellm-cleanup-backup-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
New-Item $backupDir -ItemType Directory | Out-Null

kubectl get configmap litellm-config -n $namespace -o yaml |
  Set-Content "$backupDir\litellm-config.yaml" -Encoding utf8

kubectl exec -n $namespace deployment/postgres -- sh -c `
  'PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB"' |
  Set-Content "$backupDir\litellm-postgres.sql" -Encoding utf8
```

数据库备份可能包含用户、Virtual Key 元数据和使用记录，应按敏感数据管理。

### 4.6 保存动态资源标识

在删除前记录 PV 和公网 IP，便于删除后核对云资源是否回收：

```powershell
$postgresPv = kubectl get pvc pg-data -n $namespace `
  -o jsonpath='{.spec.volumeName}' 2>$null

$ingressIp = kubectl get service ingress-nginx-controller `
  -n ingress-nginx `
  -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>$null

$litellmServiceIp = kubectl get service litellm-mi-proxy `
  -n $namespace `
  -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>$null

[pscustomobject]@{
  PostgresPV       = $postgresPv
  IngressPublicIP  = $ingressIp
  LiteLLMServiceIP = $litellmServiceIp
} | Format-List
```

## 5. 先停止外部流量

在 DNS Provider 中删除 `$hostname` 的 A/AAAA/CNAME 记录，或切换到新的网关。DNS 不是部署脚本创建的，因此 Azure 删除操作不会自动清理它。

等待 DNS TTL 后验证：

```powershell
Resolve-DnsName $hostname -Server 1.1.1.1 -ErrorAction SilentlyContinue
```

如果仍返回旧公网 IP，先处理 DNS，再继续删除。

## 6. 精确删除 LiteLLM UAMI 的外部 RBAC

只删除第 4.4 节已经确认属于该 LiteLLM UAMI 的 Assignment ID：

```powershell
foreach ($assignment in $assignmentsToDelete) {
  az role assignment delete `
    --subscription $assignment.SubscriptionId `
    --ids $assignment.AssignmentId
}
```

不要按角色名称删除整个 Scope 的所有 Assignment；同一 Azure OpenAI Resource 可能还有其他用户、应用或 Managed Identity。

## 7. 选择删除场景

### 场景 A：专用 Resource Group，整组删除

仅当第 4.2 节确认 Resource Group 内所有资源均可删除时使用。该操作会删除 RG 中的所有资源，包括不是脚本创建但后来放入该 RG 的资源。

```powershell
$confirmation = Read-Host "Type DELETE-$resourceGroup to delete the entire resource group"
if ($confirmation -ne "DELETE-$resourceGroup") {
  throw "Confirmation mismatch"
}

az group delete `
  --name $resourceGroup `
  --yes `
  --no-wait `
  --only-show-errors

az group wait `
  --name $resourceGroup `
  --deleted `
  --interval 15 `
  --timeout 3600

az group wait `
  --name $nodeResourceGroup `
  --deleted `
  --interval 15 `
  --timeout 3600
```

删除父 RG 前已经清理外部 AOAI Scope RBAC，是因为这些 Role Assignment 不属于部署 RG。

### 场景 B：共享 Resource Group，删除专用 AKS

**当前环境推荐此场景。** 保留 `rg-example-legacy` 中的 Azure OpenAI、ACR、监控和其他资源，只删除 AKS、节点 RG 和 LiteLLM UAMI。

```powershell
$confirmation = Read-Host "Type DELETE-$aksName to delete the AKS cluster"
if ($confirmation -ne "DELETE-$aksName") {
  throw "Confirmation mismatch"
}

az aks delete `
  --resource-group $resourceGroup `
  --name $aksName `
  --yes `
  --no-wait

az aks wait `
  --resource-group $resourceGroup `
  --name $aksName `
  --deleted `
  --interval 15 `
  --timeout 3600

az group wait `
  --name $nodeResourceGroup `
  --deleted `
  --interval 15 `
  --timeout 3600

az identity delete `
  --resource-group $resourceGroup `
  --name $identityName
```

删除 AKS 会连同集群内 LiteLLM、PostgreSQL、PVC、Ingress、Helm Release 和 AKS 节点资源一起删除，因此不需要先逐个执行 `kubectl delete`。

不要删除：

```text
example-foundry-resource
exampleacr
Application Insights / Log Analytics
其他 Azure OpenAI / Foundry Resource
```

### 场景 C：保留共享 AKS，仅删除 LiteLLM

只有确认 AKS 被其他应用共享时使用。

#### C1. 删除 LiteLLM namespace

```powershell
kubectl delete namespace $namespace --wait=true --timeout=10m
```

namespace 删除会删除 LiteLLM、PostgreSQL、Secret、ConfigMap、Ingress、TLS Secret 和 PVC。

如果 namespace 长时间停留在 `Terminating`，先检查 Finalizer 和存储状态，不要直接强制清空 Finalizer：

```powershell
kubectl get namespace $namespace -o yaml
kubectl get pvc,pv --all-namespaces
kubectl get volumeattachments
```

#### C2. 按所有权清理 ClusterIssuer 和 Helm Add-on

先确认没有其他工作负载使用：

```powershell
kubectl get ingress --all-namespaces
kubectl get certificates,issuers --all-namespaces
kubectl get clusterissuer
helm list --all-namespaces
```

如果 `letsencrypt-prod` 只供 LiteLLM 使用：

```powershell
kubectl delete clusterissuer letsencrypt-prod --ignore-not-found
```

如果 ingress-nginx 只供 LiteLLM 使用：

```powershell
helm uninstall ingress-nginx -n ingress-nginx --wait
kubectl delete namespace ingress-nginx --ignore-not-found --wait=true
```

如果 cert-manager 只供 LiteLLM 使用：

```powershell
helm uninstall cert-manager -n cert-manager --wait
kubectl delete namespace cert-manager --ignore-not-found --wait=true
```

Helm 通常不会删除放在 Chart `crds/` 中的 cert-manager CRD。只有确认集群中没有其他 cert-manager 用户后才删除：

```powershell
$certManagerCrds = kubectl get customresourcedefinition -o name |
  Where-Object {
    $_ -match 'cert-manager.io' -or $_ -match 'acme.cert-manager.io'
  }

$certManagerCrds | ForEach-Object { kubectl delete $_ }
```

#### C3. 从保留的 AKS VMSS 移除 UAMI

```powershell
$vmssNames = az vmss list `
  --resource-group $nodeResourceGroup `
  --query "[].name" `
  -o tsv

foreach ($vmssName in $vmssNames) {
  az vmss identity remove `
    --resource-group $nodeResourceGroup `
    --name $vmssName `
    --identities $identityId
}
```

完成第 6 节的 Role Assignment 清理后，删除 UAMI：

```powershell
az identity delete `
  --resource-group $resourceGroup `
  --name $identityName
```

如果该 UAMI 还被其他 VMSS、VM、App Service 或工作负载使用，不要移除或删除。

## 8. 删除后验证

### 8.1 场景 A：整个 RG 应不存在

```powershell
az group exists --name $resourceGroup
az group exists --name $nodeResourceGroup
```

两条命令都应返回：

```text
false
```

### 8.2 场景 B：AKS、节点 RG、UAMI 应不存在，业务 RG 应保留

```powershell
$aksStillExists = az aks show `
  --resource-group $resourceGroup `
  --name $aksName `
  --query id `
  -o tsv 2>$null

$identityStillExists = az identity show `
  --resource-group $resourceGroup `
  --name $identityName `
  --query id `
  -o tsv 2>$null

[pscustomobject]@{
  ResourceGroupExists     = (az group exists --name $resourceGroup)
  AKSExists               = [bool]$aksStillExists
  NodeResourceGroupExists = (az group exists --name $nodeResourceGroup)
  IdentityExists          = [bool]$identityStillExists
} | Format-List
```

预期：

```text
ResourceGroupExists     : true
AKSExists               : false
NodeResourceGroupExists : false
IdentityExists          : false
```

再次列出共享 RG，确认 Azure OpenAI、ACR 和监控资源仍在：

```powershell
az resource list `
  --resource-group $resourceGroup `
  --query "[].{name:name,type:type}" `
  -o table
```

### 8.3 场景 C：Kubernetes 和云资源残留检查

```powershell
kubectl get namespace $namespace 2>$null
helm list --all-namespaces
kubectl get clusterissuer letsencrypt-prod 2>$null
kubectl get customresourcedefinition |
  Select-String -Pattern 'cert-manager.io|acme.cert-manager.io'
```

如果已卸载专用 ingress-nginx/cert-manager，相关 Release、namespace、ClusterIssuer 和 CRD 均不应出现。

验证 PostgreSQL PV 已回收：

```powershell
if ($postgresPv) {
  kubectl get persistentvolume $postgresPv 2>$null
}
```

如果 PV 仍存在，查看 StorageClass 的 `reclaimPolicy`。`Retain` 策略需要在确认备份后手工删除 PV 和对应 Azure Disk。

验证旧公网 IP 不再由节点 RG 持有：

```powershell
foreach ($ip in @($ingressIp, $litellmServiceIp) | Where-Object { $_ }) {
  az network public-ip list `
    --resource-group $nodeResourceGroup `
    --query "[?ipAddress=='$ip'].{name:name,ip:ipAddress}" `
    -o table
}
```

应无匹配结果。Azure Load Balancer/Public IP 回收可能需要数分钟。

验证保留 VMSS 不再引用 UAMI：

```powershell
foreach ($vmssName in $vmssNames) {
  az vmss show `
    --resource-group $nodeResourceGroup `
    --name $vmssName `
    --query identity.userAssignedIdentities `
    -o json
}
```

输出中不应再包含 `$identityId`。

### 8.4 验证跨订阅 RBAC 已删除

Principal 已删除后仍使用之前保存的 `$principalId` 和 Scope 检查：

```powershell
$remainingRoleAssignments = @()

foreach ($account in $config.'azure-openai-list') {
  $accountSubscription = if ($account.subscription_id) {
    $account.subscription_id
  } else {
    $subscriptionId
  }
  $accountName = ([uri]$account.endpoint).Host.Split('.')[0]
  $scope = "/subscriptions/$accountSubscription/resourceGroups/$($account.resource_group)/providers/Microsoft.CognitiveServices/accounts/$accountName"

  $items = az role assignment list `
    --subscription $accountSubscription `
    --assignee-object-id $principalId `
    --scope $scope `
    --fill-principal-name false `
    -o json | ConvertFrom-Json

  $remainingRoleAssignments += $items
}

"Remaining role assignments: $($remainingRoleAssignments.Count)"
```

预期：

```text
Remaining role assignments: 0
```

RBAC 数据可能存在短暂最终一致性延迟。如果刚删除后仍能看到 Assignment，可稍后重查；不要误删其他 Principal 的权限。

对于确认专用的 UAMI，还应重复第 4.4 节的全订阅查询。`$allIdentityAssignments` 最终也必须为空；否则可能存在当前 JSON 未包含的历史 Scope 残留。

### 8.5 验证 DNS 和应用入口

```powershell
Resolve-DnsName $hostname -Server 1.1.1.1 -ErrorAction SilentlyContinue

try {
  Invoke-WebRequest "https://$hostname/v1/models" `
    -TimeoutSec 15 `
    -ErrorAction Stop
  throw "Old LiteLLM endpoint is still reachable"
} catch {
  "Endpoint unavailable as expected: $($_.Exception.Message)"
}
```

如果域名已经切换到新网关，不应要求请求失败，而应确认它解析到新 IP 且不再到达旧 AKS。

### 8.6 清理本地 kubeconfig

删除 AKS 后，`az aks get-credentials` 写入的本地 context 不会自动删除：

```powershell
kubectl config get-contexts

if ((kubectl config current-context) -eq $aksName) {
  # 先切换到另一个有效 context；没有其他集群时可跳过此命令。
  # kubectl config use-context <其他 context>
}

kubectl config delete-context $aksName
kubectl config delete-cluster $aksName
kubectl config unset "users.clusterUser_${resourceGroup}_${aksName}"
kubectl config unset "users.clusterAdmin_${resourceGroup}_${aksName}"
```

如果保留 AKS，不要删除 kubeconfig context。

## 9. 最终验收清单

### 删除专用 AKS、保留共享 RG

- [ ] 外部 DNS 不再指向旧 AKS 公网 IP；
- [ ] `az aks show` 找不到 `$aksName`；
- [ ] `$nodeResourceGroup` 已删除；
- [ ] `$identityName` 已删除；
- [ ] 所有配置 AOAI Scope 下该 `$principalId` 的 Role Assignment 为 0；
- [ ] 共享 RG 中 Azure OpenAI、ACR、监控和其他业务资源仍存在；
- [ ] 本地旧 kubeconfig context 已清理；
- [ ] 备份文件已移入受控存储或按策略销毁。

### 仅删除共享 AKS 中的 LiteLLM

- [ ] `$namespace` 不存在；
- [ ] PostgreSQL PV 和 Azure Disk 已按预期回收；
- [ ] 旧 LoadBalancer/Public IP 不存在；
- [ ] LiteLLM 专用 ClusterIssuer、Helm Release 和 CRD 已按所有权清理；
- [ ] 保留的 VMSS 不再引用 LiteLLM UAMI；
- [ ] UAMI 与 AOAI Role Assignment 已删除；
- [ ] 其他 namespace、Ingress、Certificate 和应用未受影响。

## 10. 不应作为“删除完成”证据的现象

- LiteLLM URL 暂时访问失败：可能只是 Pod 停止，Azure Disk、Public IP、AKS 和 RBAC 仍在；
- Azure Resource Graph 返回空：ARG 可能有订阅过滤或索引延迟，应以 Azure Resource Manager/CLI、AKS API 和 Kubernetes API 复核；
- `kubectl get namespace litellm` 返回 NotFound：只能证明 namespace 不存在，不能证明 AKS、节点 RG、UAMI 和 RBAC 已清理；
- `az aks delete --no-wait` 已接受：只代表删除请求提交，必须等待 AKS 和节点 RG 都不存在；
- UAMI 已删除：不能假设跨订阅 Role Assignment 已立即消失，仍需按保存的 Principal ID 检查。