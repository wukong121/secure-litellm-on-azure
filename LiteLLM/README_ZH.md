# LiteLLM旧网关参考与OSS集成

本目录保留原LiteLLM on AKS部署脚本与运维手册，作为既有客户网关的迁移基线；同时包含尚未接入部署或可靠接收器的[OSS审计回调适配器](observability/oss_audit_callback.py)。

当前客户方案入口见[项目总览](../README_ZH.md)和[安全增强迁移指南](../docs/customer-migration-guide-zh.md)，通过[Bicep](../infra/README_ZH.md)、[Kustomize](../deploy/README_ZH.md)和客户Environment配置交付。以下旧脚本不是安全增强版的原地升级入口，不得不经备份和差异审查直接重跑生产部署。旧公网入口、集群内单副本数据库与直接UI访问说明不代表新安全基线。

## 📂 结构

- `deploy_mi_aks_litellm.py`: AKS、Managed Identity、PostgreSQL 和 LiteLLM Proxy 部署脚本。
- `azure-openai.json`: 只能保存可提交的占位模板，禁止填写真实订阅 ID、资源名或 Endpoint。
- `azure-openai.loc.json`: 本机实际部署配置（已被 `.gitignore` 忽略，不要提交）。
- `USER_BUDGET_AND_MODEL_ACCESS_ZH.md`: 用户、Team、Virtual Key、预算和模型权限配置指南。
- FOUNDRY_MODEL_SYNC_ZH.md: Foundry 新增模型 deployment 后同步到 LiteLLM 的操作指南。
- `RESOURCE_CLEANUP_ZH.md`: 删除脚本创建/修改的 AKS、Managed Identity、RBAC 和 Kubernetes 资源，并验证无残留。
- `POSTGRESQL_CAPACITY_AND_SPEND_LOG_RETENTION_ZH.md`: PostgreSQL 数据影响、PVC 扩容、Spend Logs retention、kubectl 手工修复和脚本修复手册。
- `CODEX_0147_EMPTY_FUNCTIONS_DESCRIPTION_WORKAROUND_ZH.md`: Codex 0.147 Responses Lite 空 namespace description 的 LiteLLM Custom Callback 兼容方案。
- `litellm.config.yaml`: 部署脚本根据 JSON 自动生成的 LiteLLM 配置，不建议手工修改。

*注：测试与依赖项已整合至项目根目录 (`../tests/` 与 `../requirements.txt`)。*

## 旧网关参考用法

仅用于已批准的旧环境维护或隔离参考部署。真实配置使用忽略的本地文件，旧凭据必须在重新运行前从客户秘密管理系统正确注入。

部署脚本生成的 LiteLLM Deployment现在包含 startup、readiness和 liveness probes，以及保守的 `250m/1Gi` requests和 `1000m/2Gi` limits。集群内 PostgreSQL使用 `pg_isready`作为三类探针。当前两个工作负载均为单副本，应用这些 Pod模板变更会触发滚动重启，必须在维护窗口内逐个更新和验证。

部署脚本更新 `litellm-env` 时仅保留既有 `LITELLM_SALT_KEY`，避免永久 Salt被整份 Secret替换时静默删除，同时不会累积未知或过期键。该保护不代表可以直接进行 Salt迁移：生产设置 Salt前仍必须在隔离数据库中验证旧密文兼容性和 Master Key解耦。

### 1. 准备本地配置

```powershell
# 如果当前位于 LiteLLM 目录，先返回项目根目录安装依赖
cd ..
python -m pip install -r .\requirements.txt

# 登录 Azure 并明确选择创建 AKS 所使用的订阅
az login
$env:AZURE_SUBSCRIPTION_ID = "<AKS 所在订阅 ID>"
az account set --subscription $env:AZURE_SUBSCRIPTION_ID

# 返回 LiteLLM 目录，首次使用时从模板创建本地配置
cd .\LiteLLM
if (-not (Test-Path .\azure-openai.loc.json)) {
  Copy-Item .\azure-openai.json .\azure-openai.loc.json
}
```

编辑 `azure-openai.loc.json`，填写实际的区域、资源组、Azure OpenAI 资源和模型 deployment。脚本默认优先读取该文件；不存在时才读取模板 `azure-openai.json`，因此不需要在两者之间手工复制更新。

新配置使用`resource_group`指定创建或复用Resource Group、Managed Identity和AKS的资源组。旧`apim_resource_group`仅作为向后兼容输入保留；新旧字段同时存在且不同会在部署前拒绝。模板已移除不使用的`apim_name`。脚本还会根据`azure-openai-list[].subscription_id`为跨订阅Azure OpenAI资源分配Managed Identity权限。

### 2. 设置部署环境变量

以下仅为旧网关绑定域名和HTTPS的参考设置，不是安全增强版的推荐生产部署入口。`LETSENCRYPT_EMAIL`是Let's Encrypt的ACME账户和证书通知邮箱。

```powershell
# Azure 和 AKS
$env:AZURE_SUBSCRIPTION_ID = "<AKS 所在订阅 ID>"
$env:AKS_NAME = "litellm-mi-aks"
$env:AKS_VM_SIZE = "Standard_D2s_v3"
$env:AKS_NODE_COUNT = "1"

# 可选：覆盖默认的官方 LiteLLM 1.95.0 镜像；私有 ACR 需提前授予 AKS AcrPull 权限
$env:LITELLM_IMAGE = "<acr-name>.azurecr.io/litellm:1.95.0"

# 生产环境建议固定注入；至少 24 个字符，不要提交到 Git
$env:LITELLM_MASTER_KEY = "sk-<强随机密钥>"
$env:PG_PASSWORD = "<强随机数据库密码>"

# 设置域名会启用 ingress-nginx、cert-manager 和 HTTPS
$env:LITELLM_HOSTNAME = "litellm.example.com"
$env:LETSENCRYPT_EMAIL = "admin@example.com"
```

> 若未设置 `LITELLM_MASTER_KEY`，脚本默认自动生成强随机 Key，并在部署结束时显示一次。重新部署时也会再次生成新 Key，使旧 Master Key 失效，因此生产环境应始终从 Key Vault 或其他密钥管理系统注入固定值。

### 3. 执行部署

```powershell
python .\deploy_mi_aks_litellm.py
```

也可以显式传入其他配置文件：

```powershell
python .\deploy_mi_aks_litellm.py .\customer-a.loc.json
```

### 支持的环境变量

环境变量必须在启动 Python 进程前设置。

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `AZURE_SUBSCRIPTION_ID` | 当前 Azure CLI 订阅 | 创建 AKS、Managed Identity 和资源组的订阅；建议显式设置 |
| `MI_NAME` | `litellm-managed-identity` | User Assigned Managed Identity 名称 |
| `AKS_NAME` | `litellm-mi-aks` | AKS 名称 |
| `AKS_NODE_COUNT` | `1` | AKS 节点数 |
| `AKS_VM_SIZE` | `Standard_D2s_v3` | AKS 节点规格 |
| `AKS_NAMESPACE` | `litellm` | Kubernetes namespace |
| `LITELLM_IMAGE` | `docker.litellm.ai/berriai/litellm:1.95.0` | LiteLLM 官方容器镜像；生产环境可导入客户 ACR 后显式覆盖 |
| `LITELLM_MASTER_KEY` | 空 | 管理员 Key，至少 24 个字符；为空时按下一项决定是否生成 |
| `AUTO_GENERATE_MASTER_KEY` | `true` | Master Key 为空时是否自动生成；设为 `false` 可强制要求外部注入 |
| `STORE_MODEL_IN_DB` | `false` | 设为 `true` 后允许通过 Admin UI/API 持久化管理模型和 Router Settings；配置保存在 PostgreSQL |
| `DISABLE_SPEND_LOGS` | `false` | 设为 `true` 后不再写入逐请求 Spend Logs；会失去明细审计和部分用量分析能力 |
| `STORE_PROMPTS_IN_SPEND_LOGS` | `false` | 是否将 Prompt 和 Response 正文写入 Spend Logs；生产环境建议保持关闭 |
| `MAXIMUM_SPEND_LOGS_RETENTION_PERIOD` | `7d` | Spend Logs 保留期；设为空字符串可关闭自动清理，但不建议用于有限容量 PVC |
| `MAXIMUM_SPEND_LOGS_RETENTION_INTERVAL` | `1d` | Spend Logs 清理任务执行间隔 |
| `LITELLM_AFFINITY_CHECKS` | `responses_api_deployment_check,deployment_affinity,session_affinity` | Responses API 会话亲和检查；逗号分隔，设为空可关闭 |
| `DEPLOYMENT_AFFINITY_TTL_SECONDS` | `3600` | Session ID / Virtual Key 亲和映射的 TTL（秒） |
| `LITELLM_STARTUP_WAIT_SECONDS` | `180` | 等待 Prisma migration 和 Uvicorn 启动的秒数 |
| `LITELLM_HOSTNAME` | 空 | 域名；非空时启用 Ingress + HTTPS，否则使用公网 `LoadBalancer:4000` |
| `LETSENCRYPT_EMAIL` | 空 | 启用域名时必填的 Let's Encrypt 邮箱 |
| `INGRESS_PROXY_BODY_SIZE` | `100m` | NGINX 最大请求体 |
| `INGRESS_PROXY_BUFFERING` | `off` | NGINX 响应缓冲设置，流式输出建议保持关闭 |
| `INGRESS_PROXY_READ_TIMEOUT` | `600` | NGINX 上游读取超时（秒） |
| `INGRESS_PROXY_SEND_TIMEOUT` | `600` | NGINX 上游发送超时（秒） |
| `AZURE_SCOPE` | `https://cognitiveservices.azure.com/.default` | Managed Identity 获取令牌的 scope |
| `AZURE_API_VERSION` | 空 | Smoke test 使用的 Azure API version 查询参数 |
| `OPENAI_ROLE_NAME` | `Cognitive Services OpenAI User` | 分配给 Managed Identity 的角色 |
| `RUN_SMOKE_TEST` | `true` | LoadBalancer 模式下是否执行部署后测试；Ingress 模式会跳过 |
| `PG_USER` | `litellm` | PostgreSQL 用户名 |
| `PG_PASSWORD` | `litellm-local-dev` | PostgreSQL 密码；生产环境必须覆盖 |
| `PG_DB` | `litellm` | PostgreSQL 数据库名 |
| `PG_STORAGE` | `20Gi` | 新建 PostgreSQL PVC 容量；修改该值不会自动扩容已有 PVC |
| `EXPAND_EXISTING_PG_PVC` | `false` | 设为 `true` 后允许脚本将已有 PG PVC 扩大到 `PG_STORAGE`；不支持缩容 |

默认 AKS 规格为：

```text
区域：由 azure-openai.loc.json 的 region 决定
节点数：1
VM：Standard_D2s_v3
```

如果目标订阅或区域不支持该规格，可以临时指定：

```powershell
$env:AKS_VM_SIZE = "Standard_B2ms"
```

## 🔐 用户预算和模型权限

如果需要在 Admin UI 中新增模型或修改 Router Settings，部署前启用数据库配置存储：

```powershell
$env:STORE_MODEL_IN_DB = "true"
python .\deploy_mi_aks_litellm.py
```

该变量必须进入 AKS Pod；只在本机设置但不重新运行脚本，不会影响已经运行的 LiteLLM。启用后，UI 模型与路由设置保存在 PostgreSQL，重启 Pod 后仍然存在。不要长期同时用 UI 数据库和 `azure-openai*.json` 管理同一条模型 Deployment，否则可能出现重复或配置漂移。

部署完成后，打开：

```text
http://<AKS LoadBalancer IP>:4000/ui
```

然后通过 `Internal Users`、`Teams` 和 `Virtual Keys` 配置：

- 每个用户的预算和预算周期；
- Team 共享预算；
- Virtual Key 可访问的模型白名单；
- 用户和 Team 的用量统计。

详细步骤见 [`USER_BUDGET_AND_MODEL_ACCESS_ZH.md`](./USER_BUDGET_AND_MODEL_ACCESS_ZH.md)。

多 Foundry Resource 下的 Responses API 会话亲和配置见 [`SESSION_AFFINITY_ROUTING_ZH.md`](./SESSION_AFFINITY_ROUTING_ZH.md)。

## PostgreSQL 容量与 Spend Logs

启用数据库后，LiteLLM 会保存 Virtual Key、用户、团队、预算、UI 配置以及逐请求 Spend Logs。调用量较大时，`LiteLLM_SpendLogs` 通常是增长最快的表。默认配置保留 7 天明细，并关闭 Prompt/Response 正文存储。

扩容已有集群前应先备份数据库，并确认 StorageClass 支持 `allowVolumeExpansion`。重跑部署脚本前还必须通过安全方式注入客户当前使用的 `LITELLM_MASTER_KEY`、`PG_PASSWORD` 和其他既有环境变量，避免意外轮换 Key 或覆盖数据库连接配置。例如将现有 PVC 扩到 50 GiB：

```powershell
$env:PG_STORAGE = "50Gi"
$env:EXPAND_EXISTING_PG_PVC = "true"
# 通过安全 Secret 流程注入现有 LITELLM_MASTER_KEY 和 PG_PASSWORD 后再执行
python .\deploy_mi_aks_litellm.py
kubectl get pvc pg-data -n litellm
```

扩容只能增加容量，不能缩小。PostgreSQL 删除过期行后通常会复用空间，但文件系统使用量不一定立即下降。生产环境应设置 PVC 使用率告警；高调用量或有 HA/RTO 要求时，应使用 Azure Database for PostgreSQL Flexible Server，而不是 AKS 内单副本 PostgreSQL。

默认的 LiteLLM `1.95.0` 镜像已实测通过 HTTPS API Key 认证（HTTP 200）和 Responses WebSocket 握手（HTTP 101）。迁移自 `micl/litellm:mi-fix-image-gen` 时，还应单独回归 Azure image generation 的 Managed Identity 认证；旧镜像包含的相关自定义补丁不能假定已由新版完整覆盖。详见 [`IMAGE_PULL_SOLUTION_ZH.md`](./IMAGE_PULL_SOLUTION_ZH.md)。

使用真实 Codex 推理和 ingress `101` 日志验证 WebSocket 链路，见 [`CODEX_LITELLM_WEBSOCKET_VALIDATION_ZH.md`](./CODEX_LITELLM_WEBSOCKET_VALIDATION_ZH.md)。

## 🧪 验证部署

实际测试脚本文件是 `../tests/test_all_deployments.py`：

```powershell
python ..\tests\test_all_deployments.py `
  --config .\azure-openai.loc.json `
  --base-url "http://<AKS LoadBalancer IP>:4000" `
  --api-key "<LiteLLM Virtual Key>" `
  --prompt ok
```

测试会验证 OpenAI 风格和 Azure OpenAI 风格的 Chat 路由。使用 Windows 默认控制台时，建议传入 ASCII prompt，避免 `cp1252` 无法输出中文造成测试脚本提前退出。

## ⚠️ 注意事项

- 不要把管理员 Master Key 分发给普通用户；应为每个用户创建 Virtual Key。
- 未设置 `LITELLM_HOSTNAME` 时使用公网 `LoadBalancer:4000`；生产环境建议配置域名、TLS、网络访问限制和强随机 Key。
- PostgreSQL 当前是 AKS 内单副本部署，适合验证和轻量场景；生产环境建议使用高可用数据库和备份。

## 🌐 绑定自有域名并启用 HTTPS

想通过 `https://litellm.你的域名.com` 访问（而不是 `http://<IP>:4000`），请按上面的生产配置同时设置 `LITELLM_HOSTNAME` 和 `LETSENCRYPT_EMAIL`。脚本会自动配置 ingress-nginx + cert-manager（Let's Encrypt 证书）。

```powershell
$env:LITELLM_HOSTNAME = "litellm.example.com"   # 触发 Ingress 模式
$env:LETSENCRYPT_EMAIL = "you@example.com"       # Let's Encrypt 证书邮箱（必填）
$env:LITELLM_IMAGE = "<acr-name>.azurecr.io/litellm:1.95.0"
$env:LITELLM_MASTER_KEY = "sk-<强随机密钥>"
python .\deploy_mi_aks_litellm.py
```

- 前置条件：本机已安装 Helm 和 kubectl。
- 脚本结束会打印 ingress 公网 IP，你到阿里云 DNS 加一条 A 记录指向它，DNS 生效后证书自动签发。
- 不设 `LITELLM_HOSTNAME` 时保持原有 `LoadBalancer:4000` 行为。

完整步骤（买域名、DNS、证书、验证、排查、回滚）见 [`CUSTOM_DOMAIN_SETUP_ZH.md`](./CUSTOM_DOMAIN_SETUP_ZH.md)。