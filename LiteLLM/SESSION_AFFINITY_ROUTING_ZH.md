# LiteLLM Responses API 会话亲和路由

本文针对当前架构：同一个 `model_name` 对应多个 Azure OpenAI / Foundry Resource，LiteLLM 使用 `simple-shuffle` 在这些 Deployment 之间选择后端。

## 推荐配置

```yaml
router_settings:
  routing_strategy: simple-shuffle
  num_retries: 2
  optional_pre_call_checks:
    - responses_api_deployment_check
    - deployment_affinity
    - session_affinity
  deployment_affinity_ttl_seconds: 3600
```

三种检查按精确度互补：

| 检查 | 亲和依据 | 适用情况 | 影响 |
| --- | --- | --- | --- |
| `responses_api_deployment_check` | `previous_response_id` 中的原始 Deployment | Codex / Responses API 连续对话 | 最精确，不会影响无 Response ID 的请求 |
| `session_affinity` | 请求 metadata 中稳定的 `session_id` | 客户端明确传递 Session ID | 同一会话在 TTL 内固定后端 |
| `deployment_affinity` | LiteLLM Virtual Key | 没有上述字段时的兜底 | 同一 Key 在 TTL 内固定后端，降低跨区域均衡能力 |

当前建议每位研发人员使用独立 Virtual Key。若多人共享同一个 Key，`deployment_affinity` 会把这些人的请求共同固定到一个后端，不利于负载均衡。

## 对缓存命中率的影响

亲和路由让同一会话更可能回到同一个 Azure OpenAI Resource / Deployment，因此有助于满足服务端 Prompt Cache 的命中条件。但它不保证命中：Prompt 前缀仍需足够长且保持一致，模型、Deployment 和相关请求参数也必须兼容缓存规则。

不要把 LiteLLM 的 Response Cache 与这里的 Deployment Affinity 混淆：前者可能直接返回缓存响应，后者只是选择同一个上游模型后端。

## 方式一：修改部署脚本并重新执行

部署脚本现在支持：

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `LITELLM_AFFINITY_CHECKS` | `responses_api_deployment_check,deployment_affinity,session_affinity` | 逗号分隔；设为空字符串可关闭全部亲和检查 |
| `DEPLOYMENT_AFFINITY_TTL_SECONDS` | `3600` | Session ID / Virtual Key 亲和映射的 TTL |

执行：

```powershell
$env:LITELLM_AFFINITY_CHECKS = `
  "responses_api_deployment_check,deployment_affinity,session_affinity"
$env:DEPLOYMENT_AFFINITY_TTL_SECONDS = "3600"

# 保留当前生产部署所需的其他变量，例如固定 Master Key、数据库密码、域名和镜像。
python .\LiteLLM\deploy_mi_aks_litellm.py .\LiteLLM\azure-openai.loc.json
```

脚本会重新生成 `litellm.config.yaml`、更新 ConfigMap，并通过 config hash 触发 LiteLLM Pod 滚动更新。

如果只需要 `previous_response_id` 连续性、但不希望按 Virtual Key 固定后端：

```powershell
$env:LITELLM_AFFINITY_CHECKS = "responses_api_deployment_check"
```

## 方式二：只用 kubectl 修改当前集群

以下命令读取现有 ConfigMap，保留 `model_list`，只更新 `router_settings`。

```powershell
$namespace = "litellm"
$configMap = "litellm-config"
$deployment = "litellm-mi-proxy"
$configFile = Join-Path $env:TEMP "litellm-config-affinity.yaml"

# 1. 备份当前 ConfigMap
kubectl get configmap $configMap -n $namespace -o yaml `
  | Set-Content "$configFile.backup" -Encoding utf8

# 2. 提取内嵌的 LiteLLM config.yaml
kubectl get configmap $configMap -n $namespace `
  -o jsonpath="{.data.config\.yaml}" `
  | Set-Content $configFile -Encoding utf8

# 3. 使用 PyYAML 结构化更新 router_settings
$env:LITELLM_CONFIG_PATH = $configFile
@'
import os
from pathlib import Path

import yaml

path = Path(os.environ["LITELLM_CONFIG_PATH"])
config = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
router = config.setdefault("router_settings", {})
router["optional_pre_call_checks"] = [
    "responses_api_deployment_check",
    "deployment_affinity",
    "session_affinity",
]
router["deployment_affinity_ttl_seconds"] = 3600
path.write_text(
    yaml.safe_dump(config, sort_keys=False, allow_unicode=True),
    encoding="utf-8",
)
'@ | python -
Remove-Item Env:LITELLM_CONFIG_PATH

# 4. 更新 ConfigMap
kubectl create configmap $configMap -n $namespace `
  --from-file="config.yaml=$configFile" `
  --dry-run=client -o yaml `
  | kubectl apply -f -

# 5. ConfigMap 通过 subPath 挂载，不会热更新；必须重启 Pod
kubectl rollout restart deployment/$deployment -n $namespace
kubectl rollout status deployment/$deployment -n $namespace --timeout=10m

# 6. 验证新 Pod 看到的配置
kubectl exec -n $namespace deployment/$deployment -- `
  python -c "import yaml; c=yaml.safe_load(open('/app/config/config.yaml')); print(c['router_settings'])"
```

当前环境数据库中没有 `router_settings` 覆盖值。如果以后通过 Admin UI 保存了 Router Settings，数据库中的同名字段会覆盖 ConfigMap/YAML；此时应在 UI 中同步修改或选择单一配置权威来源。

### 手工方式回滚

从 ConfigMap 删除亲和配置并重启：

```powershell
$configFile = Join-Path $env:TEMP "litellm-config-affinity.yaml"
kubectl get configmap litellm-config -n litellm `
  -o jsonpath="{.data.config\.yaml}" `
  | Set-Content $configFile -Encoding utf8

$env:LITELLM_CONFIG_PATH = $configFile
@'
import os
from pathlib import Path

import yaml

path = Path(os.environ["LITELLM_CONFIG_PATH"])
config = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
router = config.setdefault("router_settings", {})
router.pop("optional_pre_call_checks", None)
router.pop("deployment_affinity_ttl_seconds", None)
path.write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True), encoding="utf-8")
'@ | python -
Remove-Item Env:LITELLM_CONFIG_PATH

kubectl create configmap litellm-config -n litellm `
  --from-file="config.yaml=$configFile" `
  --dry-run=client -o yaml `
  | kubectl apply -f -
kubectl rollout restart deployment/litellm-mi-proxy -n litellm
kubectl rollout status deployment/litellm-mi-proxy -n litellm --timeout=10m
```

## 单副本与多副本

当前 LiteLLM 是单副本，进程内 cache 可以维持 `session_affinity` 和 `deployment_affinity`。如果以后扩为多个 LiteLLM Pod，必须配置共享 Redis；否则请求落到不同 Pod 时，各 Pod 的亲和映射不一致。

`responses_api_deployment_check` 可从 `previous_response_id` 解析原 Deployment，不依赖亲和 TTL cache，但仍建议多副本环境统一使用共享状态和完整的端到端测试。

## 验证建议

1. 使用同一个 Virtual Key 连续发送多次请求。
2. 对 Responses API，后续请求携带上一轮的 `previous_response_id`。
3. 从 LiteLLM 日志或响应头记录每次请求的 `model_id` / 后端 `api_base`。
4. 验证同一会话在 TTL 内保持同一 Deployment。
5. 再使用不同 Virtual Key，确认可以被分散到其他 Deployment。
6. 比较启用前后的缓存命中 Token、输入 Token 计费、P50/P95 延迟和 429 比例。

亲和命中时若固定 Deployment 发生故障或已进入 cooldown，Router 会回到健康候选集合；亲和不能替代 retry、cooldown 和容量监控。

## 使用 Codex CLI 做端到端验证

默认的 LiteLLM `1.95.0` 镜像已实测支持 Responses WebSocket 握手（HTTP 101），Codex Provider 可以启用 WebSocket：

```toml
[model_providers.litellm]
name = "LiteLLM"
base_url = "https://litellm.example.com/v1"
env_key = "LITELLM_API_KEY"
wire_api = "responses"
supports_websockets = true
```

如果临时回退到旧的 `micl/litellm:mi-fix-image-gen` 镜像，则应重新设为 `false`，让 Codex 使用 HTTP Responses。

### 1. 在本机终端设置 Virtual Key

不要把 Key 写入命令历史、仓库或聊天记录。可以在当前 PowerShell 会话中设置：

```powershell
$env:LITELLM_API_KEY = Read-Host "LiteLLM Virtual Key"
```

### 2. 发送第一轮并记录 Codex Session ID

```powershell
$turn1File = Join-Path $env:TEMP "codex-affinity-turn1.jsonl"

codex exec `
  -c 'model_provider="litellm"' `
  -m gpt-5.6-terra `
  --sandbox read-only `
  --json `
  "Reply with exactly: AFFINITY-TURN-1" `
  | Tee-Object -FilePath $turn1File

$sessionId = Get-Content $turn1File | ForEach-Object {
  try {
    $event = $_ | ConvertFrom-Json
    if ($event.type -eq "thread.started") { $event.thread_id }
  } catch {}
} | Select-Object -First 1

"Codex session: $sessionId"
```

第一轮 `turn.completed` 通常显示：

```text
cached_input_tokens: 0
```

### 3. Resume 同一个 Session 发送第二轮

```powershell
$turn2File = Join-Path $env:TEMP "codex-affinity-turn2.jsonl"

codex exec resume $sessionId `
  -c 'model_provider="litellm"' `
  -m gpt-5.6-terra `
  --json `
  "Reply with exactly: AFFINITY-TURN-2" `
  | Tee-Object -FilePath $turn2File
```

查看两轮 Token 统计：

```powershell
Get-Content $turn1File,$turn2File | ForEach-Object {
  try {
    $event = $_ | ConvertFrom-Json
    if ($event.type -eq "turn.completed") { $event.usage }
  } catch {}
} | Format-List
```

若第二轮的 `cached_input_tokens` 大于 0，说明 Azure Prompt Cache 实际命中。亲和路由只能提高命中条件的一致性，短 Prompt、变化的前缀或服务端缓存状态仍可能使该值为 0。

### 4. 从 LiteLLM Spend Logs 验证后端一致

测试完成后立即运行下面的只读命令。它查询最近 10 分钟内的 `gpt-5.6-terra` 请求，不输出 Key 或 Prompt：

```powershell
@'
import asyncio
import os
from datetime import datetime, timedelta, timezone

from prisma import Prisma

async def main():
    db = Prisma(datasource={"url": os.environ["DATABASE_URL"]})
    await db.connect()
    since = datetime.now(timezone.utc) - timedelta(minutes=10)
    rows = await db.litellm_spendlogs.find_many(
        where={"model_group": "gpt-5.6-terra", "startTime": {"gte": since}},
        order={"startTime": "asc"},
    )
    for index, row in enumerate(rows, 1):
        print(
            f"turn={index} time={row.startTime.isoformat()} "
            f"model_id={row.model_id or ''} api_base={row.api_base or ''} "
            f"prompt_tokens={row.prompt_tokens}"
        )
    print("same_model_id=" + str(len({row.model_id for row in rows}) == 1 and len(rows) >= 2))
    print("same_api_base=" + str(len({row.api_base for row in rows}) == 1 and len(rows) >= 2))
    await db.disconnect()

asyncio.run(main())
'@ | kubectl exec -i -n litellm deployment/litellm-mi-proxy -- python -
```

成功判据：

```text
same_model_id=True
same_api_base=True
```

Codex 本地 `thread_id` 不一定原样写入 LiteLLM Spend Logs 的 `session_id`。在当前实测中，两轮 Spend Log 的 `session_id` 不同，但 `previous_response_id` 亲和仍使二者命中同一 Deployment；因此应以 `model_id` 和 `api_base` 为主要证据。

### 5. 清理本机环境变量

```powershell
Remove-Item Env:LITELLM_API_KEY
```

本环境实测结果：两轮请求均命中 `example-agent-resource` 的相同 `model_id`，第二轮 `cached_input_tokens=15347`。