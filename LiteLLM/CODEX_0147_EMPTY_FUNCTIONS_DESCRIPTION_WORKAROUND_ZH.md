# Codex 0.147 空 `functions.description` 的 LiteLLM 兼容方案

> **特定Codex/旧镜像的兼容参考，不是当前网关补丁安装流程。** 当前West US 3使用LiteLLM `1.104.0`固定源码构建及受控发布，用户已验证virtual key的Codex Responses推理，但不据此推断本文全部工具矩阵通过。不要在当前集群临时注入本文callback、修改ConfigMap或重跑旧脚本；现行版本/补丁边界见[升级验证](../docs/litellm-1.104.0-upgrade-validation-2026-10-07.md)，发布按[本地Stage2–9指南](../local_execution/stage2-9-guide-zh.md)。

## 1. 问题概述

Codex 0.147 在 Responses Lite 模式下，会把内置 function/custom tools 合并到 `functions` namespace，并将工具定义放入 `input` 的 `additional_tools` item：

```json
{
  "type": "additional_tools",
  "role": "developer",
  "tools": [
    {
      "type": "namespace",
      "name": "functions",
      "description": "",
      "tools": [
        {
          "type": "function",
          "name": "request_user_input",
          "description": "Requests structured input from the user."
        }
      ]
    }
  ]
}
```

Azure OpenAI / Foundry Responses API 要求 namespace 的 `description` 必须存在且至少包含一个字符，因此返回：

```text
Invalid 'input[0].tools[0].description': empty string.
Expected a string with minimum length 1.
```

这不是 MCP、插件、密钥、模型 Deployment 或客户项目配置问题。

## 2. 已验证事实

当前环境：

```text
Codex Desktop : 0.147.x
LiteLLM       : 1.95.0
Provider      : Azure OpenAI / Foundry Responses API
Model Group   : gpt-5.6-sol
```

最小回归结果：

| Payload | HTTP 状态 | 结果 |
|---|---:|---|
| `description: ""` | `400` | Azure `empty_string` |
| 不发送 `description` | `400` | Azure `missing_required_parameter` |
| `description: "Built-in function tools"` | `200` | 正常返回模型响应 |

因此不能通过删除字段解决，必须补充非空字符串。

## 3. 能否只修改 LiteLLM YAML

LiteLLM 1.95.0 没有内置 YAML 参数可以对以下 JSONPath 做补值：

```text
input[*].tools[*].description
```

以下设置不能解决：

```yaml
drop_params: true
modify_params: true
additional_drop_params: []
```

原因：

- `drop_params` 和 `modify_params` 面向 API 参数兼容，不会递归修改 Responses Lite 的 `additional_tools`；
- 删除 `description` 会被 Azure 以缺少必填参数拒绝；
- 内置 `custom_code` guardrail 只支持修改标准化后的 `texts`、`images` 和 `tool_calls`，无法修改完整嵌套 request body；
- ingress-nginx 不提供通用 JSON body 重写能力。

推荐使用 LiteLLM 官方支持的 **Custom Callback**：

- 不 fork LiteLLM；
- 不修改 `site-packages`；
- 不重新构建镜像；
- 使用一个小型 Python 文件；
- 通过 ConfigMap 挂载；
- 在 `litellm_settings.callbacks` 中配置启用。

## 4. WebSocket 兼容边界

LiteLLM 1.95.0 的 HTTP `/v1/responses` 每次请求都会经过 `async_pre_call_hook`。

Responses WebSocket 在建立连接时也会执行 pre-call hook，但当前实现把首个 `response.create` 保存为字符串字段：

```text
data["first_message"]
```

回调可以修改连接首帧；但原生上游 WebSocket 后续 frame 不保证逐帧再次经过相同 callback。因此短期生产方案建议：

```toml
[model_providers.litellm]
wire_api = "responses"
supports_websockets = false
```

即先使用 HTTP Responses，确保每轮工具定义都被修复。待 Codex、LiteLLM 或 Azure 正式修复后，再恢复 WebSocket 并执行完整多轮回归。

如果仍要启用 WebSocket，应至少验证：

1. 首轮请求成功；
2. 连续多轮 tool-call 请求成功；
3. ingress access log 出现 `/v1/responses` HTTP `101`；
4. LiteLLM 日志不再出现 `empty_string`。

## 5. Callback 实现

创建 `azure_namespace_compat.py`：

```python
import json
from typing import Any

from litellm.integrations.custom_logger import CustomLogger


FALLBACK_DESCRIPTION = "Built-in function tools"


def _fix_namespace_tools(tools: Any) -> int:
    if not isinstance(tools, list):
        return 0

    changes = 0
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") != "namespace":
            continue
        if str(tool.get("description") or "").strip():
            continue

        tool["description"] = FALLBACK_DESCRIPTION
        changes += 1
    return changes


def _fix_request_body(body: Any) -> int:
    if not isinstance(body, dict):
        return 0

    changes = _fix_namespace_tools(body.get("tools"))

    request_input = body.get("input")
    if isinstance(request_input, list):
        for item in request_input:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "additional_tools":
                changes += _fix_namespace_tools(item.get("tools"))

    nested_response = body.get("response")
    if isinstance(nested_response, dict):
        changes += _fix_request_body(nested_response)

    return changes


class AzureNamespaceCompatibility(CustomLogger):
    async def async_pre_call_hook(
        self,
        user_api_key_dict,
        cache,
        data: dict[str, Any],
        call_type,
    ) -> dict[str, Any]:
        _fix_request_body(data)

        first_message = data.get("first_message")
        if isinstance(first_message, str):
            try:
                event = json.loads(first_message)
            except json.JSONDecodeError:
                return data

            if _fix_request_body(event):
                data["first_message"] = json.dumps(
                    event,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )

        return data


proxy_handler_instance = AzureNamespaceCompatibility()
```

该回调具备以下约束：

- 只处理 `type=namespace`；
- 只处理缺失、空字符串或纯空白 description；
- 不覆盖已有非空 description；
- 不修改子工具 description；
- 不记录请求正文、Key 或工具参数；
- 同时兼容顶层 `tools`、HTTP `additional_tools` 和 WebSocket 首帧。

## 6. 使用 ConfigMap 部署 Callback

### 6.1 创建本地 Callback 文件

```powershell
$callbackFile = Join-Path $env:TEMP "azure_namespace_compat.py"

@'
import json
from typing import Any

from litellm.integrations.custom_logger import CustomLogger

FALLBACK_DESCRIPTION = "Built-in function tools"

def _fix_namespace_tools(tools: Any) -> int:
    if not isinstance(tools, list):
        return 0
    changes = 0
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") != "namespace":
            continue
        if str(tool.get("description") or "").strip():
            continue
        tool["description"] = FALLBACK_DESCRIPTION
        changes += 1
    return changes

def _fix_request_body(body: Any) -> int:
    if not isinstance(body, dict):
        return 0
    changes = _fix_namespace_tools(body.get("tools"))
    request_input = body.get("input")
    if isinstance(request_input, list):
        for item in request_input:
            if isinstance(item, dict) and item.get("type") == "additional_tools":
                changes += _fix_namespace_tools(item.get("tools"))
    nested_response = body.get("response")
    if isinstance(nested_response, dict):
        changes += _fix_request_body(nested_response)
    return changes

class AzureNamespaceCompatibility(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        _fix_request_body(data)
        first_message = data.get("first_message")
        if isinstance(first_message, str):
            try:
                event = json.loads(first_message)
            except json.JSONDecodeError:
                return data
            if _fix_request_body(event):
                data["first_message"] = json.dumps(
                    event,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
        return data

proxy_handler_instance = AzureNamespaceCompatibility()
'@ | Set-Content $callbackFile -Encoding utf8
```

### 6.2 创建或更新 Callback ConfigMap

```powershell
kubectl create configmap litellm-callbacks `
  --namespace litellm `
  --from-file="azure_namespace_compat.py=$callbackFile" `
  --dry-run=client `
  -o yaml |
  kubectl apply -f -
```

### 6.3 更新 LiteLLM 配置

先备份当前配置：

```powershell
$configFile = Join-Path $env:TEMP "litellm-config-callback.yaml"

kubectl get configmap litellm-config -n litellm -o yaml |
  Set-Content "$configFile.configmap-backup.yaml" -Encoding utf8

kubectl get configmap litellm-config -n litellm `
  -o jsonpath="{.data.config\.yaml}" |
  Set-Content $configFile -Encoding utf8
```

结构化添加 callback：

```powershell
$env:LITELLM_CONFIG_PATH = $configFile

@'
import os
from pathlib import Path

import yaml

path = Path(os.environ["LITELLM_CONFIG_PATH"])
config = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
settings = config.setdefault("litellm_settings", {})
callbacks = settings.setdefault("callbacks", [])
callback = "azure_namespace_compat.proxy_handler_instance"
if callback not in callbacks:
    callbacks.append(callback)
path.write_text(
    yaml.safe_dump(config, sort_keys=False, allow_unicode=True),
    encoding="utf-8",
)
'@ | python -

Remove-Item Env:LITELLM_CONFIG_PATH

kubectl create configmap litellm-config `
  --namespace litellm `
  --from-file="config.yaml=$configFile" `
  --dry-run=client `
  -o yaml |
  kubectl apply -f -
```

生成后的配置应包含：

```yaml
litellm_settings:
  enable_azure_ad_token_refresh: true
  callbacks:
    - azure_namespace_compat.proxy_handler_instance
```

### 6.4 挂载 Callback 并设置模块路径

```powershell
$patch = @{
  spec = @{
    template = @{
      spec = @{
        containers = @(
          @{
            name = "litellm"
            env = @(
              @{
                name = "PYTHONPATH"
                value = "/app/callbacks"
              }
            )
            volumeMounts = @(
              @{
                name = "litellm-callbacks"
                mountPath = "/app/callbacks"
                readOnly = $true
              }
            )
          }
        )
        volumes = @(
          @{
            name = "litellm-callbacks"
            configMap = @{
              name = "litellm-callbacks"
            }
          }
        )
      }
    }
  }
} | ConvertTo-Json -Depth 20 -Compress

kubectl patch deployment litellm-mi-proxy `
  --namespace litellm `
  --type strategic `
  --patch $patch

kubectl rollout status deployment/litellm-mi-proxy `
  --namespace litellm `
  --timeout=10m
```

ConfigMap 更新后 Python 模块不会自动重新加载。以后修改 callback 文件，需要执行：

```powershell
kubectl rollout restart deployment/litellm-mi-proxy -n litellm
kubectl rollout status deployment/litellm-mi-proxy -n litellm --timeout=10m
```

## 7. 部署验证

### 7.1 验证模块可导入

```powershell
kubectl exec -n litellm deployment/litellm-mi-proxy -- `
  python -c "import azure_namespace_compat; print(type(azure_namespace_compat.proxy_handler_instance).__name__)"
```

预期：

```text
AzureNamespaceCompatibility
```

### 7.2 验证 callback 已注册

```powershell
kubectl logs -n litellm deployment/litellm-mi-proxy --tail=300 |
  Select-String -Pattern "azure_namespace_compat|callback" -Context 1,2
```

如日志级别不足，可直接查看进程内 callback 类型：

```powershell
@'
import json
import os
import urllib.request

request = urllib.request.Request(
    "http://127.0.0.1:4000/active/callbacks",
    headers={"Authorization": f"Bearer {os.environ['LITELLM_MASTER_KEY']}"},
)
with urllib.request.urlopen(request, timeout=30) as response:
    payload = json.load(response)
print(json.dumps(payload, ensure_ascii=False, indent=2))
'@ | kubectl exec -i -n litellm deployment/litellm-mi-proxy -- python -
```

输出中的 `litellm.callbacks` 应包含 `AzureNamespaceCompatibility`。命令只在 Pod 内使用 Master Key，不会输出 Key 本身。

### 7.3 HTTP 回归

使用 Virtual Key 或 Master Key 发送 Codex 0.147 的 `additional_tools` 结构。修复后的预期结果：

```text
description=""    -> LiteLLM 补值 -> Azure HTTP 200
description 缺失  -> LiteLLM 补值 -> Azure HTTP 200
description 非空  -> 保持原值       -> Azure HTTP 200
```

LiteLLM 日志不应再出现：

```text
input[0].tools[0].description
empty_string
missing_required_parameter
```

### 7.4 真实 Codex 0.147 回归

短期建议先关闭 WebSocket：

```toml
[model_providers.litellm]
name = "LiteLLM"
base_url = "https://litellm.example.com/v1"
env_key = "LITELLM_API_KEY"
wire_api = "responses"
supports_websockets = false
```

执行普通 Codex 任务，至少验证：

- 首轮文本响应；
- 内置 `exec`；
- `wait`；
- `request_user_input`；
- Resume 多轮会话；
- LiteLLM Spend Logs 正常写入。

## 8. 持久化注意事项

当前 [`deploy_mi_aks_litellm.py`](./deploy_mi_aks_litellm.py) 会重新生成 `litellm.config.yaml`，并以 `replace_namespaced_deployment()` 替换整个 LiteLLM Deployment。

因此重新运行部署脚本后，以下手工配置可能丢失：

- `litellm_settings.callbacks`；
- `litellm-callbacks` volume；
- `/app/callbacks` volume mount；
- `PYTHONPATH`。

不修改部署脚本时，建议把第 6 节保存为部署后的 Kubernetes overlay 步骤，并在每次运行部署脚本后重新应用。

长期可选方案：

1. 在部署脚本中把 callback ConfigMap、mount 和配置作为正式资源管理；
2. 使用 Kustomize/Helm 管理 overlay；
3. 升级到包含 Azure empty namespace description 修复的 LiteLLM 版本后移除 callback。

## 9. 回滚

### 9.1 从 LiteLLM 配置删除 callback

```powershell
$configFile = Join-Path $env:TEMP "litellm-config-callback-rollback.yaml"

kubectl get configmap litellm-config -n litellm `
  -o jsonpath="{.data.config\.yaml}" |
  Set-Content $configFile -Encoding utf8

$env:LITELLM_CONFIG_PATH = $configFile

@'
import os
from pathlib import Path

import yaml

path = Path(os.environ["LITELLM_CONFIG_PATH"])
config = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
settings = config.get("litellm_settings", {})
callbacks = settings.get("callbacks", [])
callback = "azure_namespace_compat.proxy_handler_instance"
settings["callbacks"] = [item for item in callbacks if item != callback]
if not settings["callbacks"]:
    settings.pop("callbacks", None)
path.write_text(
    yaml.safe_dump(config, sort_keys=False, allow_unicode=True),
    encoding="utf-8",
)
'@ | python -

Remove-Item Env:LITELLM_CONFIG_PATH

kubectl create configmap litellm-config `
  --namespace litellm `
  --from-file="config.yaml=$configFile" `
  --dry-run=client `
  -o yaml |
  kubectl apply -f -
```

### 9.2 删除 Deployment mount

最稳妥的方式是重新运行未包含 callback overlay 的部署脚本，或使用：

```powershell
kubectl edit deployment litellm-mi-proxy -n litellm
```

删除：

```yaml
env:
  - name: PYTHONPATH
    value: /app/callbacks
volumeMounts:
  - name: litellm-callbacks
    mountPath: /app/callbacks
volumes:
  - name: litellm-callbacks
```

然后：

```powershell
kubectl delete configmap litellm-callbacks -n litellm --ignore-not-found
kubectl rollout restart deployment/litellm-mi-proxy -n litellm
kubectl rollout status deployment/litellm-mi-proxy -n litellm --timeout=10m
```

## 10. 后续正式修复判定

满足以下任一条件后，可考虑移除 callback：

- Codex 不再发送空的 `functions.description`；
- LiteLLM Azure Responses transformer 自动为嵌套 namespace 补值；
- Azure 接受默认 `functions` namespace 的空 description。

升级前后应使用相同 Payload 执行回归：

```text
空 description      -> 200
缺失 description    -> 200
已有 description    -> 保持原值
HTTP 多轮           -> 通过
WebSocket 多轮      -> 通过
工具调用            -> 通过
```

不要只验证第一轮文本响应；该兼容问题发生在工具协议，必须覆盖实际 tool-call 和多轮任务。