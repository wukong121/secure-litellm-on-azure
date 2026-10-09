# LiteLLM 官方镜像拉取与旧修复镜像迁移说明

> **历史镜像取证/旧脚本参考。** 当前West US 3私有网关使用LiteLLM `1.104.0`固定源码供应链与批准的私有ACR digest，不是下文`1.95.0`默认镜像或个人fork。旧HTTPS/WS/图像回归不能证明当前版本或Front Door入口支持同样协议；当前构建/发布见[本地Stage2–9指南](../local_execution/stage2-9-guide-zh.md)及[升级验证边界](../docs/litellm-1.104.0-upgrade-validation-2026-10-07.md)。

## 背景与问题本质

旧部署脚本默认使用官方镜像 **`docker.litellm.ai/berriai/litellm:1.95.0`**。该版本在旧参考链路实测通过HTTPS API Key认证（HTTP 200）及Responses WebSocket握手（HTTP 101），不是当前网关版本证明。

镜像默认值在 [`deploy_mi_aks_litellm.py`](deploy_mi_aks_litellm.py) 中，可用环境变量 `LITELLM_IMAGE` 覆盖：

```python
"litellm_image": os.environ.get(
  "LITELLM_IMAGE", "docker.litellm.ai/berriai/litellm:1.95.0"
),
```

此前默认的 **`micl/litellm:mi-fix-image-gen`** 是基于 LiteLLM `1.81.16` 构建的个人 fork。下面保留其取证结果，用于说明升级到官方镜像后仍需回归的行为，不再建议将该不可追溯镜像用于新部署。

对运行镜像 `sha256:9473d7409f5afab2851e08a56af1b1df3ac28492bb0b0b156f467952dccb438f` 与 LiteLLM `1.81.16` 基线进行源码对比后，能够确认的核心修复是：**Azure image generation 使用 Managed Identity 时，为底层 raw HTTP 请求补上 Entra Bearer Token**。

普通 Azure SDK 调用会在客户端内部处理 `azure_ad_token_provider`；但该版本的 image generation 路径最终调用 raw `httpx`。官方基线虽然在 `initialize_azure_sdk_client()` 中解析出了 Managed Identity Token Provider，却没有把 token 写入 raw HTTP 请求头，导致不使用 API Key 的 image generation 请求无法完成 Azure 认证。

自定义 fork 在 `litellm/llms/azure/azure.py` 中增加了以下逻辑：

```python
if api_key is None and "Authorization" not in headers:
    resolved_provider = azure_client_params.get("azure_ad_token_provider")
    resolved_token = azure_client_params.get("azure_ad_token")
    if resolved_provider is not None and resolved_token is None:
        resolved_token = resolved_provider()
    if resolved_token:
        headers.pop("api-key", None)
        headers["Authorization"] = f"Bearer {resolved_token}"
```

处理流程为：

```text
ManagedIdentityCredential
  -> initialize_azure_sdk_client() 解析 Token Provider
  -> 自定义补丁调用 Token Provider
  -> 删除空的 api-key header
  -> 写入 Authorization: Bearer <Entra Token>
  -> raw httpx image generation 请求通过 Azure RBAC 认证
```

项目曾观察到 Codex Desktop 走 Responses API 时的 `image_gen` namespace 错误：

```text
litellm.BadRequestError: AzureException BadRequestError - {
  "error": {
    "message": "Invalid Value: 'tools.namespace'. User-defined namespace 'image_gen' collides with an existing tool namespace.",
    "type": "invalid_request_error",
    "param": "tools.namespace",
    "code": null
  }
}. Received Model Group=gpt-5.6-sol
```

但当前取证没有发现镜像对 `image_gen` namespace 做重命名、删除或改写：Azure Responses 转换仍会原样透传 `type=namespace, name=image_gen`。因此不能再把 namespace collision 的消失直接归因于该镜像补丁，它还可能受到 Codex 版本、模型能力、Azure 后端行为或请求中是否同时包含内置 `image_generation` 工具的影响。

> **迁移结论：** Responses WebSocket 已在官方 `1.95.0` 镜像上验证通过。Azure image generation 的 Managed Identity 认证仍需单独执行端到端回归；若新版仍缺失该行为，应在 `1.95.0` 明确源码基线上重放最小认证补丁，而不是继续依赖不可追溯的个人镜像。

### 客户拉不到镜像的常见原因

- 企业网络禁止访问 Docker Hub
- 只允许白名单/受信任的镜像仓库
- 镜像域名不在企业网络白名单中
- 集群无公网出口

---

## 方案 A（首选）：用 `az acr import` 镜像进客户自己的 ACR

`az acr import` 在 **Azure 骨干网服务端**去 Docker Hub 拉取再写入客户 ACR，**不经过客户的受限网络出口**，所以即使客户集群/网络禁了 Docker Hub 也能成功。

### 步骤 1：创建 ACR（一次性，客户订阅里）

```powershell
$rg   = "<客户RG>"
$acr  = "<全局唯一的ACR名>"      # 只能小写字母+数字，如 litellmacr2401
$loc  = "<区域>"                  # 例如 eastus2，建议与 AKS 同区域

az acr create -g $rg -n $acr --sku Basic -l $loc
```

- SKU 选 **`Basic`** 即可（只放一个镜像，最便宜，10GB 容量）。
- ACR 名需**全局唯一**、只能小写字母数字、5–50 字符。
- 登录服务器地址为 `<acr>.azurecr.io`。

> **可能遇到的报错：`MissingSubscriptionRegistration`**
>
> ```text
> (MissingSubscriptionRegistration) The subscription is not registered to use
> namespace 'Microsoft.ContainerRegistry'.
> ```
>
> 说明订阅还没注册 ACR 的资源提供程序（订阅级一次性操作）。先注册再重建：
>
> ```powershell
> # 触发注册（异步，通常 1–2 分钟）
> az provider register --namespace Microsoft.ContainerRegistry
>
> # 轮询直到变成 Registered
> do {
>   Start-Sleep 15
>   $state = az provider show -n Microsoft.ContainerRegistry --query "registrationState" -o tsv
>   "state: $state"
> } while ($state -ne "Registered")
>
> # 注册完成后重跑创建
> az acr create -g $rg -n $acr --sku Basic -l $loc
> ```

### 步骤 2：服务端导入镜像

```powershell
az acr import `
  --name $acr `
  --source docker.litellm.ai/berriai/litellm:1.95.0 `
  --image litellm:1.95.0
```

### 步骤 3：给 AKS 授 AcrPull 权限

```powershell
az aks update -g <客户RG> -n <客户AKS> --attach-acr $acr
```

`--attach-acr` 会自动给 AKS 的 kubelet 托管身份授 **AcrPull** 角色，无需手动配置 `imagePullSecret`。

### 步骤 4：用新镜像地址重新部署

```powershell
$env:LITELLM_IMAGE = "$acr.azurecr.io/litellm:1.95.0"
python LiteLLM/deploy_mi_aks_litellm.py
```

> `$rg`、`$acr` 这类**普通 PowerShell 变量**用于当前会话里的 `az` 命令即可；唯独最后镜像地址要用 `$env:LITELLM_IMAGE`，因为它需要被 python **子进程**继承。以上命令请在**同一个 pwsh 窗口**里连续执行。

---

## 方案 B：从能联网的机器中转推送

如果连 `az acr import` 都被策略拦截（极少见），在**能访问 LiteLLM 官方镜像仓库的机器**上中转：

```powershell
docker pull docker.litellm.ai/berriai/litellm:1.95.0
docker tag  docker.litellm.ai/berriai/litellm:1.95.0 $acr.azurecr.io/litellm:1.95.0
az acr login -n $acr
docker push $acr.azurecr.io/litellm:1.95.0
```

**完全离线**场景：

```powershell
docker save docker.litellm.ai/berriai/litellm:1.95.0 -o litellm-1.95.0.tar
# 通过 U盘/内网把 tar 文件传给客户，然后在客户环境：
docker load -i litellm-1.95.0.tar
docker tag  docker.litellm.ai/berriai/litellm:1.95.0 <客户registry>/litellm:1.95.0
docker push <客户registry>/litellm:1.95.0
```

同样适用于客户内网自建仓库（Harbor 等）。

---

## 方案 C：仅在 MI 图像生成回归失败时重建补丁

如果官方 `1.95.0` 的 Azure image generation Managed Identity 端到端测试失败，再基于这个明确版本重放最小补丁，并将结果放入客户自己的 registry：

```dockerfile
FROM docker.litellm.ai/berriai/litellm:1.95.0
# 应用 Azure image generation Managed Identity 认证补丁
# COPY patched_azure.py /usr/lib/python3.13/site-packages/litellm/llms/azure/azure.py
```

建议不要覆盖整个 `azure.py`，而是在选定的新版官方源码上重放上文 Bearer Token 注入逻辑并增加回归测试。运行镜像相对 `1.81.16` 基线还有数十个其他源码差异，因此镜像整体不能被视为只有一个干净补丁；重建时只迁移经过验证且仍未被上游修复的最小逻辑。

---

## 方案 D（最不推荐）：imagePullSecrets

仅当拦截只是 Docker Hub 匿名限速/需要登录（而非硬性网络封锁）时才有用 —— 添加一个带 Docker Hub 凭据的 pull secret。对企业策略封锁无效，不建议作为主方案。

---

## 方案对比

| 方案 | 适用场景 | 推荐度 |
|---|---|---|
| **A. `az acr import` 进客户 ACR** | 客户有/可建 ACR，网络禁 Docker Hub | ⭐⭐⭐⭐⭐ 首选 |
| **B. 中转/离线推送** | 连 `acr import` 都被拦，或完全离线 | ⭐⭐⭐⭐ |
| **C. 官方镜像重建补丁** | 仅 MI 图像生成回归失败 | ⭐⭐ 条件性方案 |
| **D. imagePullSecrets** | 仅 Docker Hub 限速/登录问题 | ⭐ 兜底 |

---

## 一键脚本（方案 A 完整流程）

填好前 4 个变量即可整段运行：

```powershell
# ==== 按客户环境填写 ====
$sub  = "<客户订阅ID>"
$rg   = "<客户RG>"
$aks  = "<客户AKS名>"
$acr  = "<全局唯一ACR名>"       # 小写字母+数字
$loc  = "<区域>"                 # 建议与 AKS 同区域
# ========================

az account set --subscription $sub

# 1. 创建 ACR（已存在会报错，可忽略或跳过）
# 若报 MissingSubscriptionRegistration，先注册资源提供程序：
az provider register --namespace Microsoft.ContainerRegistry
do {
  Start-Sleep 15
  $state = az provider show -n Microsoft.ContainerRegistry --query "registrationState" -o tsv
  "provider: $state"
} while ($state -ne "Registered")

az acr create -g $rg -n $acr --sku Basic -l $loc

# 2. 服务端导入官方 LiteLLM 1.95.0 镜像
az acr import --name $acr `
  --source docker.litellm.ai/berriai/litellm:1.95.0 `
  --image litellm:1.95.0

# 3. 授权 AKS 拉取
az aks update -g $rg -n $aks --attach-acr $acr

# 4. 用新镜像重新部署
$env:LITELLM_IMAGE = "$acr.azurecr.io/litellm:1.95.0"
python LiteLLM/deploy_mi_aks_litellm.py
```
